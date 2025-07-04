import logging
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import concurrent.futures
import os
import json
from datetime import datetime
import time
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
        is_translated_search: bool = False,
        is_anime: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Scrape all configured sources for content, enrich with specific metadata, and log detailed results.
        
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
            is_anime: Flag to indicate if the content is anime
        """
        all_results = []
        instance_summary = {} # To store results count per instance
        is_episode = content_type.lower() == 'episode'

        self.scraper_timeout = get_setting('Scraping', 'scraper_timeout', 5)
        self.batch_timeout = get_setting('Scraping', 'scraper_timeout', 5)
        
        # Disable timeouts if scraper_timeout is 0
        self.use_timeout = self.scraper_timeout > 0
        self.scraper_timeout = None if self.scraper_timeout == 0 else self.scraper_timeout
        self.batch_timeout = None if self.batch_timeout == 0 else self.batch_timeout
        
        # Helper function to run a scraper and handle exceptions
        def run_scraper(instance, scraper_type, settings, is_translated):
            results = []
            scraper_call_start_time = 0
            scraper_call_duration = 0
            try:
                if scraper_type not in self.scrapers:
                     logging.error(f"Scraper function for type \'{scraper_type}\' not found.")
                     return instance, scraper_type, []

                scraper_call_start_time = time.time()
                if scraper_type in ['Nyaa', 'OldNyaa']:
                     # Nyaa has a different function signature
                     if scraper_type == 'Nyaa':
                         # Nyaa scrape function needs its specific args
                          results = self.scrapers[scraper_type](
                              title=title, year=year, content_type=content_type,
                              season=season, episode=episode,
                              episode_formats=episode_formats if is_anime and is_episode else None,
                              tmdb_id=tmdb_id, multi=multi,
                              is_translated_search=is_translated
                          )
                     else: # OldNyaa
                          results = self.scrapers[scraper_type](
                              instance=instance, settings=settings, imdb_id=imdb_id,
                              title=title, year=year, content_type=content_type,
                              season=season, episode=episode, multi=multi
                          )
                else:
                    # Prepare common arguments
                    common_args = {
                        "instance": instance, "settings": settings, "imdb_id": imdb_id,
                        "title": title, "year": year, "content_type": content_type,
                        "season": season, "episode": episode, "multi": multi
                        # tmdb_id will be added specifically below if needed by the scraper type
                    }
                    # Add specific args only if the scraper accepts them
                    if scraper_type == 'Jackett':
                         common_args["genres"] = genres
                         common_args["tmdb_id"] = tmdb_id 
                         common_args["is_translated_search"] = is_translated
                    elif scraper_type == 'Prowlarr':
                         common_args["tmdb_id"] = tmdb_id 
                         # Prowlarr's scrape_prowlarr_instance function signature:
                         # (instance, settings, imdb_id, title, year, content_type, 
                         #  season, episode, multi, tmdb_id)
                         # Most are in common_args. tmdb_id is added here.
                         # It doesn't take 'genres' or 'is_translated_search'.
                    # Add more elif for other scrapers if they need specific args

                    results = self.scrapers[scraper_type](**common_args)

                scraper_call_duration = time.time() - scraper_call_start_time
                logging.info(f"Scraper {instance} ({scraper_type}) call took {scraper_call_duration:.2f}s, found {len(results)} results.")
                return instance, scraper_type, results
            except Exception as e:
                if scraper_call_start_time > 0: # Check if timing started
                    scraper_call_duration = time.time() - scraper_call_start_time
                    logging.error(f"Error during {scraper_type} instance \'{instance}\' call (took {scraper_call_duration:.2f}s): {str(e)}", exc_info=True)
                else: # Error before scraper call (e.g., scraper not found)
                    logging.error(f"Error preparing to scrape {scraper_type} instance \'{instance}\': {str(e)}", exc_info=True)
                return instance, scraper_type, [] # Return empty list on error

        # If no IMDB ID is available, only use Nyaa (for anime) and Jackett scrapers
        if not imdb_id:
            logging.info("No IMDB ID available - limiting scrapers to Nyaa (if anime) and Jackett")
            
            # For anime episodes, try Nyaa first
            if is_anime and is_episode:
                nyaa_settings = self.get_scraper_settings('Nyaa')
                nyaa_enabled = nyaa_settings.get('enabled', False) if nyaa_settings else False
                
                if nyaa_enabled:
                    logging.info(f"Trying Nyaa for anime episode without IMDB ID: {title}")
                    instance, scraper_type, results = run_scraper('Nyaa', 'Nyaa', nyaa_settings, is_translated_search)
                    if results:
                        all_results.extend(results)
                        instance_summary[instance] = {'type': scraper_type, 'count': len(results)}
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
                instance, scraper_type, results = run_scraper(instance, scraper_type, current_settings, is_translated_search)
                if results:
                    all_results.extend(results)
                    instance_summary[instance] = {'type': scraper_type, 'count': len(results)}

            self._log_scraper_report(title, year, instance_summary)
            return all_results
        
        # For anime episodes, use ONLY Nyaa if enabled and it returns results
        logging.info(f"[ScraperManager] Anime check: is_anime={is_anime}, is_episode={is_episode}.")
        if is_anime and is_episode:
            nyaa_settings = self.get_scraper_settings('Nyaa')
            old_nyaa_settings = self.get_scraper_settings('OldNyaa')
            nyaa_enabled = nyaa_settings.get('enabled', False) if nyaa_settings else False
            old_nyaa_enabled = old_nyaa_settings.get('enabled', False) if old_nyaa_settings else False
            
            logging.info(f"[ScraperManager] Nyaa enabled: {nyaa_enabled}, OldNyaa enabled: {old_nyaa_enabled}.")
            
            if nyaa_enabled or old_nyaa_enabled:
                logging.info(f"Trying Nyaa/OldNyaa first for anime episode: {title}")
                
                # Use ThreadPoolExecutor to run anime scrapers in parallel without blocking on shutdown
                anime_scraper_tasks = []
                executor = ThreadPoolExecutor()
                future_instance_map = {}
                try:
                    if old_nyaa_enabled:
                        fut = executor.submit(run_scraper, 'OldNyaa', 'OldNyaa', old_nyaa_settings, is_translated_search)
                        anime_scraper_tasks.append(fut)
                        future_instance_map[fut] = ('OldNyaa', 'OldNyaa')
                    if nyaa_enabled:
                        fut = executor.submit(run_scraper, 'Nyaa', 'Nyaa', nyaa_settings, is_translated_search)
                        anime_scraper_tasks.append(fut)
                        future_instance_map[fut] = ('Nyaa', 'Nyaa')

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
                                instance, scraper_type, results = future.result(timeout=self.scraper_timeout)
                                if results:
                                    logging.info(f"Found {len(results)} results from {instance}")
                                    all_results.extend(results)
                                    instance_summary[instance] = {'type': scraper_type, 'count': len(results)}
                            except TimeoutError:
                                inst, stype = future_instance_map.get(future, ('Unknown', 'Unknown'))
                                logging.error(f"Individual anime scraper '{inst}' timed out after {self.scraper_timeout} seconds")
                            except Exception as e:
                                logging.error(f"Error in anime scraper: {str(e)}")

                        if not_done:
                            logging.error(
                                f"Cancelled {len(not_done)} anime scrapers that exceeded the {self.batch_timeout} second timeout")

                    except Exception as e:
                        logging.error(f"Error during anime batch scraping: {str(e)}")
                        # Cancel all remaining futures
                        for future in anime_scraper_tasks:
                            future.cancel()
                finally:
                    # Shutdown the executor without waiting for running threads to finish
                    executor.shutdown(wait=False, cancel_futures=True)
                
                # Only return early if we found results from anime scrapers
                if all_results:
                    logging.info("Returning early with results from Nyaa/OldNyaa.")
                    self._log_scraper_report(title, year, instance_summary)
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
        
        # Run all scrapers in parallel using ThreadPoolExecutor without blocking on shutdown
        executor = ThreadPoolExecutor()
        future_instance_map = {}
        try:
            futures = []
            for instance, scraper_type, settings in scraper_tasks:
                fut = executor.submit(run_scraper, instance, scraper_type, settings, is_translated_search)
                futures.append(fut)
                future_instance_map[fut] = (instance, scraper_type)

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
                        instance, scraper_type, results = future.result(timeout=self.scraper_timeout)
                        if results:
                            all_results.extend(results)
                            instance_summary[instance] = {'type': scraper_type, 'count': len(results)}
                    except TimeoutError:
                        inst, stype = future_instance_map.get(future, ('Unknown', 'Unknown'))
                        if self.use_timeout:
                            logging.error(
                                f"Individual scraper '{inst}' timed out after {self.scraper_timeout} seconds")
                        if inst not in instance_summary:
                            instance_summary[inst] = {'type': stype, 'count': 'Timed Out'}
                    except Exception as e:
                        inst, stype = future_instance_map.get(future, ('Unknown', 'Unknown'))
                        logging.error(f"Error in '{inst}' scraper future: {str(e)}")
                        if inst not in instance_summary:
                            instance_summary[inst] = {'type': stype, 'count': 'Error'}

                if not_done:
                    logging.error(
                        f"Cancelled {len(not_done)} scrapers that exceeded the {self.batch_timeout} second timeout")
                    for future in not_done:
                        inst, stype = future_instance_map.get(future, ('Unknown', 'Unknown'))
                        if inst not in instance_summary:
                            instance_summary[inst] = {'type': stype, 'count': 'Cancelled (Timeout)'}

            except Exception as e:
                logging.error(f"Error during batch scraping: {str(e)}")
                for future in futures:
                    future.cancel()
        finally:
            # Shutdown the executor without waiting for running threads to finish
            executor.shutdown(wait=False, cancel_futures=True)

        # Log the final report
        self._log_scraper_report(title, year, instance_summary)

        # --- Add logging of detailed results to separate file ---
        self._log_detailed_results(title, year, all_results)
        # --- End detailed results logging ---

        # --- Enrich results with additional metadata ---
        enriched_results = self._enrich_results(all_results, instance_summary)
        # --- End enrichment ---

        # Log the detailed results (now potentially enriched) to separate file
        self._log_detailed_results(title, year, enriched_results)

        # Return the enriched results
        return enriched_results

    def _enrich_results(self, results: List[Dict[str, Any]], instance_summary: Dict[str, Dict]) -> List[Dict[str, Any]]:
        """Adds additional metadata to results based on their source and normalizes titles."""
        instance_to_type = {name: summary.get('type') for name, summary in instance_summary.items()}

        for result in results:
            # Normalize title: remove content after "┈➤"
            current_title = result.get('title', '')
            if '┈➤' in current_title:
                result['title'] = current_title.split('┈➤')[0].strip()

            source_parts = result.get('source', '').split(' - ')
            instance_name = source_parts[0] if source_parts else None

            if not instance_name:
                continue

            scraper_type = instance_to_type.get(instance_name)
            parsed_info = result.get('parsed_info', {}) # Get parsed_info once

            if scraper_type == 'MediaFusion':
                filename = parsed_info.get('filename')
                binge_group = parsed_info.get('bingeGroup')

                if filename or binge_group:
                    additional_metadata = result.setdefault('additional_metadata', {})
                    if filename:
                        additional_metadata['filename'] = filename
                    if binge_group:
                        additional_metadata['bingeGroup'] = binge_group
            
            elif scraper_type == 'Torrentio':
                # Extract Torrentio specific metadata
                filename = parsed_info.get('filename')
                binge_group = parsed_info.get('bingeGroup')
                source_site = parsed_info.get('source_site') # Also grab source_site if available

                if filename or binge_group or source_site:
                    additional_metadata = result.setdefault('additional_metadata', {})
                    if filename:
                        additional_metadata['filename'] = filename
                    if binge_group:
                        additional_metadata['bingeGroup'] = binge_group
                    if source_site: # Add source_site too
                        additional_metadata['source_site'] = source_site

            # Add elif blocks here for other scraper types in the future
            # elif scraper_type == 'Jackett':
            #      # Extract Jackett specific metadata
            #      pass
            # elif scraper_type == 'Zilean':
            #      # Extract Zilean specific metadata
            #      pass


        return results

    def _log_scraper_report(self, title: str, year: int, instance_summary: Dict[str, Dict]):
        """Helper function to log the scraper summary report."""
        report_lines = [f"Scraper Report for '{title} ({year})':"]
        if not instance_summary:
            report_lines.append("  No scrapers were run or completed successfully.")
        else:
            # Sort summary by instance name for consistent logging
            sorted_instances = sorted(instance_summary.items())
            for instance, summary in sorted_instances:
                scraper_type = summary.get('type', 'Unknown')
                count = summary.get('count', 'N/A')
                report_lines.append(f"  - {instance} ({scraper_type}): Found {count} results.")
        
        logging.info("\n".join(report_lines))

    def _log_detailed_results(self, title: str, year: int, all_results: List[Dict[str, Any]]):
        """Helper function to log all gathered results to a separate file."""
        # disable for now
        return
    
        log_dir = os.environ.get('USER_LOGS', '/user/logs') # Get log directory from environment
        results_log_path = os.path.join(log_dir, 'scraper_results.log')
        
        try:
            # Create a structured log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "search_query": f"{title} ({year})",
                "total_results": len(all_results),
                "results": all_results
            }
            
            # Append the JSON entry to the log file
            with open(results_log_path, 'a', encoding='utf-8') as f:
                json.dump(log_entry, f, ensure_ascii=False, indent=2) # Use indent for readability
                f.write('\n') # Add newline for separation between entries
            logging.info(f"Detailed scraper results logged to {results_log_path}")
        except Exception as e:
            logging.error(f"Failed to log detailed scraper results to {results_log_path}: {e}")