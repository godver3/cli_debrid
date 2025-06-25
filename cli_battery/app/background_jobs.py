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

    def refresh_stale_metadata(self):
        """Check and refresh stale metadata for all items"""
        try:
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
                                    retry_count = 0
                                    max_retries = 3
                                    success = False
                                    while retry_count < max_retries:
                                        try:
                                            # Call refresh_metadata using only the IMDb ID
                                            result = MetadataManager.refresh_metadata(item_imdb_id)

                                            if result is not None:
                                                success = True
                                                refreshed_count += 1
                                                logger.info(f"Successfully refreshed and saved metadata for {log_identifier}")
                                                break
                                            else:
                                                logger.warning(f"Refresh attempt {retry_count + 1} failed for {log_identifier}. Save might have failed.")

                                        except requests.exceptions.HTTPError as e:
                                            if e.response is not None and e.response.status_code == 429:
                                                 logger.warning(f"Rate limit hit (429) while refreshing {log_identifier}. Attempt {retry_count + 1}/{max_retries}")
                                            else:
                                                 logger.error(f"HTTP error refreshing metadata for {log_identifier}: {e}")
                                                 break
                                        except Exception as e:
                                            logger.error(f"Unexpected error during refresh attempt for {log_identifier}: {e}")
                                            # break # Consider breaking on unexpected errors

                                        retry_count += 1
                                        if not success and retry_count < max_retries:
                                            logger.info(f"Pausing for 30 seconds before retry attempt {retry_count + 1} for {log_identifier}...")
                                            time.sleep(30)

                                    if not success:
                                        logger.error(f"Max retries reached or non-retriable error occurred for {log_identifier}")

                            except Exception as e:
                                # Log error using the fetched imdb_id
                                logger.error(f"Error processing item {log_identifier} in batch: {e}", exc_info=True)
                                continue # Move to the next item in the batch

                        # No commit/rollback needed for the query_session

                        # Log progress
                        if (offset + batch_size) % (batch_size * 5) == 0:
                             logger.info(f"Progress: {min(offset + batch_size, total_items)}/{total_items} items checked ({refreshed_count}/{stale_count} stale refreshed so far)")

                        # Delay between batches
                        logger.debug(f"Sleeping for 3 seconds after processing batch offset {offset}")
                        time.sleep(3)

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