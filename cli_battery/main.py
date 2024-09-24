from app import create_app
from app.database import init_db, Session, Base
import logging
import time
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
import threading
from app.logger_config import logger
import sys

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
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.exception("Error initializing database")
        sys.exit(1)

    # Run Flask server
    logger.info("Starting Flask server")
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()