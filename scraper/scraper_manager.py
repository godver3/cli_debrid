import logging
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from .nyaa import scrape_nyaa
from .jackett import scrape_jackett_instance
from .comet import scrape_comet_instance
from .prowlarr import scrape_prowlarr_instance
from .torrentio import scrape_torrentio_instance
from .zilean import scrape_zilean_instance
from settings import get_setting

class ScraperManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.scrapers = {
            'Jackett': scrape_jackett_instance,
            'Comet': scrape_comet_instance,
            'Prowlarr': scrape_prowlarr_instance,
            'Torrentio': scrape_torrentio_instance,
            'Zilean': scrape_zilean_instance,
            'Nyaa': scrape_nyaa
        }

    def get_scraper_settings(self, scraper_type):
        # Fetch all scraper settings
        all_scrapers = get_setting('Scrapers')
        
        # First try direct lookup
        if scraper_type in all_scrapers:
            return all_scrapers[scraper_type]
            
        # If not found directly, look for instances of the given type
        for instance, settings in all_scrapers.items():
            if isinstance(settings, dict) and settings.get('type') == scraper_type:
                logging.info(f"Found {scraper_type} settings in instance {instance}")
                return settings
                
        logging.warning(f"No settings found for scraper type: {scraper_type}")
        return {}

    def scrape_all(
        self,
        imdb_id: str,
        title: str,
        year: int,
        content_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        multi: bool = False,
        genres: List[str] = None,
        episode_formats: Optional[Dict[str, str]] = None,
        tmdb_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Scrape all configured sources for content.
        
        Args:
            imdb_id: IMDb ID of the content
            title: Title of the content
            year: Release year
            content_type: Type of content ('movie' or 'episode')
            season: Season number for episodes
            episode: Episode number
            multi: Whether to search for multiple episodes
            genres: List of genres
            episode_formats: Dictionary of episode format patterns for anime
            tmdb_id: TMDB ID of the content
        """
        all_results = []
        is_anime = genres and 'anime' in [genre.lower() for genre in genres]
        
        # For anime content, try Nyaa first if enabled
        if is_anime:
            nyaa_settings = self.get_scraper_settings('Nyaa')
            nyaa_enabled = nyaa_settings.get('enabled', False) if nyaa_settings else False
            
            if nyaa_enabled:
                logging.info(f"Using Nyaa for anime content: {title}")
                try:
                    results = self.scrapers['Nyaa'](
                        title=title,
                        year=year,
                        content_type=content_type,
                        season=season,
                        episode=episode,
                        episode_formats=episode_formats if content_type.lower() == 'episode' else None,
                        tmdb_id=tmdb_id
                    )
                    if results:
                        logging.info(f"Found {len(results)} results from Nyaa")
                        all_results.extend(results)
                except Exception as e:
                    logging.error(f"Error scraping with Nyaa: {str(e)}")

        # Proceed with all enabled scrapers
        for instance, settings in self.config.get('Scrapers', {}).items():
            # Get the latest settings for this instance
            current_settings = self.get_scraper_settings(instance)
            
            if not current_settings.get('enabled', False):
                continue
            
            scraper_type = current_settings.get('type')
            if scraper_type not in self.scrapers:
                logging.warning(f"Unknown scraper type '{scraper_type}' for instance '{instance}'. Skipping.")
                continue

            # Skip Nyaa for non-anime content
            if scraper_type == 'Nyaa' and not is_anime:
                continue

            scrape_func = self.scrapers[scraper_type]
            try:
                if scraper_type == 'Nyaa':
                    # Nyaa has a different function signature
                    results = scrape_func(
                        title=title,
                        year=year,
                        content_type=content_type,
                        season=season,
                        episode=episode,
                        episode_formats=episode_formats if is_anime and content_type.lower() == 'episode' else None,
                        tmdb_id=tmdb_id
                    )
                else:
                    # Other scrapers use the original signature
                    results = scrape_func(instance, current_settings, imdb_id, title, year, content_type, season, episode, multi)
                
                if results:
                    logging.info(f"Found {len(results)} results from {instance}")
                    all_results.extend(results)
            except Exception as e:
                logging.error(f"Error scraping {scraper_type} instance '{instance}': {str(e)}")

        return all_results