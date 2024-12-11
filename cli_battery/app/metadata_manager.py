from .database import DatabaseManager, Session, Item, Metadata, Season, Episode, TMDBToIMDBMapping
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, cast, String, or_
from sqlalchemy.orm import joinedload
from .trakt_metadata import TraktMetadata
from PIL import Image
from .logger_config import logger
import requests
from io import BytesIO
from .settings import Settings
import json
from sqlalchemy.orm import selectinload
from sqlalchemy.dialects.postgresql import JSON, insert
from sqlalchemy.exc import IntegrityError
import iso8601
from collections import defaultdict
from .settings import Settings
from datetime import datetime, timezone
import random

class MetadataManager:

    def __init__(self):
        self.base_url = TraktMetadata()

    @staticmethod
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        return DatabaseManager.add_or_update_item(imdb_id, title, year, item_type)

    @staticmethod
    def add_or_update_metadata(imdb_id, metadata_dict, provider):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                logger.error(f"Item with IMDB ID {imdb_id} not found when adding metadata.")
                return False

            for key, value in metadata_dict.items():
                metadata = session.query(Metadata).filter_by(item_id=item.id, key=key).first()
                if not metadata:
                    metadata = Metadata(item_id=item.id, key=key)
                    session.add(metadata)
                
                # Store all values as strings, without JSON encoding
                metadata.value = str(value)
                metadata.provider = provider
                metadata.last_updated = func.now()
            session.commit()
        return True

    @staticmethod
    def is_metadata_stale(last_updated):
        settings = Settings()
        if last_updated is None:
            return True
        
        # Convert last_updated to UTC if it's not already
        if last_updated.tzinfo is None or last_updated.tzinfo.utcoffset(last_updated) is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        
        # Add random variation to the staleness threshold
        day_variation = random.choice([-5, -3, -1, 1, 3, 5])
        hour_variation = random.randint(-12, 12)
        
        adjusted_threshold = max(settings.staleness_threshold + day_variation, 1)
        
        stale_threshold = timedelta(days=adjusted_threshold, hours=hour_variation)
        is_stale = (now - last_updated) > stale_threshold
                
        return is_stale

    @staticmethod
    def debug_find_item(imdb_id):
        with Session() as session:
            items = session.query(Item).filter(
                or_(
                    Item.imdb_id == imdb_id,
                    Item.imdb_id == imdb_id.lower(),
                    Item.imdb_id == imdb_id.upper()
                )
            ).all()

    @staticmethod
    def get_item(imdb_id):
        return DatabaseManager.get_item(imdb_id)

    @staticmethod
    def get_all_items():
        return DatabaseManager.get_all_items()

    @staticmethod
    def delete_item(imdb_id):
        return DatabaseManager.delete_item(imdb_id)

    @staticmethod
    def add_or_update_poster(item_id, image_data):
        DatabaseManager.add_or_update_poster(item_id, image_data)

    @staticmethod
    def get_poster(imdb_id):
        poster_data = DatabaseManager.get_poster(imdb_id)
        if poster_data:
            return poster_data

        # If poster not in database, fetch from Trakt
        trakt = TraktMetadata()
        poster_url = trakt.get_poster(imdb_id)
        if poster_url:
            response = requests.get(poster_url)
            if response.status_code == 200:
                image = Image.open(BytesIO(response.content))
                image_data = BytesIO()
                image.save(image_data, format='JPEG')
                image_data = image_data.getvalue()

                # Save poster to database
                MetadataManager.add_or_update_poster(imdb_id, image_data)

                return image_data

        return None

    @staticmethod
    def get_stats():
        with Session() as session:
            total_items = session.query(func.count(Item.id)).scalar()
            total_metadata = session.query(func.count(Metadata.id)).scalar()
            providers = session.query(Metadata.provider, func.count(Metadata.id)).group_by(Metadata.provider).all()
            last_update = session.query(func.max(Metadata.last_updated)).scalar()

            return {
                'total_items': total_items,
                'total_metadata': total_metadata,
                'providers': dict(providers),
                'last_update': last_update
            }
            
    @staticmethod
    def get_seasons(imdb_id):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
            if item:
                seasons = session.query(Season).filter_by(item_id=item.id).options(selectinload(Season.episodes)).all()
                if seasons:
                    # Check if the seasons data is stale
                    if MetadataManager.is_metadata_stale(item.updated_at):
                        return MetadataManager.refresh_seasons(imdb_id, session)
                    else:
                        seasons_data = MetadataManager.format_seasons_data(seasons)
                        return seasons_data, "battery"

        # If not in database or if seasons are missing, fetch from Trakt
        return MetadataManager.refresh_seasons(imdb_id, session)

    @staticmethod
    def refresh_seasons(imdb_id, session):
        trakt = TraktMetadata()
        seasons_data, source = trakt.get_show_seasons_and_episodes(imdb_id)
        if seasons_data:
            MetadataManager.add_or_update_seasons_and_episodes(imdb_id, seasons_data)
            return seasons_data, source
        logger.warning(f"No seasons data found for IMDB ID: {imdb_id}")
        return None, None

    @staticmethod
    def format_seasons_data(seasons):
        seasons_data = {}
        for season in seasons:
            seasons_data[season.season_number] = {
                'episode_count': season.episode_count,
                'episodes': {
                    episode.episode_number: {
                        'title': episode.title,
                        'overview': episode.overview,
                        'runtime': episode.runtime,
                        'first_aired': episode.first_aired.isoformat() if episode.first_aired else None,
                        'imdb_id': episode.imdb_id
                    } for episode in season.episodes
                }
            }
        return seasons_data

    @staticmethod
    def add_or_update_seasons_and_episodes(imdb_id, seasons_data):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                logger.error(f"Item with IMDB ID {imdb_id} not found when adding seasons and episodes.")
                return False

            for season_number, season_info in seasons_data.items():
                season = session.query(Season).filter_by(item_id=item.id, season_number=season_number).first()
                if not season:
                    season = Season(item_id=item.id, season_number=season_number, episode_count=season_info['episode_count'])
                    session.add(season)
                else:
                    season.episode_count = season_info['episode_count']

                for episode_number, episode_info in season_info['episodes'].items():
                    episode = session.query(Episode).filter_by(season_id=season.id, episode_number=episode_number).first()
                    if not episode:
                        episode = Episode(
                            season_id=season.id,
                            episode_number=episode_number,
                            title=episode_info['title'],
                            overview=episode_info['overview'],
                            runtime=episode_info['runtime'],
                            first_aired=iso8601.parse_date(episode_info['first_aired']) if episode_info['first_aired'] else None,
                            imdb_id=episode_info['imdb_id']
                        )
                        session.add(episode)
                    else:
                        episode.title = episode_info['title']
                        episode.overview = episode_info['overview']
                        episode.runtime = episode_info['runtime']
                        episode.first_aired = iso8601.parse_date(episode_info['first_aired']) if episode_info['first_aired'] else None
                        episode.imdb_id = episode_info['imdb_id']

            session.commit()
            return True

    @staticmethod
    def _process_trakt_seasons(imdb_id, seasons_data, episodes_data):

        if isinstance(episodes_data, dict):
            # If episodes_data is a dict, we assume it's structured as {season_number: [episodes]}
            processed_data = {}
            for season_number, episodes in episodes_data.items():
                processed_data[str(season_number)] = {
                    'episode_count': len(episodes),
                    'episodes': episodes
                }
        elif isinstance(episodes_data, list):
            processed_data = {}
            for season in seasons_data:
                season_number = season['number']
                season_episodes = [ep for ep in episodes_data if ep.get('season') == season_number]
                processed_data[str(season_number)] = {
                    'episode_count': len(season_episodes),
                    'episodes': season_episodes
                }
        else:
            logger.error(f"Unexpected episodes_data type for IMDb ID {imdb_id}: {type(episodes_data)}")
            return {}

        return processed_data

    @staticmethod
    def get_episodes(imdb_id, season_number):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                return {}

            season = session.query(Season).filter_by(item_id=item.id, season_number=season_number).first()
            if not season:
                return {}

            episodes = session.query(Episode).filter_by(season_id=season.id).all()
            return {
                str(episode.episode_number): {
                    'first_aired': episode.first_aired.isoformat() if episode.first_aired else None,
                    'runtime': episode.runtime,
                    'title': episode.title
                } for episode in episodes
            }
                
    @staticmethod
    def add_or_update_seasons(imdb_id, seasons_data, provider):
        with Session() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    trakt = TraktMetadata()
                    show_metadata = trakt.get_show_metadata(imdb_id)
                    if show_metadata:
                        item = Item(
                            imdb_id=imdb_id,
                            title=show_metadata.get('title', 'Unknown Title'),
                            year=show_metadata.get('year'),
                            type='show'
                        )
                        session.add(item)
                        session.flush()
                    else:
                        logger.error(f"Failed to fetch metadata for IMDB ID: {imdb_id}")
                        return False

                # Prepare bulk upsert data
                upsert_data = [
                    {
                        'item_id': item.id,
                        'season_number': season_data['number'],  # Changed from 'season' to 'number'
                        'episode_count': season_data['episode_count']
                    }
                    for season_data in seasons_data
                ]

                # Perform bulk upsert
                stmt = insert(Season).values(upsert_data)
                stmt = stmt.on_conflict_do_update(
                    constraint='uix_item_season',  # Use the constraint name we defined earlier
                    set_=dict(episode_count=stmt.excluded.episode_count)
                )
                session.execute(stmt)

                session.commit()
                return True
            except IntegrityError as e:
                session.rollback()
                logger.error(f"IntegrityError while updating seasons for {imdb_id}: {str(e)}")
                return False
            except Exception as e:
                session.rollback()
                logger.error(f"Unexpected error while updating seasons for {imdb_id}: {str(e)}")
                return False

    @staticmethod
    def get_specific_metadata(imdb_id, key):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                return None

            metadata = next((m for m in item.item_metadata if m.key == key), None)
            if not metadata:
                new_metadata = MetadataManager.refresh_metadata(imdb_id)
                return {key: new_metadata.get(key)}

            if MetadataManager.is_metadata_stale(item):
                new_metadata = MetadataManager.refresh_metadata(imdb_id)
                return {key: new_metadata.get(key, json.loads(metadata.value))}

            return {key: json.loads(metadata.value)}

    @staticmethod
    def refresh_metadata(imdb_id):
        trakt = TraktMetadata()
        new_metadata = trakt.refresh_metadata(imdb_id)
        if new_metadata:
            with Session() as session:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if item:
                    for key, value in new_metadata.items():
                        MetadataManager.add_or_update_metadata(item.id, key, value, 'Trakt')
                    item.updated_at = datetime.utcnow()
                    session.commit()
        return new_metadata

    # TODO: Implement method to refresh metadata from enabled providers
    @staticmethod
    def refresh_trakt_metadata(self, imdb_id: str) -> None:
        trakt = TraktMetadata()
        new_metadata = trakt.refresh_metadata(imdb_id)
        if new_metadata:
            for key, value in new_metadata.items():
                self.add_or_update_metadata(imdb_id, key, value, 'Trakt')

    @staticmethod
    def update_provider_rank(provider_name, rank_type, new_rank):
        settings = Settings()
        providers = settings.providers
        
        for provider in providers:
            if provider['name'] == provider_name:
                if rank_type == 'metadata':
                    provider['metadata_rank'] = int(new_rank)
                elif rank_type == 'poster':
                    provider['poster_rank'] = int(new_rank)
                break
        
        # Ensure all providers have both rank types
        for provider in providers:
            if 'metadata_rank' not in provider:
                provider['metadata_rank'] = len(providers)  # Default to last rank
            if 'poster_rank' not in provider:
                provider['poster_rank'] = len(providers)  # Default to last rank
        
        # Re-sort providers based on new ranks
        providers.sort(key=lambda x: (x.get('metadata_rank', len(providers)), x.get('poster_rank', len(providers))))
        
        settings.providers = providers
        settings.save()

    @staticmethod
    def get_ranked_providers(rank_type):
        settings = Settings()
        providers = settings.providers
        return sorted([p for p in providers if p['enabled']], key=lambda x: x[f'{rank_type}_rank'])
    
    @staticmethod
    def add_or_update_episodes(imdb_id, episodes_data, provider):
        with Session() as session:
            try:
                # If episodes_data is a string, try to parse it as JSON
                if isinstance(episodes_data, str):
                    try:
                        episodes_data = json.loads(episodes_data)
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse episodes_data as JSON for IMDB ID {imdb_id}")
                        return False

                # Ensure episodes_data is a list
                if not isinstance(episodes_data, list):
                    logger.error(f"Unexpected episodes_data type for IMDB ID {imdb_id}: {type(episodes_data)}")
                    return False

                item = session.query(Item).options(joinedload(Item.seasons)).filter_by(imdb_id=imdb_id).first()
                if not item:
                    logger.error(f"Item with IMDB ID {imdb_id} not found when adding episodes.")
                    return False

                # Create a dictionary to map season numbers to season ids
                season_map = {season.season_number: season.id for season in item.seasons}

                # Prepare bulk upsert data
                upsert_data = []
                for episode_data in episodes_data:
                    if not isinstance(episode_data, dict):
                        logger.warning(f"Skipping invalid episode data for IMDB ID {imdb_id}: {episode_data}")
                        continue

                    season_number = episode_data.get('season')
                    episode_number = episode_data.get('episode')
                    episode_imdb_id = episode_data.get('imdb_id')

                    if season_number is None or episode_number is None:
                        logger.warning(f"Skipping episode data without season or episode number for IMDB ID {imdb_id}")
                        continue

                    season_id = season_map.get(season_number)
                    if not season_id:
                        logger.warning(f"Season {season_number} not found for IMDB ID {imdb_id}. Skipping episode.")
                        continue

                    upsert_data.append({
                        'season_id': season_id,
                        'episode_number': episode_number,
                        'title': episode_data.get('title', ''),
                        'overview': episode_data.get('overview', ''),
                        'runtime': episode_data.get('runtime', 0),
                        'first_aired': episode_data.get('first_aired'),
                        'imdb_id': episode_imdb_id
                    })

                if not upsert_data:
                    logger.warning(f"No valid episode data found for IMDB ID {imdb_id}")
                    return False

                # Perform bulk upsert
                stmt = insert(Episode).values(upsert_data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['season_id', 'episode_number'],
                    set_=dict(
                        title=stmt.excluded.title,
                        overview=stmt.excluded.overview,
                        runtime=stmt.excluded.runtime,
                        first_aired=stmt.excluded.first_aired,
                        imdb_id=stmt.excluded.imdb_id
                    )
                )
                session.execute(stmt)

                session.commit()
                return True

            except Exception as e:
                session.rollback()
                logger.error(f"Error updating episodes for IMDB ID {imdb_id}: {str(e)}")
                return False

    @staticmethod
    def get_release_dates(imdb_id):
        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if item:
                metadata = session.query(Metadata).filter_by(item_id=item.id, key='release_dates').first()
                if metadata:
                    if MetadataManager.is_metadata_stale(metadata.last_updated):
                        return MetadataManager.refresh_release_dates(imdb_id, session)
                    else:
                        try:
                            value = json.loads(metadata.value)
                            return value, "battery"
                        except json.JSONDecodeError:
                            logger.error(f"Error decoding JSON for release dates of IMDB ID: {imdb_id}")
                            return MetadataManager.refresh_release_dates(imdb_id, session)

            # Fetch from Trakt if not in database or if metadata is missing
            return MetadataManager.refresh_release_dates(imdb_id, session)

    @staticmethod
    def refresh_release_dates(imdb_id, session):
        trakt = TraktMetadata()
        trakt_release_dates = trakt.get_release_dates(imdb_id)
        if trakt_release_dates:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                item = Item(imdb_id=imdb_id)
                session.add(item)
                session.flush()

            metadata = session.query(Metadata).filter_by(item_id=item.id, key='release_dates').first()
            if not metadata:
                metadata = Metadata(item_id=item.id, key='release_dates')
                session.add(metadata)

            metadata.value = json.dumps(trakt_release_dates)
            metadata.provider = 'Trakt'
            metadata.last_updated = func.now()

            session.commit()
            return trakt_release_dates, "trakt"
        logger.warning(f"No release dates found for IMDB ID: {imdb_id}")
        return None, None


    @staticmethod
    def tmdb_to_imdb(tmdb_id, media_type=None):
        with Session() as session:
            cached_mapping = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
            if cached_mapping:
                return cached_mapping.imdb_id, 'battery'

            trakt = TraktMetadata()
            imdb_id, source = trakt.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)
            
            if imdb_id:
                new_mapping = TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id)
                session.add(new_mapping)
                session.commit()
            else:
                logger.warning(f"No IMDB ID found for TMDB ID {tmdb_id} with type {media_type}")
            
            return imdb_id, source
                
    @staticmethod
    def get_metadata_by_episode_imdb(episode_imdb_id):
        with Session() as session:
            # Find the episode by IMDb ID
            episode = session.query(Episode).join(Season).join(Item).filter(
                Episode.imdb_id == episode_imdb_id
            ).first()

            if episode:
                show = episode.season.item
                show_metadata = {}
                for m in show.item_metadata:
                    try:
                        show_metadata[m.key] = json.loads(m.value) if isinstance(m.value, str) else m.value
                    except json.JSONDecodeError:
                        show_metadata[m.key] = m.value

                episode_data = {
                    'title': episode.title,
                    'overview': episode.overview,
                    'runtime': episode.runtime,
                    'first_aired': episode.first_aired.isoformat() if episode.first_aired else None,
                    'imdb_id': episode.imdb_id,
                    'season_number': episode.season.season_number,
                    'episode_number': episode.episode_number
                }

                return {'show': show_metadata, 'episode': episode_data}, "battery"

        # If not in database, fetch from Trakt
        trakt = TraktMetadata()
        trakt_data = trakt.get_episode_metadata(episode_imdb_id)
        if trakt_data:
            show_imdb_id = trakt_data['show']['imdb_id']
            show_metadata = trakt_data['show']['metadata']
            episode_data = trakt_data['episode']

            # Save episode and show metadata
            with Session() as session:
                item = session.query(Item).filter_by(imdb_id=show_imdb_id).first()
                if not item:
                    item = Item(imdb_id=show_imdb_id, title=show_metadata.get('title'), type='show', year=show_metadata.get('year'))
                    session.add(item)
                    session.flush()

                season_number = episode_data.get('season')
                season = session.query(Season).filter_by(item_id=item.id, season_number=season_number).first()
                if not season:
                    season = Season(item_id=item.id, season_number=season_number)
                    session.add(season)
                    session.flush()

                episode = Episode(
                    season_id=season.id,
                    episode_number=episode_data['number'],
                    title=episode_data.get('title', ''),
                    overview=episode_data.get('overview', ''),
                    runtime=episode_data.get('runtime', 0),
                    first_aired=episode_data.get('first_aired', None),
                    imdb_id=episode_imdb_id  # Set the IMDb ID
                )
                session.add(episode)
                session.commit()

            return {'show': show_metadata, 'episode': episode_data}, "trakt"

        return None, None

    @staticmethod
    def get_movie_metadata(imdb_id):
        with Session() as session:
            item = session.query(Item).options(joinedload(Item.item_metadata)).filter_by(imdb_id=imdb_id, type='movie').first()
            if not item:
                return MetadataManager.refresh_movie_metadata(imdb_id)

            metadata = {}
            for m in item.item_metadata:
                # No need to attempt JSON decoding, just use the value as is
                metadata[m.key] = m.value

            if MetadataManager.is_metadata_stale(item.updated_at):
                return MetadataManager.refresh_movie_metadata(imdb_id)

            return metadata, "battery"
            
    @staticmethod
    def refresh_movie_metadata(imdb_id):
        trakt = TraktMetadata()
        new_metadata = trakt.get_movie_metadata(imdb_id)
        if new_metadata:
            MetadataManager.add_or_update_item(imdb_id, new_metadata.get('title'), new_metadata.get('year'), 'movie')
            MetadataManager.add_or_update_metadata(imdb_id, new_metadata, 'Trakt')
            return new_metadata, "trakt"
        logger.warning(f"Could not fetch metadata for movie {imdb_id} from Trakt")
        return None, None

    @staticmethod
    def update_movie_metadata(item, movie_data, session):
        item.updated_at = datetime.now(timezone.utc)
        session.query(Metadata).filter_by(item_id=item.id).delete()
        for key, value in movie_data.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value)
            metadata = Metadata(item_id=item.id, key=key, value=str(value), provider='trakt')
            session.add(metadata)
        session.commit()


    @staticmethod
    def get_show_metadata(imdb_id):

        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
            if item:
                # Ensure item.updated_at is timezone-aware
                if item.updated_at.tzinfo is None:
                    item.updated_at = item.updated_at.replace(tzinfo=timezone.utc)
                
                if MetadataManager.is_metadata_stale(item.updated_at):
                    trakt = TraktMetadata()
                    show_data = trakt.get_show_metadata(imdb_id)
                    if show_data:
                        MetadataManager.update_show_metadata(item, show_data, session)
                        return show_data, "trakt (refreshed)"
                else:
                    metadata = session.query(Metadata).filter_by(item_id=item.id).all()
                    metadata_dict = {}
                    for m in metadata:
                        try:
                            metadata_dict[m.key] = json.loads(m.value) if isinstance(m.value, str) else m.value
                        except json.JSONDecodeError:
                            metadata_dict[m.key] = m.value

                    return metadata_dict, "battery"

            # Fetch from Trakt if not in database
            trakt = TraktMetadata()
            show_data = trakt.get_show_metadata(imdb_id)
            if show_data:
                try:
                    item = Item(imdb_id=imdb_id, title=show_data.get('title'), type='show', year=show_data.get('year'))
                    session.add(item)
                    session.flush()
                    MetadataManager.update_show_metadata(item, show_data, session)
                except IntegrityError:
                    session.rollback()
                    logger.warning(f"IntegrityError occurred. Item may already exist for IMDB ID: {imdb_id}")
                    # Update existing item and metadata

                return show_data, "trakt"

            logger.warning(f"No show metadata found for IMDB ID: {imdb_id}")
            return None, None

    @staticmethod
    def update_show_metadata(item, show_data, session):
        item.updated_at = datetime.now(timezone.utc)
        session.query(Metadata).filter_by(item_id=item.id).delete()
        for key, value in show_data.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value)
            metadata = Metadata(item_id=item.id, key=key, value=str(value), provider='trakt')
            session.add(metadata)
        session.commit()

