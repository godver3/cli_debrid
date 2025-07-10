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
from typing import Optional, Dict, Any, List, Tuple
from .xem_utils import fetch_xem_mapping
from sqlalchemy.orm import Session as SqlAlchemySession # Use alias
from fuzzywuzzy import fuzz # <-- ADD THIS IMPORT AT THE TOP OF THE FILE

class MetadataManager:

    def __init__(self):
        self.base_url = TraktMetadata()

    @staticmethod
    def add_or_update_item(imdb_id, title, year=None, item_type=None):
        return DatabaseManager.add_or_update_item(imdb_id, title, year, item_type)

    @staticmethod
    def add_or_update_metadata(imdb_id, metadata_dict, provider, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            if session: # Use provided session
                item = session_context.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    logger.error(f"Item with IMDB ID {imdb_id} not found when adding metadata (provided session).")
                    return False
                # --- CORRECTED LOGIC ---
                # Call helper with the *provided* session if item is found
                success = MetadataManager._update_metadata_with_session(item, metadata_dict, provider, session_context)
                # Caller of add_or_update_metadata handles commit/rollback for provided sessions
                return success
                # --- END CORRECTION ---
            else: # Create local session
                with session_context as local_session:
                    item = local_session.query(Item).filter_by(imdb_id=imdb_id).first()
                    if not item:
                        logger.error(f"Item with IMDB ID {imdb_id} not found when adding metadata (local session).")
                        return False
                    # Pass session down
                    success = MetadataManager._update_metadata_with_session(item, metadata_dict, provider, local_session)
                    if success:
                         local_session.commit()
                    else:
                         local_session.rollback() # Rollback if underlying method failed
                    return success
        except Exception as e:
            logger.error(f"Error in add_or_update_metadata for {imdb_id}: {e}", exc_info=True)
            if session: # Re-raise if session was provided
                raise
            return False # Return False for local session error

    @staticmethod
    def _update_metadata_with_session(item, metadata_dict, provider, session: SqlAlchemySession):
        """Internal method to update metadata using an existing session"""
        try:
            success = False
            # Delete existing metadata first
            session.query(Metadata).filter_by(item_id=item.id).delete(synchronize_session='fetch')
            session.flush()

            batch_size = 50
            metadata_entries = []

            from metadata.metadata import _get_local_timezone
            current_time = datetime.now(_get_local_timezone())

            for key, value in metadata_dict.items():
                processed_value = value # Start with original value
                if isinstance(value, (dict, list)):
                    try:
                        # Correctly assign the dumped value back
                        processed_value = json.dumps(value)
                    except TypeError as e:
                         logger.error(f"JSON Error for key '{key}' in {item.imdb_id}: {e}. Storing as string.")
                         processed_value = str(value) # Fallback to string
                else:
                    # Ensure non-dict/list values are strings
                    processed_value = str(value)

                metadata = Metadata(
                    item_id=item.id,
                    key=key,
                    value=processed_value, # Use the processed value
                    provider=provider,
                    last_updated=current_time
                )
                metadata_entries.append(metadata)

                if len(metadata_entries) >= batch_size:
                    session.bulk_save_objects(metadata_entries)
                    session.flush()
                    metadata_entries = []
                    success = True

            if metadata_entries:
                session.bulk_save_objects(metadata_entries)
                session.flush()
                success = True

            if not success and metadata_dict: # Log warning only if there was data to process
                logger.warning(f"No metadata entries were updated for {item.title} ({item.imdb_id}) despite input data.")
            return success # Return status, commit/rollback handled by caller
        except Exception as e:
            logger.error(f"Error in _update_metadata_with_session for item {item.imdb_id}: {str(e)}")
            raise # Re-raise the exception

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
    def is_tmdb_mapping_stale(last_updated):
        """
        Check if a TMDB to IMDB mapping is stale and should be refreshed.
        Uses a longer threshold than regular metadata since ID mappings change less frequently.
        """
        from metadata.metadata import _get_local_timezone

        if last_updated is None:
            logger.debug("TMDB mapping has no last_updated timestamp, considering stale")
            return True
        
        # Convert last_updated to UTC if it's not already
        if last_updated.tzinfo is None or last_updated.tzinfo.utcoffset(last_updated) is None:
            last_updated = last_updated.replace(tzinfo=_get_local_timezone())

        now = datetime.now(_get_local_timezone())
        
        # Use a longer threshold for ID mappings (30 days base + variation)
        # ID mappings change less frequently than metadata, so we can cache them longer
        base_threshold = 30  # days
        day_variation = random.choice([-7, -3, 0, 3, 7])
        hour_variation = random.randint(-24, 24)
        
        adjusted_threshold = max(base_threshold + day_variation, 7)  # Minimum 7 days
        
        stale_threshold = timedelta(days=adjusted_threshold, hours=hour_variation)
        age = now - last_updated
        is_stale = age > stale_threshold

        if is_stale:
            logger.debug(
                f"TMDB mapping staleness check: last_updated={last_updated.isoformat()}, "
                f"age={age.days}d {age.seconds//3600}h, "
                f"threshold={stale_threshold.days}d {stale_threshold.seconds//3600}h "
                f"(base={base_threshold}d, variation={day_variation}d {hour_variation}h) "
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
    def get_item(imdb_id, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            if session:
                return session_context.query(Item).options(joinedload(Item.item_metadata), joinedload(Item.poster)).filter_by(imdb_id=imdb_id).first()
            else:
                with session_context as local_session:
                    return local_session.query(Item).options(joinedload(Item.item_metadata), joinedload(Item.poster)).filter_by(imdb_id=imdb_id).first()
        except Exception as e:
            logger.error(f"Error in get_item for {imdb_id}: {e}", exc_info=True)
            if session: raise
            return None

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
    def get_seasons(imdb_id, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            # Define the core logic as a nested function to avoid repetition
            def _get_seasons_logic(current_session):
                item = current_session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
                if item:
                    # Eagerly load episodes when querying seasons
                    seasons = current_session.query(Season).filter_by(item_id=item.id).options(selectinload(Season.episodes)).all()
                    if seasons:
                        # Check staleness using metadata table (less efficient but matches old logic)
                        # Ideally, check item.updated_at if that's reliable
                        metadata = current_session.query(Metadata).filter_by(item_id=item.id, key='seasons').first() # Or check item.updated_at?
                        # Using item.updated_at for staleness check might be better if kept up-to-date
                        is_stale = MetadataManager.is_metadata_stale(item.updated_at)
                        # if metadata and MetadataManager.is_metadata_stale(metadata.last_updated): # Alternative check

                        if is_stale:
                            logger.debug(f"Seasons metadata is stale for {imdb_id} based on item timestamp, refreshing from Trakt")
                            # Pass the current session (provided or local) down
                            return MetadataManager.refresh_seasons(imdb_id, current_session)
                        else:
                            # Data is present and considered fresh
                            seasons_data = MetadataManager.format_seasons_data(seasons)
                            return seasons_data, "battery"
                # Item not found OR item found but no seasons relationally
                # Fetch/Refresh from Trakt
                logger.debug(f"Item {imdb_id} not found or has no seasons relationally, fetching/refreshing from Trakt.")
                return MetadataManager.refresh_seasons(imdb_id, current_session)

            # Execute the logic using the appropriate session context
            if session: # Use provided session directly
                return _get_seasons_logic(session_context)
            else: # Create and use a local session
                with session_context as local_session:
                    return _get_seasons_logic(local_session)

        except Exception as e:
            logger.error(f"Error in get_seasons for {imdb_id}: {e}", exc_info=True)
            if session: # Re-raise if session was provided to let managed_session handle rollback
                 raise
            return None, None # Return default for local session error

    @staticmethod
    def refresh_seasons(imdb_id, session: SqlAlchemySession): # Expects a session now
        trakt = TraktMetadata()
        seasons_data, source = trakt.get_show_seasons_and_episodes(imdb_id)
        if seasons_data:
            # Pass session down - assumes add_or_update handles commit/rollback based on session presence
            MetadataManager.add_or_update_seasons_and_episodes(imdb_id, seasons_data, session=session)
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
    def add_or_update_seasons_and_episodes(imdb_id, seasons_data, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            if session: # Use provided session
                item = session_context.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                     logger.error(f"Item with IMDB ID {imdb_id} not found when adding seasons and episodes.")
                     return False
                # Call helper that uses the session, helper handles internal logic but not commit/rollback
                success = MetadataManager._add_or_update_seasons_and_episodes_with_session(item, seasons_data, session_context)
                # DO NOT COMMIT/ROLLBACK HERE
                return success
            else: # Create local session
                with session_context as local_session:
                     item = local_session.query(Item).filter_by(imdb_id=imdb_id).first()
                     if not item:
                         logger.error(f"Item with IMDB ID {imdb_id} not found when adding seasons and episodes.")
                         return False
                     # Call helper
                     success = MetadataManager._add_or_update_seasons_and_episodes_with_session(item, seasons_data, local_session)
                     # Commit/rollback locally based on helper result
                     if success:
                         local_session.commit()
                     else:
                         local_session.rollback()
                     return success
        except Exception as e:
            logger.error(f"Error in add_or_update_seasons_and_episodes for {imdb_id}: {e}", exc_info=True)
            # If session was provided, rollback is handled by caller via exception
            if session: raise
            # Local session rollback handled by 'with' block error
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
    def get_specific_metadata(imdb_id, key, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            if session: # Use provided session
                item = session_context.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item: return None

                metadata = next((m for m in item.item_metadata if m.key == key), None)
                if not metadata:
                    # Pass session down
                    new_metadata = MetadataManager.refresh_metadata(imdb_id, session=session_context)
                    return {key: new_metadata.get(key)} if new_metadata else {key: None}

                if MetadataManager.is_metadata_stale(item.updated_at):
                     # Pass session down
                    new_metadata = MetadataManager.refresh_metadata(imdb_id, session=session_context)
                    # Handle potential None return from refresh
                    current_value = json.loads(metadata.value) if metadata else None
                    return {key: new_metadata.get(key, current_value)} if new_metadata else {key: current_value}


                try:
                    try: return {key: json.loads(metadata.value)}
                    except json.JSONDecodeError: return {key: metadata.value}
                except Exception as e:
                    logger.error(f"Error processing metadata for key {key}: {str(e)}")
                    return {key: metadata.value}
            else: # Create local session
                 with session_context as local_session:
                    item = local_session.query(Item).filter_by(imdb_id=imdb_id).first()
                    if not item: return None

                    metadata = next((m for m in item.item_metadata if m.key == key), None)
                    if not metadata:
                        # Pass session down
                        new_metadata = MetadataManager.refresh_metadata(imdb_id, session=local_session)
                        return {key: new_metadata.get(key)} if new_metadata else {key: None}


                    if MetadataManager.is_metadata_stale(item.updated_at):
                        # Pass session down
                        new_metadata = MetadataManager.refresh_metadata(imdb_id, session=local_session)
                        current_value = json.loads(metadata.value) if metadata else None
                        return {key: new_metadata.get(key, current_value)} if new_metadata else {key: current_value}

                    try:
                        try: return {key: json.loads(metadata.value)}
                        except json.JSONDecodeError: return {key: metadata.value}
                    except Exception as e:
                        logger.error(f"Error processing metadata for key {key}: {str(e)}")
                        return {key: metadata.value}
        except Exception as e:
            logger.error(f"Error in get_specific_metadata for {imdb_id}, key {key}: {e}", exc_info=True)
            if session: raise
            return None

    @staticmethod
    def refresh_metadata(imdb_id, session: Optional[SqlAlchemySession] = None):
        trakt = TraktMetadata()
        logger.info(f"Refreshing metadata for {imdb_id}")

        summary_data_result = trakt.refresh_metadata(imdb_id)
        # --- START DEBUG LOGGING ---
        # Log the raw data received before any processing/saving occurs
        logger.debug(f"Raw data received from trakt.refresh_metadata for {imdb_id}: {summary_data_result}")
        # --- END DEBUG LOGGING ---
        if not summary_data_result:
             logger.warning(f"Could not fetch summary metadata from Trakt for {imdb_id}")
             return None

        data_to_save = summary_data_result.get('metadata', summary_data_result)
        # --- START DEBUG LOGGING ---
        # Log the extracted data_to_save as well
        logger.debug(f"Extracted data_to_save for {imdb_id}: {data_to_save}")
         # --- END DEBUG LOGGING ---
        if not isinstance(data_to_save, dict):
            logger.error(f"Invalid summary metadata format received from Trakt for {imdb_id}")
            return None

        item_type = data_to_save.get('type')
        if not item_type:
             item_type = 'show' if 'aired_episodes' in data_to_save else 'movie'
             logger.warning(f"Item type not explicit in summary for {imdb_id}, inferred as '{item_type}'")
             data_to_save['type'] = item_type

        # Ensure item_type is correctly set in data_to_save for item creation later
        data_to_save['type'] = item_type # Make sure type is in data_to_save

        if item_type == 'show':
            logger.info(f"Fetching detailed season/episode data for show {imdb_id}")
            # Use the main TraktMetadata instance to potentially reuse cached data
            seasons_data, seasons_source = trakt.get_show_seasons_and_episodes(imdb_id)
            if seasons_data and isinstance(seasons_data, dict):
                 logger.info(f"Successfully fetched detailed seasons data for {imdb_id}")
                 data_to_save['seasons'] = seasons_data
            else:
                 logger.warning(f"Could not fetch or received invalid detailed seasons data for {imdb_id}. Proceeding without it.")
                 data_to_save['seasons'] = {}

        # --- START MODIFICATION ---
        session_context = session if session else DbSession()

        def _refresh_logic(sess):
            # Check for item *within this session* before atomic update
            item = sess.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                # Create the item if it doesn't exist using the session
                item_title = data_to_save.get('title', 'Unknown Title')
                item_year = data_to_save.get('year')
                # Type should be correctly set in data_to_save now
                item_type_create = data_to_save.get('type')
                logger.info(f"Creating new Item record for {imdb_id} (Title: {item_title}, Type: {item_type_create}) within session.")
                new_item = Item(imdb_id=imdb_id, title=item_title, year=item_year, type=item_type_create)
                sess.add(new_item)
                # Flush to ensure the item exists before the atomic update tries to query it.
                # This is crucial when passing a session down.
                try:
                    sess.flush() # Attempt to save the new item
                    logger.debug(f"Flushed session after adding new item {imdb_id}")
                    item = new_item # Use the newly added item
                except IntegrityError as ie:
                    # Handle the race condition where another process inserted the item between our check and flush
                    if "UNIQUE constraint failed: items.imdb_id" in str(ie):
                        logger.warning(f"Race condition detected for item {imdb_id}. Another process likely inserted it. Rolling back add and fetching existing.")
                        sess.rollback() # Roll back the failed flush/add
                        # Now query for the item that *must* exist
                        item = sess.query(Item).filter_by(imdb_id=imdb_id).first()
                        if not item:
                            # This should be extremely rare, but handle it just in case
                            logger.error(f"Failed to fetch item {imdb_id} after handling IntegrityError. Aborting refresh.")
                            raise # Re-raise the original error or a new one
                        logger.info(f"Successfully fetched existing item {imdb_id} after race condition.")
                    else:
                        # If it's a different IntegrityError, re-raise it
                        logger.error(f"Unhandled IntegrityError during item creation flush for {imdb_id}: {ie}", exc_info=True)
                        raise
                except Exception as flush_err:
                    logger.error(f"Error flushing session after adding item {imdb_id}: {flush_err}", exc_info=True)
                    # Re-raise the exception to ensure transaction rollback if session was provided
                    raise

            else:
                 logger.debug(f"Item {imdb_id} already exists in the session.")

            logger.info(f"Calling atomic update for {imdb_id} with {'detailed seasons' if 'seasons' in data_to_save and data_to_save.get('seasons') else 'summary only'} data.")
            # Pass the *same session context* down
            # Ensure 'item' is correctly assigned from either the initial query, the successful flush, or the race condition handling
            if not item:
                 logger.error(f"Item object is unexpectedly None before calling _update_metadata_atomic for {imdb_id}. Aborting.")
                 return None # Or raise an error

            # --- MODIFICATION FOR ATOMIC UPDATE ---
            # Pass the item object directly instead of just imdb_id to avoid re-querying inside atomic
            success = MetadataManager._update_metadata_atomic(item, data_to_save.copy(), 'Trakt', session=sess)
            # --- END MODIFICATION ---

            if success:
                logger.info(f"Successfully refreshed metadata for {imdb_id} (atomic update returned success).")
                return data_to_save
            else:
                logger.error(f"Failed to save refreshed metadata atomically for {imdb_id}.")
                # If session was provided, the error in atomic update should have raised, leading to rollback by caller.
                # If local session, atomic update handles rollback.
                return None

        try:
            if session: # Use provided session
                 return _refresh_logic(session_context)
            else: # Create local session
                 with session_context as local_session:
                     # _refresh_logic will call _update_metadata_atomic, which handles commit/rollback for local session
                     return _refresh_logic(local_session)
        except Exception as e:
             # Catch potential flush error or others from _refresh_logic
             logger.error(f"Error during refresh logic execution for {imdb_id}: {e}", exc_info=True)
             if session: raise # Re-raise for managed session rollback
             return None # Return None for local session error
        # --- END MODIFICATION ---

        # Remove old call and return (These lines are no longer needed)
        # logger.info(f"Calling atomic update for {imdb_id} with {'detailed seasons' if 'seasons' in data_to_save and data_to_save.get('seasons') else 'summary only'} data.")
        # success = MetadataManager._update_metadata_atomic(imdb_id, data_to_save.copy(), 'Trakt', session=session)
        # if success:
        #     logger.info(f"Successfully refreshed metadata for {imdb_id} (atomic update returned success).")
        #     return data_to_save
        # else:
        #     logger.error(f"Failed to save refreshed metadata atomically for {imdb_id}.")
        #     return None

    @staticmethod
    def _update_metadata_atomic(item: Item, metadata_dict: dict, provider: str, session: Optional[SqlAlchemySession] = None) -> bool: # Changed signature
        from metadata.metadata import _get_local_timezone
        session_context = session if session else DbSession()
        # Use the provided item directly
        imdb_id = item.imdb_id
        logger.debug(f"_update_metadata_atomic called for {imdb_id} (Item ID: {item.id}). Session provided: {session is not None}")
        try:
            if session: # Use provided session directly
                logger.debug(f"Using provided session for atomic update: {imdb_id}")
                # No need to query item again, we already have it

                # ... rest of the logic using session_context and item ...
                seasons_data = metadata_dict.pop('seasons', None)

                # --- MODIFIED LINE ---
                # Change synchronize_session strategy
                logger.debug(f"Attempting to delete existing Metadata records for item_id {item.id} with synchronize_session='fetch'")
                deleted_count = session_context.query(Metadata).filter(Metadata.item_id == item.id).delete(synchronize_session='fetch')
                logger.debug(f"Deleted {deleted_count} existing Metadata records for item_id {item.id}")
                # --- END MODIFICATION ---
                # Need to flush the delete before adding new metadata if keys could overlap in the same transaction
                session_context.flush()
                logger.debug(f"Flushed session after deleting metadata for {imdb_id}")


                metadata_entries = []
                current_time = datetime.now(_get_local_timezone())

                for key, value in metadata_dict.items():
                    # ... value processing ...
                    processed_value = value
                    if isinstance(value, (dict, list)):
                        try: processed_value = json.dumps(value)
                        except TypeError as e:
                            logger.error(f"JSON Error for key '{key}' in {imdb_id}: {e}. Storing as string.")
                            processed_value = str(value)
                    else:
                         if not isinstance(value, str): processed_value = str(value)

                    metadata = Metadata(item_id=item.id, key=key, value=processed_value, provider=provider, last_updated=current_time)
                    metadata_entries.append(metadata)

                if metadata_entries: session_context.add_all(metadata_entries)

                seasons_update_success = True
                if seasons_data and isinstance(seasons_data, dict):
                    # Pass session down
                    seasons_update_success = MetadataManager._add_or_update_seasons_and_episodes_with_session(item, seasons_data, session_context)
                    if not seasons_update_success: logger.error(f"Failed to update seasons/episodes within atomic transaction for {imdb_id}.")
                elif seasons_data:
                     logger.warning(f"Separated 'seasons' data for {imdb_id} was not a dictionary (type: {type(seasons_data)}). Skipping season/episode update.")
                     seasons_update_success = False # Treat invalid season data as failure for atomic op


                item.updated_at = current_time

                # ** IMPORTANT: DO NOT COMMIT OR ROLLBACK HERE **
                logger.debug(f"Atomic update logic finished for {imdb_id}. Returning success status: {seasons_update_success}")
                return seasons_update_success # Return status, commit/rollback handled by caller

            else: # Create local session
                 logger.debug(f"Creating local session for atomic update: {imdb_id}")
                 with session_context as local_session:
                    # Use the item passed into the function, but ensure it's attached to this new session
                    logger.debug(f"Merging provided item {item.id} into local session.")
                    item = local_session.merge(item) # Attach the item to the local session

                    # ... rest of the logic using local_session and item ...
                    seasons_data = metadata_dict.pop('seasons', None)

                    # --- MODIFIED LINE ---
                    # Change synchronize_session strategy
                    logger.debug(f"Attempting to delete existing Metadata records for item_id {item.id} with synchronize_session='fetch'")
                    deleted_count = local_session.query(Metadata).filter(Metadata.item_id == item.id).delete(synchronize_session='fetch')
                    logger.debug(f"Deleted {deleted_count} existing Metadata records for item_id {item.id}")
                    # --- END MODIFICATION ---
                    # Need to flush the delete before adding new metadata if keys could overlap in the same transaction
                    local_session.flush()
                    logger.debug(f"Flushed session after deleting metadata for {imdb_id} (local session)")


                    metadata_entries = []
                    current_time = datetime.now(_get_local_timezone())

                    for key, value in metadata_dict.items():
                        # ... value processing ...
                        processed_value = value
                        if isinstance(value, (dict, list)):
                            try: processed_value = json.dumps(value)
                            except TypeError as e:
                                logger.error(f"JSON Error for key '{key}' in {imdb_id}: {e}. Storing as string.")
                                processed_value = str(value)
                        else:
                            if not isinstance(value, str): processed_value = str(value)

                        metadata = Metadata(item_id=item.id, key=key, value=processed_value, provider=provider, last_updated=current_time)
                        metadata_entries.append(metadata)

                    if metadata_entries: local_session.add_all(metadata_entries)

                    seasons_update_success = True
                    if seasons_data and isinstance(seasons_data, dict):
                         # Pass session down
                         seasons_update_success = MetadataManager._add_or_update_seasons_and_episodes_with_session(item, seasons_data, local_session)
                         if not seasons_update_success: logger.error(f"Failed to update seasons/episodes within atomic transaction for {imdb_id}.")
                    elif seasons_data:
                         logger.warning(f"Separated 'seasons' data for {imdb_id} was not a dictionary (type: {type(seasons_data)}). Skipping season/episode update.")
                         seasons_update_success = False # Treat invalid season data as failure for atomic op


                    item.updated_at = current_time

                    # Commit or rollback local transaction
                    if seasons_update_success:
                        logger.info(f"Attempting commit for local atomic update {imdb_id}...")
                        local_session.commit()
                        logger.info(f"Successfully committed local atomic update for {item.title} ({item.imdb_id})")
                        return True
                    else:
                        logger.error(f"Rolling back local atomic update for {imdb_id} due to season/episode processing failure or invalid season data.")
                        # Rollback happens automatically via 'with' block on error or if we don't commit
                        return False
        except Exception as e:
            logger.error(f"Error in _update_metadata_atomic for item {imdb_id}: {str(e)}", exc_info=True)
            # If session was provided, DO NOT rollback here, re-raise
            if session:
                logger.debug(f"Re-raising exception from _update_metadata_atomic for {imdb_id} (session provided)")
                raise
            logger.debug(f"Returning False from _update_metadata_atomic for {imdb_id} due to exception (local session)")
            return False # Indicate failure for local session case
        finally:
            logger.debug(f"Exiting _update_metadata_atomic for {imdb_id}")

    @staticmethod
    def _add_or_update_seasons_and_episodes_with_session(item: Item, seasons_data: Dict, session: SqlAlchemySession) -> bool:
        """Internal helper to add/update seasons/episodes using an existing session."""
        from metadata.metadata import _get_local_timezone
        logger.debug(f"Starting season/episode update for {item.imdb_id} (item_id: {item.id}) within existing session.")
        # --- Added Logging ---
        logger.debug(f"Incoming seasons_data type: {type(seasons_data)}")
        try:
            # Log a snippet of the data, be careful with large data
            seasons_data_snippet = str(seasons_data)[:500] + ('...' if len(str(seasons_data)) > 500 else '')
            logger.debug(f"Incoming seasons_data (snippet): {seasons_data_snippet}")
        except Exception as log_err:
            logger.error(f"Error logging seasons_data snippet: {log_err}")
        # --- End Added Logging ---

        try:
            season_upsert_data = []
            all_episodes_to_process = [] # Collect all episodes first

            # --- Prepare Season Upserts and Collect All Episodes ---
            # Ensure seasons_data is a dictionary {season_number_str: season_dict}
            if not isinstance(seasons_data, dict):
                 logger.error(f"Expected seasons_data to be a dict, but got {type(seasons_data)} for {item.imdb_id}. Aborting season/episode update.")
                 return False # Cannot proceed

            logger.debug(f"Processing {len(seasons_data)} season entries from input data for {item.imdb_id}")
            for season_num_str, season_info in seasons_data.items():
                # --- Added Logging ---
                logger.debug(f"Processing season key: '{season_num_str}', value type: {type(season_info)}")
                # --- End Added Logging ---
                if not isinstance(season_info, dict):
                    logger.warning(f"Skipping season '{season_num_str}' for {item.imdb_id}: value is not a dictionary.")
                    continue

                try:
                    # Trakt uses 'number' for season number in its season list endpoint
                    # Let's be flexible, check 'number' first, then try parsing the key
                    season_number = season_info.get('number')
                    if season_number is None:
                        try:
                            season_number = int(season_num_str)
                        except ValueError:
                            logger.warning(f"Skipping season '{season_num_str}' for {item.imdb_id}: Cannot determine season number from key or 'number' field.")
                            continue

                    # Ensure episode_count is an integer, default to 0 if missing/invalid
                    episode_count = season_info.get('episode_count', 0)
                    if not isinstance(episode_count, int):
                        try:
                            episode_count = int(episode_count)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid episode_count '{season_info.get('episode_count')}' for season {season_number} of {item.imdb_id}. Defaulting to 0.")
                            episode_count = 0

                    # --- Added Logging ---
                    logger.debug(f"Prepared season upsert data for S{season_number}: item_id={item.id}, episode_count={episode_count}")
                    # --- End Added Logging ---
                    season_upsert_data.append({
                        'item_id': item.id,
                        'season_number': season_number,
                        'episode_count': episode_count, # Make sure this key exists in Trakt data or is calculated
                    })

                    # Collect episodes for this season
                    episodes_list = season_info.get('episodes', [])
                    if isinstance(episodes_list, list): # Trakt might return list or dict here, handle list
                        for episode_data in episodes_list:
                            if isinstance(episode_data, dict):
                                # Add season number for later processing
                                episode_data['season_number_for_upsert'] = season_number
                                all_episodes_to_process.append(episode_data)
                            else:
                                logger.warning(f"Skipping episode in S{season_number} for {item.imdb_id}: episode data is not a dictionary. Data: {episode_data}")
                    elif isinstance(episodes_list, dict): # Handle dict {ep_num_str: ep_dict}
                        for ep_num_str, episode_data in episodes_list.items():
                            if isinstance(episode_data, dict):
                                # Try to get episode number from data or key
                                ep_num = episode_data.get('number')
                                if ep_num is None:
                                    try: ep_num = int(ep_num_str)
                                    except ValueError: ep_num = None # Mark for skipping later
                                episode_data['number'] = ep_num # Ensure 'number' key exists
                                episode_data['season_number_for_upsert'] = season_number
                                all_episodes_to_process.append(episode_data)
                            else:
                                logger.warning(f"Skipping episode {ep_num_str} in S{season_number} for {item.imdb_id}: episode data is not a dictionary. Data: {episode_data}")
                    else:
                        logger.warning(f"Episodes data for S{season_number} of {item.imdb_id} is neither list nor dict: {type(episodes_list)}")

                except Exception as e:
                    logger.error(f"Error processing season '{season_num_str}' data for {item.imdb_id}: {e}", exc_info=True)


            # --- Bulk Upsert Seasons ---
            if not season_upsert_data:
                 logger.warning(f"No valid season data to upsert for {item.imdb_id}")
                 # Decide if this is an error or not. If no seasons is valid, return True.
                 # return True # Assuming empty is okay
            else:
                 # --- Added Logging ---
                 logger.debug(f"Prepared season_upsert_data count: {len(season_upsert_data)}")
                 # --- End Added Logging ---
                 logger.info(f"Executing bulk season upsert/update for {len(season_upsert_data)} seasons for {item.imdb_id}.")
                 season_stmt = insert(Season).values(season_upsert_data)
                 season_stmt = season_stmt.on_conflict_do_update(
                     # constraint='uix_item_season', # Use index_elements instead if constraint name varies or is unknown
                     index_elements=['item_id', 'season_number'], # Assumes a unique constraint/index exists on (item_id, season_number)
                     set_=dict(episode_count=season_stmt.excluded.episode_count)
                 )
                 session.execute(season_stmt)
                 logger.debug(f"Season upsert executed for {item.imdb_id}. Flushing session.")
                 session.flush() # Flush to get season IDs before processing episodes
                 logger.debug(f"Session flushed after season upsert for {item.imdb_id}.")

            # --- Prepare Episode Upserts ---
            # Fetch the mapping from season number to season ID *after* flushing the season upserts
            season_map = {s.season_number: s.id for s in session.query(Season.id, Season.season_number).filter_by(item_id=item.id).all()}
            logger.debug(f"Fetched season_map for item {item.id}: {season_map}") # Log the map

            episode_upsert_data = []
            logger.debug(f"Processing {len(all_episodes_to_process)} collected episode entries for {item.imdb_id}")
            for episode_data in all_episodes_to_process:
                season_number = episode_data.get('season_number_for_upsert')
                episode_number = episode_data.get('number') # Trakt uses 'number' for episode number

                # --- Added Logging ---
                logger.debug(f"Processing episode: S{season_number} E{episode_number}, Title: {episode_data.get('title')}")
                # --- End Added Logging ---

                if season_number is None or episode_number is None:
                    logger.warning(f"Skipping episode due to missing season ({season_number}) or episode number ({episode_number}). Data: {episode_data}")
                    continue

                season_id = season_map.get(season_number)
                if not season_id:
                    logger.warning(f"Skipping episode {episode_number} for season {season_number} of {item.imdb_id}: Corresponding season ID not found in season_map.")
                    continue

                # Parse 'first_aired' safely
                first_aired_str = episode_data.get('first_aired')
                first_aired_dt = None
                if first_aired_str:
                    try:
                        first_aired_dt = iso8601.parse_date(first_aired_str)
                        # Convert to offset-naive if necessary, or ensure timezone handling is consistent
                        # If your DB expects naive UTC:
                        # if first_aired_dt.tzinfo:
                        #      first_aired_dt = first_aired_dt.astimezone(timezone.utc).replace(tzinfo=None)
                        # If your DB handles timezone:
                        # Ensure it has timezone info (e.g., UTC)
                        if first_aired_dt.tzinfo is None:
                             # Assuming Trakt dates without timezone are UTC, but verify this assumption
                             first_aired_dt = first_aired_dt.replace(tzinfo=timezone.utc)

                    except iso8601.ParseError:
                        logger.warning(f"Could not parse first_aired date '{first_aired_str}' for S{season_number}E{episode_number} of {item.imdb_id}. Storing as None.")
                    except Exception as date_e:
                        logger.error(f"Unexpected error parsing date '{first_aired_str}': {date_e}")


                # Extract other fields safely
                title = episode_data.get('title') or ''
                overview = episode_data.get('overview', '')
                runtime = episode_data.get('runtime', 0)
                ids = episode_data.get('ids', {})
                episode_imdb_id = ids.get('imdb') if isinstance(ids, dict) else None

                # --- Added Logging ---
                logger.debug(f"  -> Prepared episode upsert: season_id={season_id}, ep_num={episode_number}, title='{title[:30]}...', imdb={episode_imdb_id}")
                # --- End Added Logging ---

                episode_upsert_data.append({
                    'season_id': season_id,
                    'episode_number': episode_number,
                    'title': title,
                    'overview': overview,
                    'runtime': runtime,
                    'first_aired': first_aired_dt,
                    'imdb_id': episode_imdb_id # Correct field name from Trakt is often within 'ids'
                })

            # --- Bulk Upsert Episodes ---
            if not episode_upsert_data:
                logger.warning(f"No valid episode data found to prepare for upsert for {item.imdb_id}")
                # Decide if this is an error or not. If no episodes is valid, return True.
                # return True # Assuming empty is okay
            else:
                # --- Added Logging ---
                logger.debug(f"Prepared episode_upsert_data count: {len(episode_upsert_data)}")
                # --- End Added Logging ---

                # ... Batch episode upserts logic ...
                chunk_size = 100 # Reduced chunk size for potentially better error isolation if needed
                total_episodes = len(episode_upsert_data)
                logger.info(f"Executing bulk episode upsert/update for {total_episodes} episodes for {item.imdb_id} in chunks of {chunk_size}")

                for i in range(0, total_episodes, chunk_size):
                     chunk = episode_upsert_data[i:i + chunk_size]
                     if not chunk: continue
                     logger.debug(f"Executing upsert for episode chunk {i // chunk_size + 1} ({len(chunk)} episodes)")

                     # Upsert statement
                     episode_stmt = insert(Episode).values(chunk)
                     episode_stmt = episode_stmt.on_conflict_do_update(
                         index_elements=['season_id', 'episode_number'], # Assumes unique index/constraint
                         set_=dict(
                             title=episode_stmt.excluded.title,
                             overview=episode_stmt.excluded.overview,
                             runtime=episode_stmt.excluded.runtime,
                             first_aired=episode_stmt.excluded.first_aired,
                             imdb_id=episode_stmt.excluded.imdb_id # Update imdb_id if it changes
                         )
                     )
                     session.execute(episode_stmt)
                     logger.debug(f"Episode chunk {(i // chunk_size) + 1}/{(total_episodes + chunk_size - 1) // chunk_size} executed for {item.imdb_id}.")


            # ** IMPORTANT: REMOVE COMMIT/ROLLBACK HERE **
            logger.debug(f"Successfully finished processing seasons/episodes for {item.imdb_id}. Returning True.")
            return True # Indicate success
        except Exception as e:
            # ** IMPORTANT: REMOVE COMMIT/ROLLBACK HERE **
            logger.error(f"Error in _add_or_update_seasons_and_episodes_with_session for {item.imdb_id}: {str(e)}", exc_info=True)
            logger.debug(f"Returning False from _add_or_update_seasons_and_episodes_with_session due to exception for {item.imdb_id}.")
            # Let the caller handle rollback by re-raising
            raise

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
    def get_release_dates(imdb_id, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            if session: # Use provided session
                item = session_context.query(Item).filter_by(imdb_id=imdb_id).first()
                if item:
                    metadata = session_context.query(Metadata).filter_by(item_id=item.id, key='release_dates').first()
                    if metadata:
                        if MetadataManager.is_metadata_stale(metadata.last_updated):
                            # Pass session down
                            return MetadataManager.refresh_release_dates(imdb_id, session_context)
                        else:
                            # ... handle JSON decoding ...
                            try: return json.loads(metadata.value), "battery"
                            except json.JSONDecodeError:
                                 logger.error(f"Error decoding JSON for release dates of IMDB ID: {imdb_id}")
                                 return MetadataManager.refresh_release_dates(imdb_id, session_context) # Pass session
                # Fetch from Trakt if not found (pass session)
                return MetadataManager.refresh_release_dates(imdb_id, session_context)
            else: # Create local session
                 with session_context as local_session:
                    item = local_session.query(Item).filter_by(imdb_id=imdb_id).first()
                    if item:
                        metadata = local_session.query(Metadata).filter_by(item_id=item.id, key='release_dates').first()
                        if metadata:
                            if MetadataManager.is_metadata_stale(metadata.last_updated):
                                return MetadataManager.refresh_release_dates(imdb_id, local_session) # Pass session
                            else:
                                try: return json.loads(metadata.value), "battery"
                                except json.JSONDecodeError:
                                    logger.error(f"Error decoding JSON for release dates of IMDB ID: {imdb_id}")
                                    return MetadataManager.refresh_release_dates(imdb_id, local_session) # Pass session
                    return MetadataManager.refresh_release_dates(imdb_id, local_session) # Pass session
        except Exception as e:
            logger.error(f"Error in get_release_dates for {imdb_id}: {e}", exc_info=True)
            if session: raise
            return None, None

    @staticmethod
    def refresh_release_dates(imdb_id, session: SqlAlchemySession): # Expects session
        trakt = TraktMetadata()
        
        item = session.query(Item).filter_by(imdb_id=imdb_id).first()
        if not item:
            logger.info(f"Item {imdb_id} not found during release date refresh. Fetching all metadata to create it.")
            # Fetch full metadata because we need title and year to create the item, and we should store it all.
            movie_metadata = trakt.get_movie_metadata(imdb_id)
            if not movie_metadata:
                logger.warning(f"Could not fetch full metadata for {imdb_id} to create item. Aborting release date refresh.")
                return None, None

            # Create item within the provided session
            item = Item(
                imdb_id=imdb_id,
                title=movie_metadata.get('title'),
                year=movie_metadata.get('year'),
                type='movie' # This function is for movies
            )
            session.add(item)
            session.flush() # Flush to get item.id
            logger.info(f"Created new movie item for {imdb_id} with title '{item.title}'")

            # Now that the item is created, store ALL the fetched metadata
            for key, value in movie_metadata.items():
                if isinstance(value, (dict, list)):
                    try:
                        value = json.dumps(value)
                    except TypeError:
                        value = str(value)
                else:
                    value = str(value)
                
                metadata_entry = Metadata(item_id=item.id, key=key, value=value, provider='Trakt')
                session.add(metadata_entry)

            # Also update the item's general timestamp
            from metadata.metadata import _get_local_timezone
            item.updated_at = datetime.now(_get_local_timezone())
            
            # Since we've just stored everything, we can return the release dates from the fetched data
            return movie_metadata.get('release_dates'), "trakt"

        # If item already exists, proceed to check and update just the release dates if necessary
        trakt_release_dates = trakt.get_release_dates(imdb_id)
        if trakt_release_dates:
            metadata = session.query(Metadata).filter_by(item_id=item.id, key='release_dates').first()
            if not metadata:
                metadata = Metadata(item_id=item.id, key='release_dates')
                session.add(metadata)

            metadata.value = json.dumps(trakt_release_dates)
            metadata.provider = 'Trakt'
            # Use func.now() or get current time appropriately
            from metadata.metadata import _get_local_timezone
            metadata.last_updated = datetime.now(_get_local_timezone())

            # ** DO NOT COMMIT HERE - Handled by caller **
            return trakt_release_dates, "trakt"

        logger.warning(f"No release dates found for IMDB ID: {imdb_id}")
        return None, None

    @staticmethod
    def tmdb_to_imdb(tmdb_id: str, media_type: str = None, session: Optional[SqlAlchemySession] = None) -> Optional[str]:
        session_context = session if session else DbSession()
        try:
            imdb_id = None
            source = None
            if session: # Use provided session
                cached_mapping = session_context.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                if cached_mapping:
                    # Check if the cached mapping is stale
                    is_stale = MetadataManager.is_tmdb_mapping_stale(cached_mapping.updated_at)
                    logger.debug(f"TMDB mapping for {tmdb_id}: cached={cached_mapping.imdb_id}, stale={is_stale}, last_updated={cached_mapping.updated_at}")
                    
                    if is_stale:
                        logger.info(f"TMDB mapping for {tmdb_id} is stale, refreshing from Trakt")
                        # Delete stale mapping and fetch fresh data
                        session_context.delete(cached_mapping)
                        session_context.flush()
                    else:
                        logger.debug(f"Using cached TMDB mapping for {tmdb_id}: {cached_mapping.imdb_id}")
                        return cached_mapping.imdb_id, 'battery'

                logger.info(f"Fetching fresh TMDB mapping for {tmdb_id} from Trakt")
                trakt = TraktMetadata()
                imdb_id, source = trakt.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)

                if imdb_id:
                    new_mapping = TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id)
                    session_context.add(new_mapping)
                    logger.info(f"Stored new TMDB mapping: {tmdb_id} -> {imdb_id}")
                    # ** NO COMMIT HERE **
                else:
                    logger.warning(f"No IMDB ID found for TMDB ID {tmdb_id} with type {media_type}")
                return imdb_id, source
            else: # Create local session
                 with session_context as local_session:
                    cached_mapping = local_session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                    if cached_mapping:
                        # Check if the cached mapping is stale
                        is_stale = MetadataManager.is_tmdb_mapping_stale(cached_mapping.updated_at)
                        logger.debug(f"TMDB mapping for {tmdb_id}: cached={cached_mapping.imdb_id}, stale={is_stale}, last_updated={cached_mapping.updated_at}")
                        
                        if is_stale:
                            logger.info(f"TMDB mapping for {tmdb_id} is stale, refreshing from Trakt")
                            # Delete stale mapping and fetch fresh data
                            local_session.delete(cached_mapping)
                            local_session.flush()
                        else:
                            logger.debug(f"Using cached TMDB mapping for {tmdb_id}: {cached_mapping.imdb_id}")
                            return cached_mapping.imdb_id, 'battery'

                    logger.info(f"Fetching fresh TMDB mapping for {tmdb_id} from Trakt")
                    trakt = TraktMetadata()
                    imdb_id, source = trakt.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)

                    if imdb_id:
                        new_mapping = TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id)
                        local_session.add(new_mapping)
                        local_session.commit() # Commit local transaction
                        logger.info(f"Stored new TMDB mapping: {tmdb_id} -> {imdb_id}")
                    else:
                        logger.warning(f"No IMDB ID found for TMDB ID {tmdb_id} with type {media_type}")
                    return imdb_id, source
        except Exception as e:
            logger.error(f"Error in tmdb_to_imdb for {tmdb_id}: {e}", exc_info=True)
            if session: raise
            return None, None

    @staticmethod
    def get_movie_metadata(imdb_id, session: Optional[SqlAlchemySession] = None):
        logger.debug(f"MetadataManager.get_movie_metadata called for {imdb_id}. Session provided: {session is not None}")
        session_context = session if session else DbSession()
        try:
            metadata = None
            source = None

            # Helper to check if the item is a known show
            def is_known_show(sess, current_imdb_id):
                show_item = sess.query(Item.id).filter_by(imdb_id=current_imdb_id, type='show').first()
                if show_item:
                    logger.warning(
                        f"get_movie_metadata called for IMDb ID {current_imdb_id}, which is already recorded as a 'show'. "
                        f"Skipping movie metadata fetch."
                    )
                    return True
                return False

            if session: # Use provided session
                 if is_known_show(session_context, imdb_id):
                     return None, "skipped_is_show"

                 item = session_context.query(Item).options(selectinload(Item.item_metadata)).filter_by(imdb_id=imdb_id, type='movie').first()
                 if item:
                     # ... format metadata ...
                     metadata = {}
                     for m in item.item_metadata:
                         try:
                             try: metadata[m.key] = json.loads(m.value)
                             except json.JSONDecodeError: metadata[m.key] = m.value
                         except Exception as e:
                             logger.error(f"Error processing metadata for key {m.key}: {str(e)}")
                             metadata[m.key] = m.value

                     if MetadataManager.is_metadata_stale(item.updated_at):
                         logger.info(f"Movie metadata for {imdb_id} is stale, refreshing (passing session)")
                         # Pass session down
                         refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=session_context)
                         return refreshed_data if refreshed_data else metadata, source if refreshed_data else "battery (stale, refresh failed)"
                     return metadata, "battery"

                 # If not in database, fetch (pass session)
                 logger.info(f"Movie {imdb_id} not found in database (as type 'movie'), fetching from Trakt (passing session)")
                 new_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=session_context)
                 return new_data, source

            else: # Create local session
                 with session_context as local_session:
                     if is_known_show(local_session, imdb_id):
                         return None, "skipped_is_show"

                     item = local_session.query(Item).options(selectinload(Item.item_metadata)).filter_by(imdb_id=imdb_id, type='movie').first()
                     if item:
                         # ... format metadata ...
                         metadata = {}
                         for m in item.item_metadata:
                             try:
                                 try: metadata[m.key] = json.loads(m.value)
                                 except json.JSONDecodeError: metadata[m.key] = m.value
                             except Exception as e:
                                 logger.error(f"Error processing metadata for key {m.key}: {str(e)}")
                                 metadata[m.key] = m.value

                         if MetadataManager.is_metadata_stale(item.updated_at):
                             logger.info(f"Movie metadata for {imdb_id} is stale, refreshing (local session)")
                             refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=local_session) # Pass session
                             return refreshed_data if refreshed_data else metadata, source if refreshed_data else "battery (stale, refresh failed)"
                         return metadata, "battery"

                     logger.info(f"Movie {imdb_id} not found in database (as type 'movie'), fetching from Trakt (local session)")
                     new_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=local_session) # Pass session
                     return new_data, source

        except Exception as e:
            logger.error(f"Error in get_movie_metadata for {imdb_id}: {str(e)}", exc_info=True)
            if session: raise
            return None, None

    @staticmethod
    def refresh_movie_metadata(imdb_id, session: SqlAlchemySession): # Expects session
        try:
            # Use the provided session
            trakt = TraktMetadata()
            new_metadata = trakt.get_movie_metadata(imdb_id)
            if not new_metadata:
                logger.warning(f"Could not fetch metadata for movie {imdb_id} from Trakt")
                return None, None

            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            
            if not item:
                try:
                    logger.debug(f"Item {imdb_id} not found. Attempting to create.")
                    item = Item(imdb_id=imdb_id, title=new_metadata.get('title'), year=new_metadata.get('year'), type='movie')
                    session.add(item)
                    session.flush() # Attempt to insert and get ID
                    logger.debug(f"Successfully created item {imdb_id} with new ID {item.id}")
                except IntegrityError: # Handle race condition where item was created by another process/thread
                    logger.warning(f"IntegrityError on creating item {imdb_id}. Item likely created concurrently. Re-querying.")
                    session.rollback() # Rollback the failed flush
                    item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                    if not item:
                        logger.error(f"Failed to re-query item {imdb_id} after IntegrityError. This should not happen.")
                        raise # Re-raise if item is still not found, something is seriously wrong
                    logger.debug(f"Successfully re-queried item {imdb_id} after IntegrityError. Item ID: {item.id}")
            else:
                logger.debug(f"Item {imdb_id} found with ID {item.id}. Proceeding with metadata update.")


            # Clear old metadata within the transaction
            # Ensure item is not None before proceeding
            if not item:
                logger.error(f"Item {imdb_id} is None after create/query logic. Aborting metadata update.")
                # This case should ideally be prevented by the checks above.
                # Depending on desired behavior, could return None, None or raise an error.
                return None, None

            session.query(Metadata).filter_by(item_id=item.id).delete(synchronize_session=False) # Changed from 'fetch' to False as we handle the session state

            # Add new metadata
            for key, value in new_metadata.items():
                if isinstance(value, (dict, list)):
                    try: value = json.dumps(value)
                    except TypeError as e:
                         logger.error(f"JSON Error for movie key '{key}' in {imdb_id}: {e}. Storing as string.")
                         value = str(value)
                else:
                    value = str(value)
                metadata_entry = Metadata(item_id=item.id, key=key, value=value, provider='Trakt') # Renamed to avoid conflict
                session.add(metadata_entry)

            from metadata.metadata import _get_local_timezone # Assuming this import exists or is valid
            item.updated_at = datetime.now(_get_local_timezone())

            # ** DO NOT COMMIT HERE ** Caller handles commit/rollback
            return new_metadata, "trakt"
        except IntegrityError as ie: # Catching IntegrityError specifically here again if it happens outside the item creation block
            logger.error(f"Unhandled IntegrityError during movie metadata refresh for {imdb_id}: {str(ie)}", exc_info=True)
            raise # Re-raise to be handled by the caller's transaction management
        except Exception as e:
            logger.error(f"Error refreshing movie metadata for {imdb_id}: {str(e)}", exc_info=True)
            # Let caller handle rollback via exception
            raise

    @staticmethod
    def update_movie_metadata(item, movie_data, session: SqlAlchemySession): # Expects session
        from metadata.metadata import _get_local_timezone
        try:
            # Use provided session
            item.updated_at = datetime.now(_get_local_timezone())
            # Use synchronize_session='fetch' or False, depending on needs/cascade behavior
            session.query(Metadata).filter_by(item_id=item.id).delete(synchronize_session=False)
            for key, value in movie_data.items():
                if isinstance(value, (list, dict)):
                    value = json.dumps(value)
                metadata = Metadata(item_id=item.id, key=key, value=str(value), provider='trakt')
                session.add(metadata)
            # ** DO NOT COMMIT HERE **
            return True # Indicate success
        except Exception as e:
             logger.error(f"Error in update_movie_metadata for {item.imdb_id}: {e}", exc_info=True)
             raise # Let caller handle rollback

    @staticmethod
    def get_show_metadata(imdb_id, session: Optional[SqlAlchemySession] = None):
        logger.info(f"MetadataManager.get_show_metadata called for {imdb_id}. Session provided: {session is not None}")
        session_context = session if session else DbSession()
        try:
            metadata = None
            source = None
            if session: # Use provided session
                 logger.debug(f"Using provided session for get_show_metadata: {imdb_id}")
                 # Use selectinload with the provided session
                 item = session_context.query(Item).options(
                     selectinload(Item.item_metadata),
                     selectinload(Item.seasons).selectinload(Season.episodes)
                 ).filter_by(imdb_id=imdb_id, type='show').first()

                 # --- ADDED LOGGING ---
                 if item:
                     logger.debug(f"Item {imdb_id} found in provided session. Checking loaded seasons...")
                     try:
                         # Explicitly check the loaded relationship BEFORE the refresh logic
                         loaded_seasons = item.seasons
                         logger.debug(f"Number of seasons loaded via relationship for item {item.id}: {len(loaded_seasons)}")
                         if not loaded_seasons:
                              logger.warning(f"item.seasons collection is empty for item {item.id} ({imdb_id}) immediately after query.")
                         # Optionally log season numbers if needed:
                         # season_numbers = [s.season_number for s in loaded_seasons]
                         # logger.debug(f"Loaded season numbers: {season_numbers}")
                     except Exception as e_log:
                         logger.error(f"Error accessing item.seasons for logging: {e_log}")
                 else:
                     logger.debug(f"Item {imdb_id} not found in provided session query.")
                 # --- END ADDED LOGGING ---


                 if item:
                     # ... (rest of the 'if item:' block remains the same, including the existing 'seasons_loaded' check and refresh logic) ...
                     metadata = {}
                     has_xem = False
                     tvdb_id = None
                     for m in item.item_metadata:
                         # ... (try/except json.loads) ...
                         try:
                             value = json.loads(m.value)
                             metadata[m.key] = value
                             if m.key == 'ids' and isinstance(value, dict): tvdb_id = value.get('tvdb')
                             if m.key == 'xem_mapping': has_xem = True
                         except (json.JSONDecodeError, TypeError): metadata[m.key] = m.value
                         except Exception as e:
                             logger.error(f"Error processing metadata key {m.key} for {imdb_id}: {e}")
                             metadata[m.key] = m.value

                     # ... timezone check ...
                     if item.updated_at and item.updated_at.tzinfo is None:
                         from metadata.metadata import _get_local_timezone
                         item.updated_at = item.updated_at.replace(tzinfo=_get_local_timezone())

                     is_stale = MetadataManager.is_metadata_stale(item.updated_at)
                     # --- Check if seasons were actually loaded ---
                     seasons_loaded = bool(item.seasons) # Check if the list is non-empty

                     needs_full_refresh = (not seasons_loaded or is_stale)
                     if not seasons_loaded: logger.info(f"Metadata for {imdb_id} needs refresh: No seasons found relationally (provided session).")
                     if is_stale: logger.info(f"Metadata for {imdb_id} needs refresh: Data is stale (updated_at: {item.updated_at}).")


                     if needs_full_refresh:
                         logger.info(f"Metadata for {imdb_id} requires refresh (Seasons Missing: {not seasons_loaded}, Stale: {is_stale}).")
                         # Pass session down
                         refreshed_data = MetadataManager.refresh_metadata(imdb_id, session=session_context)
                         if refreshed_data:
                             logger.info(f"Successfully refreshed and saved metadata for {imdb_id} (within provided session).")
                             # refresh_metadata now returns the data dict, source implies trakt
                             return refreshed_data, "trakt (refreshed)"
                         else:
                             logger.warning(f"Refresh failed or save failed for {imdb_id}. Returning potentially stale data from Metadata table.")
                             metadata['seasons'] = {} # Indicate seasons are missing/stale
                             return metadata, "battery (stale, refresh failed)"
                     else:
                         # Data is fresh, format from relational
                         logger.info(f"Metadata for {imdb_id} is fresh, returning from battery (provided session).")
                         metadata['seasons'] = MetadataManager.format_seasons_data(item.seasons)
                         # ... log counts ...

                         # Check/Fetch XEM (still might write if missing)
                         if not has_xem:
                            logger.info(f"Existing metadata for {imdb_id} is missing XEM mapping. Attempting to fetch...")
                            if tvdb_id:
                                try:
                                    xem_mapping_data = fetch_xem_mapping(tvdb_id)
                                    xem_mapping_data = xem_mapping_data if xem_mapping_data else {}
                                    logger.info(f"{'Successfully fetched' if xem_mapping_data else 'No'} XEM mapping for TVDB ID {tvdb_id}.")
                                    metadata['xem_mapping'] = xem_mapping_data
                                    # --- Save XEM mapping within the current transaction ---
                                    xem_meta = session_context.query(Metadata).filter_by(item_id=item.id, key='xem_mapping').first()
                                    if not xem_meta:
                                        xem_meta = Metadata(item_id=item.id, key='xem_mapping', provider='xem')
                                        session_context.add(xem_meta)
                                    xem_meta.value = json.dumps(xem_mapping_data)
                                    from metadata.metadata import _get_local_timezone
                                    xem_meta.last_updated = datetime.now(_get_local_timezone())
                                    logger.info(f"Saved/Updated XEM mapping for {imdb_id} in provided session.")
                                    # ** NO COMMIT HERE **
                                except Exception as xem_error:
                                    logger.error(f"Error fetching/saving XEM mapping for TVDB ID {tvdb_id}: {xem_error}")
                                    metadata['xem_mapping'] = {}
                            else:
                                logger.warning(f"Cannot fetch XEM mapping for {imdb_id}: TVDB ID not found.")
                                metadata['xem_mapping'] = {}
                         return metadata, "battery"

                 # Item not found in provided session, fetch from Trakt
                 logger.info(f"Item {imdb_id} not found in database, fetching from Trakt (passing session).")
                 # Pass session down
                 initial_data = MetadataManager.refresh_metadata(imdb_id, session=session_context)
                 if initial_data:
                      logger.info(f"Successfully fetched and saved initial metadata for {imdb_id} (within provided session).")
                      return initial_data, "trakt (new)"
                 else:
                      logger.error(f"Failed to fetch or save initial metadata for {imdb_id} (within provided session).")
                      return None, None

            else: # Create local session
                 logger.debug(f"Creating local session for get_show_metadata: {imdb_id}")
                 with session_context as local_session:
                    item = local_session.query(Item).options(
                        selectinload(Item.item_metadata),
                        selectinload(Item.seasons).selectinload(Season.episodes)
                    ).filter_by(imdb_id=imdb_id, type='show').first()

                    # --- ADDED LOGGING ---
                    if item:
                         logger.debug(f"Item {imdb_id} found in local session. Checking loaded seasons...")
                         try:
                             loaded_seasons = item.seasons
                             logger.debug(f"Number of seasons loaded via relationship for item {item.id}: {len(loaded_seasons)}")
                             if not loaded_seasons:
                                 logger.warning(f"item.seasons collection is empty for item {item.id} ({imdb_id}) immediately after query (local session).")
                         except Exception as e_log:
                             logger.error(f"Error accessing item.seasons for logging (local session): {e_log}")
                    else:
                         logger.debug(f"Item {imdb_id} not found in local session query.")
                    # --- END ADDED LOGGING ---

                    if item:
                         # ... (rest of the 'if item:' block remains the same, identical to the 'if session:' block above) ...
                         metadata = {}
                         has_xem = False
                         tvdb_id = None
                         for m in item.item_metadata:
                              try:
                                 value = json.loads(m.value)
                                 metadata[m.key] = value
                                 if m.key == 'ids' and isinstance(value, dict): tvdb_id = value.get('tvdb')
                                 if m.key == 'xem_mapping': has_xem = True
                              except (json.JSONDecodeError, TypeError): metadata[m.key] = m.value
                              except Exception as e:
                                 logger.error(f"Error processing metadata key {m.key} for {imdb_id}: {e}")
                                 metadata[m.key] = m.value

                         # ... timezone check ...
                         if item.updated_at and item.updated_at.tzinfo is None:
                             from metadata.metadata import _get_local_timezone
                             item.updated_at = item.updated_at.replace(tzinfo=_get_local_timezone())

                         is_stale = MetadataManager.is_metadata_stale(item.updated_at)
                         seasons_loaded = bool(item.seasons) # Check if loaded

                         needs_full_refresh = (not seasons_loaded or is_stale)
                         if not seasons_loaded: logger.info(f"Metadata for {imdb_id} needs refresh: No seasons found relationally (local session).")
                         if is_stale: logger.info(f"Metadata for {imdb_id} needs refresh: Data is stale (updated_at: {item.updated_at}).")

                         if needs_full_refresh:
                             logger.info(f"Metadata for {imdb_id} requires refresh (Seasons Missing: {not seasons_loaded}, Stale: {is_stale}).")
                             # Pass session down
                             refreshed_data = MetadataManager.refresh_metadata(imdb_id, session=local_session)
                             if refreshed_data:
                                 logger.info(f"Successfully refreshed and saved metadata for {imdb_id} (within local session).")
                                 # refresh_metadata handles commit for local session case
                                 return refreshed_data, "trakt (refreshed)"
                             else:
                                 logger.warning(f"Refresh failed or save failed for {imdb_id}. Returning potentially stale data from Metadata table.")
                                 metadata['seasons'] = {}
                                 return metadata, "battery (stale, refresh failed)"
                         else:
                             # Data is fresh, format from relational
                             logger.info(f"Metadata for {imdb_id} is fresh, returning from battery (local session).")
                             metadata['seasons'] = MetadataManager.format_seasons_data(item.seasons)
                             # ... log counts ...

                             # Check/Fetch XEM (still might write if missing)
                             if not has_xem:
                                logger.info(f"Existing metadata for {imdb_id} is missing XEM mapping. Attempting to fetch...")
                                if tvdb_id:
                                    try:
                                        xem_mapping_data = fetch_xem_mapping(tvdb_id)
                                        xem_mapping_data = xem_mapping_data if xem_mapping_data else {}
                                        logger.info(f"{'Successfully fetched' if xem_mapping_data else 'No'} XEM mapping for TVDB ID {tvdb_id}.")
                                        metadata['xem_mapping'] = xem_mapping_data
                                        # Save XEM mapping within the local transaction
                                        xem_meta = local_session.query(Metadata).filter_by(item_id=item.id, key='xem_mapping').first()
                                        if not xem_meta:
                                            xem_meta = Metadata(item_id=item.id, key='xem_mapping', provider='xem')
                                            local_session.add(xem_meta)
                                        xem_meta.value = json.dumps(xem_mapping_data)
                                        from metadata.metadata import _get_local_timezone
                                        xem_meta.last_updated = datetime.now(_get_local_timezone())
                                        # Commit happens at end of 'with' block
                                        logger.info(f"Saved/Updated XEM mapping for {imdb_id} in local session.")
                                    except Exception as xem_error:
                                        logger.error(f"Error fetching/saving XEM mapping for TVDB ID {tvdb_id}: {xem_error}")
                                        metadata['xem_mapping'] = {} # Add empty on error
                                        # Don't rollback the whole transaction just for XEM fetch error
                                else:
                                    logger.warning(f"Cannot fetch XEM mapping for {imdb_id}: TVDB ID not found.")
                                    metadata['xem_mapping'] = {}
                             return metadata, "battery"

                    # Item not found, fetch from Trakt
                    logger.info(f"Item {imdb_id} not found in database, fetching from Trakt (local session).")
                    initial_data = MetadataManager.refresh_metadata(imdb_id, session=local_session) # Pass session
                    if initial_data:
                         logger.info(f"Successfully fetched and saved initial metadata for {imdb_id} (within local session).")
                         # refresh_metadata handles commit for local session case
                         return initial_data, "trakt (new)"
                    else:
                         logger.error(f"Failed to fetch or save initial metadata for {imdb_id} (within local session).")
                         return None, None

        except Exception as e:
            logger.error(f"Error in get_show_metadata logic for {imdb_id}: {e}", exc_info=True)
            if session: # Re-raise if session was provided
                 logger.debug(f"Re-raising exception from get_show_metadata for {imdb_id} (session provided)")
                 raise
            # Otherwise, error in local session handling
            logger.debug(f"Returning None from get_show_metadata for {imdb_id} due to exception (local session)")
            return None, None # Return default if local session had error

    @staticmethod
    def update_show_metadata(item, show_data, session: SqlAlchemySession): # Expects session
        try:
            # Use provided session
            from metadata.metadata import _get_local_timezone
            current_time = datetime.now(_get_local_timezone())
            item.updated_at = current_time
            # Delete existing metadata
            deleted_count = session.query(Metadata).filter_by(item_id=item.id).delete(synchronize_session=False) # Consider synchronize_session setting
            logger.info(f"Deleted {deleted_count} existing metadata entries for {item.imdb_id}")
            session.flush()

            # --- Fetch XEM Mapping ---
            xem_mapping_data = None
            tvdb_id = show_data.get('ids', {}).get('tvdb')
            if tvdb_id:
                try:
                    xem_mapping_data = fetch_xem_mapping(tvdb_id)
                    show_data['xem_mapping'] = xem_mapping_data if xem_mapping_data else {}
                    # ... logging ...
                except Exception as xem_error:
                    logger.error(f"Error fetching XEM mapping for TVDB ID {tvdb_id}: {xem_error}")
                    show_data['xem_mapping'] = {}
            else:
                logger.warning(f"TVDB ID not found in show_data for {item.imdb_id}, storing empty XEM mapping")
                show_data['xem_mapping'] = {}

            # Add new metadata
            metadata_entries = []
            # ... prepare metadata_entries ...
            for key, value in show_data.items():
                 if isinstance(value, (list, dict)):
                     try: value = json.dumps(value)
                     except TypeError as e:
                         logger.error(f"Error converting value to JSON for key '{key}' in {item.imdb_id}: {e}. Storing as string.")
                         value = str(value)
                 if not isinstance(value, str): value = str(value)
                 metadata = Metadata(item_id=item.id, key=key, value=value, provider='trakt', last_updated=current_time)
                 metadata_entries.append(metadata)


            session.bulk_save_objects(metadata_entries)
            session.flush()

            # ** DO NOT COMMIT HERE **
            logger.info(f"Prepared {len(metadata_entries)} metadata entries for commit for {item.imdb_id}")
            return True # Indicate success
        except Exception as e:
            logger.error(f"Error in update_show_metadata for item {item.imdb_id}: {str(e)}")
            raise # Let caller handle rollback

    @staticmethod
    def get_show_aliases(imdb_id, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            aliases = None
            source = None
            if session: # Use provided session
                item = session_context.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
                if item:
                    metadata = session_context.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                    if metadata:
                        if MetadataManager.is_metadata_stale(metadata.last_updated):
                            # Pass session down
                            refreshed_data, _ = MetadataManager.refresh_show_metadata(imdb_id, session=session_context)
                            return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                        try: return json.loads(metadata.value), "battery"
                        except json.JSONDecodeError:
                            logger.error(f"Error decoding JSON for aliases of IMDB ID: {imdb_id}")
                            refreshed_data, _ = MetadataManager.refresh_show_metadata(imdb_id, session=session_context) # Pass session
                            return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                # Fetch from Trakt if not found (pass session)
                refreshed_data, source = MetadataManager.refresh_show_metadata(imdb_id, session=session_context)
                return refreshed_data.get('aliases') if refreshed_data else None, source

            else: # Create local session
                 with session_context as local_session:
                    item = local_session.query(Item).filter_by(imdb_id=imdb_id, type='show').first()
                    if item:
                        metadata = local_session.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                        if metadata:
                            if MetadataManager.is_metadata_stale(metadata.last_updated):
                                refreshed_data, _ = MetadataManager.refresh_show_metadata(imdb_id, session=local_session) # Pass session
                                return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                            try: return json.loads(metadata.value), "battery"
                            except json.JSONDecodeError:
                                logger.error(f"Error decoding JSON for aliases of IMDB ID: {imdb_id}")
                                refreshed_data, _ = MetadataManager.refresh_show_metadata(imdb_id, session=local_session) # Pass session
                                return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                    # Fetch from Trakt if not found (pass session)
                    refreshed_data, source = MetadataManager.refresh_show_metadata(imdb_id, session=local_session)
                    return refreshed_data.get('aliases') if refreshed_data else None, source
        except Exception as e:
            logger.error(f"Error in get_show_aliases for {imdb_id}: {e}", exc_info=True)
            if session: raise
            return None, None

    @staticmethod
    def get_movie_aliases(imdb_id, session: Optional[SqlAlchemySession] = None):
        session_context = session if session else DbSession()
        try:
            aliases = None
            source = None
            if session: # Use provided session
                item = session_context.query(Item).filter_by(imdb_id=imdb_id, type='movie').first()
                if item:
                    metadata = session_context.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                    if metadata:
                        if MetadataManager.is_metadata_stale(metadata.last_updated):
                            refreshed_data, _ = MetadataManager.refresh_movie_metadata(imdb_id, session=session_context) # Pass session
                            return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                        try: return json.loads(metadata.value), "battery"
                        except json.JSONDecodeError:
                            logger.error(f"Error decoding JSON for aliases of IMDB ID: {imdb_id}")
                            refreshed_data, _ = MetadataManager.refresh_movie_metadata(imdb_id, session=session_context) # Pass session
                            return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                # Fetch from Trakt if not found (pass session)
                refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=session_context)
                return refreshed_data.get('aliases') if refreshed_data else None, source
            else: # Create local session
                 with session_context as local_session:
                    item = local_session.query(Item).filter_by(imdb_id=imdb_id, type='movie').first()
                    if item:
                        metadata = local_session.query(Metadata).filter_by(item_id=item.id, key='aliases').first()
                        if metadata:
                            if MetadataManager.is_metadata_stale(metadata.last_updated):
                                refreshed_data, _ = MetadataManager.refresh_movie_metadata(imdb_id, session=local_session) # Pass session
                                return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                            try: return json.loads(metadata.value), "battery"
                            except json.JSONDecodeError:
                                logger.error(f"Error decoding JSON for aliases of IMDB ID: {imdb_id}")
                                refreshed_data, _ = MetadataManager.refresh_movie_metadata(imdb_id, session=local_session) # Pass session
                                return refreshed_data.get('aliases') if refreshed_data else None, "trakt"
                    # Fetch from Trakt if not found (pass session)
                    refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=local_session)
                    return refreshed_data.get('aliases') if refreshed_data else None, source
        except Exception as e:
            logger.error(f"Error in get_movie_aliases for {imdb_id}: {e}", exc_info=True)
            if session: raise
            return None, None

    @staticmethod
    def refresh_show_metadata(imdb_id, session: SqlAlchemySession): # Expects session
        try:
            # Use provided session
            trakt = TraktMetadata()
            show_data = trakt.get_show_metadata(imdb_id) # Fetches from API, DB interaction below
            if show_data:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                if not item:
                    item = Item(imdb_id=imdb_id, title=show_data.get('title'), type='show', year=show_data.get('year'))
                    session.add(item)
                    session.flush()

                # Pass session down to the helper that now expects it
                success = MetadataManager.update_show_metadata(item, show_data, session)
                # ** DO NOT COMMIT HERE **
                if success:
                    return show_data, "trakt"
                else:
                    # update_show_metadata should raise on error if session provided
                    logger.error(f"update_show_metadata returned False unexpectedly for {imdb_id} with provided session.")
                    return None, None # Should ideally not happen if exceptions are raised

            logger.warning(f"No show metadata found for IMDB ID: {imdb_id}")
            return None, None
        except Exception as e:
            logger.error(f"Error in refresh_show_metadata for IMDb ID {imdb_id}: {str(e)}", exc_info=True)
            raise # Let caller handle rollback

    @staticmethod
    def get_bulk_show_airs_info(imdb_ids: list[str], session: Optional[SqlAlchemySession] = None) -> dict[str, Optional[dict[str, Any]]]:
        session_context = session if session else DbSession()
        airs_info = {imdb_id: None for imdb_id in imdb_ids}
        if not imdb_ids: return airs_info

        try:
            if session: # Use provided session
                 # Corrected query to filter items by IMDb ID and type
                 items = session_context.query(Item.id, Item.imdb_id).filter(
                     Item.imdb_id.in_(imdb_ids), Item.type == 'show'
                 ).all()
                 item_id_to_imdb_id = {item.id: item.imdb_id for item in items}
                 item_ids = list(item_id_to_imdb_id.keys())
                 if not item_ids: return airs_info # No matching show items found

                 # Corrected query to filter metadata by item IDs and key
                 airs_metadata_rows = session_context.query(Metadata).filter(
                     Metadata.item_id.in_(item_ids), Metadata.key == 'airs'
                 ).all()

                 # Process airs metadata
                 for metadata in airs_metadata_rows:
                     imdb_id = item_id_to_imdb_id.get(metadata.item_id)
                     if imdb_id:
                         try:
                             airs_data = json.loads(metadata.value)
                             if isinstance(airs_data, dict):
                                 airs_info[imdb_id] = airs_data
                             else:
                                  logger.warning(f"Airs metadata for {imdb_id} (item_id {metadata.item_id}) is not a dict: {type(airs_data)}")
                         except json.JSONDecodeError:
                             logger.error(f"Failed to decode airs JSON for {imdb_id} (item_id {metadata.item_id})")
                         except Exception as e:
                              logger.error(f"Error processing airs metadata for {imdb_id}: {e}")

                 return airs_info
            else: # Create local session
                 with session_context as local_session:
                     # Corrected query to filter items by IMDb ID and type
                     items = local_session.query(Item.id, Item.imdb_id).filter(
                         Item.imdb_id.in_(imdb_ids), Item.type == 'show'
                     ).all()
                     item_id_to_imdb_id = {item.id: item.imdb_id for item in items}
                     item_ids = list(item_id_to_imdb_id.keys())
                     if not item_ids: return airs_info # No matching show items found

                     # Corrected query to filter metadata by item IDs and key
                     airs_metadata_rows = local_session.query(Metadata).filter(
                         Metadata.item_id.in_(item_ids), Metadata.key == 'airs'
                     ).all()

                     # Process airs metadata
                     for metadata in airs_metadata_rows:
                         imdb_id = item_id_to_imdb_id.get(metadata.item_id)
                         if imdb_id:
                            try:
                                airs_data = json.loads(metadata.value)
                                if isinstance(airs_data, dict):
                                     airs_info[imdb_id] = airs_data
                                else:
                                      logger.warning(f"Airs metadata for {imdb_id} (item_id {metadata.item_id}) is not a dict: {type(airs_data)}")
                            except json.JSONDecodeError:
                                logger.error(f"Failed to decode airs JSON for {imdb_id} (item_id {metadata.item_id})")
                            except Exception as e:
                                  logger.error(f"Error processing airs metadata for {imdb_id}: {e}")

                     return airs_info
        except Exception as e:
            logger.error(f"Error in get_bulk_show_airs_info: {e}", exc_info=True)
            if session: raise
            return {imdb_id: None for imdb_id in imdb_ids} # Return default on error

    @staticmethod
    def get_bulk_movie_metadata(imdb_ids: List[str], session: Optional[SqlAlchemySession] = None) -> Dict[str, Optional[Dict[str, Any]]]:
        session_context = session if session else DbSession()
        metadata_map = {imdb_id: None for imdb_id in imdb_ids}
        if not imdb_ids: return metadata_map

        try:
            if session: # Use provided session
                 # Corrected filter condition
                 items = session_context.query(Item).options(
                     selectinload(Item.item_metadata)
                 ).filter(
                     Item.imdb_id.in_(imdb_ids), Item.type == 'movie'
                 ).all()
                 # ... process items ...
                 for item in items:
                     item_metadata = {}
                     for m in item.item_metadata:
                         try:
                             try: item_metadata[m.key] = json.loads(m.value)
                             except json.JSONDecodeError: item_metadata[m.key] = m.value
                         except Exception as e:
                             logger.error(f"Error processing metadata key {m.key} for {item.imdb_id}: {e}")
                             item_metadata[m.key] = m.value
                     metadata_map[item.imdb_id] = item_metadata

                 found_ids = {item.imdb_id for item in items}
                 not_found_ids = set(imdb_ids) - found_ids
                 if not_found_ids:
                     logger.info(f"Did not find movie metadata in battery for IMDb IDs: {list(not_found_ids)}")

                 return metadata_map
            else: # Create local session
                 with session_context as local_session:
                      # Corrected filter condition
                      items = local_session.query(Item).options(
                          selectinload(Item.item_metadata)
                      ).filter(
                          Item.imdb_id.in_(imdb_ids), Item.type == 'movie'
                      ).all()
                      # ... process items ...
                      for item in items:
                          item_metadata = {}
                          for m in item.item_metadata:
                              try:
                                  try: item_metadata[m.key] = json.loads(m.value)
                                  except json.JSONDecodeError: item_metadata[m.key] = m.value
                              except Exception as e:
                                  logger.error(f"Error processing metadata key {m.key} for {item.imdb_id}: {e}")
                                  item_metadata[m.key] = m.value
                          metadata_map[item.imdb_id] = item_metadata

                      found_ids = {item.imdb_id for item in items}
                      not_found_ids = set(imdb_ids) - found_ids
                      if not_found_ids:
                          logger.info(f"Did not find movie metadata in battery for IMDb IDs: {list(not_found_ids)}")

                      return metadata_map
        except Exception as e:
            logger.error(f"Error in get_bulk_movie_metadata: {e}", exc_info=True)
            if session: raise
            return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def get_bulk_show_metadata(imdb_ids: List[str], session: Optional[SqlAlchemySession] = None) -> Dict[str, Optional[Dict[str, Any]]]:
        session_context = session if session else DbSession()
        metadata_map = {imdb_id: None for imdb_id in imdb_ids}
        if not imdb_ids: return metadata_map

        try:
            if session: # Use provided session
                 # Ensure Item.item_metadata and seasons/episodes are loaded
                 items = session_context.query(Item).options(
                     selectinload(Item.item_metadata),
                     selectinload(Item.seasons).selectinload(Season.episodes) # Eager load seasons and episodes
                 ).filter(
                     Item.imdb_id.in_(imdb_ids), Item.type == 'show'
                 ).all()
                 # ... rest of the processing ...
                 items_missing_xem = {}
                 tvdb_ids_to_fetch_xem = {}
                 item_ids_to_save_empty_xem = []

                 for item in items:
                      # --- Define and populate item_metadata dict ---\
                      item_metadata = {}
                      has_xem = False
                      tvdb_id = None
                      for m in item.item_metadata:
                           try:
                               value = json.loads(m.value)
                               item_metadata[m.key] = value
                               if m.key == 'ids' and isinstance(value, dict): tvdb_id = value.get('tvdb')
                               if m.key == 'xem_mapping': has_xem = True
                           except (json.JSONDecodeError, TypeError): item_metadata[m.key] = m.value
                           except Exception as e:
                               logger.error(f"Error processing metadata key {m.key} for {item.imdb_id}: {e}")
                               item_metadata[m.key] = m.value
                      # --- End definition ---\

                      # Add formatted seasons data if available
                      if hasattr(item, 'seasons') and item.seasons:
                          try:
                              item_metadata['seasons'] = MetadataManager.format_seasons_data(item.seasons)
                              logger.debug(f"Formatted and added season data for {item.imdb_id} in bulk fetch (provided session).")
                          except Exception as e_seasons:
                              logger.error(f"Error formatting season data for {item.imdb_id} in bulk (provided session): {e_seasons}")
                              item_metadata['seasons'] = {} # Default to empty if formatting fails
                      else:
                          item_metadata['seasons'] = {} # Default to empty if no seasons loaded
                          logger.debug(f"No seasons loaded or attribute missing for {item.imdb_id} in bulk fetch (provided session).")


                      metadata_map[item.imdb_id] = item_metadata # Use the populated dict
                      # ... check if XEM needs fetching ...
                      if not has_xem:
                          items_missing_xem[item.id] = item.imdb_id
                          if tvdb_id:
                              tvdb_ids_to_fetch_xem[tvdb_id] = item.id
                          else:
                              logger.warning(f"Cannot fetch XEM mapping for {item.imdb_id}: TVDB ID not found in existing metadata. Will store empty mapping.")
                              item_ids_to_save_empty_xem.append(item.id)


                 # --- Bulk Fetch XEM Mapping --- (No DB interaction here)
                 fetched_xem_mappings = {}
                 # ... fetch logic ...
                 if tvdb_ids_to_fetch_xem:
                     tvdb_ids = list(tvdb_ids_to_fetch_xem.keys())
                     logger.info(f"Bulk fetching XEM mappings for {len(tvdb_ids)} TVDB IDs...")
                     for tvdb_id_fetch in tvdb_ids: # Use different variable name
                          item_id = tvdb_ids_to_fetch_xem[tvdb_id_fetch]
                          imdb_id_fetch = items_missing_xem[item_id] # Use different variable name
                          try:
                              xem_data = fetch_xem_mapping(tvdb_id_fetch)
                              fetched_xem_mappings[item_id] = xem_data if xem_data else {}
                              logger.info(f"{'Found' if xem_data else 'No'} XEM mapping for TVDB {tvdb_id_fetch} (IMDb: {imdb_id_fetch})")
                          except Exception as e:
                              logger.error(f"Error fetching XEM for TVDB {tvdb_id_fetch} (IMDb: {imdb_id_fetch}): {e}")
                              fetched_xem_mappings[item_id] = {}


                 # --- Prepare and Save XEM Mappings ---
                 metadata_to_save = []
                 # ... prepare metadata_to_save list ...
                 for item_id, imdb_id_save in items_missing_xem.items(): # Use different variable name
                     xem_data_to_add = fetched_xem_mappings.get(item_id, {})
                     if metadata_map.get(imdb_id_save):
                         metadata_map[imdb_id_save]['xem_mapping'] = xem_data_to_add
                     # Prepare for DB save
                     from metadata.metadata import _get_local_timezone
                     current_time = datetime.now(_get_local_timezone())
                     # Check if xem mapping already exists for this item_id before creating a new Metadata object
                     existing_xem = session_context.query(Metadata).filter_by(item_id=item_id, key='xem_mapping').first()
                     if existing_xem:
                         existing_xem.value = json.dumps(xem_data_to_add)
                         existing_xem.last_updated = current_time
                         metadata_to_save.append(existing_xem) # Add existing object to merge
                     else:
                         metadata_to_save.append(
                             Metadata(
                                 item_id=item_id, key='xem_mapping',
                                 value=json.dumps(xem_data_to_add),
                                 provider='xem', last_updated=current_time
                             )
                         )


                 if metadata_to_save:
                     logger.info(f"Saving/Updating {len(metadata_to_save)} XEM mappings within provided session.")
                     for xem_meta in metadata_to_save:
                         session_context.merge(xem_meta) # Use merge for upsert based on PK
                     # ** NO COMMIT HERE **

                 # ... log not found ...
                 found_ids = {item.imdb_id for item in items}
                 not_found_ids = set(imdb_ids) - found_ids
                 if not_found_ids:
                      logger.info(f"Did not find show metadata in battery for IMDb IDs: {list(not_found_ids)}")
                 return metadata_map

            else: # Create local session
                 with session_context as local_session:
                      # Ensure Item.item_metadata and seasons/episodes are loaded
                      items = local_session.query(Item).options(
                          selectinload(Item.item_metadata),
                          selectinload(Item.seasons).selectinload(Season.episodes) # Eager load seasons and episodes
                      ).filter(
                          Item.imdb_id.in_(imdb_ids), Item.type == 'show'
                      ).all()
                      # ... process items, check/fetch XEM ...
                      items_missing_xem = {}
                      tvdb_ids_to_fetch_xem = {}
                      item_ids_to_save_empty_xem = []
                      for item in items:
                          # --- Define and populate item_metadata dict ---\
                          item_metadata = {}
                          has_xem = False
                          tvdb_id = None
                          for m in item.item_metadata:
                              try:
                                  value = json.loads(m.value)
                                  item_metadata[m.key] = value
                                  if m.key == 'ids' and isinstance(value, dict): tvdb_id = value.get('tvdb')
                                  if m.key == 'xem_mapping': has_xem = True
                              except (json.JSONDecodeError, TypeError): item_metadata[m.key] = m.value
                              except Exception as e:
                                  logger.error(f"Error processing metadata key {m.key} for {item.imdb_id}: {e}")
                                  item_metadata[m.key] = m.value
                          # --- End definition ---\

                          # Add formatted seasons data if available
                          if hasattr(item, 'seasons') and item.seasons:
                              try:
                                  item_metadata['seasons'] = MetadataManager.format_seasons_data(item.seasons)
                                  logger.debug(f"Formatted and added season data for {item.imdb_id} in bulk fetch (local session).")
                              except Exception as e_seasons:
                                  logger.error(f"Error formatting season data for {item.imdb_id} in bulk (local session): {e_seasons}")
                                  item_metadata['seasons'] = {} # Default to empty if formatting fails
                          else:
                              item_metadata['seasons'] = {} # Default to empty if no seasons loaded
                              logger.debug(f"No seasons loaded or attribute missing for {item.imdb_id} in bulk fetch (local session).")

                          metadata_map[item.imdb_id] = item_metadata # Use the populated dict
                          # ... check if XEM needs fetching ...
                          if not has_xem:
                              items_missing_xem[item.id] = item.imdb_id
                              if tvdb_id:
                                  tvdb_ids_to_fetch_xem[tvdb_id] = item.id
                              else:
                                  logger.warning(f"Cannot fetch XEM mapping for {item.imdb_id}: TVDB ID not found in existing metadata. Will store empty mapping.")
                                  item_ids_to_save_empty_xem.append(item.id)


                      # --- Bulk Fetch XEM Mapping ---\
                      fetched_xem_mappings = {}
                      # ... fetch logic ...
                      if tvdb_ids_to_fetch_xem:
                         tvdb_ids = list(tvdb_ids_to_fetch_xem.keys())
                         logger.info(f"Bulk fetching XEM mappings for {len(tvdb_ids)} TVDB IDs...")
                         for tvdb_id_fetch in tvdb_ids:
                              item_id = tvdb_ids_to_fetch_xem[tvdb_id_fetch]
                              imdb_id_fetch = items_missing_xem[item_id]
                              try:
                                  xem_data = fetch_xem_mapping(tvdb_id_fetch)
                                  fetched_xem_mappings[item_id] = xem_data if xem_data else {}
                                  logger.info(f"{'Found' if xem_data else 'No'} XEM mapping for TVDB {tvdb_id_fetch} (IMDb: {imdb_id_fetch})")
                              except Exception as e:
                                  logger.error(f"Error fetching XEM for TVDB {tvdb_id_fetch} (IMDb: {imdb_id_fetch}): {e}")
                                  fetched_xem_mappings[item_id] = {}


                      # --- Prepare and Save XEM Mappings ---
                      metadata_to_save = []
                      # ... prepare metadata_to_save list ...
                      for item_id, imdb_id_save in items_missing_xem.items():
                          xem_data_to_add = fetched_xem_mappings.get(item_id, {})
                          if metadata_map.get(imdb_id_save):
                              metadata_map[imdb_id_save]['xem_mapping'] = xem_data_to_add
                          from metadata.metadata import _get_local_timezone
                          current_time = datetime.now(_get_local_timezone())
                          # Check if xem mapping already exists for this item_id before creating a new Metadata object
                          existing_xem = local_session.query(Metadata).filter_by(item_id=item_id, key='xem_mapping').first()
                          if existing_xem:
                             existing_xem.value = json.dumps(xem_data_to_add)
                             existing_xem.last_updated = current_time
                             metadata_to_save.append(existing_xem) # Add existing object to merge
                          else:
                             metadata_to_save.append(
                                Metadata(
                                    item_id=item_id, key='xem_mapping',
                                    value=json.dumps(xem_data_to_add),
                                    provider='xem', last_updated=current_time
                                )
                            )


                      if metadata_to_save:
                          logger.info(f"Saving/Updating {len(metadata_to_save)} XEM mappings within local session.")
                          for xem_meta in metadata_to_save:
                              local_session.merge(xem_meta) # Use merge for upsert based on PK
                          # Commit happens at end of 'with' block

                      # ... log not found ...
                      found_ids = {item.imdb_id for item in items}
                      not_found_ids = set(imdb_ids) - found_ids
                      if not_found_ids:
                          logger.info(f"Did not find show metadata in battery for IMDb IDs: {list(not_found_ids)}")
                      return metadata_map
        except Exception as e:
            logger.error(f"Error in get_bulk_show_metadata: {e}", exc_info=True)
            if session: raise
            return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def force_refresh_item_metadata(imdb_id: str, session: Optional[SqlAlchemySession] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        logger.info(f"Force refreshing metadata for IMDb ID: {imdb_id}. Session provided: {session is not None}")
        session_context = session if session else DbSession()
        try:
            item_type = None
            refreshed_data = None
            source = None

            # Determine item type using the session context
            def get_type(sess):
                 item = sess.query(Item.type).filter_by(imdb_id=imdb_id).first()
                 return item.type if item else None

            if session: # Use provided session
                 item_type = get_type(session_context)
            else: # Use local session context
                 with session_context as local_session:
                      item_type = get_type(local_session)
                      # Don't keep local session open longer than needed

            logger.info(f"Determined item type for force refresh: {item_type}")

            # Perform refresh using the appropriate context (pass session if provided)
            if item_type == 'movie':
                logger.info(f"Item {imdb_id} identified as movie. Calling refresh_movie_metadata (passing session: {session is not None}).")
                # refresh_movie_metadata expects a session, caller handles commit/rollback if session provided
                if session:
                    # Pass the originally provided session
                    refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=session)
                else:
                    # Need a local session context for the refresh call
                    with DbSession() as refresh_session:
                         refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=refresh_session)
                         if refreshed_data: refresh_session.commit() # Commit local refresh

            elif item_type == 'show':
                logger.info(f"Item {imdb_id} identified as show. Calling refresh_metadata (passing session: {session is not None}).")
                # refresh_metadata handles passing session down and commit/rollback logic
                refreshed_data = MetadataManager.refresh_metadata(imdb_id, session=session)
                source = "trakt" if refreshed_data else None

            else: # Type unknown or item not found
                logger.warning(f"Item type for {imdb_id} unknown or item not found. Attempting refresh as show first.")
                # Pass session if provided, refresh_metadata handles commit/rollback
                refreshed_data = MetadataManager.refresh_metadata(imdb_id, session=session)
                if refreshed_data:
                    source = "trakt"
                    logger.info(f"Successfully refreshed {imdb_id} as show.")
                else:
                    logger.warning(f"Refreshing {imdb_id} as show failed. Attempting refresh as movie.")
                    if session:
                         refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=session)
                    else:
                         with DbSession() as refresh_session:
                              refreshed_data, source = MetadataManager.refresh_movie_metadata(imdb_id, session=refresh_session)
                              if refreshed_data: refresh_session.commit() # Commit local refresh

                    if refreshed_data:
                        logger.info(f"Successfully refreshed {imdb_id} as movie.")
                    else:
                        logger.error(f"Failed to refresh {imdb_id} as either show or movie.")
                        source = None # Ensure source is None on failure

            return refreshed_data, source

        except Exception as e:
             logger.error(f"Error during force_refresh_item_metadata for {imdb_id}: {e}", exc_info=True)
             if session: raise # Let managed_session handle rollback
             return None, None # Return default on error with local session

    @staticmethod
    def find_best_match_from_results(
        original_query_title: str,
        query_year: Optional[int],
        search_results: List[Dict[str, Any]],
        year_match_boost: int = 30, # How much to boost score for perfect year match
        min_score_threshold: int = 70 # Minimum combined score to be considered a good match
    ) -> Optional[Dict[str, Any]]:
        """
        Finds the best match from a list of search results based on title and year similarity.

        Args:
            original_query_title: The title used for the search (can contain dots).
            query_year: The year used for the search.
            search_results: A list of dictionaries, where each dict is a search result.
            year_match_boost: Bonus points added to the score if the year matches perfectly.
            min_score_threshold: The minimum score a result must achieve to be considered a confident match.

        Returns:
            The best matching search result dictionary, or None if no confident match is found.
        """
        if not search_results:
            logger.debug("find_best_match_from_results: No search results provided.")
            return None

        # Clean the original query title for fuzzy matching (dots to spaces, lower)
        cleaned_query_title_for_fuzz = original_query_title.replace('.', ' ').lower().strip() if original_query_title else ''
        
        best_match_candidate = None
        highest_score = -1

        logger.debug(f"find_best_match_from_results: Processing {len(search_results)} results for query '{original_query_title}' ({query_year})")

        for result_idx, result_item in enumerate(search_results):
            result_title_original = result_item.get('title', '')
            result_year = result_item.get('year') # Assumed to be int or None

            if not result_title_original: # Skip results with no title
                logger.debug(f"  Result {result_idx}: Skipping, no title. Data: {result_item}")
                continue
            
            result_title_lower = result_title_original.lower()

            # 1. Title Similarity Score (0-100)
            # WRatio is good for matching titles that might have extra words or different orderings.
            title_similarity_score = fuzz.WRatio(cleaned_query_title_for_fuzz, result_title_lower)

            # 2. Year Matching Score/Bonus
            year_bonus = 0
            if query_year is not None and result_year is not None:
                if query_year == result_year:
                    year_bonus = year_match_boost # Significant bonus for exact year match
            
            # Current combined score primarily based on title, boosted by year match
            current_combined_score = title_similarity_score + year_bonus
            
            logger.debug(f"  Result {result_idx}: '{result_title_original}' ({result_year}) | Cleaned Query: '{cleaned_query_title_for_fuzz}' ({query_year})")
            logger.debug(f"    TitleSim: {title_similarity_score}, YearBonus: {year_bonus}, CombinedScore: {current_combined_score}")

            if current_combined_score > highest_score:
                highest_score = current_combined_score
                best_match_candidate = result_item
        
        if best_match_candidate:
            logger.info(f"Best candidate for '{original_query_title}' ({query_year}) is '{best_match_candidate.get('title')}' ({best_match_candidate.get('year')}) with pre-threshold score {highest_score}.")
            if highest_score >= min_score_threshold:
                logger.info(f"  Score {highest_score} >= threshold {min_score_threshold}. Selecting as best match.")
                return best_match_candidate
            else:
                logger.warning(f"  Best candidate's score {highest_score} is below threshold {min_score_threshold}. No confident match.")
                # Log what would have been picked by the old logic for comparison
                if search_results:
                    logger.debug(f"    (Old logic would have picked: '{search_results[0].get('title')}')")
                return None # Strict: if no match meets threshold, return None.
        else:
            logger.warning(f"No suitable match candidate identified after scoring for '{original_query_title}' ({query_year}).")
            return None

    @staticmethod
    def force_refresh_tmdb_mapping(tmdb_id: str, media_type: str = None, session: Optional[SqlAlchemySession] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Force refresh a TMDB to IMDB mapping, regardless of staleness.
        Useful for manually correcting incorrect mappings.
        
        Args:
            tmdb_id: The TMDB ID to refresh
            media_type: The media type ('movie' or 'show')
            session: Optional database session
            
        Returns:
            Tuple of (imdb_id, source) or (None, None) if failed
        """
        session_context = session if session else DbSession()
        try:
            if session: # Use provided session
                # Delete existing mapping if it exists
                existing_mapping = session_context.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                if existing_mapping:
                    logger.info(f"Deleting existing TMDB mapping for {tmdb_id} to force refresh")
                    session_context.delete(existing_mapping)
                    session_context.flush()

                trakt = TraktMetadata()
                imdb_id, source = trakt.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)

                if imdb_id:
                    new_mapping = TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id)
                    session_context.add(new_mapping)
                    # ** NO COMMIT HERE **
                else:
                    logger.warning(f"No IMDB ID found for TMDB ID {tmdb_id} with type {media_type}")
                return imdb_id, source
            else: # Create local session
                 with session_context as local_session:
                    # Delete existing mapping if it exists
                    existing_mapping = local_session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                    if existing_mapping:
                        logger.info(f"Deleting existing TMDB mapping for {tmdb_id} to force refresh")
                        local_session.delete(existing_mapping)
                        local_session.flush()

                    trakt = TraktMetadata()
                    imdb_id, source = trakt.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)

                    if imdb_id:
                        new_mapping = TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id)
                        local_session.add(new_mapping)
                        local_session.commit() # Commit local transaction
                    else:
                        logger.warning(f"No IMDB ID found for TMDB ID {tmdb_id} with type {media_type}")
                    return imdb_id, source
        except Exception as e:
            logger.error(f"Error in force_refresh_tmdb_mapping for {tmdb_id}: {e}", exc_info=True)
            if session: raise
            return None, None
