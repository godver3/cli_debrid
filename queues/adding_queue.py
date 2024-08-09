import logging
import json
import time
from typing import Dict, Any, List
import requests

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet, get_magnet_files
from not_wanted_magnets import add_to_not_wanted

class AddingQueue:
    def __init__(self):
        self.items = []

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
                    first_uncached_added = False
                    cached_added = False
                    processed_hashes = set()

                    for result in scrape_results:
                        title = result.get('title', '')
                        link = result.get('magnet', '')

                        current_hash = extract_hash_from_magnet(link)
                        if not current_hash:
                            logging.warning(f"Failed to extract hash from link for {item_identifier}")
                            continue
                        if current_hash in processed_hashes:
                            logging.info(f"Skipping duplicate hash {current_hash} for {item_identifier}")
                            continue
                        processed_hashes.add(current_hash)

                        add_result = self.add_to_real_debrid_helper(link, item_identifier)

                        if add_result['success']:
                            if add_result['status'] == 'cached':
                                cached_added = True
                                self.process_cached_torrent(queue_manager, item, title, link, add_result['files'])
                            else:
                                first_uncached_added = True
                                self.process_uncached_torrent(queue_manager, item, title, link, add_result['torrent_info'])

                            if uncached_handling == 'None' or (uncached_handling == 'Hybrid' and cached_added):
                                return
                        else:
                            logging.warning(f"Failed to add content to Real-Debrid for {item_identifier}: {add_result['message']}")

                    if not (cached_added or first_uncached_added):
                        logging.info(f"No suitable results found for {item_identifier}. Moving to Sleeping.")
                        queue_manager.move_to_sleeping(item, "Adding")
                else:
                    logging.error(f"No scrape results found for {item_identifier}")
                    queue_manager.move_to_sleeping(item, "Adding")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def add_to_real_debrid_helper(self, link: str, item_identifier: str) -> Dict[str, Any]:
        try:
            hash_value = extract_hash_from_magnet(link)
            if not hash_value:
                return {"success": False, "message": "Failed to extract hash from magnet link"}

            logging.info(f"Processing magnet link for {item_identifier}. Hash: {hash_value}")

            cache_status = is_cached_on_rd(hash_value)
            logging.info(f"Cache status for {item_identifier}: {cache_status}")

            result = add_to_real_debrid(link)
            
            if not result:
                return {"success": False, "message": "Failed to add content to Real-Debrid"}

            if isinstance(result, list):  # Cached torrent
                logging.info(f"Added cached content for {item_identifier}")
                torrent_files = get_magnet_files(link)
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
        api_key = get_setting("RealDebrid", "api_key")
        if not api_key:
            logging.error("Real-Debrid API key not found in settings")
            return None

        headers = {'Authorization': f'Bearer {api_key}'}
        api_base_url = "https://api.real-debrid.com/rest/1.0"

        for _ in range(10):  # Try for about 100 seconds
            time.sleep(10)
            torrents = requests.get(f"{api_base_url}/torrents", headers=headers).json()
            for torrent in torrents:
                if torrent['hash'].lower() == hash_value.lower():
                    torrent_id = torrent['id']
                    info_url = f"{api_base_url}/torrents/info/{torrent_id}"
                    return requests.get(info_url, headers=headers).json()

        logging.warning(f"Could not find torrent info for hash: {hash_value}")
        return None

    def process_cached_torrent(self, queue_manager, item: Dict[str, Any], title: str, link: str, files: List[str]):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing cached torrent for item: {item_identifier}")
        logging.info(f"Files in cached torrent for {item_identifier}: {json.dumps(files, indent=2)}")

        if not files:
            logging.warning(f"No files found in cached torrent for {item_identifier}")
            queue_manager.move_to_scraping(item, "Adding")
            return

        if item['type'] == 'movie':
            if any(self.file_matches_item(file, item) for file in files):
                queue_manager.move_to_checking(item, "Adding", title, link)
            else:
                logging.warning(f"No matching file found for movie: {item_identifier}")
                queue_manager.move_to_scraping(item, "Adding")
        else:
            self.process_multi_pack(queue_manager, item, title, link, files)

    def process_uncached_torrent(self, queue_manager, item: Dict[str, Any], title: str, link: str, torrent_info: Dict[str, Any]):
        item_identifier = queue_manager.generate_identifier(item)
        if 'files' in torrent_info:
            files = [file['path'] for file in torrent_info['files'] if file['selected'] == 1]
            logging.info(f"Files in uncached torrent for {item_identifier}: {json.dumps(files, indent=2)}")
            if item['type'] == 'movie':
                if any(self.file_matches_item(file, item) for file in files):
                    queue_manager.move_to_checking(item, "Adding", title, link)
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
            else:
                self.process_multi_pack(queue_manager, item, title, link, files)
        else:
            logging.warning(f"No file information available for uncached torrent: {item_identifier}")

    def process_multi_pack(self, queue_manager, item: Dict[str, Any], title: str, link: str, files: List[str]) -> None:
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
            return  # Exit the function as we've moved the item back to Scraping

        # Find all matching episodes in the Wanted, Scraping, and Sleeping queues
        matching_items = []
        for queue_name in ["Wanted", "Scraping", "Sleeping"]:
            matching_items.extend([
                wanted_item for wanted_item in queue_manager.queues[queue_name].get_contents()
                if (wanted_item['type'] == 'episode' and
                    wanted_item['imdb_id'] == item['imdb_id'] and
                    wanted_item['season_number'] == item['season_number'] and
                    wanted_item['version'] == item['version'] and
                    wanted_item['id'] != item['id'])  # Exclude the original item
            ])

        logging.debug(f"Found {len(matching_items)} potential matching items in Wanted, Scraping and Sleeping queues")

        # Match files with items
        for file_path in files:
            for matching_item in matching_items:
                if self.file_matches_item(file_path, matching_item):
                    queue_manager.move_to_checking(matching_item, queue_manager.get_item_queue(matching_item), title, link)
                    matching_items.remove(matching_item)
                    break

        logging.info(f"Processed multi-pack: moved matching episodes to Checking queue")

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