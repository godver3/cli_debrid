import logging
from typing import Dict, Any, List
from datetime import datetime, date

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting
from scraper.scraper import scrape
from not_wanted_magnets import is_magnet_not_wanted

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
        logging.debug(f"Processing scraping queue. Items: {len(self.items)}")
        if self.items:
            item = self.items.pop(0)
            item_identifier = queue_manager.generate_identifier(item)
            try:
                # Check if the release date is today or earlier
                if item['release_date'] == 'Unknown':
                    logging.info(f"Item {item_identifier} has an unknown release date. Moving back to Wanted queue.")
                    queue_manager.move_to_wanted(item, "Scraping")
                    return

                try:
                    release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                    today = date.today()

                    if release_date > today:
                        logging.info(f"Item {item_identifier} has a future release date ({release_date}). Moving back to Wanted queue.")
                        queue_manager.move_to_wanted(item, "Scraping")
                        return
                except ValueError:
                    logging.warning(f"Item {item_identifier} has an invalid release date format: {item['release_date']}. Moving back to Wanted queue.")
                    queue_manager.move_to_wanted(item, "Scraping")
                    return

                # Check if there are other episodes in the same season, excluding different versions of the current episode
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

                results = self.scrape_with_fallback(item, is_multi_pack, queue_manager)

                if not results:
                    logging.warning(f"No results found for {item_identifier} after fallback.")
                    self.handle_no_results(item, queue_manager)
                    return

                # Filter out "not wanted" results
                filtered_results = [r for r in results if not is_magnet_not_wanted(r['magnet'])]
                logging.debug(f"Scrape results for {item_identifier}: {len(filtered_results)} results after filtering")

                if not filtered_results:
                    logging.warning(f"All results for {item_identifier} were filtered out as 'not wanted'. Retrying with individual scraping.")
                    # Retry with individual scraping
                    individual_results = self.scrape_with_fallback(item, False)
                    filtered_individual_results = [r for r in individual_results if not is_magnet_not_wanted(r['magnet'])]
                    
                    if not filtered_individual_results:
                        logging.warning(f"No valid results for {item_identifier} even after individual scraping. Moving to Sleeping.")
                        queue_manager.move_to_sleeping(item, "Scraping")
                        return
                    else:
                        filtered_results = filtered_individual_results

                if filtered_results:
                    best_result = filtered_results[0]
                    logging.info(f"Best result for {item_identifier}: {best_result['title']}, {best_result['magnet']}")
                    queue_manager.move_to_adding(item, "Scraping", best_result['title'], filtered_results)
                else:
                    logging.warning(f"No valid results for {item_identifier}, moving to Sleeping")
                    queue_manager.move_to_sleeping(item, "Scraping")
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                queue_manager.move_to_sleeping(item, "Scraping")

    def scrape_with_fallback(self, item, is_multi_pack, queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Scraping for {item_identifier} with is_multi_pack={is_multi_pack}")

        results = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            is_multi_pack
        )

        if results or item['type'] != 'episode':
            return results

        logging.info(f"No results for multi-pack {item_identifier}. Falling back to individual episode scraping.")

        individual_results = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            False  # Set is_multi_pack to False for individual scraping
        )

        if individual_results:
            logging.info(f"Found results for individual episode scraping of {item_identifier}.")
        else:
            logging.warning(f"No results found even after individual episode scraping for {item_identifier}.")

        return individual_results
     
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
            logging.warning(f"No results found for {item_identifier}. Moving to Sleeping queue.")
            queue_manager.move_to_sleeping(item, "Scraping")
            
    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if 'release_date' not in item or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date or unknown release date. Considering it as old.")
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
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")