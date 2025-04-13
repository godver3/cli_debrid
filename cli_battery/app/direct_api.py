from .metadata_manager import MetadataManager
from typing import Dict, Any, Tuple, Optional
from .logger_config import logger
from .database import init_db, Session as DbSession

class DirectAPI:
    def __init__(self):
        # Initialize database engine and configure session
        engine = init_db()
        DbSession.configure(bind=engine)

    @staticmethod
    def get_movie_metadata(imdb_id: str) -> Tuple[Dict[str, Any], str]:
        metadata, source = MetadataManager.get_movie_metadata(imdb_id)
        return metadata, source

    @staticmethod
    def get_movie_release_dates(imdb_id: str):
        release_dates, source = MetadataManager.get_release_dates(imdb_id)
        return release_dates, source

    @staticmethod
    def get_episode_metadata(imdb_id):
        metadata, source = MetadataManager.get_metadata_by_episode_imdb(imdb_id)
        return metadata, source

    @staticmethod
    def get_show_metadata(imdb_id):
        import logging
        logging.info(f"DirectAPI.get_show_metadata called for {imdb_id}")
        metadata, source = MetadataManager.get_show_metadata(imdb_id)
        if metadata and 'seasons' in metadata:
            logging.info(f"DirectAPI got {len(metadata['seasons'])} seasons")
            #for season_num in metadata['seasons'].keys():
                #logging.info(f"Season {season_num} has {len(metadata['seasons'][season_num].get('episodes', {}))} episodes")
        return metadata, source

    @staticmethod
    def get_show_seasons(imdb_id: str) -> Tuple[Dict[str, Any], str]:
        seasons, source = MetadataManager.get_seasons(imdb_id)
        return seasons, source

    @staticmethod
    def tmdb_to_imdb(tmdb_id: str, media_type: str = None) -> Optional[str]:
        """
        Convert TMDB ID to IMDB ID
        Args:
            tmdb_id: The TMDB ID to convert
            media_type: Either 'movie' or 'show' to specify what type of content to look for
        """
        imdb_id, source = MetadataManager.tmdb_to_imdb(tmdb_id, media_type=media_type)
        return imdb_id, source

    @staticmethod
    def get_show_aliases(imdb_id: str):
        """Get all aliases for a show by IMDb ID"""
        aliases, source = MetadataManager.get_show_aliases(imdb_id)
        return aliases, source

    @staticmethod
    def get_movie_aliases(imdb_id: str):
        """Get all aliases for a movie by IMDb ID"""
        aliases, source = MetadataManager.get_movie_aliases(imdb_id)
        return aliases, source

    @staticmethod
    def get_movie_title_translation(imdb_id: str, language_code: str) -> Tuple[Optional[str], str]:
        """
        Get the translated title for a movie in a specific language.

        Args:
            imdb_id: The IMDb ID of the movie.
            language_code: The language code (e.g., 'es', 'fr', 'th').

        Returns:
            A tuple containing the translated title (str) if found, otherwise None,
            and the source of the metadata (str).
        """
        metadata, source = DirectAPI.get_movie_metadata(imdb_id)
        translated_title = None

        if metadata and 'aliases' in metadata:
            aliases = metadata['aliases']
            if language_code in aliases:
                # The alias value seems to be a list, get the first element
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


        return translated_title, source

    @staticmethod
    def get_show_title_translation(imdb_id: str, language_code: str) -> Tuple[Optional[str], str]:
        """
        Get the translated title for a TV show in a specific language.

        Args:
            imdb_id: The IMDb ID of the show.
            language_code: The language code (e.g., 'es', 'fr').

        Returns:
            A tuple containing the translated title (str) if found, otherwise None,
            and the source of the metadata (str).
        """
        metadata, source = DirectAPI.get_show_metadata(imdb_id)
        translated_title = None

        # Assuming show metadata structure includes 'aliases' similar to movies
        if metadata and 'aliases' in metadata:
            aliases = metadata['aliases']
            if language_code in aliases:
                 # Assuming the alias value is a list, get the first element
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


        return translated_title, source

    @staticmethod
    def get_bulk_show_airs(imdb_ids: list[str]) -> dict[str, Optional[dict[str, Any]]]:
        """Gets the 'airs' metadata dictionary for multiple shows from the battery."""
        logger.info(f"DirectAPI.get_bulk_show_airs called for {len(imdb_ids)} IDs.")
        result = MetadataManager.get_bulk_show_airs_info(imdb_ids)
        found_count = sum(1 for airs in result.values() if airs is not None)
        logger.info(f"DirectAPI.get_bulk_show_airs returning airs info for {found_count} of {len(imdb_ids)} requested IDs.")
        return result