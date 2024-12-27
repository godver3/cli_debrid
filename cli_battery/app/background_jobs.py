from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.orm import Session, sessionmaker
from flask import current_app

from .database import DatabaseManager, Item, init_db
from .metadata_manager import MetadataManager
from .settings import Settings

logger = logging.getLogger(__name__)

class BackgroundJobManager:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.settings = Settings()
        self.metadata_manager = MetadataManager()
        self.app = None
        self.Session = None

    def init_app(self, app):
        """Initialize with Flask app context"""
        self.app = app
        # Initialize database engine and session factory
        engine = init_db(app)
        self.Session = sessionmaker(bind=engine)

    def start(self):
        """Start all background jobs"""
        if not self.app:
            logger.error("Cannot start background jobs: Flask app not initialized")
            return
            
        if not self.Session:
            logger.error("Cannot start background jobs: Database session not initialized")
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
            self.scheduler.add_job(
                func=self.refresh_stale_metadata,
                trigger='date',  # Run once
                run_date=datetime.now() + timedelta(seconds=30),  # 30 second delay
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
            session = self.Session()
            try:
                items = session.query(Item).all()
                total_items = len(items)
                refreshed_count = 0
                stale_count = 0
                
                logger.info(f"Checking {total_items} items for stale metadata...")
                
                for i, item in enumerate(items, 1):
                    if i % 100 == 0:  # Log progress every 100 items
                        logger.info(f"Progress: {i}/{total_items} items checked")
                    
                    if MetadataManager.is_metadata_stale(item.updated_at):
                        stale_count += 1
                        try:
                            MetadataManager.refresh_metadata(item.imdb_id)
                            refreshed_count += 1
                        except Exception as e:
                            logger.error(f"Failed to refresh metadata for {item.title} ({item.imdb_id}): {e}")
                
                logger.info(
                    f"Metadata refresh complete: {refreshed_count}/{stale_count} stale items refreshed "
                    f"({total_items} total items checked)"
                )
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Error in refresh_stale_metadata job: {e}")

# Global instance
background_jobs = BackgroundJobManager()
