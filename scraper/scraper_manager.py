import logging
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import concurrent.futures
from .nyaa import scrape_nyaa
from .jackett import scrape_jackett_instance
from .mediafusion import scrape_mediafusion_instance
from .prowlarr import scrape_prowlarr_instance
from .torrentio import scrape_torrentio_instance
from .zilean import scrape_zilean_instance
from .old_nyaa import scrape_nyaa_instance as scrape_old_nyaa_instance
from utilities.settings import get_setting

class ScraperManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.scrapers = {
            'Jackett': scrape_jackett_instance,
            'MediaFusion': scrape_mediafusion_instance,
            'Prowlarr': scrape_prowlarr_instance,
            'Torrentio': scrape_torrentio_instance,
            'Zilean': scrape_zilean_instance,
            'Nyaa': scrape_nyaa,
            'OldNyaa': scrape_old_nyaa_instance
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
        tmdb_id: Optional[str] = None,
        is_translated_search: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Scrape all configured sources for content.
        
        Args:
            imdb_id: IMDb ID of the content (can be None)
            title: Title of the content
            year: Release year
            content_type: Type of content ('movie' or 'episode')
            season: Season number for episodes
            episode: Episode number
            multi: Whether to search for multiple episodes
            genres: List of genres
            episode_formats: Dictionary of episode format patterns for anime
            tmdb_id: TMDB ID of the content
            is_translated_search: Flag indicating if the search title is a translation
        """
        all_results = []
        is_anime = genres and 'anime' in [genre.lower() for genre in genres]
        is_episode = content_type.lower() == 'episode'

        self.scraper_timeout = get_setting('Scraping', 'scraper_timeout', 5)
        self.batch_timeout = get_setting('Scraping', 'scraper_timeout', 5)
        
        # Disable timeouts if scraper_timeout is 0
        self.use_timeout = self.scraper_timeout > 0
        self.scraper_timeout = None if self.scraper_timeout == 0 else self.scraper_timeout
        self.batch_timeout = None if self.batch_timeout == 0 else self.batch_timeout
        
        # Helper function to run a scraper and handle exceptions
        def run_scraper(instance, scraper_type, settings, is_translated):
            try:
                if scraper_type in ['Nyaa', 'OldNyaa']:
                    # Nyaa has a different function signature
                    if scraper_type == 'Nyaa':
                        results = self.scrapers[scraper_type](
                            title=title,
                            year=year,
                            content_type=content_type,
                            season=season,
                            episode=episode,
                            episode_formats=episode_formats if is_anime and content_type.lower() == 'episode' else None,
                            tmdb_id=tmdb_id,
                            multi=multi,
                            is_translated_search=is_translated
                        )
                    else:  # OldNyaa
                        results = self.scrapers[scraper_type](
                            instance=instance,
                            settings=settings,
                            imdb_id=imdb_id,
                            title=title,
                            year=year,
                            content_type=content_type,
                            season=season,
                            episode=episode,
                            multi=multi
                        )
                else:
                    # Only Jackett accepts genres parameter
                    if scraper_type == 'Jackett':
                        results = self.scrapers[scraper_type](instance, settings, imdb_id, title, year, content_type, season, episode, multi, genres, is_translated_search=is_translated)
                    else:
                        results = self.scrapers[scraper_type](instance, settings, imdb_id, title, year, content_type, season, episode, multi)
                
                logging.info(f"Found {len(results)} results from {instance}")
                return instance, results
            except Exception as e:
                logging.error(f"Error scraping {scraper_type} instance '{instance}': {str(e)}")
                return instance, []

        # If no IMDB ID is available, only use Nyaa (for anime) and Jackett scrapers
        if not imdb_id:
            logging.info("No IMDB ID available - limiting scrapers to Nyaa (if anime) and Jackett")
            
            # For anime episodes, try Nyaa first
            if is_anime and is_episode:
                nyaa_settings = self.get_scraper_settings('Nyaa')
                nyaa_enabled = nyaa_settings.get('enabled', False) if nyaa_settings else False
                
                if nyaa_enabled:
                    logging.info(f"Trying Nyaa for anime episode without IMDB ID: {title}")
                    instance, results = run_scraper('Nyaa', 'Nyaa', nyaa_settings, is_translated_search)
                    if results:
                        all_results.extend(results)
                        return all_results

            # Try Jackett scrapers
            for instance, settings in self.config.get('Scrapers', {}).items():
                current_settings = self.get_scraper_settings(instance)
                if not current_settings.get('enabled', False):
                    continue
                
                scraper_type = current_settings.get('type')
                if scraper_type != 'Jackett':
                    continue

                logging.info(f"Running Jackett scraper '{instance}' without IMDB ID")
                instance, results = run_scraper(instance, scraper_type, current_settings, is_translated_search)
                if results:
                    all_results.extend(results)

            return all_results
        
        # For anime episodes, use ONLY Nyaa if enabled and it returns results
        if is_anime and is_episode:
            nyaa_settings = self.get_scraper_settings('Nyaa')
            old_nyaa_settings = self.get_scraper_settings('OldNyaa')
            nyaa_enabled = nyaa_settings.get('enabled', False) if nyaa_settings else False
            old_nyaa_enabled = old_nyaa_settings.get('enabled', False) if old_nyaa_settings else False
            
            if nyaa_enabled or old_nyaa_enabled:
                logging.info(f"Trying Nyaa/OldNyaa first for anime episode: {title}")
                
                # Use ThreadPoolExecutor to run anime scrapers in parallel
                anime_scraper_tasks = []
                with ThreadPoolExecutor() as executor:
                    if old_nyaa_enabled:
                        anime_scraper_tasks.append(
                            executor.submit(run_scraper, 'OldNyaa', 'OldNyaa', old_nyaa_settings, is_translated_search)
                        )
                    
                    if nyaa_enabled:
                        anime_scraper_tasks.append(
                            executor.submit(run_scraper, 'Nyaa', 'Nyaa', nyaa_settings, is_translated_search)
                        )
                    
                    # Collect results as they complete with timeout
                    try:
                        done, not_done = concurrent.futures.wait(
                            anime_scraper_tasks,
                            timeout=self.batch_timeout,
                            return_when=concurrent.futures.ALL_COMPLETED
                        )
                        
                        # Cancel any futures that didn't complete in time
                        for future in not_done:
                            future.cancel()
                        
                        # Process completed futures
                        for future in done:
                            try:
                                instance, results = future.result(timeout=self.scraper_timeout)
                                if results:
                                    logging.info(f"Found {len(results)} results from {instance}")
                                    all_results.extend(results)
                            except TimeoutError:
                                logging.error(f"Individual anime scraper timed out after {self.scraper_timeout} seconds")
                            except Exception as e:
                                logging.error(f"Error in anime scraper: {str(e)}")
                        
                        if not_done:
                            logging.error(f"Cancelled {len(not_done)} anime scrapers that exceeded the {self.batch_timeout} second timeout")
                    
                    except Exception as e:
                        logging.error(f"Error during anime batch scraping: {str(e)}")
                        # Cancel all remaining futures
                        for future in anime_scraper_tasks:
                            future.cancel()
                
                # Only return early if we found results from anime scrapers
                if all_results:
                    return all_results
                logging.info("No results from anime scrapers, falling back to other scrapers")

        # For all other cases (anime movies, non-anime content, or anime episodes with no results from anime scrapers)
        # Collect all enabled scrapers
        scraper_tasks = []
        for instance, settings in self.config.get('Scrapers', {}).items():
            current_settings = self.get_scraper_settings(instance)
            
            if not current_settings.get('enabled', False):
                continue
            
            scraper_type = current_settings.get('type')
            if scraper_type not in self.scrapers:
                logging.warning(f"Unknown scraper type '{scraper_type}' for instance '{instance}'. Skipping.")
                continue

            # Skip Nyaa for non-anime content
            if scraper_type in ['Nyaa', 'OldNyaa'] and not is_anime:
                continue
                
            # Skip anime scrapers if we already tried them above
            if is_anime and is_episode and scraper_type in ['Nyaa', 'OldNyaa']:
                continue
                
            scraper_tasks.append((instance, scraper_type, current_settings))
        
        # Run all scrapers in parallel using ThreadPoolExecutor
        with ThreadPoolExecutor() as executor:
            futures = [
                executor.submit(run_scraper, instance, scraper_type, settings, is_translated_search)
                for instance, scraper_type, settings in scraper_tasks
            ]
            
            # Collect results as they complete with timeout
            try:
                done, not_done = concurrent.futures.wait(
                    futures,
                    timeout=self.batch_timeout,
                    return_when=concurrent.futures.ALL_COMPLETED
                )
                
                # Cancel any futures that didn't complete in time
                for future in not_done:
                    future.cancel()
                
                # Process completed futures
                for future in done:
                    try:
                        instance, results = future.result(timeout=self.scraper_timeout)
                        if results:
                            all_results.extend(results)
                    except TimeoutError:
                        if self.use_timeout:
                            logging.error(f"Individual scraper timed out after {self.scraper_timeout} seconds")
                    except Exception as e:
                        logging.error(f"Error in scraper: {str(e)}")
                
                if not_done:
                    logging.error(f"Cancelled {len(not_done)} scrapers that exceeded the {self.batch_timeout} second timeout")
            
            except Exception as e:
                logging.error(f"Error during batch scraping: {str(e)}")
                # Cancel all remaining futures
                for future in futures:
                    future.cancel()

        return all_results