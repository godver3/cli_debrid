import logging
from datetime import datetime, date, timedelta
import time
from database import get_all_media_items, update_media_item_state, get_media_item_by_id, add_collected_items
from scraper.scraper import scrape
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet
from utilities.plex_functions import get_collected_from_plex
import pickle
import json
from settings import get_setting

class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def load_not_wanted_magnets():
    try:
        with open('db_content/not_wanted_magnets.pkl', 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        # If the file is empty or not a valid pickle object, return an empty set
        return set()
    except FileNotFoundError:
        # If the file does not exist, create it and return an empty set
        with open('db_content/not_wanted_magnets.pkl', 'wb') as f:
            pickle.dump(set(), f)
        return set()

def save_not_wanted_magnets(not_wanted_set):
    with open('db_content/not_wanted_magnets.pkl', 'wb') as f:
        pickle.dump(not_wanted_set, f)

def add_to_not_wanted(magnet):
    not_wanted = load_not_wanted_magnets()
    not_wanted.add(magnet)
    save_not_wanted_magnets(not_wanted)

def is_magnet_not_wanted(magnet):
    not_wanted = load_not_wanted_magnets()
    return magnet in not_wanted

class QueueManager:
    def __init__(self):
        self.queues = {
            "Wanted": [],
            "Scraping": [],
            "Adding": [],
            "Checking": [],
            "Sleeping": [],
            "Unreleased": []  # Add Unreleased state
        }
        self.scraping_cap = 5  # Cap for scraping queue
        self.checking_queue_times = {}
        self.sleeping_queue_times = {}
        self.wake_counts = {}
        self.update_all_queues()  # Initialize queues on startup

    def update_all_queues(self):
        logging.debug("Updating all queues")
        for state in self.queues.keys():
            items = get_all_media_items(state=state)
            self.queues[state] = [dict(row) for row in items]
        logging.debug(f"Queue contents after update: {self.get_queue_contents()}")

    def get_queue_contents(self):
        return {state: list(queue) for state, queue in self.queues.items()}

    def process_wanted(self):
        logging.debug("Processing wanted queue")
        current_date = datetime.now().date()
        seasons_in_scraping = set()
        # Get seasons already in Scraping queue
        for item in self.queues["Scraping"]:
            seasons_in_scraping.add((item['imdb_id'], item['season_number']))
        
        items_to_move = []
        items_to_unreleased = []
        
        # Process each item in the Wanted queue
        for item in list(self.queues["Wanted"]):
            try:
                # Check if release_date is None, empty string, or "Unknown"
                if not item['release_date'] or item['release_date'].lower() == "unknown":
                    logging.debug(f"Release date is missing or unknown for item: {item['title']}. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                    continue
                
                release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                if release_date > current_date:
                    logging.info(f"Item {item['title']} is not released yet. Moving to Unreleased state.")
                    items_to_unreleased.append(item)
                else:
                    # Check if we've reached the scraping cap
                    if len(self.queues["Scraping"]) + len(items_to_move) >= self.scraping_cap:
                        logging.debug(f"Scraping cap reached. Keeping {item['title']} in Wanted queue.")
                        break  # Exit the loop as we've reached the cap
                    # Check if we're already scraping an item from this season
                    season_key = (item['imdb_id'], item['season_number'])
                    if season_key in seasons_in_scraping:
                        logging.debug(f"Already scraping an item from season {item['season_number']} of {item['imdb_id']}. Keeping {item['title']} in Wanted queue.")
                        continue
                    if item['type'] == 'episode':
                        logging.info(f"Item {item['title']} S{item['season_number']}E{item['episode_number']} has been released. Marking for move to Scraping.")
                    else:
                        logging.info(f"Item {item['title']} ({item['year']}) has been released. Marking for move to Scraping.")
                    items_to_move.append(item)
                    seasons_in_scraping.add(season_key)
            except ValueError as e:
                logging.error(f"Error processing release date for item {item['title']}: {str(e)}")
                logging.error(f"Item details: {json.dumps(item, indent=2, cls=DateTimeEncoder)}")
                # Move to Unreleased state if there's an error processing the date
                items_to_unreleased.append(item)
            except Exception as e:
                logging.error(f"Unexpected error processing item {item['title']}: {str(e)}")
                logging.error(f"Item details: {json.dumps(item, indent=2, cls=DateTimeEncoder)}")
                # Move to Unreleased state if there's an unexpected error
                items_to_unreleased.append(item)
        
        # Move marked items to Scraping queue
        for item in items_to_move:
            self.queues["Wanted"].remove(item)
            update_media_item_state(item['id'], 'Scraping')
            updated_item = get_media_item_by_id(item['id'])
            if updated_item:
                self.queues["Scraping"].append(updated_item)
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
        
        # Move items to Unreleased state and remove from Wanted queue
        for item in items_to_unreleased:
            self.queues["Wanted"].remove(item)
            update_media_item_state(item['id'], 'Unreleased')
            logging.info(f"Moved item {item['title']} to Unreleased state and removed from Wanted queue.")
        
        logging.debug(f"Wanted queue processing complete. Items moved to Scraping queue: {len(items_to_move)}")
        logging.debug(f"Items moved to Unreleased state and removed from Wanted queue: {len(items_to_unreleased)}")
        logging.debug(f"Total items in Scraping queue: {len(self.queues['Scraping'])}")
        logging.debug(f"Remaining items in Wanted queue: {len(self.queues['Wanted'])}")

    def process_scraping(self):
        logging.debug(f"Processing scraping queue. Items: {len(self.queues['Scraping'])}")
        if self.queues["Scraping"]:
            item = self.queues["Scraping"].pop(0)
            try:
                # Check if the release date is today or earlier
                if item['release_date'] == 'Unknown':
                    logging.info(f"Item {item['title']} has an unknown release date. Moving back to Wanted queue.")
                    self.move_to_wanted(item)
                    return

                try:
                    release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                    today = date.today()

                    if release_date > today:
                        logging.info(f"Item {item['title']} has a future release date ({release_date}). Moving back to Wanted queue.")
                        self.move_to_wanted(item)
                        return
                except ValueError:
                    logging.warning(f"Item {item['title']} has an invalid release date format: {item['release_date']}. Moving back to Wanted queue.")
                    self.move_to_wanted(item)
                    return

                is_multi_pack = any(
                    wanted_item['type'] == 'episode' and
                    wanted_item['season_number'] == item['season_number'] and
                    wanted_item['id'] != item['id']
                    for wanted_item in self.queues["Wanted"]
                )

                if item['type'] == 'episode':
                    logging.info(f"Scraping for {item['title']} S{item['season_number']}E{item['episode_number']} ({item['year']})")
                else:
                    logging.info(f"Scraping for {item['title']} ({item['year']})")

                results = scrape(
                    item['imdb_id'],
                    item['title'],
                    item['year'],
                    item['type'],
                    item.get('season_number'),
                    item.get('episode_number'),
                    is_multi_pack
                )

                if not results:
                    wake_count = self.wake_counts.get(item['id'], 0)
                    logging.warning(f"No results returned for {item['title']}. Moving to Sleeping queue with wake count {wake_count}")
                    update_media_item_state(item['id'], 'Sleeping')
                    updated_item = get_media_item_by_id(item['id'])
                    if updated_item:
                        self.queues["Sleeping"].append(updated_item)
                        self.wake_counts[item['id']] = wake_count  # Preserve the wake count
                        self.sleeping_queue_times[item['id']] = datetime.now()
                        logging.debug(f"Set wake count for {item['title']} (ID: {item['id']}) to {wake_count}")
                    return

                # Filter out "not wanted" results
                filtered_results = [r for r in results if not is_magnet_not_wanted(r['magnet'])]
                logging.debug(f"Scrape results for {item['title']}: {len(filtered_results)} results after filtering")

                if filtered_results:
                    best_result = filtered_results[0]
                    logging.info(f"Best result for {item['title']}: {best_result['title']}, {best_result['magnet']}")
                    update_media_item_state(item['id'], 'Adding', filled_by_title=best_result['title'], scrape_results=filtered_results)
                    updated_item = get_media_item_by_id(item['id'])
                    if updated_item:
                        logging.debug(f"Updated {updated_item['id']}, title {updated_item['title']} (Scraping)")
                        self.queues["Adding"].append(updated_item)
                    else:
                        logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
                else:
                    if item['type'] == 'episode':
                        logging.warning(f"No valid results for {item['title']} S{item['season_number']}E{item['episode_number']} ({item['year']}) moving to Sleeping")
                    else:
                        logging.warning(f"No valid results for {item['title']} ({item['year']}), moving to Sleeping")
                    update_media_item_state(item['id'], 'Sleeping')
                    updated_item = get_media_item_by_id(item['id'])
                    if updated_item:
                        self.queues["Sleeping"].append(updated_item)
                    else:
                        logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
            except Exception as e:
                logging.error(f"Error processing item {item['title']}: {str(e)}", exc_info=True)

    def process_adding(self):
        logging.debug("Processing adding queue")
        if self.queues["Adding"]:
            item = self.queues["Adding"].pop(0)
            updated_item = get_media_item_by_id(item['id'])
            if updated_item:
                logging.debug(f"Updated {updated_item['id']}, title {updated_item['title']}, filled by {updated_item['filled_by_magnet']} (Adding)")
                scrape_results_str = updated_item.get('scrape_results', '')
                if scrape_results_str:
                    try:
                        scrape_results = json.loads(scrape_results_str)
                    except json.JSONDecodeError:
                        logging.error(f"Error parsing JSON scrape results for item (ID: {updated_item['id']})")
                        return
                    for result in scrape_results:
                        title = result.get('title', '')
                        magnet = result.get('magnet', '')
                        hash_value = extract_hash_from_magnet(magnet)
                        if hash_value:
                            cache_status = is_cached_on_rd(hash_value)
                            logging.debug(f"Cache status for {hash_value}: {cache_status}")
                            if hash_value in cache_status and cache_status[hash_value]:
                                logging.info(f"Item {updated_item['title']} is cached on Real-Debrid. Adding...")
                                add_to_real_debrid(magnet)
                                update_media_item_state(updated_item['id'], 'Checking', filled_by_magnet=magnet)
                                checked_item = get_media_item_by_id(updated_item['id'])
                                if checked_item:
                                    self.queues["Checking"].append(checked_item)
                                    logging.debug(f"Item {checked_item['title']} was a multi-pack result: {result['is_multi_pack']}")
                                    if result['is_multi_pack']:
                                        # Process multi-pack for all matching episodes
                                        self.process_multi_pack(checked_item, magnet)
                                else:
                                    logging.error(f"Failed to retrieve checked item for ID: {updated_item['id']}")
                                break
                            else:
                                logging.info(f"Item {updated_item['title']} is not cached on Real-Debrid. Moving to next result.")
                                add_to_not_wanted(magnet)
                        else:
                            logging.warning(f"Failed to extract hash from magnet link for {updated_item['title']}")
                    else:
                        logging.info(f"No cached results found for {updated_item['title']}. Moving back to Wanted.")
                        self.move_to_wanted(updated_item)
                else:
                    logging.error(f"No scrape results found for {updated_item['title']}.")
                    self.move_to_wanted(updated_item)
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def process_multi_pack(self, item, magnet):
        logging.debug(f"Starting process_multi_pack for item: {item['title']} (ID: {item['id']})")
        logging.debug(f"Item details: type={item['type']}, imdb_id={item['imdb_id']}, season_number={item['season_number']}")
        
        # Log the current state of the Wanted queue
        logging.debug(f"Current Wanted queue size: {len(self.queues['Wanted'])}")
        
        # Log details of all items in the Wanted queue
        logging.debug("Wanted queue contents:")
        for wanted_item in self.queues["Wanted"]:
            logging.debug(f"  ID: {wanted_item['id']}, Title: {wanted_item['title']}, Type: {wanted_item.get('type', 'N/A')}, "
                          f"IMDB ID: {wanted_item.get('imdb_id', 'N/A')}, Season: {wanted_item.get('season_number', 'N/A')}, "
                          f"Episode: {wanted_item.get('episode_number', 'N/A')}")
        
        # Find all matching episodes in the Wanted queue
        matching_items = [
            wanted_item for wanted_item in self.queues["Wanted"]
            if (wanted_item['type'] == 'episode' and
                wanted_item['imdb_id'] == item['imdb_id'] and
                wanted_item['season_number'] == item['season_number'] and
                wanted_item['id'] != item['id'])
        ]
        
        logging.debug(f"Found {len(matching_items)} matching items in Wanted queue")
        
        # Log details of matching items
        for match in matching_items:
            logging.debug(f"Matching item: ID={match['id']}, title={match['title']}, type={match['type']}, "
                          f"imdb_id={match['imdb_id']}, season={match['season_number']}, episode={match.get('episode_number', 'N/A')}")

        # Move matching items to Checking queue
        moved_items = 0
        for matching_item in matching_items:
            logging.debug(f"Updating state for item: {matching_item['id']}")
            update_media_item_state(matching_item['id'], 'Checking', filled_by_magnet=magnet)
            updated_matching_item = get_media_item_by_id(matching_item['id'])
            if updated_matching_item:
                self.queues["Checking"].append(updated_matching_item)
                self.queues["Wanted"].remove(matching_item)
                moved_items += 1
                logging.debug(f"Moved item {matching_item['id']} from Wanted to Checking queue")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {matching_item['id']}")

        logging.info(f"Processed multi-pack: moved {moved_items} matching episodes to Checking queue")
        logging.debug(f"Updated Wanted queue size: {len(self.queues['Wanted'])}")
        logging.debug(f"Updated Checking queue size: {len(self.queues['Checking'])}")

    def move_to_wanted(self, item):
        update_media_item_state(item['id'], 'Wanted', filled_by_title=None, filled_by_magnet=None)
        wanted_item = get_media_item_by_id(item['id'])
        if wanted_item:
            self.queues["Wanted"].append(wanted_item)
        else:
            logging.error(f"Failed to retrieve wanted item for ID: {item['id']}")

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
            if item['id'] not in self.checking_queue_times:
                self.checking_queue_times[item['id']] = current_time
            
            time_in_queue = current_time - self.checking_queue_times[item['id']]
            logging.debug(f"{item['title']} has been in checking queue for {time_in_queue}")

            if time_in_queue > 9000:  # 30 minutes = 1800 seconds
                magnet = item.get('filled_by_magnet')
                if magnet:
                    add_to_not_wanted(magnet)
                    logging.info(f"Marked magnet as unwanted for item: {item['title']}")

                update_media_item_state(item['id'], 'Wanted', filled_by_magnet=None)
                logging.info(f"Moving item back to Wanted: {item['title']}")
                self.queues['Wanted'].append(item)
                items_to_remove.append(item)
                del self.checking_queue_times[item['id']]

        # Remove processed items from the Checking queue
        for item in items_to_remove:
            self.queues['Checking'].remove(item)

        # Clean up checking_queue_times for items no longer in the Checking queue
        for item_id in list(self.checking_queue_times.keys()):
            if item_id not in [item['id'] for item in self.queues['Checking']]:
                del self.checking_queue_times[item_id]

        logging.debug(f"Finished processing checking queue. Remaining items: {len(self.queues['Checking'])}")

    def process_sleeping(self):
        logging.debug("Processing sleeping queue")
        current_time = datetime.now()
        wake_limit = int(get_setting("Queue", "wake_limit", default=3))
        sleep_duration = timedelta(minutes=30)

        items_to_wake = []
        items_to_blacklist = []

        for item in self.queues["Sleeping"]:
            item_id = item['id']
            logging.debug(f"Processing sleeping item: {item['title']} (ID: {item_id})")

            if item_id not in self.sleeping_queue_times:
                self.sleeping_queue_times[item_id] = current_time
                if item_id not in self.wake_counts:
                    self.wake_counts[item_id] = 0

            time_asleep = current_time - self.sleeping_queue_times[item_id]
            logging.debug(f"Item {item['title']} (ID: {item_id}) has been asleep for {time_asleep}")
            logging.debug(f"Current wake count for item {item['title']} (ID: {item_id}): {self.wake_counts[item_id]}")

            if time_asleep >= sleep_duration:
                self.wake_counts[item_id] += 1
                logging.debug(f"Incremented wake count for {item['title']} (ID: {item_id}) to {self.wake_counts[item_id]}")

                if self.wake_counts[item_id] > wake_limit:
                    items_to_blacklist.append(item)
                    logging.debug(f"Adding {item['title']} (ID: {item_id}) to items_to_blacklist list")
                else:
                    items_to_wake.append(item)
                    logging.debug(f"Adding {item['title']} (ID: {item_id}) to items_to_wake list")
            else:
                logging.debug(f"Item {item['title']} (ID: {item_id}) hasn't slept long enough yet. Time left: {sleep_duration - time_asleep}")

        if items_to_wake:
            logging.debug(f"Waking {len(items_to_wake)} items")
            self.wake_items(items_to_wake)
        else:
            logging.debug("No items to wake")

        if items_to_blacklist:
            logging.debug(f"Blacklisting {len(items_to_blacklist)} items")
            self.blacklist_items(items_to_blacklist)
        else:
            logging.debug("No items to blacklist")

        # Clean up sleeping_queue_times and wake_counts for items no longer in the Sleeping queue
        for item_id in list(self.sleeping_queue_times.keys()):
            if item_id not in [item['id'] for item in self.queues["Sleeping"]]:
                del self.sleeping_queue_times[item_id]
                if item_id in self.wake_counts:
                    del self.wake_counts[item_id]

        logging.debug(f"Finished processing sleeping queue. Remaining items: {len(self.queues['Sleeping'])}")

    def wake_items(self, items):
        logging.debug(f"Attempting to wake {len(items)} items")
        for item in items:
            item_id = item['id']
            wake_count = self.wake_counts.get(item_id, 0)
            logging.debug(f"Waking item: {item['title']} (ID: {item_id}, Wake count: {wake_count})")
            
            update_media_item_state(item['id'], 'Wanted')
            updated_item = get_media_item_by_id(item['id'])
            
            if updated_item:
                self.queues["Wanted"].append(updated_item)
                self.queues["Sleeping"].remove(item)
                del self.sleeping_queue_times[item_id]
                # Don't delete the wake count, we want to keep track of it
                logging.info(f"Moved item {item['title']} (ID: {item_id}) from Sleeping to Wanted queue (Wake count: {wake_count})")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item_id}")
        
        logging.debug(f"Woke up {len(items)} items")
        logging.debug(f"Current Sleeping queue size: {len(self.queues['Sleeping'])}")
        logging.debug(f"Current Wanted queue size: {len(self.queues['Wanted'])}")

    def blacklist_items(self, items):
        for item in items:
            update_media_item_state(item['id'], 'Blacklisted')
            self.queues["Sleeping"].remove(item)
            del self.sleeping_queue_times[item['id']]
            del self.wake_counts[item['id']]  # Remove wake count for blacklisted items
            logging.info(f"Moved item {item['title']} to Blacklisted state")
