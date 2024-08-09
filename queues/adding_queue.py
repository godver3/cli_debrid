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
                    
                    cached_added = False
                    uncached_added = False

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

                            if add_result['status'] == 'cached':
                                cached_added = self.process_cached_torrent(queue_manager, item, title, link, add_result['files'])
                            else:
                                uncached_added = self.process_uncached_torrent(queue_manager, item, title, link, add_result['torrent_info'])

                            if uncached_handling == 'None' and (cached_added or uncached_added):
                                return
                            elif uncached_handling == 'Hybrid':
                                if cached_added:
                                    return
                                elif uncached_added:
                                    # Continue to look for a cached release
                                    continue
                        else:
                            logging.warning(f"Failed to add content to Real-Debrid for {item_identifier}: {add_result['message']}")

                    if not (cached_added or uncached_added):
                        logging.info(f"No suitable results found for {item_identifier}. Moving to Sleeping.")
                        queue_manager.move_to_sleeping(item, "Adding")
                else:
                    logging.error(f"No scrape results found for {item_identifier}")
                    queue_manager.move_to_sleeping(item, "Adding")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

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

    def add_to_real_debrid_helper(self, link: str, item_identifier: str, hash_value: str) -> Dict[str, Any]:
        try:
            logging.info(f"Processing link for {item_identifier}. Hash: {hash_value}")

            cache_status = is_cached_on_rd(hash_value)
            logging.info(f"Cache status for {item_identifier}: {cache_status}")

            result = add_to_real_debrid(link)
            
            if not result:
                return {"success": False, "message": "Failed to add content to Real-Debrid"}

            if isinstance(result, list):  # Cached torrent
                logging.info(f"Added cached content for {item_identifier}")
                torrent_files = get_magnet_files(link) if link.startswith('magnet:') else self.get_torrent_files(hash_value)
                return {
                    "success": True,
                    "message": "Cached torrent added successfully",
                    "status": "cached",
                    "links": result,
                    "files": torrent_files.get('cached_files', []) if torrent_files else []
                }
            elif result in ['downloading', 'queued']:  # Uncached torrent
                logging.info(f"Added uncached content for {item_identifier}. Status: {result}")
                
                # Poll for torrent status and retrieve file list
                torrent_info = self.get_torrent_info(hash_value)
                if torrent_info:
                    return {
                        "success": True,
                        "message": f"Uncached torrent added successfully. Status: {result}",
                        "status": "uncached",
                        "torrent_info": torrent_info
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

    def process_cached_torrent(self, queue_manager, item: Dict[str, Any], title: str, link: str, files: List[str]) -> bool:
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing cached torrent for item: {item_identifier}")
        logging.info(f"Files in cached torrent for {item_identifier}: {json.dumps(files, indent=2)}")

        if not files:
            logging.warning(f"No files found in cached torrent for {item_identifier}")
            return False

        if item['type'] == 'movie':
            if any(self.file_matches_item(file, item) for file in files):
                queue_manager.move_to_checking(item, "Adding", title, link)
                return True
            else:
                logging.warning(f"No matching file found for movie: {item_identifier}")
                return False
        else:
            return self.process_multi_pack(queue_manager, item, title, link, files)

    def process_uncached_torrent(self, queue_manager, item: Dict[str, Any], title: str, link: str, torrent_info: Dict[str, Any]) -> bool:
        item_identifier = queue_manager.generate_identifier(item)
        if 'files' in torrent_info:
            files = [file['path'] for file in torrent_info['files'] if file['selected'] == 1]
            logging.info(f"Files in uncached torrent for {item_identifier}: {json.dumps(files, indent=2)}")
            if item['type'] == 'movie':
                if any(self.file_matches_item(file, item) for file in files):
                    queue_manager.move_to_checking(item, "Adding", title, link)
                    return True
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
                    return False
            else:
                return self.process_multi_pack(queue_manager, item, title, link, files)
        else:
            logging.warning(f"No file information available for uncached torrent: {item_identifier}")
            return False

    def process_multi_pack(self, queue_manager, item: Dict[str, Any], title: str, link: str, files: List[str]) -> bool:
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing multi-pack for item: {item_identifier}")

        # Check if the original item matches any file
        original_item_matched = False
        for file_path in files:
            if self.file_matches_item(file_path, item):
                queue_manager.move_to_checking(item, "Adding", title, link)
                original_item_matched = True
                break

        if not original_item_matched:
            logging.warning(f"Original item {item_identifier} did not match any files in the torrent. Moving back to Scraping.")
            queue_manager.move_to_scraping(item, "Adding")
            return False

        # Find all matching episodes in the Wanted, Scraping, and Sleeping queues
        matching_items = []
        for queue_name in ["Wanted", "Scraping", "Sleeping"]:
            matching_items.extend([
                wanted_item for wanted_item in queue_manager.queues[queue_name].get_contents()
                if (wanted_item['type'] == 'episode' and
                    wanted_item['imdb_id'] == item['imdb_id'] and
                    wanted_item['version'] == item['version'] and
                    wanted_item['id'] != item['id'])  # Exclude the original item
            ])

        logging.debug(f"Found {len(matching_items)} potential matching items in Wanted, Scraping and Sleeping queues")

        # Match files with items
        moved_items = 0
        for file_path in files:
            for matching_item in matching_items:
                if self.file_matches_item(file_path, matching_item):
                    queue_manager.move_to_checking(matching_item, queue_manager.get_item_queue(matching_item), title, link)
                    matching_items.remove(matching_item)
                    moved_items += 1
                    break

        logging.info(f"Processed multi-pack: moved {moved_items} matching episodes to Checking queue")
        return True

    def file_matches_item(self, file_path: str, item: Dict[str, Any]) -> bool:
        file_name = file_path.split('/')[-1].lower()
        
        if item['type'] == 'movie':
            # For movies, check if the title and year are in the file name
            title_year_pattern = re.compile(re.escape(item['title'].lower()) + r'.*' + re.escape(str(item['year'])))
            return bool(title_year_pattern.search(file_name))
        elif item['type'] == 'episode':
            # For episodes, check for season and episode numbers
            season_pattern = f"s{item['season_number']:02d}"
            episode_pattern = f"e{item['episode_number']:02d}"
            return season_pattern in file_name and episode_pattern in file_name
        
        return False
