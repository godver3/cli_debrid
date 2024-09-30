from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, LargeBinary, Text, JSON
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from sqlalchemy.ext.declarative import declarative_base
from flask import current_app, jsonify
from datetime import datetime
from sqlalchemy import or_, func, cast, String
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError
from .logger_config import logger
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.types import JSON  # Add this import
import os  # Add this import


# Create a base class for declarative models
Base = declarative_base()

# Create a scoped session
Session = scoped_session(sessionmaker())

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
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        with Session() as session:
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

    @staticmethod
    def add_or_update_metadata(imdb_id, metadata_dict, provider):
        with Session() as session:
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

def init_db(app):
    connection_string = 'sqlite:////user/db_content/cli_battery.db'

    try:
        print(f"Attempting to connect to database: {connection_string}")
        engine = create_engine(connection_string, echo=False)

        # Ensure the directory exists
        os.makedirs('/user/db_content', exist_ok=True)

        # Test the connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Bind the engine to the session and create tables
        Session.configure(bind=engine)
        Base.metadata.bind = engine
        Base.metadata.create_all(engine)

        logger.info(f"Successfully connected to database: {connection_string}")
        logger.info("All database tables created successfully.")
        return engine
    except Exception as e:
        logger.error(f"Failed to connect to {connection_string}: {str(e)}")
        logger.critical("Database connection failed.")
        raise

# Initialize the database connection
engine = init_db(current_app)
