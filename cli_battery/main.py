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
            engine = init_db()
            
            # Test both read and write operations
            with engine.connect() as connection:
                # Ensure WAL mode and other PRAGMA settings are applied
                connection.execute(text("PRAGMA journal_mode=WAL"))
                connection.execute(text("PRAGMA busy_timeout=30000"))
                connection.execute(text("PRAGMA synchronous=NORMAL"))
                
                # Test read
                connection.execute(text("SELECT 1"))
                
                # Test write capability with a transaction
                with connection.begin():
                    connection.execute(text("CREATE TABLE IF NOT EXISTS _write_test (test INTEGER)"))
                    connection.execute(text("INSERT INTO _write_test VALUES (1)"))
                    connection.execute(text("DROP TABLE IF EXISTS _write_test"))
            
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
            logger.error(f"Failed to initialize database: {str(e)}")
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
            
            try:
                # Initialize and start background jobs with Flask app
                background_jobs.init_app(app)
                background_jobs.start()
                
                # Ensure background jobs are stopped and database connections are cleaned up on exit
                def cleanup():
                    logger.info("Cleaning up resources...")
                    try:
                        background_jobs.stop()
                    except Exception as e:
                        logger.error(f"Error stopping background jobs: {str(e)}")
                    finally:
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
                logger.error(f"Error initializing background jobs: {str(e)}")
                engine.dispose()
                sys.exit(1)
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()