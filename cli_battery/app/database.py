from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, LargeBinary, Text
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone
from sqlalchemy import func, inspect as sa_inspect
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
from contextlib import contextmanager


def get_timezone_aware_now():
    """Get current datetime with proper timezone handling"""
    try:
        from metadata.metadata import _get_local_timezone
        return datetime.now(_get_local_timezone())
    except ImportError:
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


def is_valid_imdb_id(imdb_id: str) -> bool:
    """
    Validate IMDb ID format.

    Valid format: tt followed by 7-10 digits (e.g., tt1234567, tt12345678)

    Args:
        imdb_id: The IMDb ID to validate

    Returns:
        True if valid IMDb ID format, False otherwise
    """
    import re
    if not imdb_id:
        return False
    return bool(re.match(r'^tt\d{7,10}$', imdb_id))


def normalize_imdb_id(imdb_id: Optional[str]) -> Optional[str]:
    """
    Normalize IMDb ID by removing trailing slashes and whitespace.
    Returns None if the ID is not a valid IMDb ID format.

    Args:
        imdb_id: The IMDb ID to normalize

    Returns:
        Normalized IMDb ID if valid, None otherwise
    """
    if not imdb_id:
        return None

    # Remove trailing/leading slashes and whitespace
    normalized = imdb_id.strip().rstrip('/')

    # Validate the normalized ID
    if is_valid_imdb_id(normalized):
        return normalized

    # If it's numeric only (likely Trakt/TMDB ID), return None
    if normalized.isdigit():
        logger.warning(f"Rejecting numeric-only ID '{normalized}' - likely Trakt/TMDB ID, not IMDb ID")
        return None

    # If it doesn't match valid format, return None
    logger.warning(f"Rejecting invalid IMDb ID format: '{imdb_id}'")
    return None


Base = declarative_base()

# Scoped session — configured by init_db()
Session = scoped_session(sessionmaker())


@contextmanager
def managed_session():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        Session.remove()


def init_db():
    global engine
    if engine is not None:
        return engine

    db_directory = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    os.makedirs(db_directory, exist_ok=True)

    db_path = os.path.join(db_directory, 'cli_battery.db')
    connection_string = f'sqlite:///{db_path}'

    try:
        engine = create_engine(
            connection_string,
            echo=False,
            connect_args={
                'timeout': 60,
                'check_same_thread': False,
            },
            pool_size=20,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
        )

        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=60000"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
            conn.execute(text("PRAGMA cache_size=-2000"))
            conn.execute(text("PRAGMA temp_store=MEMORY"))
            conn.execute(text("PRAGMA mmap_size=268435456"))
            conn.commit()

        Session.remove()
        Session.configure(bind=engine)

        inspector = sa_inspect(engine)
        existing_tables = inspector.get_table_names()
        required_tables = {'items', 'metadata', 'seasons', 'episodes', 'posters',
                           'tmdb_to_imdb_mapping', 'tvdb_to_imdb_mapping'}

        if not all(table in existing_tables for table in required_tables):
            logger.info("Some required tables are missing. Creating all tables...")
            Base.metadata.create_all(engine)
            logger.info("Successfully created all required tables.")
        else:
            logger.debug("All required tables already exist.")

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
                logger.error(f"Failed to create database tables: {create_error}")
                engine = None
                raise
        else:
            logger.error(f"Database operational error: {oe}")
            engine = None
            raise
    except Exception as e:
        logger.error(f"Failed to connect to cli_battery database at {connection_string}: {e}")
        engine = None
        raise


def run_migrations(eng):
    """Run database migrations for existing tables."""
    try:
        with eng.connect() as conn:
            inspector = sa_inspect(eng)

            # --- tmdb_to_imdb_mapping: add timestamp + media_type columns ---
            if 'tmdb_to_imdb_mapping' in inspector.get_table_names():
                columns = [col['name'] for col in inspector.get_columns('tmdb_to_imdb_mapping')]
                if 'created_at' not in columns:
                    logger.info("Adding created_at column to tmdb_to_imdb_mapping table...")
                    conn.execute(text("ALTER TABLE tmdb_to_imdb_mapping ADD COLUMN created_at DATETIME"))
                    conn.execute(text("UPDATE tmdb_to_imdb_mapping SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                if 'updated_at' not in columns:
                    logger.info("Adding updated_at column to tmdb_to_imdb_mapping table...")
                    conn.execute(text("ALTER TABLE tmdb_to_imdb_mapping ADD COLUMN updated_at DATETIME"))
                    conn.execute(text("UPDATE tmdb_to_imdb_mapping SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
                if 'media_type' not in columns:
                    logger.info("Adding media_type column to tmdb_to_imdb_mapping table...")
                    conn.execute(text("ALTER TABLE tmdb_to_imdb_mapping ADD COLUMN media_type VARCHAR"))
                    # Existing rows without media_type will have NULL — they'll be ignored on typed lookups
                    # and overwritten when next looked up with a media_type

            # --- episodes: add absolute_episode, null_airdate_checked_at, plex_guid ---
            if 'episodes' in inspector.get_table_names():
                columns = [col['name'] for col in inspector.get_columns('episodes')]
                if 'absolute_episode' not in columns:
                    logger.info("Adding absolute_episode column to episodes table...")
                    conn.execute(text("ALTER TABLE episodes ADD COLUMN absolute_episode INTEGER"))
                if 'null_airdate_checked_at' not in columns:
                    logger.info("Adding null_airdate_checked_at column to episodes table...")
                    conn.execute(text("ALTER TABLE episodes ADD COLUMN null_airdate_checked_at DATETIME"))
                if 'plex_guid' not in columns:
                    logger.info("Adding plex_guid column to episodes table...")
                    conn.execute(text("ALTER TABLE episodes ADD COLUMN plex_guid STRING"))
                if 'tmdb_id' not in columns:
                    logger.info("Adding tmdb_id column to episodes table...")
                    conn.execute(text("ALTER TABLE episodes ADD COLUMN tmdb_id STRING"))
                if 'tvdb_id' not in columns:
                    logger.info("Adding tvdb_id column to episodes table...")
                    conn.execute(text("ALTER TABLE episodes ADD COLUMN tvdb_id STRING"))

            # --- seasons: add plex_guid, tmdb_id, tvdb_id ---
            if 'seasons' in inspector.get_table_names():
                columns = [col['name'] for col in inspector.get_columns('seasons')]
                if 'plex_guid' not in columns:
                    logger.info("Adding plex_guid column to seasons table...")
                    conn.execute(text("ALTER TABLE seasons ADD COLUMN plex_guid STRING"))
                if 'tmdb_id' not in columns:
                    logger.info("Adding tmdb_id column to seasons table...")
                    conn.execute(text("ALTER TABLE seasons ADD COLUMN tmdb_id STRING"))
                if 'tvdb_id' not in columns:
                    logger.info("Adding tvdb_id column to seasons table...")
                    conn.execute(text("ALTER TABLE seasons ADD COLUMN tvdb_id STRING"))

            # --- items: add media_status, last_trakt_fetch ---
            if 'items' in inspector.get_table_names():
                columns = [col['name'] for col in inspector.get_columns('items')]
                if 'media_status' not in columns:
                    logger.info("Adding media_status column to items table...")
                    conn.execute(text("ALTER TABLE items ADD COLUMN media_status STRING"))
                if 'last_trakt_fetch' not in columns:
                    logger.info("Adding last_trakt_fetch column to items table...")
                    conn.execute(text("ALTER TABLE items ADD COLUMN last_trakt_fetch DATETIME"))
                    conn.execute(text("UPDATE items SET last_trakt_fetch = updated_at WHERE last_trakt_fetch IS NULL AND updated_at IS NOT NULL"))
                    logger.info("Seeded last_trakt_fetch from updated_at for existing items.")

            conn.commit()
            logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.error(f"Error running database migrations: {e}")
        raise


# Initialize the database engine
engine = None


# ─── ORM Models ──────────────────────────────────────────────────────────────

class Item(Base):
    __tablename__ = 'items'

    id = Column(Integer, primary_key=True)
    imdb_id = Column(String, unique=True, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer)
    type = Column(String)
    created_at = Column(DateTime, default=get_timezone_aware_now)
    updated_at = Column(DateTime, default=get_timezone_aware_now, onupdate=get_timezone_aware_now)
    media_status = Column(String, nullable=True)
    last_trakt_fetch = Column(DateTime, nullable=True)
    item_metadata = relationship("Metadata", back_populates="item", cascade="all, delete-orphan")
    seasons = relationship("Season", back_populates="item", cascade="all, delete-orphan")
    poster = relationship("Poster", back_populates="item", uselist=False, cascade="all, delete-orphan")


class Metadata(Base):
    __tablename__ = 'metadata'

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.id'), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=False)
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
    plex_guid = Column(String, nullable=True)
    tmdb_id = Column(String, nullable=True)
    tvdb_id = Column(String, nullable=True)
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
    tmdb_id = Column(String, nullable=True)
    tvdb_id = Column(String, nullable=True)
    absolute_episode = Column(Integer, nullable=True)
    null_airdate_checked_at = Column(DateTime, nullable=True)
    plex_guid = Column(String, nullable=True)
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
    tmdb_id = Column(String, index=True)
    media_type = Column(String)  # 'movie' or 'show' — prevents cross-type cache collisions
    imdb_id = Column(String, index=True)
    created_at = Column(DateTime, default=get_timezone_aware_now)
    updated_at = Column(DateTime, default=get_timezone_aware_now, onupdate=get_timezone_aware_now)


class TVDBToIMDBMapping(Base):
    __tablename__ = 'tvdb_to_imdb_mapping'

    id = Column(Integer, primary_key=True)
    tvdb_id = Column(String, unique=True, index=True)
    imdb_id = Column(String, unique=True, index=True)
    media_type = Column(String)


class MigrationFlag(Base):
    __tablename__ = 'migration_flags'

    id = Column(Integer, primary_key=True)
    migration_name = Column(String, unique=True, index=True)
    completed_at = Column(DateTime, default=get_timezone_aware_now)
    description = Column(String)


# ─── DatabaseManager ─────────────────────────────────────────────────────────

class DatabaseManager:
    @staticmethod
    @retry_on_db_lock()
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        if engine is None:
            init_db()
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
            except Exception as e:
                session.rollback()
                logger.error(f"Error in add_or_update_item: {e}")
                raise

    @staticmethod
    @retry_on_db_lock()
    def add_or_update_metadata(imdb_id, metadata_dict, provider):
        if engine is None:
            init_db()
        with Session() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    item_title = metadata_dict.get('title') or "Unknown Title - Created from Metadata"
                    item = Item(imdb_id=imdb_id, title=item_title)
                    session.add(item)
                    session.flush()

                item_type = metadata_dict.get('type')
                if item_type:
                    item.type = item_type
                elif 'aired_episodes' in metadata_dict:
                    item.type = 'show'

                for key, value in metadata_dict.items():
                    meta_record = session.query(Metadata).filter_by(item_id=item.id, key=key, provider=provider).first()
                    if meta_record:
                        if meta_record.value != value:
                            meta_record.value = value
                            meta_record.last_updated = get_timezone_aware_now()
                    else:
                        new_meta = Metadata(
                            item_id=item.id, key=key, value=value,
                            provider=provider, last_updated=get_timezone_aware_now(),
                        )
                        session.add(new_meta)

                session.commit()
                return item.id
            except Exception as e:
                session.rollback()
                logger.error(f"Error in add_or_update_metadata: {e}")
                raise

    @staticmethod
    def get_item(imdb_id):
        with Session() as session:
            return session.query(Item).options(
                joinedload(Item.item_metadata), joinedload(Item.poster)
            ).filter_by(imdb_id=imdb_id).first()

    @staticmethod
    def get_all_items():
        with Session() as session:
            return session.query(Item).options(
                selectinload(Item.item_metadata),
                selectinload(Item.seasons).selectinload(Season.episodes),
            ).all()

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
                logger.error(f"Error deleting all items: {e}")
                session.rollback()
                return False

    @staticmethod
    @retry_on_db_lock(max_attempts=10, initial_wait=0.2)
    def remove_metadata(imdb_id: str) -> bool:
        if engine is None:
            init_db()
        with Session() as session:
            try:
                item = session.query(Item).options(selectinload(Item.item_metadata)).filter_by(imdb_id=imdb_id).first()
                if not item:
                    return False
                if not item.item_metadata:
                    return True
                for meta_record in item.item_metadata:
                    session.delete(meta_record)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                logger.error(f"Error removing metadata for {imdb_id}: {e}")
                return False

    @staticmethod
    def add_tvdb_to_imdb_mapping(tvdb_id: str, imdb_id: str, media_type: str = 'show') -> bool:
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
                logger.error(f"Error adding TVDB to IMDB mapping: {e}")
                session.rollback()
                return False

    @staticmethod
    def get_imdb_from_tvdb(tvdb_id: str) -> Optional[str]:
        with Session() as session:
            try:
                mapping = session.query(TVDBToIMDBMapping).filter_by(tvdb_id=tvdb_id).first()
                return mapping.imdb_id if mapping else None
            except Exception as e:
                logger.error(f"Error getting IMDB ID from TVDB ID: {e}")
                return None

    @staticmethod
    @retry_on_db_lock()
    def check_migration_flag(migration_name: str) -> bool:
        if engine is None:
            init_db()
        with Session() as session:
            try:
                flag = session.query(MigrationFlag).filter_by(migration_name=migration_name).first()
                return flag is not None
            except Exception as e:
                logger.error(f"Error checking migration flag {migration_name}: {e}")
                return False

    @staticmethod
    @retry_on_db_lock()
    def set_migration_flag(migration_name: str, description: str = "") -> bool:
        if engine is None:
            init_db()
        with Session() as session:
            try:
                flag = MigrationFlag(migration_name=migration_name, description=description)
                session.add(flag)
                session.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting migration flag {migration_name}: {e}")
                session.rollback()
                return False

    @staticmethod
    @retry_on_db_lock()
    def clear_migration_flag(migration_name: str) -> bool:
        if engine is None:
            init_db()
        with Session() as session:
            try:
                flag = session.query(MigrationFlag).filter_by(migration_name=migration_name).first()
                if flag:
                    session.delete(flag)
                    session.commit()
                    return True
                return False
            except Exception as e:
                logger.error(f"Error clearing migration flag {migration_name}: {e}")
                session.rollback()
                return False

    @staticmethod
    @retry_on_db_lock()
    def list_migration_flags() -> list:
        if engine is None:
            init_db()
        with Session() as session:
            try:
                flags = session.query(MigrationFlag).all()
                return [{
                    'migration_name': flag.migration_name,
                    'completed_at': flag.completed_at,
                    'description': flag.description,
                } for flag in flags]
            except Exception as e:
                logger.error(f"Error listing migration flags: {e}")
                return []
