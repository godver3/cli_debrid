from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, LargeBinary, Text, JSON
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from sqlalchemy.ext.declarative import declarative_base
from flask import current_app, jsonify
from datetime import datetime
from sqlalchemy import or_, func, cast, String
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError, OperationalError
from .logger_config import logger
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.types import JSON
import os
import time
import random
from functools import wraps

def retry_on_db_lock(max_attempts=5, initial_wait=0.1, backoff_factor=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < max_retries - 1:
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

_engine = None

def init_db():
    global _engine
    if _engine is not None:
        return _engine

    # Get db_content directory from environment variable with fallback
    db_directory = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    os.makedirs(db_directory, exist_ok=True)

    db_path = os.path.join(db_directory, 'cli_battery.db')
    connection_string = f'sqlite:///{db_path}'

    try:
        print(f"Attempting to connect to database: {connection_string}")
        _engine = create_engine(
            connection_string,
            echo=False,
            connect_args={
                'timeout': 30,  # Increase SQLite busy timeout
                'check_same_thread': False,  # Allow multi-threaded access
            }
        )

        # Set PRAGMA statements and test connection
        with _engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Set PRAGMA statements (these run outside of transaction control)
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA busy_timeout=30000")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            
            # Test connection
            conn.exec_driver_sql("SELECT 1")

        # Configure the session with the engine
        Session.configure(bind=_engine)

        # Create tables
        Base.metadata.create_all(_engine)

        print(f"Successfully connected to cli_battery database: {connection_string}")
        return _engine
    except Exception as e:
        print(f"Failed to connect to cli_battery database at {connection_string}: {str(e)}")
        print("Database connection failed.")
        raise

class Item(Base):
    __tablename__ = 'items'

    id = Column(Integer, primary_key=True)
    imdb_id = Column(String, unique=True, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer)
    type = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
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
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
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

    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey('seasons.id'), nullable=False)
    episode_number = Column(Integer, nullable=False)
    episode_imdb_id = Column(String, unique=True, index=True)
    title = Column(String)
    overview = Column(Text)
    runtime = Column(Integer)
    first_aired = Column(DateTime)
    season = relationship("Season", back_populates="episodes")
    imdb_id = Column(String)  # Add this line to include the imdb_id column

    # Relationships
    season = relationship('Season', back_populates='episodes')

class Poster(Base):
    __tablename__ = 'posters'

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.id'), nullable=False, unique=True)
    image_data = Column(LargeBinary)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    item = relationship("Item", back_populates="poster")

class TMDBToIMDBMapping(Base):
    __tablename__ = 'tmdb_to_imdb_mapping'

    id = Column(Integer, primary_key=True)
    tmdb_id = Column(String, unique=True, index=True)
    imdb_id = Column(String, unique=True, index=True)

class DatabaseManager:
    @staticmethod
    @retry_on_db_lock()
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        with Session() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if item:
                    item.title = title
                    if year is not None:
                        item.year = year
                    if item_type is not None:
                        item.type = item_type
                    item.updated_at = datetime.utcnow()
                else:
                    item = Item(imdb_id=imdb_id, title=title, year=year, type=item_type)
                    session.add(item)
                session.commit()
                return item.id
            except Exception as e:
                session.rollback()
                raise

    @staticmethod
    @retry_on_db_lock()
    def add_or_update_metadata(imdb_id, metadata_dict, provider):
        with Session() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    item = Item(imdb_id=imdb_id, title=metadata_dict.get('title', ''))
                    session.add(item)
                    session.flush()

                item_type = metadata_dict.get('type')
                if item_type:
                    item.type = item_type
                elif 'aired_episodes' in metadata_dict:
                    item.type = 'show'
                else:
                    item.type = 'movie'

                now = datetime.utcnow()
                for key, value in metadata_dict.items():
                    if key != 'type':
                        metadata = session.query(Metadata).filter_by(item_id=item.id, key=key).first()
                        if metadata:
                            metadata.value = value
                            metadata.last_updated = now
                        else:
                            metadata = Metadata(item_id=item.id, key=key, value=value, provider=provider, last_updated=now)
                            session.add(metadata)

                session.commit()
            except Exception as e:
                session.rollback()
                raise

    @staticmethod
    def get_item(imdb_id):
        with Session() as session:
            return session.query(Item).options(joinedload(Item.item_metadata), joinedload(Item.poster)).filter_by(imdb_id=imdb_id).first()

    @staticmethod
    def get_all_items():
        with Session() as session:
            items = session.query(Item).options(joinedload(Item.item_metadata)).all()
            for item in items:
                year_metadata = next((m.value for m in item.item_metadata if m.key == 'year'), None)
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
                poster.last_updated = datetime.utcnow()
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
    def remove_metadata(imdb_id: str) -> bool:
        """Remove all metadata entries for a given IMDB ID."""
        with Session() as session:
            try:
                # Get the item
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    logger.warning(f"No item found with IMDB ID {imdb_id}")
                    return False

                # Delete the item itself - this will cascade delete all metadata, seasons, episodes, and poster
                session.delete(item)
                session.commit()
                
                logger.info(f"Successfully removed item and all metadata for IMDB ID {imdb_id}")
                return True
            except Exception as e:
                logger.error(f"Error removing metadata for IMDB ID {imdb_id}: {str(e)}")
                session.rollback()
                return False
