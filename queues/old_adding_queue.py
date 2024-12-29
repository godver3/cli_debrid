import logging
import json
import time
from typing import Dict, Any, List, Optional, Tuple, Union
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
import tempfile

class AddingQueue:
    """
    A queue manager for handling the addition of media items to a debrid service.
    Manages the processing of movies and TV shows, including handling of cached and uncached content.
    """

    def __init__(self) -> None:
        """
        Initialize the AddingQueue with empty items list and necessary configurations.
        Sets up debrid provider connections.
        """
        self.items: List[Dict[str, Any]] = []
        self.api_key: Optional[str] = None
        self.episode_count_cache: Dict[str, Dict[int, int]] = {}  
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
                    validated_results = self._validate_scrape_results(item, scrape_results_str)
                    if not validated_results:
                        self.handle_failed_item(queue_manager, item, "Adding")
                        return
                else:
                    logging.error(f"No scrape results found for {item_identifier}")
                    self.handle_failed_item(queue_manager, item, "Adding")
            else:
                logging.error(f"Failed to retrieve updated item for ID: {item['id']}")
                self.handle_failed_item(queue_manager, item, "Adding")

    def _validate_scrape_results(self, item: Dict[str, Any], scrape_results_str: str) -> Optional[List[Dict[str, Any]]]:
        """
        Validate and parse scrape results from string format.
        
        Args:
            item: Media item being processed
            scrape_results_str: String containing scrape results
            
        Returns:
            Optional[List[Dict[str, Any]]]: Parsed scrape results if valid, None if invalid
        """
        item_identifier = self.generate_identifier(item)
        
        if not scrape_results_str:
            logging.warning(f"Empty scrape results for item: {item_identifier}")
            return None
            
        try:
            if isinstance(scrape_results_str, str):
                scrape_results = json.loads(scrape_results_str)
            elif isinstance(scrape_results_str, list):
                scrape_results = scrape_results_str
            else:
                raise ValueError(f"Unexpected scrape_results type: {type(scrape_results_str)}")
            
            if not scrape_results:
                logging.warning(f"Empty scrape results for item: {item_identifier}")
                return None
                
            return scrape_results
            
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"Error parsing scrape results for item: {item_identifier}. Error: {str(e)}")
            return None

    def _process_cached_content(self, queue_manager: Any, item: Dict[str, Any], result: Dict[str, Any], 
                              scrape_results: List[Dict[str, Any]]) -> bool:
        """
        Process cached content from a scrape result.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item being processed
            result: Single scrape result to process
            scrape_results: Complete list of scrape results
            
        Returns:
            bool: True if processing was successful
        """
        item_identifier = self.generate_identifier(item)
        try:
            link = result.get('magnet', '')
            if not link:
                logging.warning(f"No magnet link found in cached result for {item_identifier}")
                return False

            current_hash = extract_hash_from_magnet(link) if link.startswith('magnet:') else None
            if not current_hash:
                logging.warning(f"Could not extract hash from cached result for {item_identifier}")
                return False

            success = self.process_result(queue_manager, item, result, current_hash, True, scrape_results)
            if success:
                logging.info(f"Successfully processed cached content for {item_identifier}")
                return True
            
            return False
            
        except Exception as e:
            logging.error(f"Error processing cached content for {item_identifier}: {str(e)}")
            return False

    def _process_uncached_content(self, queue_manager: Any, item: Dict[str, Any], result: Dict[str, Any],
                                scrape_results: List[Dict[str, Any]], mode: str) -> bool:
        """
        Process uncached content from a scrape result.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item being processed
            result: Single scrape result to process
            scrape_results: Complete list of scrape results
            mode: Processing mode ('none', 'hybrid', or 'full')
            
        Returns:
            bool: True if processing was successful
        """
        item_identifier = self.generate_identifier(item)
        try:
            if mode == 'none':
                logging.debug(f"Skipping uncached result for {item_identifier} in 'none' mode")
                return False

            link = result.get('magnet', '')
            if not link:
                logging.warning(f"No magnet link found in uncached result for {item_identifier}")
                return False

            current_hash = extract_hash_from_magnet(link) if link.startswith('magnet:') else None
            if not current_hash:
                logging.warning(f"Could not extract hash from uncached result for {item_identifier}")
                return False

            success = self.process_result(queue_manager, item, result, current_hash, False, scrape_results)
            if success:
                logging.info(f"Successfully processed uncached content for {item_identifier}")
                return True
            
            return False
            
        except Exception as e:
            logging.error(f"Error processing uncached content for {item_identifier}: {str(e)}")
            return False

    def process_item(self, queue_manager: Any, item: Dict[str, Any], scrape_results: List[Dict[str, Any]], 
                    mode: str, upgrade: bool = False) -> bool:
        """
        Process a single media item with its scrape results based on the specified mode.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item to be processed
            scrape_results: List of scraping results to process
            mode: Processing mode ('none', 'hybrid', or 'full')
            upgrade: Whether this is an upgrade attempt for existing content
            
        Returns:
            bool: True if processing was successful, False otherwise
        """
        item_identifier = self.generate_identifier(item)
        logging.debug(f"Processing {mode} mode for item: {item_identifier}")

        validated_results = self._validate_scrape_results(item, scrape_results)
        if not validated_results:
            self.handle_failed_item(queue_manager, item, "Adding")
            return False

        if get_setting('Debug', 'sort_by_uncached_status'):
            validated_results = self.sort_results_by_cache_status(validated_results)

        # Try cached content first in hybrid mode
        if mode == 'hybrid':
            for result in validated_results:
                if is_cached_on_rd(extract_hash_from_magnet(result.get('magnet', ''))):
                    if self._process_cached_content(queue_manager, item, result, validated_results):
                        return True

        # Process all results based on mode
        for result in validated_results:
            try:
                link = result.get('magnet', '')
                if not link:
                    continue

                current_hash = extract_hash_from_magnet(link) if link.startswith('magnet:') else None
                if not current_hash:
                    continue

                is_cached = is_cached_on_rd(current_hash)
                
                if is_cached:
                    if self._process_cached_content(queue_manager, item, result, validated_results):
                        return True
                elif self._process_uncached_content(queue_manager, item, result, validated_results, mode):
                    return True
                    
            except Exception as e:
                logging.error(f"Error processing result for {item_identifier}: {str(e)}")
                continue

        # If we reach here, no results were successfully processed
        logging.warning(f"No valid results found for {item_identifier}")
        
        # For episodes, try individual episode scraping
        if item['type'] == 'episode' and not upgrade:
            logging.info(f"Attempting individual episode scraping for {item_identifier}")
            individual_results = self.scrape_individual_episode(item)
            if individual_results:
                return self.process_item(queue_manager, item, individual_results, mode, True)

        if not upgrade:
            self.handle_failed_item(queue_manager, item, "Adding")
        return False

    def sort_results_by_cache_status(self, scrape_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Sort scrape results by their cache status, with uncached results first.
        
        Args:
            scrape_results: List of scrape results to sort
            
        Returns:
            List[Dict[str, Any]]: Sorted list of scrape results
        """
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

    def process_torrent(self, queue_manager: Any, item: Dict[str, Any], title: str, link: str, add_result: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Process a torrent after it has been added to the debrid service.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item being processed
            title: Title of the content
            link: Magnet/torrent link
            add_result: Result from adding torrent to debrid service
            
        Returns:
            Tuple[bool, Optional[str]]: Success status and torrent ID if available
        """
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

    def process_result(self, queue_manager: Any, item: Dict[str, Any], result: Dict[str, Any], current_hash: str, is_cached: bool, scrape_results: List[Dict[str, Any]], temp_file_path: Optional[str] = None) -> bool:
        """
        Process a single scrape result for a media item.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item being processed
            result: Single scrape result to process
            current_hash: Hash of the torrent/magnet
            is_cached: Whether the content is cached on debrid service
            scrape_results: Complete list of scrape results
            temp_file_path: Optional path to temporary downloaded torrent file
            
        Returns:
            bool: True if processing was successful, False otherwise
        """
        item_identifier = self.generate_identifier(item)
        title = result.get('title', '')
        link = result.get('magnet', '')
        mode = get_setting('Scraping', 'uncached_content_handling', 'None').lower()
        torrent_id = result.get('torrent_id')

        try:
            # Validate inputs
            if not link:
                logging.error(f"No magnet link found for {item_identifier}")
                return False
            
            # Check if hash is in not wanted list
            if is_magnet_not_wanted(current_hash):
                logging.info(f"Hash {current_hash} for {item_identifier} is in not_wanted_magnets. Skipping.")
                self._cleanup_temp_file(temp_file_path)
                return False

            # Add to debrid service
            add_result = add_to_real_debrid(link, temp_file_path)
            self._cleanup_temp_file(temp_file_path)

            if not add_result:
                logging.error(f"Failed to add torrent to Real-Debrid for {item_identifier}")
                return False

            if not isinstance(add_result, dict):
                logging.warning(f"Unexpected result type from Real-Debrid for {item_identifier}: {type(add_result)}")
                self._cleanup_torrent(torrent_id)
                return False

            # Process add result
            status = add_result.get('status')
            torrent_id = add_result.get('torrent_id') or add_result.get('id')

            # Handle non-downloaded torrents
            if status != 'downloaded' and mode != 'full':
                logging.info(f"Removing non-downloaded torrent (status: {status}) for {item_identifier}")
                if torrent_id:
                    self._cleanup_torrent(torrent_id)
                    self.add_to_not_wanted(current_hash, item_identifier, item)
                    self.add_to_not_wanted_url(link, item_identifier, item)
                return False

            # Process content if downloaded or in full mode
            if status == 'downloaded' or mode == 'full':
                logging.info(f"Processing {'cached' if status == 'downloaded' else 'uncached'} content for {item_identifier}")
                if status == 'downloaded':
                    self.add_to_not_wanted(current_hash, item_identifier, item)
                    self.add_to_not_wanted_url(link, item_identifier, item)

                success, returned_torrent_id = self.process_torrent(queue_manager, item, title, link, add_result)
                if success:
                    return True

                logging.warning(f"Failed to process torrent for {item_identifier}")
                self._cleanup_torrent(returned_torrent_id or torrent_id)
                return False

            return False

        except Exception as e:
            logging.error(f"Error processing result for {item_identifier}: {str(e)}", exc_info=True)
            if torrent_id:
                self._cleanup_torrent(torrent_id)
            self._cleanup_temp_file(temp_file_path)
            return False

    def _cleanup_temp_file(self, temp_file_path: Optional[str]) -> None:
        """
        Clean up a temporary file if it exists.
        
        Args:
            temp_file_path: Path to temporary file
        """
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
            except Exception as e:
                logging.error(f"Error cleaning up temporary file {temp_file_path}: {str(e)}")

    def _cleanup_torrent(self, torrent_id: Optional[str]) -> None:
        """
        Clean up a torrent from the debrid service.
        
        Args:
            torrent_id: ID of the torrent to remove
        """
        if torrent_id:
            try:
                self.remove_unwanted_torrent(torrent_id)
                logging.debug(f"Removed torrent {torrent_id}")
            except Exception as e:
                logging.error(f"Error removing torrent {torrent_id}: {str(e)}")

    def move_related_season_items(self, queue_manager: Any, item: Dict[str, Any], season_pack: Union[List[int], int], title: str, link: str) -> None:
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
        
    def handle_failed_item(self, queue_manager: Any, item: Dict[str, Any], from_queue: str) -> None:
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
      
    def download_and_extract_hash(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Download a torrent file and extract its hash.
        
        Args:
            url: URL of the torrent file
            
        Returns:
            Tuple[Optional[str], Optional[str]]: Tuple of (hash, temporary file path)
        """
        return self.debrid_provider.download_and_extract_hash(url)

    def get_torrent_info(self, hash_value: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a torrent from the debrid service.
        
        Args:
            hash_value: Hash of the torrent
            
        Returns:
            Optional[Dict[str, Any]]: Torrent information if available
        """
        return self.debrid_provider.get_torrent_info(hash_value)

    def get_torrent_files(self, hash_value: str) -> Optional[Dict[str, List[str]]]:
        """
        Get list of files in a torrent from the debrid service.
        
        Args:
            hash_value: Hash of the torrent
            
        Returns:
            Optional[Dict[str, List[str]]]: Dictionary of file information if available
        """
        return self.debrid_provider.get_torrent_files(hash_value)

    def remove_unwanted_torrent(self, torrent_id: str) -> None:
        """
        Remove a torrent from the debrid service.
        
        Args:
            torrent_id: ID of the torrent to remove
        """
        return self.debrid_provider.remove_torrent(torrent_id)

    def add_to_not_wanted(self, hash_value: str, item_identifier: str, item: Dict[str, Any]) -> None:
        """
        Add a torrent hash to the not wanted list if the item is old enough.
        
        Args:
            hash_value: Hash of the torrent
            item_identifier: Identifier of the media item
            item: Media item details
        """
        identifier = self.generate_identifier(item)
        if not self.is_item_past_24h(item):
            logging.info(f"Not adding hash {hash_value} to not_wanted_magnets for {item_identifier} as it's less than 24h old")
            return

        add_to_not_wanted(hash_value, item_identifier, item)
        logging.info(f"Added hash {hash_value} to not_wanted_magnets for {item_identifier}")
        
        # Add this line to log the current contents of the not_wanted list
        #logging.debug(f"Current not_wanted_magnets: {get_not_wanted_magnets()}")
    
    def add_to_not_wanted_url(self, url: str, item_identifier: str, item: Dict[str, Any]) -> None:
        """
        Add a URL to the not wanted list if the item is old enough.
        
        Args:
            url: URL to add to not wanted list
            item_identifier: Identifier of the media item
            item: Media item details
        """
        identifier = self.generate_identifier(item)
        if not self.is_item_past_24h(item):
            logging.info(f"Not adding URL {url} to not_wanted_urls for {item_identifier} as it's less than 24h old")
            return

        add_to_not_wanted_urls(url)
        logging.info(f"Added URL {url} to not_wanted_urls for {item_identifier}")
        
        # Add this line to log the current contents of the not_wanted list
        #logging.debug(f"Current not_wanted_urls: {get_not_wanted_urls()}")
    
    def is_item_past_24h(self, item: Dict[str, Any]) -> bool:
        """
        Check if an item's release date is more than 24 hours old.
        
        Args:
            item: Media item to check
            
        Returns:
            bool: True if item is more than 24 hours old
        """
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

    def process_multi_pack(self, queue_manager: Any, item: Dict[str, Any], title: str, link: str, files: List[Dict[str, Any]], torrent_id: str) -> Tuple[bool, str]:
        """
        Process a multi-file torrent pack.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item being processed
            title: Title of the content
            link: Magnet/torrent link
            files: List of files in the torrent
            torrent_id: ID of the torrent
            
        Returns:
            Tuple[bool, str]: Success status and status message
        """
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Processing multi-pack for item: {item_identifier}")

        # Get all matching items from other queues
        matching_items = self.get_matching_items_from_queues(queue_manager, item)
        matching_items.append(item)  
        logging.info(f"Total matching items (including original): {len(matching_items)}")

        # Use regular matching for TV shows
        logging.info("Using regular matching for TV show")
        matches = self.match_regular_tv_show(files, matching_items)

        if matches is None:
            logging.warning("No matches found")
            return False, "No matching episodes found"

        if not matches:
            logging.warning("No matches found")
            return False, "No matching episodes found"

        # Process matches
        for file_path, matched_item in matches:
            matched_identifier = queue_manager.generate_identifier(matched_item)
            logging.info(f"Moving {matched_identifier} to Checking queue with file: {file_path}")
            queue_manager.move_to_checking(matched_item, "Adding", title, link, os.path.basename(file_path), torrent_id)

        return True, f"Successfully matched {len(matches)} files"

    def match_regular_tv_show(self, files: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Match files from a torrent to TV show episodes.
        
        Args:
            files: List of files from the torrent
            items: List of TV show episodes to match against
            
        Returns:
            List[Tuple[str, Dict[str, Any]]]: List of tuples containing file paths and matching episodes
        """
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

    def handle_multi_episode_file(self, file_path: str, file_season: int, file_episodes: List[int], items: List[Dict[str, Any]], matches: List[Tuple[str, Dict[str, Any]]]) -> None:
        """
        Handle a file that contains multiple episodes.
        
        Args:
            file_path: Path to the multi-episode file
            file_season: Season number of the episodes
            file_episodes: List of episode numbers in the file
            items: List of episodes to match against
            matches: List to store matches in
        """
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

    def get_matching_items_from_queues(self, queue_manager: Any, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Find items in other queues that match the current item's series.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Current media item
            
        Returns:
            List[Dict[str, Any]]: List of matching items from other queues
        """
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
        """
        Check if a file matches a media item based on its filename.
        
        Args:
            file: Filename to check
            item: Media item to match against
            
        Returns:
            bool: True if file matches the item
        """
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
        """
        Check if a file is a video file based on its extension.
        
        Args:
            filename: Name of the file to check
            
        Returns:
            bool: True if file is a video file
        """
        video_extensions = ['.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm']
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    def match_movie(self, guess: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        """
        Check if a movie file matches a movie item.
        
        Args:
            guess: Parsed information from filename
            item: Movie item to match against
            filename: Original filename
            
        Returns:
            bool: True if file matches the movie
        """
        if self.is_video_file(filename):
            logging.debug(f"Video file match found: {filename}")
            return True
        
        logging.debug(f"Not a video file: {filename}")
        return False

    def match_episode(self, guess: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """
        Check if an episode file matches an episode item.
        
        Args:
            guess: Parsed information from filename
            item: Episode item to match against
            
        Returns:
            bool: True if file matches the episode
        """
        if guess.get('type') != 'episode':
            return False

        season_match = guess.get('season') == int(item['season_number'])
        episode_match = guess.get('episode') == int(item['episode_number'])

        if season_match and episode_match:
            logging.debug(f"Episode match found: {guess.get('title')} S{guess.get('season', '')}E{guess.get('episode', '')}")
            return True

        return False

    def process_single_result(self, queue_manager: Any, item: Dict[str, Any], result: Dict[str, Any], cached_only: bool = False) -> bool:
        """
        Process a single scrape result, handling both cached and uncached content.
        
        Args:
            queue_manager: Manager instance handling different queue states
            item: Media item being processed
            result: Scrape result to process
            cached_only: Whether to only process cached content
            
        Returns:
            bool: True if processing was successful, False otherwise
        """
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


    def scrape_individual_episode(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
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