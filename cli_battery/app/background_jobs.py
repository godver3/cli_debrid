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
from .trakt_metadata import TraktMetadata

class BackgroundJobManager:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=_get_local_timezone())
        self.settings = Settings()
        self.app = None
        self.engine = None
        # Rate limiting for Trakt API
        self.last_api_call = 0
        self.min_call_interval = 3.0  # Minimum 1 second between API calls

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
            func=self.refresh_trakt_updates,
            trigger=IntervalTrigger(hours=6),
            id='refresh_trakt_updates',
            name='Refresh Metadata via Trakt Updates',
            replace_existing=True
        )
        
        try:
            self.scheduler.start()
            logger.info("Background job scheduler started")
            
            # Schedule initial refresh to run after a delay
            local_tz = _get_local_timezone()
            self.scheduler.add_job(
                func=self.refresh_trakt_updates,
                trigger='date',  # Run once
                run_date=datetime.now(local_tz) + timedelta(seconds=5),
                id='initial_refresh',
                name='Initial Trakt Updates Refresh'
            )
            logger.info("Initial Trakt updates refresh scheduled in 5 seconds")
            
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

    def _round_timestamp_to_hour(self, timestamp_str):
        """Round timestamp to the hour as required by Trakt API"""
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            # Round down to the hour
            rounded = dt.replace(minute=0, second=0, microsecond=0)
            return rounded.isoformat().replace('+00:00', 'Z')
        except Exception as e:
            logger.warning(f"Failed to round timestamp {timestamp_str}: {e}")
            return timestamp_str

    def refresh_trakt_updates(self):
        """Fetch Trakt updated shows/movies since last cursor and refresh only those items."""
        try:
            trakt = TraktMetadata()
            cursors = self.settings.trakt_updates or {}
            # Bootstrap: if no cursors, do a single 3-month backfill, then continue incrementally
            now_iso = datetime.now(_get_local_timezone()).isoformat()
            default_since = (datetime.now(_get_local_timezone()) - timedelta(days=90)).isoformat()
            shows_since = cursors.get('shows_last_updated_at') or default_since
            movies_since = cursors.get('movies_last_updated_at') or default_since

            # Round current cursors to the hour as required by Trakt API
            rounded_shows_since = self._round_timestamp_to_hour(shows_since)
            rounded_movies_since = self._round_timestamp_to_hour(movies_since)

            logger.info(f"Fetching Trakt updated shows since {rounded_shows_since} and movies since {rounded_movies_since}")

            # Fetch updates
            updated_shows = trakt.get_updated_shows(rounded_shows_since) or []
            updated_movies = trakt.get_updated_movies(rounded_movies_since) or []

            logger.info(f"Received {len(updated_shows)} show updates and {len(updated_movies)} movie updates from Trakt")

            # Filter for items that exist in our database
            with DbSession() as session:
                # Get all existing imdb_ids from our database
                existing_items = session.query(Item.imdb_id).all()
                existing_imdb_ids = {item[0] for item in existing_items}
                logger.info(f"Found {len(existing_imdb_ids)} existing items in database")

            # Filter updates to only include items we're monitoring
            relevant_shows = [entry for entry in updated_shows if entry.get('imdb_id') in existing_imdb_ids]
            relevant_movies = [entry for entry in updated_movies if entry.get('imdb_id') in existing_imdb_ids]
            
            logger.info(f"Filtered to {len(relevant_shows)} relevant show updates and {len(relevant_movies)} relevant movie updates")

            # Process updates; collect max updated_at to advance cursors
            max_show_updated = shows_since
            max_movie_updated = movies_since

            # Refresh shows
            processed_shows = 0
            for entry in relevant_shows:
                imdb_id = entry.get('imdb_id')
                if not imdb_id:
                    continue
                self._enforce_rate_limit()
                try:
                    result = MetadataManager.refresh_metadata(imdb_id)
                    if result is not None:
                        logger.info(f"Updated show metadata via Trakt updates for {imdb_id}")
                    else:
                        logger.warning(f"Refresh returned no data for updated show {imdb_id}")
                except Exception as e:
                    logger.error(f"Error refreshing updated show {imdb_id}: {e}", exc_info=True)
                # Track cursor
                updated_at = entry.get('updated_at')
                if updated_at and updated_at > (max_show_updated or ''):
                    max_show_updated = updated_at
                processed_shows += 1

            logger.info(f"Processed {processed_shows}/{len(relevant_shows)} relevant show updates")

            # Refresh movies
            processed_movies = 0
            for entry in relevant_movies:
                imdb_id = entry.get('imdb_id')
                if not imdb_id:
                    continue
                self._enforce_rate_limit()
                try:
                    result = MetadataManager.refresh_metadata(imdb_id)
                    if result is not None:
                        logger.info(f"Updated movie metadata via Trakt updates for {imdb_id}")
                    else:
                        logger.warning(f"Refresh returned no data for updated movie {imdb_id}")
                except Exception as e:
                    logger.error(f"Error refreshing updated movie {imdb_id}: {e}", exc_info=True)
                # Track cursor
                updated_at = entry.get('updated_at')
                if updated_at and updated_at > (max_movie_updated or ''):
                    max_movie_updated = updated_at
                processed_movies += 1

            logger.info(f"Processed {processed_movies}/{len(relevant_movies)} relevant movie updates")

            # Advance cursors if we processed anything
            if relevant_shows or relevant_movies:
                # Fallback to now if max not set properly
                if not max_show_updated:
                    max_show_updated = now_iso
                if not max_movie_updated:
                    max_movie_updated = now_iso
                
                # Round timestamps to the hour as required by Trakt API
                rounded_show_cursor = self._round_timestamp_to_hour(max_show_updated)
                rounded_movie_cursor = self._round_timestamp_to_hour(max_movie_updated)
                
                self.settings.update_trakt_updates(
                    shows_last_updated_at=rounded_show_cursor,
                    movies_last_updated_at=rounded_movie_cursor,
                )
                logger.info(
                    f"Advanced Trakt update cursors to shows={rounded_show_cursor}, movies={rounded_movie_cursor}"
                )
            else:
                logger.info(f"No relevant Trakt updates to process (received {len(updated_shows)} shows, {len(updated_movies)} movies, {len(relevant_shows)} relevant shows, {len(relevant_movies)} relevant movies)")

        except Exception as e:
            logger.error(f"Error in refresh_trakt_updates: {e}", exc_info=True)

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