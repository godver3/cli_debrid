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
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet, get_magnet_files, API_BASE_URL
from not_wanted_magnets import add_to_not_wanted, is_magnet_not_wanted, get_not_wanted_magnets
from scraper.scraper import scrape
from metadata.metadata import get_all_season_episode_counts, get_overseerr_cookies
from guessit import guessit
import functools
from .anime_matcher import AnimeMatcher
import functools

class AddingQueue:
    def __init__(self):
        self.items = []
        self.api_key = get_setting("RealDebrid", "api_key")
        self.episode_count_cache = {}  # Add this line
        self.anime_matcher = AnimeMatcher(self.calculate_absolute_episode)

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

                    uncached_handling = get_setting('Scraping', 'uncached_content_handling', 'None').lower()

                    self.process_item(queue_manager, item, scrape_results, uncached_handling)

                else:
                    logging.error(f"No scrape results found for {item_identifier}")
                    queue_manager.move_to_sleeping(item, "Adding")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")

    def process_item(self, queue_manager, item, scrape_results, mode):
        item_identifier = queue_manager.generate_identifier(item)
        logging.debug(f"Processing {mode} mode for item: {item_identifier}")
        logging.debug(f"Total scrape results: {len(scrape_results)}")

        processed_results = []
        hashes = []

        # First pass: extract hashes and prepare results
        for index, result in enumerate(scrape_results):
            title = result.get('title', '')
            link = result.get('magnet', '')
            logging.debug(f"Processing result {index + 1}: Title: {title}")

            current_hash = extract_hash_from_magnet(link) if link.startswith('magnet:') else self.download_and_extract_hash(link)

            if not current_hash:
                logging.warning(f"Failed to extract hash from link for result {index + 1}: {item_identifier}")
                continue

            hashes.append(current_hash)
            processed_results.append({
                'result': result,
                'hash': current_hash,
                'title': title,
                'link': link,
                'original_index': index
            })

        logging.debug(f"Processed results: {len(processed_results)}")

        # Check cache status for all hashes at once
        cache_status = is_cached_on_rd(hashes) if hashes else {}
        logging.info(f"Cache status returned: {cache_status}")

        # Process results based on mode
        for processed_result in processed_results:
            current_hash = processed_result['hash']
            result = processed_result['result']
            original_index = processed_result['original_index']
            is_cached = cache_status.get(current_hash, False)

            if mode == 'none' and not is_cached:
                logging.debug(f"Skipping uncached result {original_index + 1} for {item_identifier}")
                continue

            if mode == 'hybrid' and not is_cached and any(cache_status.values()):
                logging.debug(f"Skipping uncached result {original_index + 1} for {item_identifier} in hybrid mode")
                continue

            logging.info(f"Processing result {original_index + 1} for {item_identifier}. Cached: {is_cached}")
            success = self.process_result(queue_manager, item, result, current_hash, is_cached=is_cached)
            
            if success:
                logging.info(f"Successfully processed result {original_index + 1} for {item_identifier}")
                return True
            else:
                logging.warning(f"Failed to process result {original_index + 1} for {item_identifier}. Continuing to next result.")

        # If we're here, no results were successfully processed.
        logging.info(f"No results successfully processed for {item_identifier}")

        # For episodes, try individual episode scraping
        if item['type'] == 'episode':
            logging.info(f"Attempting individual episode scraping for {item_identifier}")
            individual_results = self.scrape_individual_episode(item)
            logging.debug(f"Individual episode scraping returned {len(individual_results)} results")
            for index, result in enumerate(individual_results):
                logging.debug(f"Processing individual result {index + 1}")
                if self.process_single_result(queue_manager, item, result, cached_only=(mode == 'none')):
                    logging.info(f"Successfully processed individual result {index + 1} for {item_identifier}")
                    return True
                else:
                    logging.debug(f"Failed to process individual result {index + 1} for {item_identifier}")

        logging.warning(f"No results successfully processed for {item_identifier}")
        self.handle_failed_item(queue_manager, item, "Adding")
        return False

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
                matching_files = [file for file in files if self.file_matches_item(file, item)]
                if matching_files:
                    logging.info(f"Matching file(s) found for movie: {item_identifier}")
                    filled_by_file = os.path.basename(matching_files[0])
                    queue_manager.move_to_checking(item, "Adding", title, link, filled_by_file, torrent_id)
                    logging.debug(f"Moved movie {item_identifier} to Checking queue with filled_by_file: {filled_by_file}")
                    return True, torrent_id
                else:
                    logging.warning(f"No matching file found for movie: {item_identifier}")
                    return False, torrent_id
            else:  # TV show
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

    def process_result(self, queue_manager, item, result, current_hash, is_cached):
        item_identifier = queue_manager.generate_identifier(item)
        title = result.get('title', '')
        link = result.get('magnet', '')

        torrent_id = result.get('torrent_id')
        logging.debug(f"Torrent ID: {torrent_id}")

        if not link:
            logging.error(f"No magnet link found for {item_identifier}")
            return False
        
        # Check if the hash is in the not_wanted list
        if is_magnet_not_wanted(current_hash):
            logging.info(f"Hash {current_hash} for {item_identifier} is in not_wanted_magnets. Skipping.")
            return False
        
        add_result = add_to_real_debrid(link)
        if add_result:
            if isinstance(add_result, dict):
                status = add_result.get('status')
                if status == 'downloaded' or (status in ['downloading', 'queued'] and 'files' in add_result):
                    logging.info(f"Torrent added to Real-Debrid successfully for {item_identifier}. Status: {status}")
                    self.add_to_not_wanted(current_hash, item_identifier)
                    success, torrent_id = self.process_torrent(queue_manager, item, title, link, add_result)
                    if success:
                        return True
                    else:
                        logging.warning(f"Failed to process torrent for {item_identifier}")
                        logging.debug(f"Torrent ID: {torrent_id}")
                        logging.debug(f"ID: {add_result.get('id')}")
                        self.remove_unwanted_torrent(torrent_id or add_result.get('id'))
                        return False
                else:
                    logging.warning(f"Unexpected result from Real-Debrid for {item_identifier}: {add_result}")
                    self.remove_unwanted_torrent(add_result.get('id'))
            elif add_result in ['downloading', 'queued']:
                logging.info(f"Uncached torrent added to Real-Debrid successfully for {item_identifier}")
                self.add_to_not_wanted(current_hash, item_identifier)
                success, torrent_id = self.process_torrent(queue_manager, item, title, link, {'status': 'uncached', 'files': [], 'torrent_id': None})
                if success:
                    return True
                else:
                    logging.warning(f"Failed to process uncached torrent for {item_identifier}")
                    self.remove_unwanted_torrent(torrent_id)  # This will be None for uncached torrents
                    return False
            else:
                logging.warning(f"Unexpected result from Real-Debrid for {item_identifier}: {add_result}")
                self.remove_unwanted_torrent(None)
        else:
            logging.error(f"Failed to add torrent to Real-Debrid for {item_identifier}")

        return False

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
      
    def download_and_extract_hash(self, url: str) -> str:
        def obfuscate_url(url: str) -> str:
            parts = url.split('/')
            if len(parts) > 3:
                return '/'.join(parts[:3] + ['...'] + parts[-1:])
            return url

        obfuscated_url = obfuscate_url(url)
        try:
            logging.debug(f"Attempting to download torrent file from URL: {obfuscated_url}")
            response = api.get(url, timeout=30, stream=True)
            torrent_content = response.content
            logging.debug(f"Successfully downloaded torrent file from {obfuscated_url}. Content length: {len(torrent_content)} bytes")
            
            # Decode the torrent file
            torrent_data = bencodepy.decode(torrent_content)
            
            # Extract the info dictionary
            info = torrent_data[b'info']
            
            # Encode the info dictionary
            encoded_info = bencodepy.encode(info)
            
            # Calculate the SHA1 hash
            hash_result = hashlib.sha1(encoded_info).hexdigest()
            logging.debug(f"Successfully extracted hash: {hash_result}")
            return hash_result
        except api.exceptions.RequestException as e:
            logging.error(f"Network error while downloading torrent file from {obfuscated_url}: {str(e)}")
        except bencodepy.exceptions.BencodeDecodeError as e:
            logging.error(f"Error decoding torrent file from {obfuscated_url}: {str(e)}")
        except KeyError as e:
            logging.error(f"Error extracting info from torrent file from {obfuscated_url}: {str(e)}")
        except Exception as e:
            logging.error(f"Unexpected error processing torrent file from {obfuscated_url}: {str(e)}")
        return None


    def add_to_real_debrid_helper(self, link: str, item_identifier: str, hash_value: str, add_if_uncached: bool = True) -> Dict[str, Any]:
        try:
            logging.info(f"Processing link for {item_identifier}. Hash: {hash_value}")


            # Check if the hash is already in the not wanted list
            if is_magnet_not_wanted(hash_value):
                logging.info(f"Hash {hash_value} for {item_identifier} is already in not_wanted_magnets. Skipping.")
                return {"success": False, "message": "Hash already in not wanted list"}
    
            cache_status = is_cached_on_rd(hash_value)
            logging.info(f"Cache status for {item_identifier}: {cache_status}")
    
            if not cache_status[hash_value] and not add_if_uncached:
                return {"success": False, "message": "Skipping uncached content in hybrid mode"}

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
    
                # Add to not wanted list only for uncached torrents
                add_to_not_wanted(hash_value)
                logging.info(f"Added hash {hash_value} to not_wanted_magnets for {item_identifier}")
    
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
            torrents = api.get(f"{API_BASE_URL}/torrents", headers=headers).json()
            for torrent in torrents:
                if torrent['hash'].lower() == hash_value.lower():
                    torrent_id = torrent['id']
                    info_url = f"{API_BASE_URL}/torrents/info/{torrent_id}"
                    return api.get(info_url, headers=headers).json()

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
            
            # Add this line to log the current contents of the not_wanted list
            logging.debug(f"Current not_wanted_magnets: {get_not_wanted_magnets()}")
        except Exception as e:
            logging.error(f"Error adding hash {hash_value} to not_wanted_magnets for {item_identifier}: {str(e)}")

    def process_multi_pack(self, queue_manager, item, title, link, files, torrent_id):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Processing multi-pack for item: {item_identifier}")

        # Get all matching items from other queues
        matching_items = self.get_matching_items_from_queues(queue_manager, item)
        matching_items.append(item)  # Include the original item
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
            file_info = guessit(file)
            file_season = file_info.get('season')
            file_episodes = file_info.get('episode')
            
            # Convert to list if it's a single episode
            if isinstance(file_episodes, int):
                file_episodes = [file_episodes]
            
            for item in items:
                item_season = int(item['season_number'])
                item_episode = int(item['episode_number'])
                
                if file_season == item_season and item_episode in file_episodes:
                    matches.append((file, item))
                    
                    # For multi-episode files, we need to find all matching items
                    if len(file_episodes) > 1:
                        self.handle_multi_episode_file(file, file_season, file_episodes, items, matches)
                    
                    break  # Move to next file after finding a match
        
        return matches

    def handle_multi_episode_file(self, file, file_season, file_episodes, items, matches):
        for episode in file_episodes:
            for item in items:
                if (int(item['season_number']) == file_season and 
                    int(item['episode_number']) == episode and 
                    (file, item) not in matches):
                    matches.append((file, item))


    def get_matching_items_from_queues(self, queue_manager, item):
        matching_items = []
        for queue_name in ["Wanted", "Scraping", "Sleeping"]:
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


    def remove_unwanted_torrent(self, torrent_id):
        try:
            headers = {'Authorization': f'Bearer {self.api_key}'}
            response = api.delete(f"{API_BASE_URL}/torrents/delete/{torrent_id}", headers=headers)
            response.raise_for_status()
            logging.info(f"Successfully removed unwanted torrent with ID: {torrent_id}")
        except api.exceptions.RequestException as e:
            logging.error(f"Error removing unwanted torrent with ID {torrent_id}: {str(e)}")

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
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)
        return get_all_season_episode_counts(overseerr_url, overseerr_api_key, tmdb_id, cookies)

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
        return results  # We're not using filtered_out here, but you could if needed

    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{item['version']}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{item['version']}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")
