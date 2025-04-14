from .database import DatabaseManager, Session as DbSession, Item, Metadata, Season, Episode, TMDBToIMDBMapping
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
from sqlalchemy.exc import IntegrityError, OperationalError
import iso8601
from collections import defaultdict
from .settings import Settings
from datetime import datetime, timezone
import random
from typing import Optional, Dict, Any, List
import logging
from .xem_utils import fetch_xem_mapping

class MetadataManager:

    def __init__(self):
        self.base_url = TraktMetadata()

    @staticmethod
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        return DatabaseManager.add_or_update_item(imdb_id, title, year, item_type)

    @staticmethod
    def add_or_update_metadata(imdb_id, metadata_dict, provider):
        with DbSession() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                logger.error(f"Item with IMDB ID {imdb_id} not found when adding metadata.")
                return False

            return MetadataManager._update_metadata_with_session(item, metadata_dict, provider, session)

    @staticmethod
    def _update_metadata_with_session(item, metadata_dict, provider, session):
        """Internal method to update metadata using an existing session"""
        try:
            success = False
            # Delete existing metadata first
            session.query(Metadata).filter_by(item_id=item.id).delete()
            session.flush()
            
            # Add new metadata in batches
            batch_size = 50
            metadata_entries = []
            
            # Get current timestamp once for all entries
            from metadata.metadata import _get_local_timezone
            current_time = datetime.now(_get_local_timezone())
            
            for key, value in metadata_dict.items():
                # Convert complex objects to JSON strings
                if isinstance(value, (dict, list)):
                    value = json.dumps(value)
                else:
                    value = str(value)
                
                metadata = Metadata(
                    item_id=item.id,
                    key=key,
                    value=value,
                    provider=provider,
                    last_updated=current_time
                )
                metadata_entries.append(metadata)
                
                # Process in batches to avoid long transactions
                if len(metadata_entries) >= batch_size:
                    session.bulk_save_objects(metadata_entries)
                    session.flush()
                    metadata_entries = []
                    success = True
            
            # Process any remaining entries
            if metadata_entries:
                session.bulk_save_objects(metadata_entries)
                session.flush()
                success = True
            
            if not success:
                logger.warning(f"No metadata entries were updated for {item.title} ({item.imdb_id})")
            return success
        except Exception as e:
            logger.error(f"Error in _update_metadata_with_session for item {item.imdb_id}: {str(e)}")
            session.rollback()
            raise

    @staticmethod
    def is_metadata_stale(last_updated):
        from metadata.metadata import _get_local_timezone

        settings = Settings()
        if last_updated is None:
            logger.debug("Item has no last_updated timestamp, considering stale")
            return True
        
        # Convert last_updated to UTC if it's not already
        if last_updated.tzinfo is None or last_updated.tzinfo.utcoffset(last_updated) is None:
            last_updated = last_updated.replace(tzinfo=_get_local_timezone())

        now = datetime.now(_get_local_timezone())
        
        # Add random variation to the staleness threshold
        day_variation = random.choice([-5, -3, -1, 1, 3, 5])
        hour_variation = random.randint(-12, 12)
        
        adjusted_threshold = max(settings.staleness_threshold + day_variation, 1)
        
        stale_threshold = timedelta(days=adjusted_threshold, hours=hour_variation)
        age = now - last_updated
        is_stale = age > stale_threshold

        if is_stale:
            logger.debug(
                f"Staleness check: last_updated={last_updated.isoformat()}, "
                f"age={age.days}d {age.seconds//3600}h, "
                f"threshold={stale_threshold.days}d {stale_threshold.seconds//3600}h "
                f"(base={settings.staleness_threshold}d, variation={day_variation}d {hour_variation}h) "
                f"-> stale"
            )
                
        return is_stale

    @staticmethod
    def debug_find_item(imdb_id):
        with DbSession() as session:
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
        with DbSession() as session:
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
        with DbSession() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
            if item:
                seasons = session.query(Season).filter_by(item_id=item.id).options(selectinload(Season.episodes)).all()
                if seasons:
                    # Check if any metadata is stale
                    metadata = session.query(Metadata).filter_by(item_id=item.id, key='seasons').first()
                    if metadata and MetadataManager.is_metadata_stale(metadata.last_updated):
                        logger.debug(f"Seasons metadata is stale for {imdb_id}, refreshing from Trakt")
                        # Fetch fresh data from Trakt
                        trakt = TraktMetadata()
                        seasons_data, source = trakt.get_show_seasons_and_episodes(imdb_id)
                        if seasons_data:
                            # Update the database with new data
                            MetadataManager.add_or_update_seasons_and_episodes(imdb_id, seasons_data)
                            return seasons_data, "trakt"
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
        from metadata.metadata import _get_local_timezone
        with DbSession() as session:
            try:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    logger.error(f"Item with IMDB ID {imdb_id} not found when adding seasons and episodes.")
                    return False

                # Update item's timestamp
                item.updated_at = datetime.now(_get_local_timezone())

                # Update metadata timestamp
                metadata = session.query(Metadata).filter_by(item_id=item.id, key='seasons').first()
                if not metadata:
                    metadata = Metadata(item_id=item.id, key='seasons')
                    session.add(metadata)
                metadata.value = json.dumps(seasons_data)
                metadata.provider = 'trakt'
                metadata.last_updated = datetime.now(_get_local_timezone())

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
            except Exception as e:
                session.rollback()
                logger.error(f"Error in add_or_update_seasons_and_episodes: {str(e)}")
                return False

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
        with DbSession() as session:
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
        with DbSession() as session:
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
        with DbSession() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                return None

            metadata = next((m for m in item.item_metadata if m.key == key), None)
            if not metadata:
                new_metadata = MetadataManager.refresh_metadata(imdb_id)
                return {key: new_metadata.get(key)}

            if MetadataManager.is_metadata_stale(item.updated_at):
                new_metadata = MetadataManager.refresh_metadata(imdb_id)
                return {key: new_metadata.get(key, json.loads(metadata.value))}

            try:
                # Always try to parse as JSON first, fall back to string if it fails
                try:
                    return {key: json.loads(metadata.value)}
                except json.JSONDecodeError:
                    return {key: metadata.value}
            except Exception as e:
                logger.error(f"Error processing metadata for key {key}: {str(e)}")
                return {key: metadata.value}

    @staticmethod
    def refresh_metadata(imdb_id, existing_session=None):
        """
        Refresh metadata for an item.
        Args:
            imdb_id: The IMDb ID of the item
            existing_session: Optional existing session to use instead of creating a new one
        """
        trakt = TraktMetadata()
        logger.debug(f"Refreshing metadata for {imdb_id}")
        new_metadata = trakt.refresh_metadata(imdb_id)
        if new_metadata:
            logger.debug(f"Got new metadata for {imdb_id}")
            # Extract the actual metadata from the response
            metadata_to_store = new_metadata.get('metadata', new_metadata)
            if not isinstance(metadata_to_store, dict):
                logger.error(f"Invalid metadata format received for {imdb_id}")
                return None
            
            try:
                # Use existing session if provided, otherwise create a new one
                if existing_session:
                    session = existing_session
                    should_commit = False  # Don't commit if using existing session
                else:
                    session = DbSession()
                    should_commit = True  # Commit if we created the session
                
                try:
                    # Disable autoflush to prevent premature flushes
                    session.autoflush = False
                    
                    item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                    if item:
                        logger.debug(f"Before update: {item.title} last_updated={item.updated_at}")
                        
                        # Update metadata with the same session
                        try:
                            success = MetadataManager._update_metadata_with_session(item, metadata_to_store, 'Trakt', session)
                            if success:
                                # Use Python datetime for consistency
                                from metadata.metadata import _get_local_timezone
                                item.updated_at = datetime.now(_get_local_timezone())
                                
                                if should_commit:
                                    session.commit()
                                else:
                                    session.flush()
                                
                                # Re-query the item instead of using refresh
                                item = session.query(Item).get(item.id)
                                if item:
                                    logger.debug(f"After update: {item.title} last_updated={item.updated_at}")
                                else:
                                    logger.error(f"Failed to re-query item {imdb_id} after update")
                            else:
                                logger.error(f"Failed to update metadata for {imdb_id}")
                                session.rollback()
                                return None
                        except OperationalError as e:
                            if "database is locked" in str(e).lower():
                                logger.warning(f"Database locked while updating metadata for {imdb_id}. Will retry later.")
                                session.rollback()
                                return new_metadata
                            raise
                finally:
                    # Only close the session if we created it
                    if not existing_session:
                        session.close()
            except Exception as e:
                logger.error(f"Error updating metadata for {imdb_id}: {str(e)}")
                return None
        else:
            logger.warning(f"No new metadata received for {imdb_id}")
        return new_metadata

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
        with DbSession() as session:
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
        with DbSession() as session:
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
    def tmdb_to_imdb(tmdb_id: str, media_type: str = None) -> Optional[str]:
        with DbSession() as session:
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
        with DbSession() as session:
            # Find the episode by IMDb ID
            episode = session.query(Episode).join(Season).join(Item).filter(
                Episode.imdb_id == episode_imdb_id
            ).first()

            if episode:
                show = episode.season.item
                show_metadata = {}
                for m in show.item_metadata:
                    try:
                        # Always try to parse as JSON first, fall back to string if it fails
                        try:
                            show_metadata[m.key] = json.loads(m.value)
                        except json.JSONDecodeError:
                            show_metadata[m.key] = m.value
                    except Exception as e:
                        logger.error(f"Error processing metadata for key {m.key}: {str(e)}")
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
            with DbSession() as session:
                item = session.query(Item).filter_by(imdb_id=show_imdb_id).first()
                if not item:
                    item = Item(imdb_id=show_imdb_id)
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
        try:
            with DbSession() as session:
                item = session.query(Item).options(joinedload(Item.item_metadata)).filter_by(imdb_id=imdb_id, type='movie').first()
                if item:
                    metadata = {}
                    for m in item.item_metadata:
                        try:
                            # Always try to parse as JSON first, fall back to string if it fails
                            try:
                                metadata[m.key] = json.loads(m.value)
                            except json.JSONDecodeError:
                                metadata[m.key] = m.value
                        except Exception as e:
                            logger.error(f"Error processing metadata for key {m.key}: {str(e)}")
                            metadata[m.key] = m.value

                    if MetadataManager.is_metadata_stale(item.updated_at):
                        logger.info(f"Movie metadata for {imdb_id} is stale, refreshing from Trakt")
                        return MetadataManager.refresh_movie_metadata(imdb_id)

                    return metadata, "battery"

                # If not in database, try to get from Trakt
                logger.info(f"Movie {imdb_id} not found in database, fetching from Trakt")
                return MetadataManager.refresh_movie_metadata(imdb_id)
        except Exception as e:
            logger.error(f"Error in get_movie_metadata for {imdb_id}: {str(e)}")
            return None, None
            
    @staticmethod
    def refresh_movie_metadata(imdb_id):
        try:
            with DbSession() as session:
                trakt = TraktMetadata()
                new_metadata = trakt.get_movie_metadata(imdb_id)
                if not new_metadata:
                    logger.warning(f"Could not fetch metadata for movie {imdb_id} from Trakt")
                    return None, None

                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    item = Item(imdb_id=imdb_id, title=new_metadata.get('title'), year=new_metadata.get('year'), type='movie')
                    session.add(item)
                    session.flush()
                
                # Clear out old metadata
                session.query(Metadata).filter_by(item_id=item.id).delete()
                
                # Add new metadata
                for key, value in new_metadata.items():
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value)
                    else:
                        value = str(value)
                    metadata = Metadata(item_id=item.id, key=key, value=value, provider='Trakt')
                    session.add(metadata)

                from metadata.metadata import _get_local_timezone
                item.updated_at = datetime.now(_get_local_timezone())
                session.commit()
                return new_metadata, "trakt"
        except Exception as e:
            logger.error(f"Error refreshing movie metadata for {imdb_id}: {str(e)}")
            if 'session' in locals():
                session.rollback()
            return None, None

    @staticmethod
    def update_movie_metadata(item, movie_data, session):
        from metadata.metadata import _get_local_timezone
        item.updated_at = datetime.now(_get_local_timezone())
        session.query(Metadata).filter_by(item_id=item.id).delete()
        for key, value in movie_data.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value)
            metadata = Metadata(item_id=item.id, key=key, value=str(value), provider='trakt')
            session.add(metadata)
        session.commit()

    @staticmethod
    def get_show_metadata(imdb_id):
        logging.info(f"MetadataManager.get_show_metadata called for {imdb_id}")
        with DbSession() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
            if item:
                # Ensure item.updated_at is timezone-aware
                if item.updated_at.tzinfo is None:
                    item.updated_at = item.updated_at.replace(tzinfo=timezone.utc)
                
                metadata = {}
                for m in item.item_metadata:
                    try:
                        # Always try to parse JSON first, fall back to string if it fails
                        try:
                            metadata[m.key] = json.loads(m.value)
                        except json.JSONDecodeError:
                            metadata[m.key] = m.value
                    except Exception as e:
                        logger.error(f"Error processing metadata for key {m.key}: {str(e)}")
                        metadata[m.key] = m.value

                # Check if metadata needs a full refresh (stale or missing seasons)
                needs_full_refresh = ('seasons' not in metadata or MetadataManager.is_metadata_stale(item.updated_at))

                if needs_full_refresh:
                    if 'seasons' not in metadata:
                        logging.info(f"No seasons data found for {imdb_id}, forcing refresh from Trakt")
                    else:
                        logging.info(f"Metadata for {imdb_id} is stale, refreshing from Trakt")
                    
                    # Perform full refresh (this now includes XEM fetch within update_show_metadata)
                    trakt = TraktMetadata()
                    show_data = trakt.get_show_metadata(imdb_id)
                    if show_data:
                        try:
                            logging.info(f"Got show data from Trakt, updating metadata for {imdb_id}")
                            MetadataManager.update_show_metadata(item, show_data, session)
                            logging.info(f"Successfully updated metadata for {imdb_id}")
                            return show_data, "trakt (refreshed)"
                        except Exception as e:
                            logger.error(f"Error saving refreshed show metadata for {imdb_id}: {str(e)}")
                            session.rollback() # Rollback potential partial changes from update_show_metadata
                            # Return the fetched data even if save failed, but indicate the source issue
                            return show_data, "trakt (refresh save failed)" 
                    else:
                         # If refresh fails, return existing (stale) data if available
                         logger.warning(f"Failed to refresh metadata for {imdb_id}. Returning potentially stale data from battery.")
                         return metadata, "battery (stale, refresh failed)"

                else:
                    # Metadata is fresh, return from battery, but check for missing XEM mapping
                    logging.info(f"Metadata for {imdb_id} is fresh, returning from battery.")
                    
                    # --- Check and Fetch Missing XEM Mapping ---
                    if 'xem_mapping' not in metadata:
                        logging.info(f"Existing metadata for {imdb_id} is missing XEM mapping. Attempting to fetch...")
                        tvdb_id = metadata.get('ids', {}).get('tvdb')
                        if tvdb_id:
                            try:
                                xem_mapping_data = fetch_xem_mapping(tvdb_id)
                                # Always store the mapping result, even if it's None/empty
                                xem_mapping_data = xem_mapping_data if xem_mapping_data else {}
                                logger.info(f"{'Successfully fetched' if xem_mapping_data else 'No'} XEM mapping for TVDB ID {tvdb_id}. Adding to DB and response.")
                                
                                # Add to the dictionary we are returning
                                metadata['xem_mapping'] = xem_mapping_data
                                
                                # Save the new mapping to the database immediately
                                try:
                                    from metadata.metadata import _get_local_timezone
                                    new_xem_metadata = Metadata(
                                        item_id=item.id,
                                        key='xem_mapping',
                                        value=json.dumps(xem_mapping_data),
                                        provider='xem', # Use 'xem' as provider for clarity
                                        last_updated=datetime.now(_get_local_timezone())
                                    )
                                    session.add(new_xem_metadata)
                                    session.commit() # Commit this specific addition
                                    logger.info(f"Successfully saved XEM mapping to database for {imdb_id}.")
                                except Exception as xem_error:
                                    logger.error(f"Error fetching XEM mapping for TVDB ID {tvdb_id}: {xem_error}")
                                    # Store empty mapping on error to prevent repeated failed attempts
                                    metadata['xem_mapping'] = {}
                                    new_xem_metadata = Metadata(
                                        item_id=item.id,
                                        key='xem_mapping',
                                        value=json.dumps({}),
                                        provider='xem',
                                        last_updated=datetime.now(_get_local_timezone())
                                    )
                                    session.add(new_xem_metadata)
                                    session.commit()
                            except Exception as xem_error:
                                logger.error(f"Error fetching XEM mapping for TVDB ID {tvdb_id}: {xem_error}")
                                # Store empty mapping on error to prevent repeated failed attempts
                                metadata['xem_mapping'] = {}
                                new_xem_metadata = Metadata(
                                    item_id=item.id,
                                    key='xem_mapping',
                                    value=json.dumps({}),
                                    provider='xem',
                                    last_updated=datetime.now(_get_local_timezone())
                                )
                                session.add(new_xem_metadata)
                                session.commit()
                        else:
                            logger.warning(f"Cannot fetch XEM mapping for {imdb_id}: TVDB ID not found in existing metadata. Storing empty mapping.")
                            metadata['xem_mapping'] = {}
                            new_xem_metadata = Metadata(
                                item_id=item.id,
                                key='xem_mapping',
                                value=json.dumps({}),
                                provider='xem',
                                last_updated=datetime.now(_get_local_timezone())
                            )
                            session.add(new_xem_metadata)
                            session.commit()
                    # --- End Check ---

                    # Log season info if present
                    if 'seasons' in metadata:
                        logger.info(f"Found {len(metadata['seasons'])} seasons in cached metadata")

                    return metadata, "battery"

            # Fetch from Trakt if not in database (initial fetch)
            logging.info(f"Item {imdb_id} not found in database, fetching from Trakt")
            trakt = TraktMetadata()
            show_data = trakt.get_show_metadata(imdb_id)
            if show_data:
                try:
                    item = Item(imdb_id=imdb_id, title=show_data.get('title'), type='show', year=show_data.get('year'))
                    session.add(item)
                    session.flush()  # Get the item.id
                    logging.info(f"Created new item for {imdb_id}")
                    MetadataManager.update_show_metadata(item, show_data, session)
                    logging.info(f"Added initial metadata for {imdb_id}")
                    return show_data, "trakt (new)"
                except Exception as e:
                    logger.error(f"Error creating show metadata for {imdb_id}: {str(e)}")
                    session.rollback()
                    return show_data, "trakt (save failed)"
            return None, None

    @staticmethod
    def update_show_metadata(item, show_data, session):
        try:
            from metadata.metadata import _get_local_timezone
            current_time = datetime.now(_get_local_timezone())
            item.updated_at = current_time
            # Delete existing metadata in a separate query
            deleted_count = session.query(Metadata).filter_by(item_id=item.id).delete()
            logger.info(f"Deleted {deleted_count} existing metadata entries for {item.imdb_id}")
            session.flush()  # Ensure the delete is processed

            # --- Fetch XEM Mapping --- 
            xem_mapping_data = None
            tvdb_id = show_data.get('ids', {}).get('tvdb')
            if tvdb_id:
                try:
                    xem_mapping_data = fetch_xem_mapping(tvdb_id)
                    # Always store the mapping result, even if it's None/empty
                    show_data['xem_mapping'] = xem_mapping_data if xem_mapping_data else {}
                    if xem_mapping_data:
                        logger.info(f"Successfully fetched XEM mapping for TVDB ID {tvdb_id}")
                    else:
                        logger.info(f"No XEM mapping found for TVDB ID {tvdb_id}, storing empty mapping")
                except Exception as xem_error:
                    logger.error(f"Error fetching XEM mapping for TVDB ID {tvdb_id}: {xem_error}")
                    # Store empty mapping on error to prevent repeated failed attempts
                    show_data['xem_mapping'] = {}
            else:
                logger.warning(f"TVDB ID not found in show_data for {item.imdb_id}, storing empty XEM mapping")
                show_data['xem_mapping'] = {}
            # --- End Fetch XEM Mapping ---

            # Add new metadata
            metadata_entries = []
            logger.info(f"Processing show data keys for {item.imdb_id}: {list(show_data.keys())}")
            
            if 'seasons' in show_data:
                logger.info(f"Found seasons data for {item.imdb_id}: {list(show_data['seasons'].keys()) if show_data['seasons'] else 'Empty'}")
                if show_data['seasons']:
                    for season_num, season_data in show_data['seasons'].items():
                        logger.info(f"Season {season_num} has {len(season_data.get('episodes', {}))} episodes")
            else:
                logger.warning(f"No seasons data found in show_data for {item.imdb_id}")

            for key, value in show_data.items():
                if isinstance(value, (list, dict)):
                    # Convert list/dict to JSON string for storage
                    try:
                        value = json.dumps(value)
                    except TypeError as e:
                        logger.error(f"Error converting value to JSON for key '{key}' in {item.imdb_id}: {e}. Storing as string.")
                        value = str(value) # Fallback to string representation
                        
                # Ensure value is a string before creating Metadata object
                if not isinstance(value, str):
                    value = str(value)
                    
                metadata = Metadata(
                    item_id=item.id,
                    key=key,
                    value=value, # Ensure value is a string
                    provider='trakt', # Provider is still trakt for core data
                    last_updated=current_time
                )
                metadata_entries.append(metadata)
                logger.info(f"Added metadata entry for {item.imdb_id}: {key} (length: {len(value)})")

            # Bulk insert all metadata entries
            session.bulk_save_objects(metadata_entries)
            session.flush()  # Ensure the inserts are processed
            
            # Commit the transaction
            session.commit()
            logger.info(f"Committed transaction with {len(metadata_entries)} metadata entries for {item.imdb_id}")
            
            # Verify the metadata was saved
            saved_metadata = session.query(Metadata).filter_by(item_id=item.id).all()
            if not saved_metadata:
                logger.error(f"Failed to save metadata for item {item.imdb_id}")
                raise Exception("Metadata save verification failed")
            logger.info(f"Verified {len(saved_metadata)} metadata entries were saved for {item.imdb_id}")
            
            # Verify seasons data was saved
            seasons_metadata = session.query(Metadata).filter_by(item_id=item.id, key='seasons').first()
            if seasons_metadata:
                try:
                    saved_seasons = json.loads(seasons_metadata.value)
                    logger.info(f"Verified seasons data for {item.imdb_id}: {list(saved_seasons.keys()) if saved_seasons else 'Empty'}")
                    for season_num, season_data in saved_seasons.items():
                        logger.info(f"Saved season {season_num} has {len(season_data.get('episodes', {}))} episodes")
                except json.JSONDecodeError:
                    logger.error(f"Failed to decode saved seasons data for {item.imdb_id}")
            else:
                logger.warning(f"No seasons metadata found after save for {item.imdb_id}")
            
            return True
        except Exception as e:
            logger.error(f"Error in update_show_metadata for item {item.imdb_id}: {str(e)}")
            session.rollback()
            raise

    @staticmethod
    def get_show_aliases(imdb_id):
        """Get all aliases for a show"""
        with DbSession() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
            if item:
                metadata = session.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                if metadata:
                    if MetadataManager.is_metadata_stale(metadata.last_updated):
                        return MetadataManager.refresh_show_metadata(imdb_id)[0].get('aliases'), "trakt"
                    try:
                        return json.loads(metadata.value), "battery"
                    except json.JSONDecodeError:
                        logger.error(f"Error decoding JSON for aliases of IMDB ID: {imdb_id}")
                        return MetadataManager.refresh_show_metadata(imdb_id)[0].get('aliases'), "trakt"

            # If not in database or if metadata is missing, fetch from Trakt
            trakt = TraktMetadata()
            show_data = trakt.get_show_metadata(imdb_id)
            if show_data and 'aliases' in show_data:
                # Save to database
                if not item:
                    item = Item(imdb_id=imdb_id, title=show_data.get('title'), type='show', year=show_data.get('year'))
                    session.add(item)
                    session.flush()
                
                # Add or update the aliases metadata
                metadata = session.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                if not metadata:
                    metadata = Metadata(item_id=item.id, key='aliases')
                    session.add(metadata)
                
                metadata.value = json.dumps(show_data['aliases'])
                metadata.provider = 'trakt'
                from metadata.metadata import _get_local_timezone
                metadata.last_updated = datetime.now(_get_local_timezone())
                session.commit()
                
                return show_data['aliases'], "trakt"
            
            return None, None

    @staticmethod
    def get_movie_aliases(imdb_id):
        """Get all aliases for a movie"""
        with DbSession() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id, type='movie').first()
            if item:
                metadata = session.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                if metadata:
                    if MetadataManager.is_metadata_stale(metadata.last_updated):
                        return MetadataManager.refresh_movie_metadata(imdb_id)[0].get('aliases'), "trakt"
                    try:
                        return json.loads(metadata.value), "battery"
                    except json.JSONDecodeError:
                        logger.error(f"Error decoding JSON for aliases of IMDB ID: {imdb_id}")
                        return MetadataManager.refresh_movie_metadata(imdb_id)[0].get('aliases'), "trakt"

            # If not in database or if metadata is missing, fetch from Trakt
            trakt = TraktMetadata()
            movie_data = trakt.get_movie_metadata(imdb_id)
            if movie_data and 'aliases' in movie_data:
                # Save to database
                if not item:
                    item = Item(imdb_id=imdb_id, title=movie_data.get('title'), type='movie', year=movie_data.get('year'))
                    session.add(item)
                    session.flush()
                
                # Add or update the aliases metadata
                metadata = session.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                if not metadata:
                    metadata = Metadata(item_id=item.id, key='aliases')
                    session.add(metadata)
                
                metadata.value = json.dumps(movie_data['aliases'])
                metadata.provider = 'trakt'
                from metadata.metadata import _get_local_timezone
                metadata.last_updated = datetime.now(_get_local_timezone())
                session.commit()
                
                return movie_data['aliases'], "trakt"
            
            return None, None

    @staticmethod
    def refresh_show_metadata(imdb_id):
        try:
            with DbSession() as session:
                trakt = TraktMetadata()
                show_data = trakt.get_show_metadata(imdb_id)
                if show_data:
                    item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                    if not item:
                        item = Item(imdb_id=imdb_id, title=show_data.get('title'), type='show', year=show_data.get('year'))
                        session.add(item)
                        session.flush()
                    
                    # Clear existing metadata
                    session.query(Metadata).filter_by(item_id=item.id).delete()
                    
                    # Add all metadata including aliases
                    for key, value in show_data.items():
                        if isinstance(value, (list, dict)):
                            value = json.dumps(value)
                        metadata = Metadata(item_id=item.id, key=key, value=str(value), provider='trakt')
                        session.add(metadata)

                    from metadata.metadata import _get_local_timezone
                    item.updated_at = datetime.now(_get_local_timezone())
                    session.commit()
                    return show_data, "trakt"
                
                logger.warning(f"No show metadata found for IMDB ID: {imdb_id}")
                return None, None
        except Exception as e:
            logger.error(f"Error in refresh_show_metadata for IMDb ID {imdb_id}: {str(e)}")
            return None, None
            
    @staticmethod
    def get_bulk_show_airs_info(imdb_ids: list[str]) -> dict[str, Optional[dict[str, Any]]]:
        """
        Gets the 'airs' metadata for a list of show IMDb IDs from the battery.
        Focuses on bulk reading and does not trigger automatic refresh on stale.
        Returns a dictionary mapping imdb_id to its 'airs' dict (or None if not found/error).
        """
        airs_info = {imdb_id: None for imdb_id in imdb_ids} # Initialize with None
        if not imdb_ids:
            return airs_info

        with DbSession() as session:
            try:
                # Find item IDs corresponding to the imdb_ids
                items = session.query(Item.id, Item.imdb_id).filter(
                    Item.imdb_id.in_(imdb_ids),
                    Item.type == 'show'
                ).all()

                item_id_to_imdb_id = {item.id: item.imdb_id for item in items}
                item_ids = list(item_id_to_imdb_id.keys())

                if not item_ids:
                    logger.warning(f"No items found in battery for IMDb IDs: {imdb_ids}")
                    return airs_info # Return dict with all Nones

                # Fetch 'airs' metadata for these item IDs
                airs_metadata_rows = session.query(Metadata).filter(
                    Metadata.item_id.in_(item_ids),
                    Metadata.key == 'airs'
                ).all()

                for metadata_row in airs_metadata_rows:
                    imdb_id = item_id_to_imdb_id.get(metadata_row.item_id)
                    if imdb_id:
                        try:
                            airs_value = json.loads(metadata_row.value)
                            if isinstance(airs_value, dict):
                                airs_info[imdb_id] = airs_value
                            else:
                                logger.warning(f"Airs metadata for {imdb_id} (item_id {metadata_row.item_id}) is not a dict: {type(airs_value)}")
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to decode 'airs' JSON for {imdb_id} (item_id {metadata_row.item_id}). Value: {metadata_row.value[:100]}...")
                        except Exception as e:
                             logger.error(f"Error processing airs metadata for {imdb_id}: {e}")

                # Log which IDs were not found
                found_ids = {imdb for imdb, airs in airs_info.items() if airs is not None}
                not_found_ids = set(imdb_ids) - found_ids
                if not_found_ids:
                     logger.info(f"Did not find 'airs' metadata in battery for IMDb IDs: {list(not_found_ids)}")

                return airs_info

            except Exception as e:
                logger.error(f"Error in get_bulk_show_airs_info: {e}")
                # Return the initialized dict with Nones on error
                return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def get_bulk_movie_metadata(imdb_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """
        Gets metadata for a list of movie IMDb IDs from the battery.
        Does not automatically refresh stale data in this bulk version.
        Returns a dictionary mapping imdb_id to its metadata dict (or None if not found).
        """
        metadata_map = {imdb_id: None for imdb_id in imdb_ids}
        if not imdb_ids:
            return metadata_map

        with DbSession() as session:
            try:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata) # Use selectinload for efficiency
                ).filter(
                    Item.imdb_id.in_(imdb_ids),
                    Item.type == 'movie'
                ).all()

                if not items:
                    logger.warning(f"No movie items found in battery for IMDb IDs: {imdb_ids}")
                    return metadata_map

                for item in items:
                    item_metadata = {}
                    for m in item.item_metadata:
                        try:
                            # Always try to parse as JSON first, fall back to string if it fails
                            try:
                                item_metadata[m.key] = json.loads(m.value)
                            except json.JSONDecodeError:
                                item_metadata[m.key] = m.value
                        except Exception as e:
                            logger.error(f"Error processing metadata for key {m.key} in movie {item.imdb_id}: {str(e)}")
                            item_metadata[m.key] = m.value # Store raw value on error
                    metadata_map[item.imdb_id] = item_metadata

                # Log which IDs were not found
                found_ids = {item.imdb_id for item in items}
                not_found_ids = set(imdb_ids) - found_ids
                if not_found_ids:
                    logger.info(f"Did not find movie metadata in battery for IMDb IDs: {list(not_found_ids)}")

                return metadata_map

            except Exception as e:
                logger.error(f"Error in get_bulk_movie_metadata: {e}")
                # Return the initialized dict with Nones on error
                return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def get_bulk_show_metadata(imdb_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """
        Gets metadata for a list of show IMDb IDs from the battery.
        Does not automatically refresh stale data in this bulk version.
        Includes check for missing XEM mapping similar to single get_show_metadata.
        Returns a dictionary mapping imdb_id to its metadata dict (or None if not found).
        """
        metadata_map = {imdb_id: None for imdb_id in imdb_ids}
        if not imdb_ids:
            return metadata_map

        with DbSession() as session:
            try:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata) # Use selectinload for efficiency
                ).filter(
                    Item.imdb_id.in_(imdb_ids),
                    Item.type == 'show'
                ).all()

                if not items:
                    logger.warning(f"No show items found in battery for IMDb IDs: {imdb_ids}")
                    return metadata_map
                
                items_missing_xem = {} # Store items that need XEM check {item_id: imdb_id}
                tvdb_ids_to_fetch_xem = {} # Store {tvdb_id: item_id} for fetching
                item_ids_to_save_empty_xem = [] # Store item_ids where TVDB ID was missing

                for item in items:
                    item_metadata = {}
                    has_xem = False
                    tvdb_id = None
                    for m in item.item_metadata:
                        try:
                            # Always try to parse as JSON first, fall back to string if it fails
                            try:
                                value = json.loads(m.value)
                                item_metadata[m.key] = value
                                if m.key == 'ids' and isinstance(value, dict):
                                    tvdb_id = value.get('tvdb')
                                if m.key == 'xem_mapping':
                                    has_xem = True # Mark if xem_mapping key exists
                            except json.JSONDecodeError:
                                item_metadata[m.key] = m.value
                        except Exception as e:
                            logger.error(f"Error processing metadata for key {m.key} in show {item.imdb_id}: {str(e)}")
                            item_metadata[m.key] = m.value # Store raw value on error
                    
                    metadata_map[item.imdb_id] = item_metadata # Store processed metadata

                    # Check if XEM mapping needs fetching
                    if not has_xem:
                        items_missing_xem[item.id] = item.imdb_id
                        if tvdb_id:
                             # Only add to fetch list if we have a TVDB ID
                             tvdb_ids_to_fetch_xem[tvdb_id] = item.id
                        else:
                            logger.warning(f"Cannot fetch XEM mapping for {item.imdb_id}: TVDB ID not found in existing metadata. Will store empty mapping.")
                            item_ids_to_save_empty_xem.append(item.id)


                # --- Bulk Fetch XEM Mapping ---
                fetched_xem_mappings = {} # Store {item_id: xem_data}
                if tvdb_ids_to_fetch_xem:
                    tvdb_ids = list(tvdb_ids_to_fetch_xem.keys())
                    logger.info(f"Bulk fetching XEM mappings for {len(tvdb_ids)} TVDB IDs...")
                    # Assuming fetch_xem_mapping can handle a list or we adapt it.
                    # For now, iterate, but ideally batch this if possible.
                    for tvdb_id in tvdb_ids:
                        item_id = tvdb_ids_to_fetch_xem[tvdb_id]
                        imdb_id = items_missing_xem[item_id]
                        try:
                            xem_data = fetch_xem_mapping(tvdb_id)
                            fetched_xem_mappings[item_id] = xem_data if xem_data else {} # Store empty dict if None
                            logger.info(f"{'Found' if xem_data else 'No'} XEM mapping for TVDB {tvdb_id} (IMDb: {imdb_id})")
                        except Exception as e:
                            logger.error(f"Error fetching XEM for TVDB {tvdb_id} (IMDb: {imdb_id}): {e}")
                            fetched_xem_mappings[item_id] = {} # Store empty on error


                # --- Add fetched/empty XEM mappings to results and prepare DB save ---
                metadata_to_save = []
                from metadata.metadata import _get_local_timezone
                current_time = datetime.now(_get_local_timezone())

                for item_id, imdb_id in items_missing_xem.items():
                    xem_data_to_add = fetched_xem_mappings.get(item_id, {}) # Default to empty if fetch failed or TVDB ID missing

                    # Add to the response dictionary
                    if metadata_map.get(imdb_id): # Check if metadata dict exists
                        metadata_map[imdb_id]['xem_mapping'] = xem_data_to_add
                    else:
                        logger.warning(f"Metadata map entry missing for {imdb_id} when trying to add XEM mapping.")

                    # Prepare for database update/insert
                    metadata_to_save.append(
                        Metadata(
                            item_id=item_id,
                            key='xem_mapping',
                            value=json.dumps(xem_data_to_add),
                            provider='xem',
                            last_updated=current_time
                        )
                    )

                # --- Save new XEM mappings to DB ---
                if metadata_to_save:
                    try:
                        logger.info(f"Saving {len(metadata_to_save)} new XEM mappings to the database.")
                        session.bulk_save_objects(metadata_to_save)
                        session.commit()
                        logger.info("Successfully saved new XEM mappings.")
                    except Exception as db_err:
                        logger.error(f"Error saving XEM mappings to database: {db_err}")
                        session.rollback()

                # Log which original IDs were not found in the DB query
                found_ids = {item.imdb_id for item in items}
                not_found_ids = set(imdb_ids) - found_ids
                if not_found_ids:
                    logger.info(f"Did not find show metadata in battery for IMDb IDs: {list(not_found_ids)}")

                return metadata_map

            except Exception as e:
                logger.error(f"Error in get_bulk_show_metadata: {e}")
                # Return the initialized dict with Nones on error
                return {imdb_id: None for imdb_id in imdb_ids}
