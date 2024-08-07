import logging
from datetime import datetime, date, timedelta
import time
from database import get_all_media_items, update_media_item_state, get_media_item_by_id, add_collected_items, get_media_item_status
from scraper.scraper import scrape
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet, RealDebridUnavailableError
from utilities.plex_functions import get_collected_from_plex
import pickle
import json
from settings import get_setting
import requests
from upgrading_db import get_items_to_check, update_check_count, remove_from_upgrading, add_to_upgrading
from typing import Dict, Any
from upgrading_queue import UpgradingQueue
from not_wanted_magnets import load_not_wanted_magnets, save_not_wanted_magnets, add_to_not_wanted, is_magnet_not_wanted
from collections import OrderedDict

class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

class QueueManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(QueueManager, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def initialize(self):
        self.queues = {
            "Wanted": [],
            "Scraping": [],
            "Adding": [],
            "Checking": [],
            "Sleeping": [],
            "Unreleased": [],
            "Blacklisted": []
        }
        self.scraping_cap = 5  # Cap for scraping queue
        self.checking_queue_times = {}
        self.sleeping_queue_times = {}
        self.wake_counts = {}
        self.upgrading_queue = UpgradingQueue()
        self.update_all_queues()  # Initialize queues on startup

    def generate_identifier(self, item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")

    def update_all_queues(self):
        logging.debug("Updating all queues")
        for state in self.queues.keys():
            if state == "Upgrading":
                self.queues[state] = self.upgrading_queue.get_queue_contents()
            else:
                items = get_all_media_items(state=state)
                self.queues[state] = [dict(row) for row in items]


    def get_queue_contents(self):
        contents = OrderedDict()
        
        # Add Upgrading queue first
        upgrading_contents = self.upgrading_queue.get_queue_contents()
        for item in upgrading_contents:
            item['time_added'] = item.get('time_added', datetime.now().isoformat())
            item['upgrades_found'] = item.get('upgrades_found', 0)
            # Ensure version is included and not overwritten
            if 'version' not in item:
                item['version'] = 'Unknown'
        contents['Upgrading'] = upgrading_contents
        
        # Add other queues in the specified order
        for state, queue in self.queues.items():
            contents[state] = []
            for item in queue:
                new_item = item.copy()  # Create a copy to avoid modifying the original item
                if state == "Sleeping":
                    new_item["wake_count"] = self.wake_counts.get(item['id'], 0)
                elif state == "Checking":
                    new_item["time_added"] = self.checking_queue_times.get(item['id'], datetime.now()).isoformat()
                
                # Ensure version is included and not overwritten
                if 'version' not in new_item:
                    new_item['version'] = 'Unknown'
                
                contents[state].append(new_item)
        
        return contents

    def process_wanted(self):
        logging.debug("Processing wanted queue")
        current_date = datetime.now().date()
        seasons_in_queues = set()
        # Get seasons already in Scraping or Adding queue
        for queue_name in ["Scraping", "Adding"]:
            for item in self.queues[queue_name]:
                if item['type'] == 'episode':
                    seasons_in_queues.add((item['imdb_id'], item['season_number'], item['version']))
        
        items_to_move = []
        items_to_unreleased = []
        
        # Process each item in the Wanted queue
        for item in list(self.queues["Wanted"]):
            item_identifier = self.generate_identifier(item)
            try:
                # Check if release_date is None, empty string, or "Unknown"
                if not item['release_date'] or item['release_date'].lower() == "unknown":
                    logging.debug(f"Release date is missing or unknown for item: {item_identifier}. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                    continue
                
                release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                if release_date > current_date:
                    logging.info(f"Item {item_identifier} is not released yet. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                else:
                    # Check if we've reached the scraping cap
                    if len(self.queues["Scraping"]) + len(items_to_move) >= self.scraping_cap:
                        logging.debug(f"Scraping cap reached. Keeping {item_identifier} in Wanted queue.")
                        break  # Exit the loop as we've reached the cap
                    # Check if we're already processing an item from this season
                    if item['type'] == 'episode':
                        season_key = (item['imdb_id'], item['season_number'], item['version'])
                        if season_key in seasons_in_queues:
                            logging.debug(f"Already processing an item from {item_identifier}. Keeping in Wanted queue.")
                            continue
                    logging.info(f"Item {item_identifier} has been released. Marking for move to Scraping.")
                    items_to_move.append(item)
                    if item['type'] == 'episode':
                        seasons_in_queues.add(season_key)
            except ValueError as e:
                logging.error(f"Error processing release date for item {item_identifier}: {str(e)}")
                logging.error(f"Item details: {json.dumps(item, indent=2, cls=DateTimeEncoder)}")
                # Move to Unreleased state if there's an error processing the date
                items_to_unreleased.append(item)
            except Exception as e:
                logging.error(f"Unexpected error processing item {item_identifier}: {str(e)}")
                logging.error(f"Item details: {json.dumps(item, indent=2, cls=DateTimeEncoder)}")
                # Move to Unreleased state if there's an unexpected error
                items_to_unreleased.append(item)
        
        # Move marked items to Scraping queue
        for item in items_to_move:
            item_identifier = self.generate_identifier(item)
            self.queues["Wanted"].remove(item)
            update_media_item_state(item['id'], 'Scraping')
            updated_item = get_media_item_by_id(item['id'])
            if updated_item:
                self.queues["Scraping"].append(updated_item)
                logging.info(f"Moved item {item_identifier} from Wanted to Scraping queue")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
        
        # Move items to Unreleased state and remove from Wanted queue
        for item in items_to_unreleased:
            item_identifier = self.generate_identifier(item)
            self.queues["Wanted"].remove(item)
            update_media_item_state(item['id'], 'Unreleased')
            logging.info(f"Moved item {item_identifier} to Unreleased state and removed from Wanted queue.")
        
        logging.debug(f"Wanted queue processing complete. Items moved to Scraping queue: {len(items_to_move)}")
        logging.debug(f"Items moved to Unreleased state and removed from Wanted queue: {len(items_to_unreleased)}")
        logging.debug(f"Total items in Scraping queue: {len(self.queues['Scraping'])}")
        logging.debug(f"Remaining items in Wanted queue: {len(self.queues['Wanted'])}")

    def process_scraping(self):
        logging.debug(f"Processing scraping queue. Items: {len(self.queues['Scraping'])}")
        if self.queues["Scraping"]:
            item = self.queues["Scraping"].pop(0)
            item_identifier = self.generate_identifier(item)
            try:
                # Check if the release date is today or earlier
                if item['release_date'] == 'Unknown':
                    logging.info(f"Item {item_identifier} has an unknown release date. Moving back to Wanted queue.")
                    self.move_to_wanted(item)
                    return

                try:
                    release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                    today = date.today()

                    if release_date > today:
                        logging.info(f"Item {item_identifier} has a future release date ({release_date}). Moving back to Wanted queue.")
                        self.move_to_wanted(item)
                        return
                except ValueError:
                    logging.warning(f"Item {item_identifier} has an invalid release date format: {item['release_date']}. Moving back to Wanted queue.")
                    self.move_to_wanted(item)
                    return

                is_multi_pack = any(
                    wanted_item['type'] == 'episode' and
                    wanted_item['season_number'] == item['season_number'] and
                    wanted_item['version'] == item['version'] and
                    wanted_item['id'] != item['id']
                    for wanted_item in self.queues["Wanted"]
                )

                logging.info(f"Scraping for {item_identifier}")

                results = self.scrape_with_fallback(item, is_multi_pack)

                if not results:
                    wake_count = self.wake_counts.get(item['id'], 0)
                    logging.warning(f"No results returned for {item_identifier}. Moving to Sleeping queue with wake count {wake_count}")
                    update_media_item_state(item['id'], 'Sleeping')
                    updated_item = get_media_item_by_id(item['id'])
                    if updated_item:
                        self.queues["Sleeping"].append(updated_item)
                        self.wake_counts[item['id']] = wake_count  # Preserve the wake count
                        self.sleeping_queue_times[item['id']] = datetime.now()
                        logging.debug(f"Set wake count for {item_identifier} to {wake_count}")
                    return

                # Filter out "not wanted" results
                filtered_results = [r for r in results if not is_magnet_not_wanted(r['magnet'])]
                logging.debug(f"Scrape results for {item_identifier}: {len(filtered_results)} results after filtering")

                if filtered_results:
                    best_result = filtered_results[0]
                    logging.info(f"Best result for {item_identifier}: {best_result['title']}, {best_result['magnet']}")
                    update_media_item_state(item['id'], 'Adding', filled_by_title=best_result['title'], scrape_results=filtered_results)
                    updated_item = get_media_item_by_id(item['id'])
                    if updated_item:
                        updated_item_identifier = self.generate_identifier(updated_item)
                        logging.debug(f"Updated {updated_item_identifier}")
                        self.queues["Adding"].append(updated_item)
                    else:
                        logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
                else:
                    logging.warning(f"No valid results for {item_identifier}, moving to Sleeping")
                    update_media_item_state(item['id'], 'Sleeping')
                    updated_item = get_media_item_by_id(item['id'])
                    if updated_item:
                        self.queues["Sleeping"].append(updated_item)
                    else:
                        logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)

    def scrape_with_fallback(self, item, is_multi_pack):
        item_identifier = self.generate_identifier(item)
        
        # First, try scraping with the original parameters
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

        if results or item['type'] != 'episode' or not is_multi_pack:
            return results

        # If no results and it's a multi-pack episode, try individual episode scraping
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

    def process_adding(self):
        logging.debug("Processing adding queue")
        if self.queues["Adding"]:
            item = self.queues["Adding"].pop(0)
            item_identifier = self.generate_identifier(item)
            updated_item = get_media_item_by_id(item['id'])
            if updated_item:
                updated_item_identifier = self.generate_identifier(updated_item)
                logging.debug(f"Processing item: {updated_item_identifier}")
                scrape_results_str = updated_item.get('scrape_results', '')
                if scrape_results_str:
                    try:
                        scrape_results = json.loads(scrape_results_str)
                    except json.JSONDecodeError:
                        logging.error(f"Error parsing JSON scrape results for item: {updated_item_identifier}")
                        self.move_to_sleeping(updated_item)
                        return

                    uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None')
                    first_uncached_added = False
                    cached_added = False

                    for result in scrape_results:
                        title = result.get('title', '')
                        magnet = result.get('magnet', '')
                        hash_value = extract_hash_from_magnet(magnet)
                        if hash_value:
                            cache_status = is_cached_on_rd(hash_value)
                            logging.debug(f"Cache status for {hash_value} ({updated_item_identifier}): {cache_status}")
                            is_cached = hash_value in cache_status and cache_status[hash_value]

                            if is_cached or (uncached_handling != 'None' and not first_uncached_added):
                                try:
                                    add_to_real_debrid(magnet)
                                    if is_cached:
                                        cached_added = True
                                        logging.info(f"Added cached content for {updated_item_identifier}")
                                    else:
                                        first_uncached_added = True
                                        logging.info(f"Added uncached content for {updated_item_identifier}")
                                except RealDebridUnavailableError:
                                    logging.error(f"Real-Debrid service is unavailable. Moving item {updated_item_identifier} to Sleeping to retry later.")
                                    self.move_to_sleeping(updated_item)
                                    return
                                except Exception as e:
                                    logging.error(f"Error adding magnet to Real-Debrid for {updated_item_identifier}: {str(e)}")
                                    add_to_not_wanted(magnet)
                                    continue

                                update_media_item_state(updated_item['id'], 'Checking', filled_by_title=title, filled_by_magnet=magnet)
                                checked_item = get_media_item_by_id(updated_item['id'])
                                if checked_item:
                                    checked_item_identifier = self.generate_identifier(checked_item)
                                    self.queues["Checking"].append(checked_item)
                                    self.upgrading_queue.add_item(checked_item)
                                    logging.info(f"Added item {checked_item_identifier} to UpgradingQueue")
                                    logging.debug(f"Item {checked_item_identifier} was a multi-pack result: {result.get('is_multi_pack', False)}")
                                    if result.get('is_multi_pack', False):
                                        self.process_multi_pack(checked_item, title, magnet, result.get('season_pack', ''))
                                else:
                                    logging.error(f"Failed to retrieve checked item for ID: {updated_item['id']}")

                                if uncached_handling == 'None' or (uncached_handling == 'Hybrid' and cached_added):
                                    return
                            else:
                                logging.info(f"Skipping uncached content for {updated_item_identifier}")
                        else:
                            logging.warning(f"Failed to extract hash from magnet link for {updated_item_identifier}")

                    if not (cached_added or first_uncached_added):
                        logging.info(f"No suitable results found for {updated_item_identifier}. Moving to Sleeping.")
                        self.move_to_sleeping(updated_item)
                else:
                    logging.error(f"No scrape results found for {updated_item_identifier}")
                    self.move_to_sleeping(updated_item)
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def process_multi_pack(self, item: Dict[str, Any], title: str, magnet: str, season_pack: str):
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Starting process_multi_pack for item: {item_identifier}")
        logging.debug(f"Item details: type={item['type']}, imdb_id={item['imdb_id']}, season_number={item['season_number']}, version={item['version']}")
        logging.debug(f"Season pack: {season_pack}")
        logging.debug(f"Title: {title}")
        logging.debug(f"Magnet: {magnet}")

        # Log the current state of the Wanted and Scraping queues
        logging.debug(f"Current Wanted queue size: {len(self.queues['Wanted'])}")
        logging.debug(f"Current Scraping queue size: {len(self.queues['Scraping'])}")

        # Convert season_pack to a list of integers
        seasons = [int(s.strip()) for s in season_pack.split(',') if s.strip().isdigit()] if season_pack else [item['season_number']]

        # Find all matching episodes in the Wanted, Scraping, and Sleeping queues
        matching_items = [
            wanted_item for wanted_item in self.queues["Wanted"] + self.queues["Scraping"] + self.queues["Sleeping"]
            if (wanted_item['type'] == 'episode' and
                wanted_item['imdb_id'] == item['imdb_id'] and
                wanted_item['season_number'] in seasons and
                wanted_item['version'] == item['version'] and
                wanted_item['id'] != item['id'])
        ]

        logging.debug(f"Found {len(matching_items)} matching items in Wanted, Scraping and Sleeping queues")

        # Log details of matching items
        for match in matching_items:
            match_identifier = self.generate_identifier(match)
            logging.debug(f"Matching item: {match_identifier}")

        # Move matching items to Checking queue and add to UpgradingQueue
        moved_items = 0
        for matching_item in matching_items:
            matching_item_identifier = self.generate_identifier(matching_item)
            logging.debug(f"Updating state for item: {matching_item_identifier}")
            update_media_item_state(matching_item['id'], 'Checking', filled_by_title=title, filled_by_magnet=magnet)
            updated_matching_item = get_media_item_by_id(matching_item['id'])
            if updated_matching_item:
                self.queues["Checking"].append(updated_matching_item)

                # Add to UpgradingQueue
                self.upgrading_queue.add_item(updated_matching_item)
                updated_matching_item_identifier = self.generate_identifier(updated_matching_item)
                logging.debug(f"Added item {updated_matching_item_identifier} to UpgradingQueue")

                if matching_item in self.queues["Wanted"]:
                    self.queues["Wanted"].remove(matching_item)
                elif matching_item in self.queues["Scraping"]:
                    self.queues["Scraping"].remove(matching_item)
                elif matching_item in self.queues["Sleeping"]:
                    self.queues["Sleeping"].remove(matching_item)
                moved_items += 1
                logging.debug(f"Moved item {matching_item_identifier} to Checking queue and added to UpgradingQueue")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {matching_item['id']}")

        logging.info(f"Processed multi-pack: moved {moved_items} matching episodes to Checking queue and added to UpgradingQueue")
        logging.debug(f"Updated Wanted queue size: {len(self.queues['Wanted'])}")
        logging.debug(f"Updated Scraping queue size: {len(self.queues['Scraping'])}")
        logging.debug(f"Updated Checking queue size: {len(self.queues['Checking'])}")
        logging.debug(f"Updated UpgradingQueue size: {len(self.upgrading_queue.queue)}")
    
    def move_to_wanted(self, item):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Wanted queue")
        update_media_item_state(item['id'], 'Wanted', filled_by_title=None, filled_by_magnet=None)
        wanted_item = get_media_item_by_id(item['id'])
        if wanted_item:
            wanted_item_identifier = self.generate_identifier(wanted_item)
            self.queues["Wanted"].append(wanted_item)
            logging.debug(f"Successfully moved item {wanted_item_identifier} to Wanted queue")
        else:
            logging.error(f"Failed to retrieve wanted item for ID: {item['id']}")

    def move_to_sleeping(self, item):
        item_identifier = self.generate_identifier(item)
        logging.info(f"Moving item {item_identifier} to Sleeping queue")
        update_media_item_state(item['id'], 'Sleeping')
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            updated_item_identifier = self.generate_identifier(updated_item)
            self.queues["Sleeping"].append(updated_item)
            wake_count = self.wake_counts.get(item['id'], 0)  # Get existing count or default to 0
            self.wake_counts[item['id']] = wake_count  # Preserve the wake count
            self.sleeping_queue_times[item['id']] = datetime.now()
            logging.debug(f"Successfully moved item {updated_item_identifier} to Sleeping queue (Wake count: {wake_count})")
        else:
            logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def process_checking(self):
        logging.debug("Processing checking queue")
        current_time = time.time()

        # Process collected content from Plex
        collected_content = get_collected_from_plex('recent')
        if collected_content:
            add_collected_items(collected_content['movies'] + collected_content['episodes'], recent=True)

        # Process items in the Checking queue
        items_to_remove = []
        for item in self.queues['Checking']:
            item_identifier = self.generate_identifier(item)
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = current_time
            
            time_in_queue = current_time - self.checking_queue_times[item['id']]
            logging.debug(f"{item_identifier} has been in checking queue for {time_in_queue:.2f} seconds")

            if time_in_queue > 3600:  # 1 hour
                magnet = item.get('filled_by_magnet')
                if magnet:
                    add_to_not_wanted(magnet)
                    logging.info(f"Marked magnet as unwanted for item: {item_identifier}")

                update_media_item_state(item['id'], 'Wanted', filled_by_magnet=None)
                logging.info(f"Moving item back to Wanted: {item_identifier}")
                updated_item = get_media_item_by_id(item['id'])
                if updated_item:
                    updated_item_identifier = self.generate_identifier(updated_item)
                    self.queues['Wanted'].append(updated_item)
                    logging.debug(f"Successfully moved {updated_item_identifier} to Wanted queue")
                else:
                    logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
                
                items_to_remove.append(item)
                del self.checking_queue_times[item['id']]

        # Remove processed items from the Checking queue
        for item in items_to_remove:
            item_identifier = self.generate_identifier(item)
            self.queues['Checking'].remove(item)
            logging.debug(f"Removed {item_identifier} from Checking queue")

        # Clean up checking_queue_times for items no longer in the Checking queue
        for item_id in list(self.checking_queue_times.keys()):
            if item_id not in [item['id'] for item in self.queues['Checking']]:
                del self.checking_queue_times[item_id]
                logging.debug(f"Cleaned up checking_queue_times for item ID: {item_id}")

        logging.debug(f"Finished processing checking queue. Remaining items: {len(self.queues['Checking'])}")

    def process_sleeping(self):
        logging.debug("Processing sleeping queue")
        current_time = datetime.now()
        wake_limit = int(get_setting("Queue", "wake_limit", default=3))
        sleep_duration = timedelta(minutes=30)
        one_week_ago = current_time - timedelta(days=7)

        items_to_wake = []
        items_to_blacklist = []

        for item in self.queues["Sleeping"]:
            item_id = item['id']
            item_identifier = self.generate_identifier(item)
            logging.debug(f"Processing sleeping item: {item_identifier}")

            if item_id not in self.sleeping_queue_times:
                self.sleeping_queue_times[item_id] = current_time
                if item_id not in self.wake_counts:
                    self.wake_counts[item_id] = 0

            time_asleep = current_time - self.sleeping_queue_times[item_id]
            logging.debug(f"Item {item_identifier} has been asleep for {time_asleep}")
            logging.debug(f"Current wake count for item {item_identifier}: {self.wake_counts[item_id]}")

            release_date = item.get('release_date')
            if release_date and datetime.strptime(release_date, '%Y-%m-%d') < one_week_ago:
                items_to_blacklist.append(item)
                logging.debug(f"Adding {item_identifier} to items_to_blacklist list due to old release date")
            elif time_asleep >= sleep_duration:
                self.wake_counts[item_id] += 1
                logging.debug(f"Incremented wake count for {item_identifier} to {self.wake_counts[item_id]}")

                if self.wake_counts[item_id] > wake_limit:
                    items_to_blacklist.append(item)
                    logging.debug(f"Adding {item_identifier} to items_to_blacklist list due to exceeding wake limit")
                else:
                    items_to_wake.append(item)
                    logging.debug(f"Adding {item_identifier} to items_to_wake list")
            else:
                logging.debug(f"Item {item_identifier} hasn't slept long enough yet. Time left: {sleep_duration - time_asleep}")

        if items_to_wake:
            logging.info(f"Waking {len(items_to_wake)} items")
            self.wake_items(items_to_wake)
        else:
            logging.debug("No items to wake")

        if items_to_blacklist:
            logging.info(f"Blacklisting {len(items_to_blacklist)} items")
            self.blacklist_items(items_to_blacklist)
        else:
            logging.debug("No items to blacklist")

        # Clean up sleeping_queue_times and wake_counts for items no longer in the Sleeping queue
        for item_id in list(self.sleeping_queue_times.keys()):
            if item_id not in [item['id'] for item in self.queues["Sleeping"]]:
                del self.sleeping_queue_times[item_id]
                if item_id in self.wake_counts:
                    del self.wake_counts[item_id]
                logging.debug(f"Cleaned up sleeping_queue_times and wake_counts for item ID: {item_id}")

        logging.debug(f"Finished processing sleeping queue. Remaining items: {len(self.queues['Sleeping'])}")

    def wake_items(self, items):
        logging.debug(f"Attempting to wake {len(items)} items")
        for item in items:
            item_id = item['id']
            item_identifier = self.generate_identifier(item)
            wake_count = self.wake_counts.get(item_id, 0)
            logging.debug(f"Waking item: {item_identifier} (Wake count: {wake_count})")

            update_media_item_state(item_id, 'Wanted')
            updated_item = get_media_item_by_id(item_id)

            if updated_item:
                updated_item_identifier = self.generate_identifier(updated_item)
                self.queues["Wanted"].append(updated_item)
                self.queues["Sleeping"].remove(item)
                del self.sleeping_queue_times[item_id]
                # Don't delete the wake count, we want to keep track of it
                logging.info(f"Moved item {updated_item_identifier} from Sleeping to Wanted queue (Wake count: {wake_count})")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item_id}")

        logging.debug(f"Woke up {len(items)} items")
        logging.debug(f"Current Sleeping queue size: {len(self.queues['Sleeping'])}")
        logging.debug(f"Current Wanted queue size: {len(self.queues['Wanted'])}")

    def blacklist_items(self, items):
        for item in items:
            item_id = item['id']
            item_identifier = self.generate_identifier(item)
            update_media_item_state(item_id, 'Blacklisted')
            self.queues["Sleeping"].remove(item)
            del self.sleeping_queue_times[item_id]
            del self.wake_counts[item_id]  # Remove wake count for blacklisted items
            logging.info(f"Moved item {item_identifier} to Blacklisted state")
        
        logging.debug(f"Blacklisted {len(items)} items")
        logging.debug(f"Current Sleeping queue size: {len(self.queues['Sleeping'])}")
        logging.debug(f"Current Blacklisted queue size: {len(self.queues['Blacklisted'])}")