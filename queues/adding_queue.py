import logging
import json
import time
from typing import Dict, Any, List
from api_tracker import api
import hashlib
import bencodepy
from datetime import datetime
import os
from database import get_all_media_items, get_media_item_by_id, update_media_item_state
from settings import get_setting
from debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet, get_magnet_files, get_active_downloads, get_debrid_provider
from not_wanted_magnets import add_to_not_wanted, is_magnet_not_wanted, get_not_wanted_magnets, is_url_not_wanted, add_to_not_wanted_urls, get_not_wanted_urls
from scraper.scraper import scrape
from database.database_reading import get_all_season_episode_counts
from guessit import guessit
import functools
from .anime_matcher import AnimeMatcher
import functools
import tempfile

class AddingQueue:
    def __init__(self):
        self.items = []
        self.api_key = None
        self.episode_count_cache = {}  
        self.anime_matcher = AnimeMatcher(self.calculate_absolute_episode)
        self.debrid_provider = get_debrid_provider()

    def get_api_key(self):
        return get_setting('RealDebrid', 'api_key')

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Adding")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        if self.items:
            item = self.items[0]  
            item_identifier = queue_manager.generate_identifier(item)
            updated_item = get_media_item_by_id(item['id'])
            if updated_item:
                logging.debug(f"Processing item: {item_identifier}")
                scrape_results_str = updated_item.get('scrape_results', '')
                if scrape_results_str:
                    try:
                        if isinstance(scrape_results_str, str):
                            scrape_results = json.loads(scrape_results_str)
                        elif isinstance(scrape_results_str, list):
                            scrape_results = scrape_results_str
                        else:
                            raise ValueError(f"Unexpected scrape_results type: {type(scrape_results_str)}")
                        
                        if not scrape_results:
                            logging.warning(f"Empty scrape results for item: {item_identifier}")
                            self.handle_failed_item(queue_manager, item, "Adding")
                            return
                    except (json.JSONDecodeError, ValueError) as e:
                        logging.error(f"Error parsing scrape results for item: {item_identifier}. Error: {str(e)}")
                        self.handle_failed_item(queue_manager, item, "Adding")
                        return

                    uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None').lower()

                    self.process_item(queue_manager, item, scrape_results, uncached_handling)
                else:
                    logging.error(f"No scrape results found for {item_identifier}")
                    self.handle_failed_item(queue_manager, item, "Adding")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
                self.handle_failed_item(queue_manager, item, "Adding")

    def process_item(self, queue_manager, item, scrape_results, mode, upgrade=False):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing {mode} mode for item: {item_identifier}")
        logging.debug(f"Scrape results type: {type(scrape_results)}")
        logging.debug(f"Scrape results content: {scrape_results}")

        if isinstance(scrape_results, str):
            try:
                scrape_results = json.loads(scrape_results)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse scrape_results JSON for {item_identifier}")
                scrape_results = []

        if not isinstance(scrape_results, list):
            logging.error(f"Unexpected scrape_results format for {item_identifier}. Expected list, got {type(scrape_results)}")
            scrape_results = []

        logging.debug(f"Total scrape results: {len(scrape_results)}")

        for index, result in enumerate(scrape_results):
            if isinstance(result, dict):
                logging.info(f"Index: {index} - Scrape result: {result.get('title', 'No title')}")
            elif isinstance(result, str):
                logging.info(f"Index: {index} - Scrape result (string): {result[:50]}...")  
            else:
                logging.info(f"Index: {index} - Scrape result type: {type(result)}")
    
        if get_setting('Debug', 'sort_by_uncached_status'):
            scrape_results = self.sort_results_by_cache_status(scrape_results)
    
        uncached_results = []  
        cached_successfully_processed = False
        for index, result in enumerate(scrape_results):
            title = result.get('title', '')
            link = result.get('magnet', '')
            temp_file_path = None
            
            try:
                if not link:
                    logging.warning(f"No magnet link found for result {index + 1}: {item_identifier}")
                    continue
    
                if link.startswith('magnet:'):
                    current_hash = extract_hash_from_magnet(link)
                else:
                    logging.info(f"Link: {link}")
                    logging.info(f"is_url_not_wanted: {is_url_not_wanted(link)}")
                    if is_url_not_wanted(link):
                        logging.info(f"URL {link} for {item_identifier} is in not_wanted_urls. Skipping.")
                        continue
                    current_hash, temp_file_path = self.download_and_extract_hash(link)
    
                if not current_hash:
                    logging.warning(f"Failed to extract hash from link for result {index + 1}: {item_identifier}")
                    continue
    
                cache_status = is_cached_on_rd(current_hash)
                logging.debug(f"Cache status for result {index + 1}: {cache_status}")
                
                # Check if any hash in the response is True (cached)
                is_cached = any(status for status in cache_status.values())
                
                logging.debug(f"Is cached: {is_cached}")
    
                if mode == 'none' and not is_cached:
                    logging.debug(f"Skipping uncached result {index + 1} for {item_identifier}")
                    continue
    
                if mode == 'hybrid':
                    if is_cached:
                        logging.info(f"Found cached result {index + 1} for {item_identifier}")
                        success = self.process_result(queue_manager, item, result, current_hash, is_cached=True, scrape_results=scrape_results)
                        if success:
                            cached_successfully_processed = True
                            return True
                    else:
                        uncached_results.append((result, current_hash))
                    continue
    
                logging.info(f"Processing result {index + 1} for {item_identifier}. Cached: {is_cached}")
                if temp_file_path:  
                    success = self.process_result(queue_manager, item, result, current_hash, is_cached=is_cached, scrape_results=scrape_results, temp_file_path=temp_file_path)
                else:
                    success = self.process_result(queue_manager, item, result, current_hash, is_cached=is_cached, scrape_results=scrape_results)
                
                if success:
                    logging.info(f"Successfully processed result {index + 1} for {item_identifier}")
                    self.add_to_not_wanted(current_hash, item_identifier, item)
                    self.add_to_not_wanted_url(link, item_identifier, item)
                    logging.info(f"Added to not_wanted_url {link} for {item_identifier}")
                    return True
    
            except Exception as e:
                logging.error(f"Error processing result {index + 1} for {item_identifier}: {str(e)}")
                continue
    
        # If we're in hybrid mode and found uncached results but no cached results were successfully processed
        if mode == 'hybrid' and not cached_successfully_processed and uncached_results:
            logging.info(f"No acceptable cached results found for {item_identifier}. Processing uncached results.")
            for result, current_hash in uncached_results:
                success = self.process_result(queue_manager, item, result, current_hash, is_cached=False, scrape_results=scrape_results)
                if success:
                    return True  
    
        # If we reach here, no results were successfully processed
        logging.warning(f"No valid results found for {item_identifier}")
    
        # For episodes, try individual episode scraping
        if item['type'] == 'episode' and not upgrade:
            logging.info(f"Attempting individual episode scraping for {item_identifier}")
            individual_results = self.scrape_individual_episode(item)
            logging.debug(f"Individual episode scraping returned {len(individual_results)} results")
            
            for index, result in enumerate(individual_results):
                logging.debug(f"Processing individual result {index + 1}")
                
                # Get torrent ID before processing
                link = result.get('magnet', '')
                if link.startswith('magnet:'):
                    current_hash = extract_hash_from_magnet(link)
                else:
                    current_hash, _ = self.download_and_extract_hash(link)
                    
                # Try to get torrent ID from the result or add_result
                torrent_id = result.get('torrent_id')
                
                if self.process_single_result(queue_manager, item, result, cached_only=(mode == 'none')):
                    logging.info(f"Successfully processed individual result {index + 1} for {item_identifier}")
                    return True
                else:
                    # Check if failure was due to not_wanted - if so, continue to next result
                    link = result.get('magnet', '')
                    if link.startswith('magnet:'):
                        current_hash = extract_hash_from_magnet(link)
                    else:
                        current_hash, _ = self.download_and_extract_hash(link)
                        
                    if current_hash and is_magnet_not_wanted(current_hash):
                        logging.debug(f"Skipping result {index + 1} as it's in not_wanted, trying next result")
                        continue
                        
                    logging.debug(f"Failed to process individual result {index + 1} for {item_identifier}")
                    # Clean up the torrent if we have an ID
                    if torrent_id:
                        logging.debug(f"Removing failed torrent {torrent_id}")
                        self.remove_unwanted_torrent(torrent_id)
                    # If we don't have a torrent_id but have a hash, try to find and remove the torrent
                    elif current_hash:
                        torrent_info = self.get_torrent_info(current_hash)
                        if torrent_info and 'id' in torrent_info:
                            logging.debug(f"Removing failed torrent {torrent_info['id']} found by hash")
                            self.remove_unwanted_torrent(torrent_info['id'])

        logging.warning(f"No results successfully processed for {item_identifier}")
        
        if not upgrade:
            self.handle_failed_item(queue_manager, item, "Adding")

        return False

    def sort_results_by_cache_status(self, scrape_results):
        def get_cache_status(result):
            link = result.get('magnet', '')
            if link.startswith('magnet:'):
                current_hash = extract_hash_from_magnet(link)
            else:
                current_hash = self.download_and_extract_hash(link)
            
            if current_hash:
                cache_status = is_cached_on_rd(current_hash)
                return cache_status.get(current_hash, False)
            return False

        # Add cache status to each result
        for result in scrape_results:
            result['is_cached'] = get_cache_status(result)

        # Sort results: uncached first, then cached
        return sorted(scrape_results, key=lambda x: x['is_cached'])


    def process_torrent(self, queue_manager, item, title, link, add_result):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing torrent for item: {item_identifier}")
        logging.debug(f"Add result: {json.dumps(add_result, indent=2)}")

        files = add_result.get('files', [])
        if not files and 'links' in add_result:
            files = add_result['links'].get('files', [])

        torrent_id = add_result.get('torrent_id')
        logging.debug(f"Torrent ID: {torrent_id}")

        if files:
            logging.info(f"Files in torrent for {item_identifier}: {json.dumps(files, indent=2)}")

            if item['type'] == 'movie':
                matching_files = [file for file in files if self.file_matches_item(file['path'] if isinstance(file, dict) else file, item)]
                if matching_files:
                    logging.info(f"Matching file(s) found for movie: {item_identifier}")
                    filled_by_file = os.path.basename(matching_files[0]['path'] if isinstance(matching_files[0], dict) else matching_files[0])
                    queue_manager.move_to_checking(item, "Adding", title, link, filled_by_file, torrent_id)
                    logging.debug(f"Moved movie {item_identifier} to Checking queue with filled_by_file: {filled_by_file}")
                    return True, torrent_id
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
                    return False, torrent_id
            else:  
                success, message = self.process_multi_pack(queue_manager, item, title, link, files, torrent_id)
                if success:
                    logging.info(f"Successfully processed TV show item: {item_identifier}. {message}")
                    return True, torrent_id
                else:
                    logging.warning(f"Failed to process TV show item: {item_identifier}. {message}")
                    return False, torrent_id
        else:
            logging.warning(f"No file information available for torrent: {item_identifier}")
            return False, torrent_id

    def process_result(self, queue_manager, item, result, current_hash, is_cached, scrape_results, temp_file_path=None):
        item_identifier = queue_manager.generate_identifier(item)
        title = result.get('title', '')
        link = result.get('magnet', '')
        mode = get_setting('Scraping', 'uncached_content_handling', 'None').lower()

        torrent_id = result.get('torrent_id')
        logging.info(f"Torrent ID: {torrent_id}")
        logging.info(f"Link: {link}")

        if not link:
            logging.error(f"No magnet link found for {item_identifier}")
            return False
        
        # Check if the hash is in the not wanted list
        if is_magnet_not_wanted(current_hash):
            logging.info(f"Hash {current_hash} for {item_identifier} is in not_wanted_magnets. Skipping.")
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
            return False

        add_result = add_to_real_debrid(link, temp_file_path)
        
        # Clean up the temporary file
        if temp_file_path and os.path.exists(temp_file_path):
            os.unlink(temp_file_path)

        if add_result:
            if isinstance(add_result, dict):
                status = add_result.get('status')
                torrent_id = add_result.get('torrent_id') or add_result.get('id')
                
                # Always remove non-downloaded torrents unless in full mode
                if status != 'downloaded' and mode != 'full':
                    logging.info(f"Removing non-downloaded torrent (status: {status}) for {item_identifier}")
                    if torrent_id:
                        self.remove_unwanted_torrent(torrent_id)
                        logging.debug(f"Removed torrent {torrent_id} due to non-downloaded status")
                        # Add to not_wanted since we've determined it's not usable
                        self.add_to_not_wanted(current_hash, item_identifier, item)
                        self.add_to_not_wanted_url(link, item_identifier, item)
                    return False

                # Process only if downloaded or in full mode
                if status == 'downloaded' or mode == 'full':
                    logging.info(f"Processing {'cached' if status == 'downloaded' else 'uncached'} content for {item_identifier}")
                    if status == 'downloaded':  # Only add to not_wanted if it's actually downloaded
                        self.add_to_not_wanted(current_hash, item_identifier, item)
                        self.add_to_not_wanted_url(link, item_identifier, item)
                    success, returned_torrent_id = self.process_torrent(queue_manager, item, title, link, add_result)
                    
                    if success:
                        return True
                    else:
                        logging.warning(f"Failed to process torrent for {item_identifier}")
                        logging.debug(f"Torrent ID: {returned_torrent_id or torrent_id}")
                        self.remove_unwanted_torrent(returned_torrent_id or torrent_id)
                        return False

            # Handle unexpected result type
            logging.warning(f"Unexpected result type from Real-Debrid for {item_identifier}: {type(add_result)}")
            if torrent_id:
                self.remove_unwanted_torrent(torrent_id)
        else:
            logging.error(f"Failed to add torrent to Real-Debrid for {item_identifier}")

        return False

    def move_related_season_items(self, queue_manager, item, season_pack, title, link):
        item_identifier = queue_manager.generate_identifier(item)
        
        if not isinstance(season_pack, list):
            season_pack = [season_pack]
        
        
        for season in season_pack:
            for queue_name in ['Wanted', 'Scraping']:
                queue = queue_manager.queues[queue_name]
                queue_contents = queue.get_contents()

                for queue_item in queue_contents:
                    item_identifier = queue_manager.generate_identifier(queue_item)

                related_items = [
                    related_item for related_item in queue_contents
                    if (related_item.get('type') == 'episode' and
                        related_item.get('imdb_id') == item.get('imdb_id') and
                        str(related_item.get('season_number')) == str(season))
                ]
                
                logging.info(f"Found {len(related_items)} related items in {queue_name} queue for season {season}")
                
                for related_item in related_items:
                    related_identifier = queue_manager.generate_identifier(related_item)
                    logging.info(f"Moving related item {related_identifier} to Pending Uncached Additions queue.")
                    queue_manager.move_to_pending_uncached(related_item, queue_name, title, link)
                
                if not related_items:
                    logging.info(f"No related items found in {queue_name} queue for season {season}")
        
    def handle_failed_item(self, queue_manager, item, from_queue):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Handling failed item: {item_identifier}")
        
        if self.is_item_old(item):
            self.blacklist_old_season_items(item, queue_manager)
        else:
            queue_manager.move_to_sleeping(item, from_queue)

    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if 'release_date' not in item or not item['release_date']:
            logging.info(f"Item {self.generate_identifier(item)} has no release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            return (datetime.now().date() - release_date).days > 7
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  

    def blacklist_old_season_items(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Blacklisting item {item_identifier} and related old season items with the same version")

        # Check if the item is in the Checking queue before blacklisting
        if queue_manager.get_item_queue(item) != 'Checking':
            self.blacklist_item(item, queue_manager)
        else:
            logging.info(f"Skipping blacklisting of {item_identifier} as it's already in Checking queue")

        # Find and blacklist related items in the same season with the same version that are also old
        related_items = self.find_related_season_items(item, queue_manager)
        for related_item in related_items:
            related_identifier = queue_manager.generate_identifier(related_item)
            if self.is_item_old(related_item) and related_item['version'] == item['version']:
                # Check if the related item is in the Checking queue before blacklisting
                if queue_manager.get_item_queue(related_item) != 'Checking':
                    self.blacklist_item(related_item, queue_manager)
                else:
                    logging.info(f"Skipping blacklisting of {related_identifier} as it's already in Checking queue")
            else:
                logging.debug(f"Not blacklisting {related_identifier} as it's either not old enough or has a different version")

    def find_related_season_items(self, item: Dict[str, Any], queue_manager) -> List[Dict[str, Any]]:
        related_items = []
        if item['type'] == 'episode':
            for queue in queue_manager.queues.values():
                for queue_item in queue.get_contents():
                    if (queue_item['type'] == 'episode' and
                        queue_item['imdb_id'] == item['imdb_id'] and
                        queue_item['season_number'] == item['season_number'] and
                        queue_item['id'] != item['id'] and
                        queue_item['version'] == item['version']):
                        related_items.append(queue_item)
        return related_items

    def blacklist_item(self, item: Dict[str, Any], queue_manager):
        item_id = item['id']
        item_identifier = queue_manager.generate_identifier(item)
        update_media_item_state(item_id, 'Blacklisted')

        # Remove from current queue and add to blacklisted queue
        current_queue_name = queue_manager.get_item_queue(item)
        if current_queue_name:
            current_queue = queue_manager.queues.get(current_queue_name)
            if current_queue:
                current_queue.remove_item(item)
            else:
                logging.error(f"Queue {current_queue_name} not found in queue_manager")
        else:
            logging.warning(f"Item {item_identifier} not found in any queue")

        queue_manager.queues['Blacklisted'].add_item(item)

        logging.info(f"Moved item {item_identifier} to Blacklisted state")
      
    def download_and_extract_hash(self, url: str) -> tuple:
        return self.debrid_provider.download_and_extract_hash(url)

    def get_torrent_info(self, hash_value: str) -> Dict[str, Any] or None:
        return self.debrid_provider.get_torrent_info(hash_value)

    def get_torrent_files(self, hash_value: str) -> Dict[str, List[str]] or None:
        return self.debrid_provider.get_torrent_files(hash_value)

    def remove_unwanted_torrent(self, torrent_id):
        return self.debrid_provider.remove_torrent(torrent_id)

    def add_to_not_wanted(self, hash_value: str, item_identifier: str, item: Dict[str, Any]):
        identifier = self.generate_identifier(item)
        if not self.is_item_past_24h(item):
            logging.info(f"Not adding hash {hash_value} to not_wanted_magnets for {item_identifier} as it's less than 24h old")
            return

        add_to_not_wanted(hash_value, item_identifier, item)
        logging.info(f"Added hash {hash_value} to not_wanted_magnets for {item_identifier}")
        
        # Add this line to log the current contents of the not_wanted list
        #logging.debug(f"Current not_wanted_magnets: {get_not_wanted_magnets()}")
    
    def add_to_not_wanted_url(self, url: str, item_identifier: str, item: Dict[str, Any]):
        identifier = self.generate_identifier(item)
        if not self.is_item_past_24h(item):
            logging.info(f"Not adding URL {url} to not_wanted_urls for {item_identifier} as it's less than 24h old")
            return

        add_to_not_wanted_urls(url)
        logging.info(f"Added URL {url} to not_wanted_urls for {item_identifier}")
        
        # Add this line to log the current contents of the not_wanted list
        #logging.debug(f"Current not_wanted_urls: {get_not_wanted_urls()}")
    
    def is_item_past_24h(self, item: Dict[str, Any]) -> bool:
        identifier = self.generate_identifier(item)
        
        # Get latest state from database since item in memory might be outdated
        current_item = get_media_item_by_id(item['id'])
        if current_item and current_item.get('state') == 'Checking':
            logging.info(f"Item {identifier} is in Checking queue - allowing not_wanted additions")
            return True
        
        # First try using airtime as it's most accurate
        if 'airtime' in item and item['airtime'] and 'release_date' in item and item['release_date']:
            try:
                # Combine release date with airtime
                release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
                air_time = datetime.strptime(item['airtime'], '%H:%M').time()
                airtime = datetime.combine(release_date, air_time)
                current_time = datetime.now()
                
                time_since_air = current_time - airtime
                hours_since_air = time_since_air.total_seconds() / 3600
                
                logging.info(f"Item {identifier} - Using airtime - Air time: {airtime}, Current time: {current_time}")
                logging.info(f"Hours since air: {hours_since_air:.1f}")
                
                is_past_24h = hours_since_air >= 24
                if not is_past_24h:
                    logging.info(f"Item {identifier} is too new ({hours_since_air:.1f} hours since air time). Not adding to not_wanted.")
                return is_past_24h
            except (ValueError, TypeError) as e:
                logging.warning(f"Error parsing airtime for {identifier}: {e}. Falling back to release date.")
        
        # Fall back to release date if no airtime or error parsing airtime
        if 'release_date' not in item or not item['release_date']:
            logging.info(f"Item {identifier} has no release date. Considering it past 24h.")
            return True
        
        try:
            # Assume release is at midnight of the release date
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d')
            current_time = datetime.now()
            
            # Calculate hours since release
            time_since_release = current_time - release_date
            hours_since_release = time_since_release.total_seconds() / 3600
            
            logging.info(f"Item {identifier} - Using release date - Release date: {release_date}, Current time: {current_time}")
            logging.info(f"Hours since release: {hours_since_release:.1f}")
            
            is_past_24h = hours_since_release >= 24
            if not is_past_24h:
                logging.info(f"Item {identifier} is too new ({hours_since_release:.1f} hours old). Not adding to not_wanted.")
            return is_past_24h
            
        except (ValueError, TypeError) as e:
            logging.error(f"Error checking if item {identifier} is past 24h: {e}")
            return False

    def process_multi_pack(self, queue_manager, item, title, link, files, torrent_id):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Processing multi-pack for item: {item_identifier}")

        # Get all matching items from other queues
        matching_items = self.get_matching_items_from_queues(queue_manager, item)
        matching_items.append(item)  
        logging.info(f"Total matching items (including original): {len(matching_items)}")

        # Check if the item is an anime
        is_anime = 'anime' in item.get('genres', [])

        if is_anime:
            # Use AnimeMatcher for anime series
            logging.info("Using AnimeMatcher to match files to items")
            matches = self.anime_matcher.match_anime_files(files, matching_items)
        else:
            # Use regular matching for non-anime TV shows
            logging.info("Using regular matching for non-anime TV show")
            matches = self.match_regular_tv_show(files, matching_items)

        if matches is None:
            logging.warning("No matches found")
            return False, "No matching episodes found"

        # Process matches
        for file, matched_item in matches:
            filled_by_file = os.path.basename(file)
            current_queue = queue_manager.get_item_queue(matched_item)
            queue_manager.move_to_checking(matched_item, current_queue, title, link, filled_by_file, torrent_id)
            logging.info(f"Moved item {queue_manager.generate_identifier(matched_item)} to Checking queue with filled_by_file: {filled_by_file}")

        if matches:
            return True, f"Moved {len(matches)} matching episodes to Checking queue"
        else:
            logging.warning("No matches found")
            return False, "No matching episodes found"

    def match_regular_tv_show(self, files, items):
        matches = []
        for file in files:
            file_path = file['path'] if isinstance(file, dict) else file
            try:
                file_info = guessit(file_path)
                file_season = file_info.get('season')
                file_episodes = file_info.get('episode')
                
                # Convert to list if it's a single episode
                if isinstance(file_episodes, int):
                    file_episodes = [file_episodes]
                elif file_episodes is None:
                    continue  # Skip files with no episode info
                
                for item in items:
                    item_season = int(item['season_number'])
                    item_episode = int(item['episode_number'])
                    
                    if file_season == item_season and item_episode in file_episodes:
                        matches.append((file_path, item))
                        
                        # For multi-episode files, we need to find all matching items
                        if len(file_episodes) > 1:
                            self.handle_multi_episode_file(file_path, file_season, file_episodes, items, matches)
                        
                        break  
            except Exception as e:
                logging.error(f"Error parsing file {file_path}: {str(e)}")
                continue
        
        return matches

    def handle_multi_episode_file(self, file_path, file_season, file_episodes, items, matches):
        """Handle matching multiple episodes in a single file"""
        for episode in file_episodes:
            for item in items:
                item_season = int(item['season_number'])
                item_episode = int(item['episode_number'])
                
                if file_season == item_season and item_episode == episode:
                    # Don't add if this item is already matched
                    if not any(matched_item == item for _, matched_item in matches):
                        matches.append((file_path, item))
                    break

    def get_matching_items_from_queues(self, queue_manager, item):
        matching_items = []
        for queue_name in ["Wanted", "Scraping", "Sleeping", "Unreleased"]:
            queue_items = queue_manager.queues[queue_name].get_contents()
            matching_items.extend([
                wanted_item for wanted_item in queue_items
                if (wanted_item['type'] == 'episode' and
                    wanted_item['imdb_id'] == item['imdb_id'] and
                    wanted_item['version'] == item['version'] and
                    wanted_item['id'] != item['id'])
            ])
        return matching_items
           
    def file_matches_item(self, file: str, item: Dict[str, Any]) -> bool:
        filename = os.path.basename(file)
        logging.debug(f"Analyzing filename: {filename}")

        guess = guessit(filename)
        logging.debug(f"Guessit result: {guess}")

        if 'sample' in guess.get('other', []):
            logging.debug(f"Skipping sample file: {filename}")
            return False

        if item['type'] == 'movie':
            return self.match_movie(guess, item, filename)
        elif item['type'] == 'episode':
            return self.match_episode(guess, item)

        logging.debug(f"No match found for {filename}")
        return False

    def is_video_file(self, filename: str) -> bool:
        video_extensions = ['.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm']
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    def match_movie(self, guess: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        if self.is_video_file(filename):
            logging.debug(f"Video file match found: {filename}")
            return True
        
        logging.debug(f"Not a video file: {filename}")
        return False

    def match_episode(self, guess: Dict[str, Any], item: Dict[str, Any]) -> bool:
        if guess.get('type') != 'episode':
            return False

        season_match = guess.get('season') == int(item['season_number'])
        episode_match = guess.get('episode') == int(item['episode_number'])

        if season_match and episode_match:
            logging.debug(f"Episode match found: {guess.get('title')} S{guess.get('season', '')}E{guess.get('episode', '')}")
            return True

        return False


    def process_single_result(self, queue_manager, item, result, cached_only=False):
        item_identifier = queue_manager.generate_identifier(item)
        title = result.get('title', '')
        link = result.get('magnet', '')
        torrent_id = None  

        try:
            if link.startswith('magnet:'):
                current_hash = extract_hash_from_magnet(link)
            else:
                current_hash = self.download_and_extract_hash(link)

            if not current_hash:
                logging.warning(f"Failed to extract hash from link for {item_identifier}")
                return False

            is_cached = is_cached_on_rd(current_hash)
            if cached_only and not is_cached:
                return False

            add_result = self.add_to_real_debrid_helper(link, item_identifier, current_hash)
            torrent_id = add_result.get('torrent_id')  

            if add_result['success']:
                status = add_result.get('status')

                # Always remove non-downloaded torrents unless in full mode
                if status != 'downloaded' and cached_only:
                    logging.info(f"Removing non-downloaded torrent (status: {status}) for {item_identifier}")
                    if torrent_id:
                        self.remove_unwanted_torrent(torrent_id)
                        logging.debug(f"Removed torrent {torrent_id} due to non-downloaded status")
                        # Add to not_wanted since we've determined it's not usable
                        self.add_to_not_wanted(current_hash, item_identifier, item)
                        self.add_to_not_wanted_url(link, item_identifier, item)
                    return False

                # Process only if downloaded or not in cached_only mode
                if status == 'downloaded' or not cached_only:
                    if self.process_torrent(queue_manager, item, title, link, add_result):
                        return True
                    else:
                        logging.warning(f"Failed to process torrent for {item_identifier}")
                        if torrent_id:
                            self.remove_unwanted_torrent(torrent_id)
                            logging.debug(f"Removed torrent {torrent_id} due to processing failure")
                        return False

            else:
                # Handle failed add_result
                if torrent_id:
                    logging.warning(f"Failed to add torrent for {item_identifier}")
                    self.remove_unwanted_torrent(torrent_id)
                    logging.debug(f"Removed torrent {torrent_id} due to add_result failure")
                return False

        except Exception as e:
            logging.error(f"Error in process_single_result for {item_identifier}: {str(e)}")
            # Ensure cleanup happens even if an exception occurs
            if torrent_id:
                self.remove_unwanted_torrent(torrent_id)
                logging.debug(f"Removed torrent {torrent_id} due to exception")
            return False

    def calculate_absolute_episode(self, item: Dict[str, Any]) -> int:
        season_number = int(item['season_number'])
        episode_number = int(item['episode_number'])
        tmdb_id = item['tmdb_id']

        # Check if we have cached the episode counts for this series
        if tmdb_id not in self.episode_count_cache:
            self.episode_count_cache[tmdb_id] = self.get_season_episode_counts(tmdb_id)

        season_episode_counts = self.episode_count_cache[tmdb_id]

        return sum(season_episode_counts.get(s, 0) for s in range(1, season_number)) + episode_number
    
    @functools.lru_cache(maxsize=1000)
    def get_season_episode_counts(self, tmdb_id: str) -> Dict[int, int]:
        return get_all_season_episode_counts(tmdb_id)

    def scrape_individual_episode(self, item):
        logging.info(f"Performing individual episode scrape for {self.generate_identifier(item)}")
        results, filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            multi=False,
            genres=item['genres']
        )
        return results  

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")