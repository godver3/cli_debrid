from .metadata_manager import MetadataManager
from typing import Dict, Any, Tuple, Optional, List
from .logger_config import logger
from .database import init_db, Session as DbSession
from contextlib import contextmanager
import logging
from sqlalchemy.orm import Session as SqlAlchemySession
from .trakt_metadata import TraktMetadata
from functools import lru_cache

# Aliases & caching utilities
from typing import Tuple as _Tup, Optional as _Opt, Dict as _Dict, Any as _Any

_MOVIE_ALIAS_CACHE: _Dict[str, _Tup[_Opt[_Dict[str, _Any]], _Opt[str]]] = {}
_SHOW_ALIAS_CACHE: _Dict[str, _Tup[_Opt[_Dict[str, _Any]], _Opt[str]]] = {}

@contextmanager
def managed_session():
    """Provide a transactional scope around a series of operations."""
    from .database import Session as GlobalDbSession
    session = GlobalDbSession()
    logger.debug("Managed session created.")
    try:
        yield session
        logger.debug("Managed session logic completed without errors, attempting commit.")
        session.commit()
        logger.info("Managed session committed successfully.")
    except Exception as e:
        logger.error(f"Managed session encountered error, rolling back: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        logger.debug("Closing managed session.")
        GlobalDbSession.remove()

class DirectAPI:
    def __init__(self):
        # Initialize database engine ONLY. Session is configured in init_db.
        engine = init_db()
        # Ensure engine is initialized
        if engine is None:
             raise RuntimeError("Database engine failed to initialize in DirectAPI.")
        logger.info("DirectAPI initialized, database engine ready.")

    @staticmethod
    @lru_cache()
    def get_movie_metadata(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            with managed_session() as session:
                metadata, source = MetadataManager.get_movie_metadata(imdb_id, session=session)
                return metadata, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_movie_metadata for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_movie_release_dates(imdb_id: str):
        try:
            with managed_session() as session:
                release_dates, source = MetadataManager.get_release_dates(imdb_id, session=session)
                return release_dates, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_movie_release_dates for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    @lru_cache()
    def get_show_metadata(imdb_id):
        logging.info(f"DirectAPI.get_show_metadata called for {imdb_id}")
        try:
            with managed_session() as session:
                metadata, source = MetadataManager.get_show_metadata(imdb_id, session=session)
                if metadata and 'seasons' in metadata:
                    season_count = len(metadata['seasons'])
                    logging.info(f"DirectAPI got {season_count} seasons (within managed session scope)")
                else:
                    status = "No metadata" if not metadata else "No seasons dictionary" if 'seasons' not in metadata else f"{len(metadata.get('seasons', {}))} seasons"
                    logging.info(f"DirectAPI: Status for {imdb_id}: {status} (within managed session scope)")
                return metadata, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_show_metadata for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_seasons(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            with managed_session() as session:
                seasons, source = MetadataManager.get_seasons(imdb_id, session=session)
                return seasons, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_show_seasons for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def tmdb_to_imdb(tmdb_id: str, media_type: str = None) -> Optional[str]:
        """
        Convert TMDB ID to IMDB ID with comprehensive fallback system.
        
        Fallback layers:
        1. Primary: Trakt TMDB-to-IMDB API (via MetadataManager)
        2. Fallback 1: TMDB External IDs API (most authoritative)
        3. Fallback 2: Trakt title search
        4. Fallback 3: TVDB-to-IMDB conversion (if TVDB ID available)
        """
        logger.info(f"DirectAPI.tmdb_to_imdb starting conversion for TMDB ID {tmdb_id} with media_type: {media_type}")
        
        try:
            # Primary method: Use existing MetadataManager (Trakt-based)
            with managed_session() as session:
                imdb_id, source = MetadataManager.tmdb_to_imdb(tmdb_id, media_type=media_type, session=session)
                if imdb_id:
                    logger.info(f"DirectAPI.tmdb_to_imdb: Primary method succeeded for {tmdb_id} -> {imdb_id}")
                    return imdb_id, source
                    
            logger.warning(f"DirectAPI.tmdb_to_imdb: Primary method failed for {tmdb_id}, trying fallbacks...")
            
            # Fallback 1: TMDB External IDs API (most reliable since TMDB is authoritative)
            try:
                from utilities.settings import get_setting
                import requests
                
                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    logger.info(f"DirectAPI.tmdb_to_imdb: Trying TMDB External IDs API for {tmdb_id}")
                    
                    # Determine endpoint based on media type
                    if media_type == 'movie':
                        tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
                    else:  # Default to TV for 'show', 'tv', or None
                        tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
                    
                    tmdb_response = requests.get(tmdb_url, timeout=10)
                    if tmdb_response.status_code == 200:
                        tmdb_data = tmdb_response.json()
                        tmdb_imdb_id = tmdb_data.get('imdb_id')
                        tvdb_id = tmdb_data.get('tvdb_id')  # Save for potential fallback
                        
                        if tmdb_imdb_id:
                            logger.info(f"DirectAPI.tmdb_to_imdb: TMDB External IDs success for {tmdb_id} -> {tmdb_imdb_id}")
                            
                            # Cache the successful mapping for future use
                            try:
                                with managed_session() as cache_session:
                                    from .metadata_manager import TMDBToIMDBMapping
                                    new_mapping = TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=tmdb_imdb_id)
                                    cache_session.add(new_mapping)
                                    # Session will commit automatically due to managed_session context
                                    logger.info(f"DirectAPI.tmdb_to_imdb: Cached TMDB mapping {tmdb_id} -> {tmdb_imdb_id}")
                            except Exception as cache_error:
                                logger.warning(f"DirectAPI.tmdb_to_imdb: Failed to cache mapping: {cache_error}")
                            
                            return tmdb_imdb_id, 'tmdb_external_ids'
                    else:
                        logger.warning(f"DirectAPI.tmdb_to_imdb: TMDB External IDs API failed with status {tmdb_response.status_code}")
                        
            except Exception as tmdb_error:
                logger.warning(f"DirectAPI.tmdb_to_imdb: TMDB External IDs fallback failed: {tmdb_error}")
            
            # Fallback 2: Trakt Title Search
            try:
                logger.info(f"DirectAPI.tmdb_to_imdb: Trying Trakt title search for {tmdb_id}")
                
                # First get title from TMDB to search with
                from utilities.settings import get_setting
                import requests
                
                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    # Get TMDB metadata for title
                    if media_type == 'movie':
                        tmdb_details_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    else:
                        tmdb_details_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    
                    tmdb_details_response = requests.get(tmdb_details_url, timeout=10)
                    if tmdb_details_response.status_code == 200:
                        tmdb_details = tmdb_details_response.json()
                        
                        if media_type == 'movie':
                            show_title = tmdb_details.get('title')
                            release_date = tmdb_details.get('release_date')
                            show_year = int(release_date[:4]) if release_date else None
                        else:
                            show_title = tmdb_details.get('name')  # TV shows use 'name' not 'title'
                            first_air_date = tmdb_details.get('first_air_date')
                            show_year = int(first_air_date[:4]) if first_air_date else None
                        
                        if show_title:
                            logger.info(f"DirectAPI.tmdb_to_imdb: Searching Trakt for '{show_title}' ({show_year})")
                            
                            # Search Trakt by title
                            trakt = TraktMetadata()
                            search_media_type = 'show' if media_type in ['tv', 'show'] else 'movie'
                            search_results = trakt.search_media(show_title, year=show_year, media_type=search_media_type)
                            
                            if search_results:
                                # Look for exact TMDB ID match first
                                for result in search_results:
                                    if result.get('imdb_id') and result.get('tmdb_id') == int(tmdb_id):
                                        logger.info(f"DirectAPI.tmdb_to_imdb: Found exact TMDB match via Trakt search: {result['imdb_id']}")
                                        return result['imdb_id'], 'trakt_title_search'
                                
                                # If no exact match, try first result with IMDB ID
                                for result in search_results:
                                    if result.get('imdb_id'):
                                        logger.info(f"DirectAPI.tmdb_to_imdb: Using first IMDB ID from Trakt search: {result['imdb_id']} (no exact TMDB match)")
                                        return result['imdb_id'], 'trakt_title_search_fallback'
                            
                            logger.warning(f"DirectAPI.tmdb_to_imdb: Trakt title search for '{show_title}' returned no usable results")
                        else:
                            logger.warning(f"DirectAPI.tmdb_to_imdb: Could not get title from TMDB details for {tmdb_id}")
                    else:
                        logger.warning(f"DirectAPI.tmdb_to_imdb: TMDB details API failed with status {tmdb_details_response.status_code}")
                        
            except Exception as trakt_search_error:
                logger.warning(f"DirectAPI.tmdb_to_imdb: Trakt title search fallback failed: {trakt_search_error}")
            
            # Fallback 3: TVDB-to-IMDB conversion (if we got TVDB ID from TMDB External IDs)
            if 'tvdb_id' in locals() and tvdb_id:
                try:
                    logger.info(f"DirectAPI.tmdb_to_imdb: Trying TVDB-to-IMDB conversion for TVDB ID {tvdb_id}")
                    
                    trakt = TraktMetadata()
                    tvdb_search_url = f"{trakt.base_url}/search/tvdb/{tvdb_id}?type=show"
                    response = trakt._make_request(tvdb_search_url)
                    
                    if response and response.status_code == 200:
                        tvdb_results = response.json()
                        if tvdb_results:
                            show = tvdb_results[0]['show']
                            tvdb_imdb_id = show['ids'].get('imdb')
                            if tvdb_imdb_id:
                                logger.info(f"DirectAPI.tmdb_to_imdb: TVDB-to-IMDB conversion success: {tvdb_imdb_id}")
                                return tvdb_imdb_id, 'tvdb_conversion'
                    
                    logger.warning(f"DirectAPI.tmdb_to_imdb: TVDB-to-IMDB conversion failed for TVDB ID {tvdb_id}")
                    
                except Exception as tvdb_error:
                    logger.warning(f"DirectAPI.tmdb_to_imdb: TVDB-to-IMDB fallback failed: {tvdb_error}")
            
            # All fallbacks exhausted
            logger.error(f"DirectAPI.tmdb_to_imdb: All conversion methods failed for TMDB ID {tmdb_id}")
            return None, None
            
        except Exception as e:
            logger.error(f"Error during DirectAPI.tmdb_to_imdb for {tmdb_id}: {e}", exc_info=True)
            return None, None

    # ----------------------------------------------------------------------
    # Alias fetching with caching
    # ----------------------------------------------------------------------

    @staticmethod
    def get_show_aliases(imdb_id: str):
        """Return aliases for a TV show, using an in-memory cache to avoid
        repeated database hits in tight loops (e.g., filter_results)."""

        if imdb_id in _SHOW_ALIAS_CACHE:
            return _SHOW_ALIAS_CACHE[imdb_id]

        try:
            with managed_session() as session:
                aliases, source = MetadataManager.get_show_aliases(imdb_id, session=session)
                # Cache even if aliases is None so we don't hammer DB on bad IDs.
                _SHOW_ALIAS_CACHE[imdb_id] = (aliases, source)
                return aliases, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_show_aliases for {imdb_id}: {e}", exc_info=True)
            # Cache the failure to avoid repeated exceptions
            _SHOW_ALIAS_CACHE[imdb_id] = (None, None)
            return None, None

    @staticmethod
    def get_movie_aliases(imdb_id: str):
        """Return aliases for a movie, using an in-memory cache to avoid
        repeated database hits."""

        if imdb_id in _MOVIE_ALIAS_CACHE:
            return _MOVIE_ALIAS_CACHE[imdb_id]

        try:
            with managed_session() as session:
                aliases, source = MetadataManager.get_movie_aliases(imdb_id, session=session)
                _MOVIE_ALIAS_CACHE[imdb_id] = (aliases, source)
                return aliases, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_movie_aliases for {imdb_id}: {e}", exc_info=True)
            _MOVIE_ALIAS_CACHE[imdb_id] = (None, None)
            return None, None

    @staticmethod
    def get_movie_title_translation(imdb_id: str, language_code: str) -> Tuple[Optional[str], str]:
        try:
            metadata, source = DirectAPI.get_movie_metadata(imdb_id)
            translated_title = None

            if metadata and 'aliases' in metadata:
                aliases = metadata['aliases']
                if language_code in aliases:
                    if aliases[language_code]:
                        translated_title = aliases[language_code][0]
                    else:
                        logger.warning(f"Found language code '{language_code}' for movie {imdb_id}, but the alias list was empty.")
                else:
                    logger.info(f"Language code '{language_code}' not found in aliases for movie {imdb_id}.")
            elif metadata:
                logger.info(f"No 'aliases' key found in metadata for movie {imdb_id}.")
            else:
                logger.info(f"No metadata retrieved for movie {imdb_id}.")

            return translated_title, source if metadata else None
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_movie_title_translation for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_title_translation(imdb_id: str, language_code: str) -> Tuple[Optional[str], str]:
        try:
            metadata, source = DirectAPI.get_show_metadata(imdb_id)
            translated_title = None

            if metadata and 'aliases' in metadata:
                aliases = metadata['aliases']
                if language_code in aliases:
                    if aliases[language_code]:
                        translated_title = aliases[language_code][0]
                    else:
                        logger.warning(f"Found language code '{language_code}' for show {imdb_id}, but the alias list was empty.")
                else:
                    logger.info(f"Language code '{language_code}' not found in aliases for show {imdb_id}.")
            elif metadata:
                logger.info(f"No 'aliases' key found in metadata for show {imdb_id}.")
            else:
                logger.info(f"No metadata retrieved for show {imdb_id}.")

            return translated_title, source if metadata else None
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_show_title_translation for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_bulk_show_airs(imdb_ids: list[str]) -> dict[str, Optional[dict[str, Any]]]:
        logger.info(f"DirectAPI.get_bulk_show_airs called for {len(imdb_ids)} IDs.")
        try:
            with managed_session() as session:
                result = MetadataManager.get_bulk_show_airs_info(imdb_ids, session=session)
                found_count = sum(1 for airs in result.values() if airs is not None)
                logger.info(f"DirectAPI.get_bulk_show_airs returning airs info for {found_count} of {len(imdb_ids)} requested IDs.")
                return result
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_bulk_show_airs: {e}", exc_info=True)
            return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def get_bulk_movie_metadata(imdb_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        logger.info(f"DirectAPI.get_bulk_movie_metadata called for {len(imdb_ids)} movie IDs.")
        try:
            with managed_session() as session:
                result = MetadataManager.get_bulk_movie_metadata(imdb_ids, session=session)
                found_count = sum(1 for data in result.values() if data is not None)
                logger.info(f"DirectAPI.get_bulk_movie_metadata returning data for {found_count} of {len(imdb_ids)} requested IDs.")
                return result
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_bulk_movie_metadata: {e}", exc_info=True)
            return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def get_bulk_show_metadata(imdb_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        logger.info(f"DirectAPI.get_bulk_show_metadata called for {len(imdb_ids)} show IDs.")
        try:
            with managed_session() as session:
                result = MetadataManager.get_bulk_show_metadata(imdb_ids, session=session)
                found_count = sum(1 for data in result.values() if data is not None)
                logger.info(f"DirectAPI.get_bulk_show_metadata returning data for {found_count} of {len(imdb_ids)} requested IDs.")
                return result
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_bulk_show_metadata: {e}", exc_info=True)
            return {imdb_id: None for imdb_id in imdb_ids}

    @staticmethod
    def force_refresh_metadata(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        logger.info(f"DirectAPI.force_refresh_metadata called for {imdb_id}")
        try:
            with managed_session() as session:
                refreshed_data, source = MetadataManager.force_refresh_item_metadata(imdb_id, session=session)
                if refreshed_data:
                    logger.info(f"DirectAPI received refreshed data for {imdb_id} from source: {source}")
                else:
                    logger.warning(f"DirectAPI: Force refresh failed for {imdb_id}")
                return refreshed_data, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.force_refresh_metadata for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    @lru_cache()
    def search_media(query: str, year: Optional[int] = None, media_type: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """
        Search for media using Trakt. Caches results in memory.
        Args:
            query: The search query (title).
            year: Optional year to filter by.
            media_type: Optional type ('movie' or 'show').
        Returns:
            A tuple containing:
                - A list of search result dictionaries (or None on error).
                - The source ('trakt' or None).
        """
        logger.info(f"DirectAPI.search_media called: query='{query}', year={year}, type={media_type}")
        try:
            # Instantiate TraktMetadata to use its search method
            trakt_api = TraktMetadata()
            results = trakt_api.search_media(query=query, year=year, media_type=media_type)
            # Search always comes from Trakt if successful
            source = 'trakt' if results is not None else None
            return results, source
        except Exception as e:
            logger.error(f"Error during DirectAPI.search_media for query '{query}': {e}", exc_info=True)
            return None, None