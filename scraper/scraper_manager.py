import logging
from typing import List, Dict, Any
from .jackett import scrape_jackett_instance
from .comet import scrape_comet_instance
from .prowlarr import scrape_prowlarr_instance
from .torrentio import scrape_torrentio_instance
from .zilean import scrape_zilean_instance
from .nyaa import scrape_nyaa_instance

class ScraperManager:
    def __init__(self, config):
        self.config = config
        self.scrapers = {
            'Jackett': scrape_jackett_instance,
            'Comet': scrape_comet_instance,
            'Prowlarr': scrape_prowlarr_instance,
            'Torrentio': scrape_torrentio_instance,
            'Zilean': scrape_zilean_instance,
            'Nyaa': scrape_nyaa_instance
        }

    def scrape_all(self, imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None) -> List[Dict[str, Any]]:
        all_results = []
        is_anime = genres and 'anime' in [genre.lower() for genre in genres]

        for instance, settings in self.config.get('Scrapers', {}).items():
            if not settings.get('enabled', False):
                logging.info(f"Scraper {instance} is disabled. Skipping.")
                continue
            
            scraper_type = settings.get('type')
            if scraper_type not in self.scrapers:
                logging.warning(f"Unknown scraper type '{scraper_type}' for instance '{instance}'. Skipping.")
                continue
            
            # Skip Nyaa if the content is not anime
            if scraper_type == 'Nyaa' and not is_anime:
                logging.info(f"Skipping Nyaa scraper for non-anime content: {title}")
                continue

            scrape_func = self.scrapers[scraper_type]
            try:
                logging.info(f"Scraping with {instance} ({scraper_type})")
                results = scrape_func(instance, settings, imdb_id, title, year, content_type, season, episode, multi)
                logging.info(f"Found {len(results)} results from {instance}")
                all_results.extend(results)
            except Exception as e:
                logging.error(f"Error scraping {scraper_type} instance '{instance}': {str(e)}", exc_info=True)

        logging.info(f"Total results from all scrapers: {len(all_results)}")
        return all_results