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
            
            # Test both read and write operations
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                # Test read
                connection.exec_driver_sql("SELECT 1")
                
                # Test write capability with a transaction
                connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS _write_test (test INTEGER)")
                connection.exec_driver_sql("INSERT INTO _write_test VALUES (1)")
                connection.exec_driver_sql("DROP TABLE IF EXISTS _write_test")
            
            # Verify tables
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            
            if "items" not in tables:
                raise Exception("The 'items' table was not created.")
            
            return engine
        except OperationalError as e:
            if "readonly database" in str(e).lower():
                logger.error("Database is readonly! Please check file permissions and disk space.")
                raise Exception("Database is in readonly mode. Check file permissions and disk space.") from e
            if attempt < max_retries - 1:
                logger.warning(f"Database initialization attempt {attempt + 1} failed, retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                raise
        except Exception as e:
            raise

    return None

def main():
    logger.info("Starting cli_battery")
    
    try:
        app = create_app()
        
        with app.app_context():
            engine = initialize_database(app)
            if engine is None:
                logger.error("Failed to initialize database after multiple attempts")
                sys.exit(1)
            
            # Initialize and start background jobs with Flask app
            background_jobs.init_app(app)
            background_jobs.start()
            
            # Ensure background jobs are stopped and database connections are cleaned up on exit
            def cleanup():
                logger.info("Cleaning up resources...")
                background_jobs.stop()
                Session.remove()  # Clean up any lingering sessions
                engine.dispose()  # Close all connections in the pool
            
            atexit.register(cleanup)
            
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