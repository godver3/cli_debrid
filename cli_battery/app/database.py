from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, LargeBinary, Text, JSON
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from sqlalchemy.ext.declarative import declarative_base
from flask import current_app, jsonify
from datetime import datetime, timezone
from sqlalchemy import or_, func, cast, String, inspect
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError, OperationalError
from .logger_config import logger
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.types import JSON
import os
import time
import random
from functools import wraps
from typing import Optional

def get_timezone_aware_now():
    """Get current datetime with proper timezone handling"""
    try:
        from metadata.metadata import _get_local_timezone
        return datetime.now(_get_local_timezone())
    except ImportError:
        # Fallback to UTC if metadata module is not available
        return datetime.now(timezone.utc)

def retry_on_db_lock(max_attempts=5, initial_wait=0.1, backoff_factor=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < max_attempts - 1:
                        attempt += 1
                        wait_time = initial_wait * (backoff_factor ** attempt) + random.uniform(0, 0.1)
                        logger.warning(f"Database locked. Retrying in {wait_time:.2f} seconds... (Attempt {attempt + 1}/{max_attempts})")
                        time.sleep(wait_time)
                    else:
                        raise
            raise Exception(f"Failed to execute database operation after {max_attempts} attempts due to database locks")
        return wrapper
    return decorator

# Create a base class for declarative models
Base = declarative_base()

# Create a scoped session
Session = scoped_session(sessionmaker())

def init_db():
    global engine
    if engine is not None:
        return engine

    # Get db_content directory from environment variable with fallback
    db_directory = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    os.makedirs(db_directory, exist_ok=True)

    db_path = os.path.join(db_directory, 'cli_battery.db')
    connection_string = f'sqlite:///{db_path}'

    try:
        engine = create_engine(
            connection_string,
            echo=False,
            connect_args={
                'timeout': 60,  # Increased SQLite busy timeout to 60 seconds
                'check_same_thread': False,  # Allow multi-threaded access
            },
            pool_size=20,  # Set a reasonable pool size
            max_overflow=10,  # Allow some overflow connections
            pool_timeout=30,  # Wait up to 30 seconds for a connection
            pool_recycle=1800  # Recycle connections every 30 minutes
        )

        # Configure PRAGMA settings for better concurrency handling
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))  # Use Write-Ahead Logging
            conn.execute(text("PRAGMA busy_timeout=60000"))  # 60 second busy timeout
            conn.execute(text("PRAGMA synchronous=NORMAL"))  # Faster synchronization with reasonable safety
            conn.execute(text("PRAGMA cache_size=-2000"))  # Use 2MB of memory for cache
            conn.execute(text("PRAGMA temp_store=MEMORY"))  # Store temp tables and indices in memory
            conn.execute(text("PRAGMA mmap_size=268435456"))  # Use memory-mapped I/O (256MB)
            conn.commit()

        # Configure the session with the engine
        Session.remove()  # Clear any existing sessions
        Session.configure(bind=engine)

        # Check if tables exist and create them if they don't
        from sqlalchemy import inspect
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        required_tables = {'items', 'metadata', 'seasons', 'episodes', 'posters', 'tmdb_to_imdb_mapping', 'tvdb_to_imdb_mapping'}
        
        if not all(table in existing_tables for table in required_tables):
            logger.info("Some required tables are missing. Creating all tables...")
            try:
                Base.metadata.create_all(engine)
                logger.info("Successfully created all required tables.")
            except Exception as table_error:
                logger.error(f"Error creating tables: {str(table_error)}")
                raise
        else:
            logger.debug("All required tables already exist.")

        # Run migrations for existing tables
        run_migrations(engine)

        return engine
    except OperationalError as oe:
        if "no such table" in str(oe).lower():
            logger.warning("Database tables don't exist. Attempting to create them...")
            try:
                Base.metadata.create_all(engine)
                logger.info("Successfully created database tables.")
                return engine
            except Exception as create_error:
                logger.error(f"Failed to create database tables: {str(create_error)}")
                engine = None
                raise
        else:
            logger.error(f"Database operational error: {str(oe)}")
            engine = None
            raise
    except Exception as e:
        logger.error(f"Failed to connect to cli_battery database at {connection_string}: {str(e)}")
        engine = None  # Reset engine on failure
        raise

def run_migrations(engine):
    """Run database migrations for existing tables."""
    try:
        with engine.connect() as conn:
            # Check if tmdb_to_imdb_mapping table exists and add timestamp columns if needed
            inspector = inspect(engine)
            if 'tmdb_to_imdb_mapping' in inspector.get_table_names():
                columns = [col['name'] for col in inspector.get_columns('tmdb_to_imdb_mapping')]
                
                if 'created_at' not in columns:
                    logger.info("Adding created_at column to tmdb_to_imdb_mapping table...")
                    conn.execute(text("ALTER TABLE tmdb_to_imdb_mapping ADD COLUMN created_at DATETIME"))
                    # Set default timestamp for existing records
                    conn.execute(text("UPDATE tmdb_to_imdb_mapping SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                    logger.info("Successfully added created_at column to tmdb_to_imdb_mapping table.")
                
                if 'updated_at' not in columns:
                    logger.info("Adding updated_at column to tmdb_to_imdb_mapping table...")
                    conn.execute(text("ALTER TABLE tmdb_to_imdb_mapping ADD COLUMN updated_at DATETIME"))
                    # Set default timestamp for existing records
                    conn.execute(text("UPDATE tmdb_to_imdb_mapping SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
                    logger.info("Successfully added updated_at column to tmdb_to_imdb_mapping table.")
            
            conn.commit()
            logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.error(f"Error running database migrations: {str(e)}")
        raise

# Initialize the database engine
engine = None

class Item(Base):
    __tablename__ = 'items'

    id = Column(Integer, primary_key=True)
    imdb_id = Column(String, unique=True, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer)
    type = Column(String)
    created_at = Column(DateTime, default=get_timezone_aware_now)
    updated_at = Column(DateTime, default=get_timezone_aware_now, onupdate=get_timezone_aware_now)
    item_metadata = relationship("Metadata", back_populates="item", cascade="all, delete-orphan")
    seasons = relationship("Season", back_populates="item", cascade="all, delete-orphan")
    poster = relationship("Poster", back_populates="item", uselist=False, cascade="all, delete-orphan")

class Metadata(Base):
    __tablename__ = 'metadata'

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.id'), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=False)  # Ensure JSON type is compatible
    provider = Column(String)
    last_updated = Column(DateTime, default=get_timezone_aware_now, onupdate=get_timezone_aware_now)
    item = relationship("Item", back_populates="item_metadata")

class Season(Base):
    __tablename__ = 'seasons'
    __table_args__ = (UniqueConstraint('item_id', 'season_number', name='uix_item_season'),)

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.id'), nullable=False)
    season_number = Column(Integer, nullable=False)
    episode_count = Column(Integer)
    item = relationship("Item", back_populates="seasons")
    episodes = relationship("Episode", back_populates="season", cascade="all, delete-orphan")

class Episode(Base):
    __tablename__ = 'episodes'
    __table_args__ = (UniqueConstraint('season_id', 'episode_number', name='uix_season_episode'),)

    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey('seasons.id'), nullable=False)
    episode_number = Column(Integer, nullable=False)
    episode_imdb_id = Column(String, unique=True, index=True, nullable=True)
    title = Column(String)
    overview = Column(Text)
    runtime = Column(Integer)
    first_aired = Column(DateTime)
    imdb_id = Column(String)
    season = relationship("Season", back_populates="episodes")

class Poster(Base):
    __tablename__ = 'posters'

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.id'), nullable=False, unique=True)
    image_data = Column(LargeBinary)
    last_updated = Column(DateTime, default=get_timezone_aware_now, onupdate=get_timezone_aware_now)
    item = relationship("Item", back_populates="poster")

class TMDBToIMDBMapping(Base):
    __tablename__ = 'tmdb_to_imdb_mapping'

    id = Column(Integer, primary_key=True)
    tmdb_id = Column(String, unique=True, index=True)
    imdb_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=get_timezone_aware_now)
    updated_at = Column(DateTime, default=get_timezone_aware_now, onupdate=get_timezone_aware_now)

class TVDBToIMDBMapping(Base):
    __tablename__ = 'tvdb_to_imdb_mapping'

    id = Column(Integer, primary_key=True)
    tvdb_id = Column(String, unique=True, index=True)
    imdb_id = Column(String, unique=True, index=True)
    media_type = Column(String)  # 'show' or 'movie'

class DatabaseManager:
    @staticmethod
    @retry_on_db_lock()
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        if engine is None:
            init_db()
            if engine is None:
                 logger.error("Database engine not initialized in add_or_update_item.")
                 raise Exception("Database engine not initialized.")

        with Session() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if item:
                    item.title = title
                    if year is not None:
                        item.year = year
                    if item_type is not None:
                        item.type = item_type
                    item.updated_at = get_timezone_aware_now()
                else:
                    item = Item(imdb_id=imdb_id, title=title, year=year, type=item_type)
                    session.add(item)
                session.commit()
                return item.id
            except OperationalError as oe:
                session.rollback()
                logger.error(f"OperationalError in add_or_update_item: {oe}")
                raise
            except Exception as e:
                session.rollback()
                logger.error(f"Error in add_or_update_item: {e}")
                raise

    @staticmethod
    @retry_on_db_lock()
    def add_or_update_metadata(imdb_id, metadata_dict, provider):
        if engine is None:
            init_db()
            if engine is None:
                 logger.error("Database engine not initialized in add_or_update_metadata.")
                 raise Exception("Database engine not initialized.")

        with Session() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                created_new_item = False
                if not item:
                    item_title = metadata_dict.get('title')
                    if not item_title:
                        logger.warning(f"Attempting to create item for {imdb_id} via metadata but title is missing.")
                        item_title = "Unknown Title - Created from Metadata"
                    item = Item(imdb_id=imdb_id, title=item_title)
                    session.add(item)
                    session.flush()
                    created_new_item = True

                item_type = metadata_dict.get('type')
                if item_type:
                    item.type = item_type
                elif 'aired_episodes' in metadata_dict:
                    item.type = 'show'

                existing_metadata_keys = {md.key for md in item.item_metadata}
                new_metadata_keys = set()

                for key, value in metadata_dict.items():
                    new_metadata_keys.add(key)
                    meta_record = session.query(Metadata).filter_by(item_id=item.id, key=key, provider=provider).first()
                    if meta_record:
                        if meta_record.value != value:
                            meta_record.value = value
                            meta_record.last_updated = get_timezone_aware_now()
                    else:
                        new_meta = Metadata(
                            item_id=item.id, 
                            key=key, 
                            value=value, 
                            provider=provider,
                            last_updated=get_timezone_aware_now()
                        )
                        session.add(new_meta)
                
                session.commit()
                return item.id
            except OperationalError as oe:
                session.rollback()
                logger.error(f"OperationalError in add_or_update_metadata: {oe}")
                raise
            except Exception as e:
                session.rollback()
                logger.error(f"Error in add_or_update_metadata: {e}")
                raise

    @staticmethod
    def get_item(imdb_id):
        with Session() as session:
            return session.query(Item).options(joinedload(Item.item_metadata), joinedload(Item.poster)).filter_by(imdb_id=imdb_id).first()

    @staticmethod
    def get_all_items():
        with Session() as session:
            # Use selectinload for better performance with relationships
            items = session.query(Item).options(
                selectinload(Item.item_metadata),
                selectinload(Item.seasons).selectinload(Season.episodes) # Load seasons and episodes
            ).all()
            # Assign display_year for potential use, though it's handled again in the route
            for item in items:
                year_metadata = next((m.value for m in item.item_metadata if m.key == 'year'), None)
                # You might not need this if display_year is consistently set in the route
                # item.display_year = year_metadata or item.year
            return items

    @staticmethod
    def delete_item(imdb_id):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if item:
                session.delete(item)
                session.commit()
                return True
            return False

    @staticmethod
    def add_or_update_poster(item_id, image_data):
        with Session() as session:
            poster = session.query(Poster).filter_by(item_id=item_id).first()
            if poster:
                poster.image_data = image_data
                poster.last_updated = get_timezone_aware_now()
            else:
                poster = Poster(item_id=item_id, image_data=image_data)
                session.add(poster)
            session.commit()

    @staticmethod
    def get_poster(imdb_id):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if item and item.poster:
                return item.poster.image_data
        return None

    @staticmethod
    def delete_all_items():
        with Session() as session:
            try:
                session.query(Item).delete()
                session.query(Metadata).delete()
                session.query(Season).delete()
                session.query(Poster).delete()
                session.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting all items: {str(e)}")
                session.rollback()
                return False

    @staticmethod
    @retry_on_db_lock(max_attempts=10, initial_wait=0.2)
    def remove_metadata(imdb_id: str) -> bool:
        if engine is None:
            init_db()
            if engine is None:
                 logger.error("Database engine not initialized in remove_metadata.")
                 raise Exception("Database engine not initialized.")

        with Session() as session:
            try:
                item = session.query(Item).options(selectinload(Item.item_metadata)).filter_by(imdb_id=imdb_id).first()
                if not item:
                    logger.warning(f"Item with IMDB ID {imdb_id} not found. Cannot remove metadata.")
                    return False

                if not item.item_metadata:
                    logger.info(f"No metadata found for item IMDB ID {imdb_id} to remove.")
                    return True

                for meta_record in item.item_metadata:
                    session.delete(meta_record)
                
                session.commit()
                logger.info(f"Successfully removed all metadata for item IMDB ID {imdb_id}.")
                return True
            except OperationalError as oe:
                session.rollback()
                logger.error(f"OperationalError in remove_metadata for {imdb_id}: {oe}")
                raise
            except Exception as e:
                session.rollback()
                logger.error(f"Error removing metadata for {imdb_id}: {e}")
                return False

    @staticmethod
    def add_tvdb_to_imdb_mapping(tvdb_id: str, imdb_id: str, media_type: str = 'show') -> bool:
        """Add or update a TVDB to IMDB mapping."""
        with Session() as session:
            try:
                mapping = session.query(TVDBToIMDBMapping).filter_by(tvdb_id=tvdb_id).first()
                if not mapping:
                    mapping = TVDBToIMDBMapping(tvdb_id=tvdb_id, imdb_id=imdb_id, media_type=media_type)
                    session.add(mapping)
                else:
                    mapping.imdb_id = imdb_id
                    mapping.media_type = media_type
                session.commit()
                return True
            except Exception as e:
                logger.error(f"Error adding TVDB to IMDB mapping: {str(e)}")
                session.rollback()
                return False

    @staticmethod
    def get_imdb_from_tvdb(tvdb_id: str) -> Optional[str]:
        """Get IMDB ID from TVDB ID."""
        with Session() as session:
            try:
                mapping = session.query(TVDBToIMDBMapping).filter_by(tvdb_id=tvdb_id).first()
                return mapping.imdb_id if mapping else None
            except Exception as e:
                logger.error(f"Error getting IMDB ID from TVDB ID: {str(e)}")
                return None