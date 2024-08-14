import logging
import json
import time
from typing import Dict, Any, List
import requests
import hashlib
import bencodepy
import re
from datetime import datetime, timedelta
import os

from database import get_all_media_items, get_media_item_by_id, update_media_item_state
from settings import get_setting
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet, get_magnet_files, get, API_BASE_URL
from not_wanted_magnets import add_to_not_wanted
from scraper.scraper import scrape

class AddingQueue:
    def __init__(self):
        self.items = []
        self.api_key = get_setting("RealDebrid", "api_key")
        if not self.api_key:
            logging.error("Real-Debrid API key not found in settings")
            #raise ValueError("Real-Debrid API key not found in settings")

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Adding")]

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        logging.debug("Processing adding queue")
        if self.items:
            item = self.items[0]  # Process the first item in the queue
            item_identifier = queue_manager.generate_identifier(item)
            updated_item = get_media_item_by_id(item['id'])
            if updated_item:
                logging.debug(f"Processing item: {item_identifier}")
                scrape_results_str = updated_item.get('scrape_results', '')
                if scrape_results_str:
                    try:
                        scrape_results = json.loads(scrape_results_str)
                    except json.JSONDecodeError:
                        logging.error(f"Error parsing JSON scrape results for item: {item_identifier}")
                        queue_manager.move_to_sleeping(item, "Adding")
                        return

                    uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None')

                    if uncached_handling == 'Full':
                        self.process_full_mode(queue_manager, item, scrape_results)
                    elif uncached_handling == 'None':
                        self.process_none_mode(queue_manager, item, scrape_results)
                    elif uncached_handling == 'Hybrid':
                        self.process_hybrid_mode(queue_manager, item, scrape_results)
                    else:
                        logging.error(f"Unknown uncached_handling setting: {uncached_handling}")
                        queue_manager.move_to_sleeping(item, "Adding")
                else:
                    logging.error(f"No scrape results found for {item_identifier}")
                    queue_manager.move_to_sleeping(item, "Adding")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def process_full_mode(self, queue_manager, item, scrape_results):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing full mode for item: {item_identifier}")

        for result in scrape_results:
            title = result.get('title', '')
            link = result.get('magnet', '')

            if link.startswith('magnet:'):
                current_hash = extract_hash_from_magnet(link)
            else:
                current_hash = self.download_and_extract_hash(link)

            if not current_hash:
                logging.warning(f"Failed to extract hash from link for {item_identifier}")
                continue

            add_result = self.add_to_real_debrid_helper(link, item_identifier, current_hash)

            if add_result['success']:
                self.add_to_not_wanted(current_hash, item_identifier)
                if self.process_torrent(queue_manager, item, title, link, add_result):
                    return
                else:
                    self.remove_unwanted_torrent(add_result['torrent_id'])

        # If we've gone through all results without success, try individual episode scraping
        if item['type'] == 'episode':
            logging.info(f"No matching files found for {item_identifier}. Attempting individual episode scraping.")
            individual_results = self.scrape_individual_episode(item)
            if individual_results:
                for result in individual_results:
                    if self.process_single_result(queue_manager, item, result):
                        return

        # If we've gone through all results without success, handle as a failed item
        self.handle_failed_item(queue_manager, item, "Adding")

    def process_none_mode(self, queue_manager, item, scrape_results):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing none mode for item: {item_identifier}")

        for result in scrape_results:
            title = result.get('title', '')
            link = result.get('magnet', '')

            if link.startswith('magnet:'):
                current_hash = extract_hash_from_magnet(link)
            else:
                current_hash = self.download_and_extract_hash(link)

            if not current_hash:
                logging.warning(f"Failed to extract hash from link for {item_identifier}")
                continue

            cache_status = is_cached_on_rd(current_hash)

            # Only proceed if the content is cached
            if cache_status[current_hash]:
                add_result = self.add_to_real_debrid_helper(link, item_identifier, current_hash)
                if add_result['success']:
                    self.add_to_not_wanted(current_hash, item_identifier)
                    if self.process_torrent(queue_manager, item, title, link, add_result):
                        return
                    else:
                        self.remove_unwanted_torrent(add_result['torrent_id'])

        # If we've gone through all results without success, try individual episode scraping
        if item['type'] == 'episode':
            logging.info(f"No matching cached files found for {item_identifier}. Attempting individual episode scraping.")
            individual_results = self.scrape_individual_episode(item)
            if individual_results:
                for result in individual_results:
                    if self.process_single_result(queue_manager, item, result, cached_only=True):
                        return

        # If we've gone through all results without success, handle as a failed item
        self.handle_failed_item(queue_manager, item, "Adding")

    def process_hybrid_mode(self, queue_manager, item, scrape_results):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing hybrid mode for item: {item_identifier}")

        # First pass: look for cached results
        for result in scrape_results:
            title = result.get('title', '')
            link = result.get('magnet', '')

            if link.startswith('magnet:'):
                current_hash = extract_hash_from_magnet(link)
            else:
                current_hash = self.download_and_extract_hash(link)

            if not current_hash:
                logging.warning(f"Failed to extract hash from link for {item_identifier}")
                continue

            is_cached = is_cached_on_rd(current_hash)
            logging.debug(f"Cache status for {current_hash}: {is_cached}")

            if is_cached:
                add_result = self.add_to_real_debrid_helper(link, item_identifier, current_hash, add_if_uncached=False)
                if add_result['success']:
                    self.add_to_not_wanted(current_hash, item_identifier)
                    if self.process_torrent(queue_manager, item, title, link, add_result):
                        return
                    else:
                        self.remove_unwanted_torrent(add_result['torrent_id'])
                        continue  # Try the next cached result if this one didn't work

        # Second pass: look for uncached results
        logging.debug(f"No cached results found for {item_identifier}. Searching for uncached results.")
        for result in scrape_results:
            title = result.get('title', '')
            link = result.get('magnet', '')

            if link.startswith('magnet:'):
                current_hash = extract_hash_from_magnet(link)
            else:
                current_hash = self.download_and_extract_hash(link)

            if not current_hash:
                logging.warning(f"Failed to extract hash from link for {item_identifier}")
                continue

            add_result = self.add_to_real_debrid_helper(link, item_identifier, current_hash, add_if_uncached=True)
            if add_result['success']:
                self.add_to_not_wanted(current_hash, item_identifier)
                if self.process_torrent(queue_manager, item, title, link, add_result):
                    return
                else:
                    self.remove_unwanted_torrent(add_result['torrent_id'])

        # If we've gone through all results without success, try individual episode scraping
        if item['type'] == 'episode':
            logging.info(f"No matching files found for {item_identifier}. Attempting individual episode scraping.")
            individual_results = self.scrape_individual_episode(item)
            if individual_results:
                # First try cached results from individual scraping
                for result in individual_results:
                    if self.process_single_result(queue_manager, item, result, cached_only=True):
                        return
                # Then try uncached results from individual scraping
                for result in individual_results:
                    if self.process_single_result(queue_manager, item, result, cached_only=False):
                        return

        # If we've gone through all results without success, handle as a failed item
        self.handle_failed_item(queue_manager, item, "Adding")

    def handle_failed_item(self, queue_manager, item, from_queue):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Handling failed item: {item_identifier}")
        
        if self.is_item_old(item):
            self.blacklist_old_season_items(item, queue_manager)
        else:
            queue_manager.move_to_sleeping(item, from_queue)

    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if 'release_date' not in item or item['release_date'] == 'Unknown':
            logging.info(f"Item {self.generate_identifier(item)} has no release date or unknown release date. Considering it as old.")
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            return (datetime.now().date() - release_date).days > 7
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old

    def blacklist_old_season_items(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Blacklisting item {item_identifier} and related old season items with the same version")

        # Blacklist the current item
        self.blacklist_item(item, queue_manager)

        # Find and blacklist related items in the same season with the same version that are also old
        related_items = self.find_related_season_items(item, queue_manager)
        for related_item in related_items:
            if self.is_item_old(related_item) and related_item['version'] == item['version']:
                self.blacklist_item(related_item, queue_manager)
            else:
                logging.debug(f"Not blacklisting {queue_manager.generate_identifier(related_item)} as it's either not old enough or has a different version")

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

    def process_torrent(self, queue_manager, item, title, link, add_result):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing torrent for item: {item_identifier}")
        logging.debug(f"Add result: {json.dumps(add_result, indent=2)}")

        if 'files' in add_result:
            files = [file for file in add_result['files'] if not file.endswith('.txt')]  # Exclude .txt files
            logging.info(f"Files in torrent for {item_identifier}: {json.dumps(files, indent=2)}")

            if item['type'] == 'movie':
                matching_files = [file for file in files if self.file_matches_item(file, item)]
                if matching_files:
                    logging.info(f"Matching file(s) found for movie: {item_identifier}")
                    filled_by_file = os.path.basename(matching_files[0])  # Get the filename
                    queue_manager.move_to_checking(item, "Adding", title, link, filled_by_file)
                    logging.debug(f"Moved movie {item_identifier} to Checking queue with filled_by_file: {filled_by_file}")
                    return True
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
                    return False
            else:
                return self.process_multi_pack(queue_manager, item, title, link, files)
        else:
            logging.warning(f"No file information available for torrent: {item_identifier}")
            return False

    def download_and_extract_hash(self, url: str) -> str:
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            torrent_content = response.content
            
            # Decode the torrent file
            torrent_data = bencodepy.decode(torrent_content)
            
            # Extract the info dictionary
            info = torrent_data[b'info']
            
            # Encode the info dictionary
            encoded_info = bencodepy.encode(info)
            
            # Calculate the SHA1 hash
            return hashlib.sha1(encoded_info).hexdigest()
        except Exception as e:
            logging.error(f"Error downloading or processing torrent file: {str(e)}")
            return None

    def add_to_real_debrid_helper(self, link: str, item_identifier: str, hash_value: str, add_if_uncached: bool = True) -> Dict[str, Any]:
        try:
            logging.info(f"Processing link for {item_identifier}. Hash: {hash_value}")

            cache_status = is_cached_on_rd(hash_value)
            logging.info(f"Cache status for {item_identifier}: {cache_status}")

            if not cache_status[hash_value] and not add_if_uncached:
                return {"success": False, "message": "Skipping uncached content in first pass of hybrid mode"}

            result = add_to_real_debrid(link)
            logging.debug(f"add_to_real_debrid result: {result}")

            if not result:
                return {"success": False, "message": "Failed to add content to Real-Debrid"}

            if isinstance(result, list):  # Cached torrent
                logging.info(f"Added cached content for {item_identifier}")
                torrent_files = get_magnet_files(link) if link.startswith('magnet:') else self.get_torrent_files(hash_value)
                logging.debug(f"Torrent files: {json.dumps(torrent_files, indent=2)}")
                torrent_info = self.get_torrent_info(hash_value)
                logging.debug(f"Torrent info: {json.dumps(torrent_info, indent=2)}")
                return {
                    "success": True,
                    "message": "Cached torrent added successfully",
                    "status": "cached",
                    "links": result,
                    "files": torrent_files.get('cached_files', []) if torrent_files else [],
                    "torrent_id": torrent_info.get('id') if torrent_info else None
                }
            elif result in ['downloading', 'queued']:  # Uncached torrent
                logging.info(f"Added uncached content for {item_identifier}. Status: {result}")

                # Poll for torrent status and retrieve file list
                torrent_info = self.get_torrent_info(hash_value)
                logging.debug(f"Torrent info for uncached torrent: {json.dumps(torrent_info, indent=2)}")
                if torrent_info:
                    files = [file['path'] for file in torrent_info.get('files', []) if file.get('selected') == 1]
                    return {
                        "success": True,
                        "message": f"Uncached torrent added successfully. Status: {result}",
                        "status": "uncached",
                        "torrent_info": torrent_info,
                        "files": files,
                        "torrent_id": torrent_info.get('id')
                    }
                else:
                    return {"success": False, "message": "Failed to retrieve torrent information"}
            else:
                return {"success": False, "message": f"Unexpected result from Real-Debrid: {result}"}

        except Exception as e:
            logging.error(f"Error adding content to Real-Debrid for {item_identifier}: {str(e)}")
            return {"success": False, "message": f"Error: {str(e)}"}

    def get_torrent_info(self, hash_value: str) -> Dict[str, Any] or None:
        headers = {'Authorization': f'Bearer {self.api_key}'}

        for _ in range(10):  # Try for about 100 seconds
            time.sleep(10)
            torrents = requests.get(f"{API_BASE_URL}/torrents", headers=headers).json()
            for torrent in torrents:
                if torrent['hash'].lower() == hash_value.lower():
                    torrent_id = torrent['id']
                    info_url = f"{API_BASE_URL}/torrents/info/{torrent_id}"
                    return requests.get(info_url, headers=headers).json()

        logging.warning(f"Could not find torrent info for hash: {hash_value}")
        return None

    def get_torrent_files(self, hash_value: str) -> Dict[str, List[str]] or None:
        torrent_info = self.get_torrent_info(hash_value)
        if torrent_info and 'files' in torrent_info:
            return {'cached_files': [file['path'] for file in torrent_info['files']]}
        return None

    def add_to_not_wanted(self, hash_value: str, item_identifier: str):
        try:
            add_to_not_wanted(hash_value)
            logging.info(f"Added hash {hash_value} to not_wanted_magnets for {item_identifier}")
        except Exception as e:
            logging.error(f"Error adding hash {hash_value} to not_wanted_magnets for {item_identifier}: {str(e)}")

    def process_cached_torrent(self, queue_manager, item, title, link, files, hybrid_flag=None):
        try:
            item_identifier = queue_manager.generate_identifier(item)
            logging.debug(f"Entered process_cached_torrent for item: {item_identifier}")
            logging.debug(f"Title: {title}")
            logging.debug(f"Link: {link}")
            logging.debug(f"Hybrid flag: {hybrid_flag}")
            logging.debug(f"Number of files: {len(files)}")

            if not files:
                logging.warning(f"No files found in cached torrent for {item_identifier}")
                return False

            logging.debug(f"Files in cached torrent: {json.dumps(files[:5], indent=2)}...")  # Log first 5 files

            if item['type'] == 'movie':
                logging.debug(f"Processing movie item: {item_identifier}")
                matching_files = [file for file in files if self.file_matches_item(file, item)]
                logging.debug(f"Matching files for movie: {matching_files}")
                if matching_files:
                    logging.info(f"Matching file(s) found for movie: {item_identifier}")
                    queue_manager.move_to_checking(item, "Adding", title, link)
                    logging.debug(f"Moved movie {item_identifier} to Checking queue")
                    return True
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
                    return False
            else:
                logging.debug(f"Processing TV show item: {item_identifier}")
                result = self.process_multi_pack(queue_manager, item, title, link, files, hybrid_flag)
                logging.debug(f"process_multi_pack result for {item_identifier}: {result}")
                return result
        except Exception as e:
            logging.error(f"Exception in process_cached_torrent for {item_identifier}: {str(e)}")
            logging.exception("Traceback:")
            return False

    def process_uncached_torrent(self, queue_manager, item, title, link, add_result, hybrid_flag=None):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing uncached torrent for item: {item_identifier}")
        logging.debug(f"Add result: {json.dumps(add_result, indent=2)}")

        if 'files' in add_result:
            files = [file for file in add_result['files'] if not file.endswith('.txt')]  # Exclude .txt files
            logging.info(f"Files in uncached torrent for {item_identifier}: {json.dumps(files, indent=2)}")
            
            if item['type'] == 'movie':
                matching_files = [file for file in files if self.file_matches_item(file, item)]
                if matching_files:
                    logging.info(f"Matching file(s) found for movie: {item_identifier}")
                    if hybrid_flag:
                        queue_manager.move_to_wanted(item, "Adding", hybrid_flag=hybrid_flag)
                        logging.debug(f"Moved movie {item_identifier} to Wanted queue with hybrid_flag={hybrid_flag}")
                    else:
                        queue_manager.move_to_checking(item, "Adding", title, link)
                        logging.debug(f"Moved movie {item_identifier} to Checking queue")
                    return True
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
                    return False
            else:
                return self.process_multi_pack(queue_manager, item, title, link, files, hybrid_flag)
        else:
            logging.warning(f"No file information available for uncached torrent: {item_identifier}")
            return False

    def process_multi_pack(self, queue_manager, item, title, link, files):
        try:
            item_identifier = queue_manager.generate_identifier(item)
            logging.debug(f"Entered process_multi_pack for item: {item_identifier}")
            logging.debug(f"Title: {title}")
            logging.debug(f"Link: {link}")
            logging.debug(f"Number of files: {len(files)}")

            # Check if the original item matches any file
            matching_files = [file for file in files if self.file_matches_item(file, item)]
            if matching_files:
                logging.debug(f"Original item matched files: {matching_files}")
                filled_by_file = os.path.basename(matching_files[0])  # Get the filename
                queue_manager.move_to_checking(item, "Adding", title, link, filled_by_file)
                logging.debug(f"Moved original item {item_identifier} to Checking queue with filled_by_file: {filled_by_file}")
            else:
                logging.warning(f"Original item {item_identifier} did not match any files in the torrent.")
                return False

            # Find all matching episodes in the Wanted, Scraping, and Sleeping queues
            matching_items = []
            for queue_name in ["Wanted", "Scraping", "Sleeping"]:
                queue_items = queue_manager.queues[queue_name].get_contents()
                matching_items.extend([
                    wanted_item for wanted_item in queue_items
                    if (wanted_item['type'] == 'episode' and
                        wanted_item['imdb_id'] == item['imdb_id'] and
                        wanted_item['version'] == item['version'] and
                        wanted_item['id'] != item['id'])  # Exclude the original item
                ])
            logging.debug(f"Found {len(matching_items)} potential matching items in other queues")

            # Match files with items
            moved_items = 0
            for file_path in files:
                for matching_item in matching_items:
                    if self.file_matches_item(file_path, matching_item):
                        item_id = queue_manager.generate_identifier(matching_item)
                        logging.debug(f"Matched file {file_path} with item {item_id}")
                        current_queue = queue_manager.get_item_queue(matching_item)
                        filled_by_file = os.path.basename(file_path)  # Get the filename
                        queue_manager.move_to_checking(matching_item, current_queue, title, link, filled_by_file)
                        logging.debug(f"Moved item {item_id} to Checking queue with filled_by_file: {filled_by_file}")
                        matching_items.remove(matching_item)
                        moved_items += 1
                        break

            logging.info(f"Processed multi-pack: moved {moved_items + 1} matching episodes (including original item) to Checking queue")
            return True
        except Exception as e:
            logging.error(f"Exception in process_multi_pack for {item_identifier}: {str(e)}")
            logging.exception("Traceback:")
            return False
            
    def file_matches_item(self, file_path: str, item: Dict[str, Any]) -> bool:
        logging.debug(f"Checking if file {file_path} matches item {item.get('id')}")
        file_name = file_path.split('/')[-1].lower()
        
        if item['type'] == 'movie':
            # For movies, check if the file has a common video extension
            common_video_extensions = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv')
            if file_name.endswith(common_video_extensions):
                logging.debug(f"Movie file match found: {file_name}")
                return True
            logging.debug(f"No match found for movie file: {file_name}")
            return False
        elif item['type'] == 'episode':
            # For episodes, use a more flexible regex pattern
            season_number = item['season_number']
            episode_number = item['episode_number']
            
            # Improved pattern to match various season and episode formats
            pattern = rf'(?i)(?:s0*{season_number}(?:[.-]?|\s*)e0*{episode_number}|' \
                      rf'0*{season_number}x0*{episode_number}|' \
                      rf'season\s*0*{season_number}\s*episode\s*0*{episode_number}|' \
                      rf'(?<!\d)0*{season_number}x0*{episode_number}(?!\d))'
            
            if re.search(pattern, file_name):
                logging.debug(f"Episode match found: {file_name}")
                return True
            
            # If no match found, try matching with the episode title (if available)
            if 'name' in item:
                episode_title = item['name'].lower()
                if episode_title in file_name:
                    logging.debug(f"Episode match found by title: {file_name}")
                    return True
            
            logging.debug(f"No match found for episode file: {file_name}")
            return False
        
        logging.debug(f"No match found for file {file_path}")
        return False

    def remove_unwanted_torrent(self, torrent_id):
        try:
            headers = {'Authorization': f'Bearer {self.api_key}'}
            response = requests.delete(f"{API_BASE_URL}/torrents/delete/{torrent_id}", headers=headers)
            response.raise_for_status()
            logging.info(f"Successfully removed unwanted torrent with ID: {torrent_id}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error removing unwanted torrent with ID {torrent_id}: {str(e)}")

    def move_to_blacklist_if_older_than_7_days(self, queue_manager, item, from_queue: str):
        item_identifier = queue_manager.generate_identifier(item)
        release_date_str = item.get('release_date')
        
        if release_date_str:
            release_date = datetime.strptime(release_date_str, "%Y-%m-%d")
            if release_date < datetime.now() - timedelta(days=7):
                logging.info(f"Item {item_identifier} is older than 7 days. Moving to blacklist.")
                queue_manager.move_to_blacklisted(item, from_queue)
                return True
        
        return False

    def process_single_result(self, queue_manager, item, result, cached_only=False):
        item_identifier = queue_manager.generate_identifier(item)
        title = result.get('title', '')
        link = result.get('magnet', '')

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

        if add_result['success']:
            self.add_to_not_wanted(current_hash, item_identifier)
            if self.process_torrent(queue_manager, item, title, link, add_result):
                return True
            else:
                self.remove_unwanted_torrent(add_result['torrent_id'])

        return False

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
            multi=False  # Force individual episode scraping
        )
        return results  # We're not using filtered_out here, but you could if needed

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")
