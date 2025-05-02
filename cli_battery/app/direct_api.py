from .metadata_manager import MetadataManager
from typing import Dict, Any, Tuple, Optional, List
from .logger_config import logger
from .database import init_db, Session as DbSession
from contextlib import contextmanager
import logging
from sqlalchemy.orm import Session as SqlAlchemySession

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
        try:
            with managed_session() as session:
                imdb_id, source = MetadataManager.tmdb_to_imdb(tmdb_id, media_type=media_type, session=session)
                return imdb_id, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.tmdb_to_imdb for {tmdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_aliases(imdb_id: str):
        try:
            with managed_session() as session:
                aliases, source = MetadataManager.get_show_aliases(imdb_id, session=session)
                return aliases, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_show_aliases for {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_movie_aliases(imdb_id: str):
        try:
            with managed_session() as session:
                aliases, source = MetadataManager.get_movie_aliases(imdb_id, session=session)
                return aliases, source
        except Exception as e:
            logging.error(f"Error during DirectAPI.get_movie_aliases for {imdb_id}: {e}", exc_info=True)
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