from app import create_app
from app.database import init_db, Session, Base
from app.background_jobs import background_jobs
import logging
import time
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
import threading
from app.logger_config import logger
import sys
import atexit
import os

def initialize_database(app):
    max_retries = 5
    retry_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            
            # Initialize the database
            engine = init_db(app)
            
            # Test connection
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            
            # Verify tables
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            
            if "items" not in tables:
                raise Exception("The 'items' table was not created.")
            
            return engine
        except OperationalError as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                raise
        except Exception as e:
            raise

    return None

def main():
    logger.info("Starting cli_battery")
    
    app = create_app()
    
    try:
        engine = initialize_database(app)
        if engine is None:
            logger.error("Failed to initialize database after multiple attempts")
            sys.exit(1)
            
        # Initialize and start background jobs with Flask app
        background_jobs.init_app(app)
        background_jobs.start()
        # Ensure background jobs are stopped on exit
        atexit.register(background_jobs.stop)
        
        logger.info("Database initialized successfully")
        
        # Get port from environment variable or use default
        port = int(os.environ.get('CLI_DEBRID_BATTERY_PORT', 5001))
        
        # Run Flask server
        logger.info(f"Starting Flask server on port {port}")
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()