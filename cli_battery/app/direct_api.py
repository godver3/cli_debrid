from .metadata_manager import MetadataManager
from typing import Dict, Any, Tuple, Optional
from .logger_config import logger

class DirectAPI:

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
        metadata, source = MetadataManager.get_show_metadata(imdb_id)
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