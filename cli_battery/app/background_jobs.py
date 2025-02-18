from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import func
from flask import current_app
import time

from .database import DatabaseManager, Item, init_db, Session as DbSession
from .metadata_manager import MetadataManager
from .settings import Settings
from metadata.metadata import _get_local_timezone

logger = logging.getLogger(__name__)

class BackgroundJobManager:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=_get_local_timezone())
        self.settings = Settings()
        self.metadata_manager = MetadataManager()
        self.app = None
        self.engine = None

    def init_app(self, app):
        """Initialize with Flask app context"""
        self.app = app
        # Initialize database engine
        self.engine = init_db()
        # Configure the global session
        DbSession.configure(bind=self.engine)

    def start(self):
        """Start all background jobs"""
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
                run_date=datetime.now(local_tz) + timedelta(seconds=30),  # 30 second delay
                id='initial_refresh',
                name='Initial Metadata Refresh'
            )
            logger.info("Initial metadata refresh scheduled in 30 seconds")
            
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
            with DbSession() as session:
                # Disable autoflush to prevent premature flushes during iteration
                session.autoflush = False
                
                # Get all items in batches to reduce memory usage and lock time
                batch_size = 50
                total_items = session.query(func.count(Item.id)).scalar()
                refreshed_count = 0
                stale_count = 0
                
                logger.info(f"Checking {total_items} items for stale metadata...")
                
                try:
                    # Process items in batches
                    for offset in range(0, total_items, batch_size):
                        items_batch = session.query(Item).limit(batch_size).offset(offset).all()
                        
                        for item in items_batch:
                            try:
                                # Ensure item.updated_at has timezone info
                                if item.updated_at and item.updated_at.tzinfo is None:
                                    item.updated_at = item.updated_at.replace(tzinfo=_get_local_timezone())
                                
                                if MetadataManager.is_metadata_stale(item.updated_at):
                                    stale_count += 1
                                    try:
                                        # Pass the current session to refresh_metadata
                                        MetadataManager.refresh_metadata(item.imdb_id, existing_session=session)
                                        refreshed_count += 1
                                        # Re-query the item to ensure it's still bound to the session
                                        item = session.query(Item).get(item.id)
                                    except Exception as e:
                                        logger.error(f"Failed to refresh metadata for {item.title} ({item.imdb_id}): {e}")
                                        continue
                            except Exception as e:
                                logger.error(f"Error processing item {item.imdb_id}: {e}")
                                continue
                        
                        # Commit after each batch to prevent long transactions
                        session.commit()
                        
                        # Log progress every few batches
                        if (offset + batch_size) % (batch_size * 5) == 0:
                            logger.info(f"Progress: {min(offset + batch_size, total_items)}/{total_items} items checked")
                        
                        # Small delay between batches to reduce database pressure
                        time.sleep(0.1)
                    
                    logger.info(
                        f"Metadata refresh complete: {refreshed_count}/{stale_count} stale items refreshed "
                        f"({total_items} total items checked)"
                    )
                except Exception as e:
                    logger.error(f"Error processing items batch: {e}")
                    session.rollback()
        except Exception as e:
            logger.error(f"Error in refresh_stale_metadata job: {e}")
        finally:
            # Ensure the session is closed
            DbSession.remove()

# Global instance
background_jobs = BackgroundJobManager()