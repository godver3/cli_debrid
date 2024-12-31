import logging
from typing import Dict, Any, List
from datetime import datetime, date, timedelta

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting
from scraper.scraper import scrape
from not_wanted_magnets import is_magnet_not_wanted, is_url_not_wanted
from wake_count_manager import wake_count_manager

class ScrapingQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Scraping")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        processed_count = 0
        had_error = False
        
        if self.items:
            item = self.items.pop(0)
            item_identifier = queue_manager.generate_identifier(item)
            try:
                logging.info(f"Starting to process scraping results for {item_identifier}")
                
                # Check release date logic
                if item['release_date'] == 'Unknown':
                    logging.info(f"Item {item_identifier} has an unknown release date. Moving back to Wanted queue.")
                    queue_manager.move_to_wanted(item, "Scraping")
                    processed_count += 1
                    return True  # Continue processing other items
                    
                try:
                    release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                    today = date.today()

                    if release_date > today:
                        logging.info(f"Item {item_identifier} has a future release date ({release_date}). Moving back to Wanted queue.")
                        queue_manager.move_to_wanted(item, "Scraping")
                        processed_count += 1
                        return True
                except ValueError:
                    logging.warning(f"Item {item_identifier} has an invalid release date format: {item['release_date']}. Moving back to Wanted queue.")
                    queue_manager.move_to_wanted(item, "Scraping")
                    processed_count += 1
                    return True

                # Multi-pack check logic
                is_multi_pack = False
                if item['type'] == 'episode':
                    is_multi_pack = any(
                        wanted_item['type'] == 'episode' and
                        wanted_item['imdb_id'] == item['imdb_id'] and
                        wanted_item['season_number'] == item['season_number'] and
                        (wanted_item['episode_number'] != item['episode_number'] or
                         (wanted_item['episode_number'] == item['episode_number'] and
                          wanted_item['version'] == item['version']))
                        for wanted_item in queue_manager.queues["Wanted"].get_contents() + self.items
                    )

                logging.info(f"Scraping for {item_identifier}")
                results, filtered_out_results = self.scrape_with_fallback(item, is_multi_pack, queue_manager)
                
                # Ensure both results and filtered_out_results are lists
                results = results if results is not None else []
                filtered_out_results = filtered_out_results if filtered_out_results is not None else []
                
                logging.info(f"Received {len(results)} initial results and {len(filtered_out_results)} filtered out results for {item_identifier}")

                if not results:
                    logging.warning(f"No results found for {item_identifier} after fallback.")
                    self.handle_no_results(item, queue_manager)
                    processed_count += 1
                    logging.info(f"Processed count after handling no results: {processed_count}")
                    return True

                # Filter and process results
                filtered_results = []
                for result in results:
                    if is_magnet_not_wanted(result['magnet']):
                        logging.info(f"Result '{result['title']}' filtered out by not_wanted_magnets check")
                        continue
                    if is_url_not_wanted(result['magnet']):
                        logging.info(f"Result '{result['title']}' filtered out by not_wanted_urls check")
                        continue
                    filtered_results.append(result)
                
                logging.info(f"Found {len(filtered_results)} valid results after filtering for {item_identifier} (filtered out {len(results) - len(filtered_results)} results)")

                if not filtered_results:
                    logging.warning(f"All results filtered out for {item_identifier}. Retrying individual scraping.")
                    individual_results, individual_filtered_out = self.scrape_with_fallback(item, False, queue_manager)
                    logging.info(f"Individual scraping returned {len(individual_results)} results")
                    
                    filtered_individual_results = []
                    for result in individual_results:
                        if is_magnet_not_wanted(result['magnet']):
                            logging.info(f"Individual result '{result['title']}' filtered out by not_wanted_magnets check")
                            continue
                        if is_url_not_wanted(result['magnet']):
                            logging.info(f"Individual result '{result['title']}' filtered out by not_wanted_urls check")
                            continue
                        filtered_individual_results.append(result)
                    
                    logging.info(f"After filtering, individual scraping has {len(filtered_individual_results)} valid results (filtered out {len(individual_results) - len(filtered_individual_results)} results)")
                    
                    if not filtered_individual_results:
                        logging.warning(f"No valid results after individual scraping for {item_identifier}. Moving to Sleeping.")
                        queue_manager.move_to_sleeping(item, "Scraping")
                        processed_count += 1
                        logging.info(f"Processed count after moving to sleeping: {processed_count}")
                        return True
                    filtered_results = filtered_individual_results

                if filtered_results:
                    best_result = filtered_results[0]
                    logging.info(f"Best result for {item_identifier}: {best_result['title']}")
                    
                    if get_setting("Debug", "enable_reverse_order_scraping", default=False):
                        logging.info(f"Reverse order scraping enabled. Reversing results.")
                        filtered_results.reverse()
                    
                    logging.info(f"Moving {item_identifier} to Adding queue with {len(filtered_results)} results")
                    try:
                        queue_manager.move_to_adding(item, "Scraping", best_result['title'], filtered_results)
                        logging.info(f"Successfully moved {item_identifier} to Adding queue")
                    except Exception as e:
                        logging.error(f"Failed to move {item_identifier} to Adding queue: {str(e)}", exc_info=True)
                        had_error = True
                else:
                    logging.info(f"No valid results for {item_identifier}, moving to Sleeping")
                    queue_manager.move_to_sleeping(item, "Scraping")
                
                processed_count += 1
                logging.info(f"Final processed count: {processed_count}")
                
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                queue_manager.move_to_sleeping(item, "Scraping")
                had_error = True
                processed_count += 1

        # Return True if there are more items to process or if we processed something
        return len(self.items) > 0 or processed_count > 0

    def scrape_with_fallback(self, item, is_multi_pack, queue_manager, skip_filter=False):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Scraping for {item_identifier} with is_multi_pack={is_multi_pack}, skip_filter={skip_filter}")

        results, filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            is_multi_pack,
            item.get('genres')
        )

        # Ensure results and filtered_out are lists
        results = results if results is not None else []
        filtered_out = filtered_out if filtered_out is not None else []

        logging.info(f"Raw scrape results for {item_identifier}: {len(results)} results")
        for result in results:
            logging.debug(f"Scrape result: {result}")

        if not skip_filter:
            # Filter out unwanted magnets and URLs
            filtered_results = [r for r in results if not (is_magnet_not_wanted(r['magnet']) or is_url_not_wanted(r['magnet']))]
            if len(filtered_results) < len(results):
                logging.info(f"Filtered out {len(results) - len(filtered_results)} results due to not wanted magnets/URLs")
            results = filtered_results

        # For episodes, filter by exact season/episode match
        if item['type'] == 'episode' and not is_multi_pack:
            season = item.get('season_number')
            episode = item.get('episode_number')
            filtered_results = [
                r for r in results 
                if r.get('parsed_info', {}).get('season_episode_info', {}).get('seasons', []) == [season]
                and r.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', []) == [episode]
            ]
            if len(filtered_results) < len(results):
                logging.info(f"Filtered out {len(results) - len(filtered_results)} results due to season/episode mismatch")
            results = filtered_results

        if results or item['type'] != 'episode':
            return results, filtered_out

        logging.info(f"No results for multi-pack {item_identifier}. Falling back to individual episode scraping.")

        individual_results, individual_filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            False,
            item.get('genres')
        )

        # Ensure individual results and filtered_out are lists
        individual_results = individual_results if individual_results is not None else []
        individual_filtered_out = individual_filtered_out if individual_filtered_out is not None else []

        # Filter out unwanted magnets and URLs for individual results
        if not skip_filter:
            individual_results = [r for r in individual_results if not (is_magnet_not_wanted(r['magnet']) or is_url_not_wanted(r['magnet']))]

        # For episodes, ensure we have the correct season and episode
        if item['type'] == 'episode':
            season = item.get('season_number')
            episode = item.get('episode_number')
            if season is not None and episode is not None:
                individual_results = [
                    r for r in individual_results 
                    if r.get('parsed_info', {}).get('season_episode_info', {}).get('seasons', []) == [season]
                    and r.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', []) == [episode]
                ]
                logging.info(f"After filtering individual results for specific episode S{season}E{episode}: {len(individual_results)} results remain for {item_identifier}")

        if individual_results:
            logging.info(f"Found results for individual episode scraping of {item_identifier}.")
        else:
            logging.warning(f"No results found even after individual episode scraping for {item_identifier}.")

        return individual_results, individual_filtered_out
     
    def handle_no_results(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        if self.is_item_old(item):
            if item['type'] == 'episode':
                logging.info(f"No results found for old episode {item_identifier}. Blacklisting item and related season items.")
                queue_manager.queues["Blacklisted"].blacklist_old_season_items(item, queue_manager)
            elif item['type'] == 'movie':
                logging.info(f"No results found for old movie {item_identifier}. Blacklisting item.")
                queue_manager.move_to_blacklisted(item, "Scraping")
            else:
                logging.warning(f"Unknown item type {item['type']} for {item_identifier}. Blacklisting item.")
                queue_manager.move_to_blacklisted(item, "Scraping")
        else:
            logging.warning(f"No results found for {item_identifier}. Moving to Sleeping queue.")
            wake_count = wake_count_manager.get_wake_count(item['id'])
            logging.debug(f"Wake count before moving to Sleeping: {wake_count}")
            queue_manager.move_to_sleeping(item, "Scraping")
            logging.debug(f"Updated wake count in Sleeping queue: {wake_count}")
            
    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if 'release_date' not in item or item['release_date'] is None or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date, None, or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            # Define thresholds for considering items as old
            movie_threshold = 30  # Consider movies old after 30 days
            episode_threshold = 7  # Consider episodes old after 7 days
            
            if item['type'] == 'movie':
                return days_since_release > movie_threshold
            elif item['type'] == 'episode':
                return days_since_release > episode_threshold
            else:
                logging.warning(f"Unknown item type: {item['type']}. Considering it as old.")
                return True
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old
            
    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{'_'.join(item['version'].split())}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{'_'.join(item['version'].split())}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")