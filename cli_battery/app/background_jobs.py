from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timezone, timedelta
from app.logger_config import logger
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import func, select
from flask import current_app
import time
import requests

from .database import DatabaseManager, Item, init_db, Session as DbSession
from .metadata_manager import MetadataManager
from .settings import Settings
from metadata.metadata import _get_local_timezone
from .trakt_auth import TraktAuth

class BackgroundJobManager:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=_get_local_timezone())
        self.settings = Settings()
        self.app = None
        self.engine = None
        # Rate limiting for Trakt API
        self.last_api_call = 0
        self.min_call_interval = 1.0  # Minimum 1 second between API calls

    def init_app(self, app):
        """Initialize with Flask app context"""
        self.app = app
        self.engine = init_db()
        DbSession.configure(bind=self.engine)

    def start(self):
        """Start all background jobs"""
        try:
            trakt_auth = TraktAuth()
            if not trakt_auth.is_authenticated():
                logger.warning("Trakt is not authenticated. Background jobs will not be started.")
                return
        except Exception as e:
            logger.error(f"Failed to check Trakt authentication: {e}")
            logger.warning("Background jobs will not be started due to Trakt auth check failure.")
            return

        if not self.app:
            logger.error("Cannot start background jobs: Flask app not initialized")
            return
            
        if not self.engine:
            logger.error("Cannot start background jobs: Database engine not initialized")
            return

        # Add jobs here
        self.scheduler.add_job(
            func=self.refresh_stale_metadata,
            trigger=IntervalTrigger(hours=6),
            id='refresh_stale_metadata',
            name='Refresh Stale Metadata',
            replace_existing=True
        )
        
        try:
            self.scheduler.start()
            logger.info("Background job scheduler started")
            
            # Schedule initial refresh to run after a delay
            local_tz = _get_local_timezone()
            self.scheduler.add_job(
                func=self.refresh_stale_metadata,
                trigger='date',  # Run once
                run_date=datetime.now(local_tz) + timedelta(seconds=5),
                id='initial_refresh',
                name='Initial Metadata Refresh'
            )
            logger.info("Initial metadata refresh scheduled in 5 seconds")
            
        except Exception as e:
            logger.error(f"Failed to start background job scheduler: {e}")

    def stop(self):
        """Stop all background jobs"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Background job scheduler stopped")

    def _enforce_rate_limit(self):
        """Enforce minimum interval between API calls"""
        current_time = time.time()
        time_since_last_call = current_time - self.last_api_call
        
        if time_since_last_call < self.min_call_interval:
            sleep_time = self.min_call_interval - time_since_last_call
            logger.debug(f"Rate limiting: sleeping for {sleep_time:.2f} seconds")
            time.sleep(sleep_time)
        
        self.last_api_call = time.time()

    def refresh_stale_metadata(self):
        """Check and refresh stale metadata for all items"""
        try:
            # Rate limiting strategy:
            # - Trakt allows 1000 GET requests per 5 minutes (~3.33 requests/second)
            # - We enforce 1-second minimum intervals between API calls
            # - This ensures we stay well within the rate limits
            # - Additional 5-second delays between batches provide extra breathing room
            
            # Use a session ONLY for querying item IDs and timestamps
            with DbSession() as query_session:
                query_session.autoflush = False # Still useful for query-only session

                batch_size = 50
                # --- Select only specific columns ---
                total_items_stmt = select(func.count(Item.id))
                total_items = query_session.execute(total_items_stmt).scalar_one()
                # --- End Selection ---

                refreshed_count = 0
                stale_count = 0

                logger.info(f"Checking {total_items} items for stale metadata...")

                try:
                    # Process items in batches
                    for offset in range(0, total_items, batch_size):
                        # --- Select only specific columns ---
                        stmt = select(Item.id, Item.imdb_id, Item.updated_at).\
                               order_by(Item.id).\
                               limit(batch_size).\
                               offset(offset)
                        items_batch = query_session.execute(stmt).all() # Returns Row objects
                        # --- End Selection ---

                        batch_imdb_ids = [item.imdb_id for item in items_batch]
                        logger.debug(f"Processing batch offset {offset}, imdb_ids: {batch_imdb_ids}")

                        # --- Iterate through Row objects ---
                        for item_row in items_batch:
                            item_id = item_row.id # Access via attribute or index
                            item_imdb_id = item_row.imdb_id
                            item_updated_at = item_row.updated_at
                            # Use imdb_id for logging instead of title
                            log_identifier = item_imdb_id or f"DB_ID_{item_id}"
                            # --- End Iteration Setup ---

                            try:
                                # Check staleness using the fetched timestamp
                                if item_updated_at and item_updated_at.tzinfo is None:
                                    item_updated_at = item_updated_at.replace(tzinfo=_get_local_timezone())

                                if MetadataManager.is_metadata_stale(item_updated_at):
                                    stale_count += 1
                                    logger.info(f"Item {log_identifier} is stale. Attempting refresh.")
                                    
                                    # Enforce rate limiting before the remote call.
                                    self._enforce_rate_limit()
                                    
                                    try:
                                        # The underlying refresh_metadata call now has its own retry logic.
                                        # We will call it once and let it handle transient errors.
                                        result = MetadataManager.refresh_metadata(item_imdb_id)

                                        if result is not None:
                                            refreshed_count += 1
                                            logger.info(f"Successfully refreshed and saved metadata for {log_identifier}")
                                        else:
                                            # This log now indicates that the refresh operation failed after all its internal retries.
                                            logger.error(f"Failed to refresh metadata for {log_identifier} after multiple attempts.")

                                    except Exception as e:
                                        logger.error(f"An unexpected error occurred during the refresh process for {log_identifier}: {e}", exc_info=True)
                                
                                # No rate-limiting for non-stale items.

                            except Exception as e:
                                # Log error using the fetched imdb_id
                                logger.error(f"Error processing item {log_identifier} in batch: {e}", exc_info=True)
                                continue # Move to the next item in the batch

                        # No commit/rollback needed for the query_session

                        # Log progress
                        if (offset + batch_size) % (batch_size * 5) == 0:
                             logger.info(f"Progress: {min(offset + batch_size, total_items)}/{total_items} items checked ({refreshed_count}/{stale_count} stale refreshed so far)")

                        # Additional delay between batches to give the API a breather
                        logger.debug(f"Sleeping for 0.1 seconds after processing batch offset {offset}")
                        time.sleep(0.1)

                    logger.info(
                        f"Metadata refresh complete: {refreshed_count}/{stale_count} stale items refreshed "
                        f"({total_items} total items checked)"
                    )
                except Exception as e:
                    logger.error(f"Error processing item batches: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in refresh_stale_metadata job setup: {e}", exc_info=True)

# Global instance
background_jobs = BackgroundJobManager()