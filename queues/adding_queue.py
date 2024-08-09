import logging
import json
import time
from typing import Dict, Any, List
import requests
import hashlib
import bencodepy
import re

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet, get_magnet_files, get, API_BASE_URL
from not_wanted_magnets import add_to_not_wanted

class AddingQueue:
    def __init__(self):
        self.items = []
        self.api_key = get_setting("RealDebrid", "api_key")
        if not self.api_key:
            logging.error("Real-Debrid API key not found in settings")
            raise ValueError("Real-Debrid API key not found in settings")

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

        queue_manager.move_to_sleeping(item, "Adding")

    def process_none_mode(self, queue_manager, item, scrape_results):
        item_identifier = queue_manager.generate_identifier(item)
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

            if is_cached_on_rd(current_hash):
                add_result = self.add_to_real_debrid_helper(link, item_identifier, current_hash)
                if add_result['success']:
                    self.add_to_not_wanted(current_hash, item_identifier)
                    if self.process_torrent(queue_manager, item, title, link, add_result):
                        return
                    else:
                        self.remove_unwanted_torrent(add_result['torrent_id'])

        queue_manager.move_to_sleeping(item, "Adding")

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

        logging.info(f"No suitable results found for {item_identifier} in hybrid mode.")
        queue_manager.move_to_sleeping(item, "Adding")

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
                    queue_manager.move_to_checking(item, "Adding", title, link)
                    logging.debug(f"Moved movie {item_identifier} to Checking queue")
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
                queue_manager.move_to_checking(item, "Adding", title, link)
                logging.debug(f"Moved original item {item_identifier} to Checking queue")
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
                        queue_manager.move_to_checking(matching_item, current_queue, title, link)
                        logging.debug(f"Moved item {item_id} to Checking queue")
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
            # For movies, check if the title and year are in the file name
            title_year_pattern = re.compile(re.escape(item['title'].lower()) + r'.*' + re.escape(str(item['year'])))
            match = bool(title_year_pattern.search(file_name))
            logging.debug(f"Movie match result: {match}")
            return match
        elif item['type'] == 'episode':
            # For episodes, check for season and episode numbers
            season_pattern = f"s{item['season_number']:02d}"
            episode_pattern = f"e{item['episode_number']:02d}"
            match = season_pattern in file_name and episode_pattern in file_name
            logging.debug(f"Episode match result: {match}")
            return match

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
