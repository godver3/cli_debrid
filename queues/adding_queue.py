"""
Queue management for handling the addition of media items to a debrid service.
Separates queue management from content processing logic.
"""

import logging
import json
import re
import os
from typing import Dict, Any, List, Optional, Tuple
from database import get_all_media_items, get_media_item_by_id, update_media_item_state, update_media_item
from debrid import get_debrid_provider
from debrid.real_debrid import RealDebridProvider
from debrid.status import TorrentStatus
from .media_matcher import MediaMatcher
from settings import get_setting
import hashlib
import tempfile
import requests
import bencodepy
from guessit import guessit

class ContentProcessor:
    """Handles the processing of media content after it's been added to the debrid service"""
    
    def __init__(self, debrid_provider):
        self.debrid_provider = debrid_provider
        self.media_matcher = MediaMatcher()

    def process_content(self, item: Dict[str, Any], torrent_info: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Process content after it's been added to the debrid service
        
        Args:
            item: Media item to process
            torrent_info: Information about the added torrent
            
        Returns:
            Tuple of (success, message)
        """
        try:
            files = torrent_info.get('files', [])
            if not files:
                return False, "No files found in torrent"

            matches = self.media_matcher.match_content(files, item)
            if not matches:
                return False, "No matching files found"

            if len(matches) > 1 and item.get('type') == 'movie':
                return False, "Multiple matches found for movie"

            return True, "Content processed successfully"
            
        except Exception as e:
            logging.error(f"Error processing content: {str(e)}")
            return False, f"Error processing content: {str(e)}"

class AddingQueue:
    """Manages the queue of items being added to the debrid service"""
    
    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.debrid_provider = get_debrid_provider()
        self.content_processor = ContentProcessor(self.debrid_provider)

    def reinitialize_provider(self):
        """Reinitialize the debrid provider and content processor"""
        self.debrid_provider = get_debrid_provider()
        self.content_processor = ContentProcessor(self.debrid_provider)

    def update(self):
        """Update the queue with current items in 'Adding' state"""
        self.items = [dict(row) for row in get_all_media_items(state="Adding")]

    def get_contents(self) -> List[Dict[str, Any]]:
        """Get current queue contents"""
        return self.items

    def add_item(self, item: Dict[str, Any]):
        """Add an item to the queue"""
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        """Remove an item from the queue"""
        self.items = [i for i in self.items if i['id'] != item['id']]

    def _match_file_to_episode(self, file_path: str, item: Dict[str, Any]) -> bool:
        """Check if a file matches an episode's season/episode numbers and version"""
        if item.get('type') != 'episode':
            logging.info(f"Item type is not episode: {item.get('type')}")
            return False
            
        # Check version first
        version = item.get('version')
        if version and version.lower() not in file_path.lower():
            logging.info(f"Version mismatch: looking for {version} in {file_path}")
            return False
            
        season = item.get('season_number')
        episode = item.get('episode_number')
        if season is None or episode is None:
            logging.info(f"Missing season or episode: season={season}, episode={episode}")
            return False
            
        # Use guessit to parse episode info
        try:
            # Clean up file path - remove leading slash and normalize spaces
            clean_path = file_path.lstrip('/').strip()
            guess = guessit(clean_path)
            logging.info(f"Guessit parsed: {guess}")
            
            if 'season' not in guess or 'episode' not in guess:
                logging.info(f"Guessit couldn't find season/episode in {clean_path}")
                return False
                
            match = guess['season'] == season and guess['episode'] == episode
            logging.info(f"Checking file {clean_path} for S{season:02d}E{episode:02d}: {'match' if match else 'no match'}")
            return match
            
        except Exception as e:
            logging.error(f"Error parsing file with guessit: {e}")
            return False

    def _get_scraping_queue_items(self, queue_manager: Any) -> List[Dict[str, Any]]:
        """Get all items currently in the Scraping queue"""
        return [dict(row) for row in get_all_media_items(state="Scraping")]

    def process(self, queue_manager: Any):
        """Process the next item in the queue"""
        if not self.items:
            return

        mode = get_setting('Scraping', 'uncached_content_handling', 'None').lower()

        item = self.items[0]
        process_uncached = item.get('process_uncached', False)

        # Handle full mode separately for RealDebrid provider only
        if mode == "full" and isinstance(self.debrid_provider, RealDebridProvider):
            from .adding_queue_full import FullModeProcessor
            full_processor = FullModeProcessor(self.content_processor)
            full_processor.process_full_mode(item, queue_manager)
            self.remove_item(item)
            return

        # Get and validate scrape results
        scrape_results = self._get_scrape_results(item)
        if not scrape_results:
            self._handle_failed_item(queue_manager, item)
            return

        # Get items in scraping queue for potential multi-pack matching
        scraping_items = self._get_scraping_queue_items(queue_manager)

        # Process each result until we find a working one
        for result in scrape_results:
            if not isinstance(result, dict):
                logging.warning(f"Invalid result format: {result}")
                continue

            hash_value = result.get('hash')
            temp_file = None
            if not hash_value and 'magnet' in result:
                hash_value, temp_file = self._extract_hash_from_magnet(result['magnet'])
                if not hash_value:
                    logging.warning(f"Could not extract hash from result magnet: {result.get('magnet', '')}")
                    continue

            result['hash'] = hash_value  # Store the hash in the result
            logging.info(f"\nChecking result with hash {hash_value}")

            try:
                # Check if provider supports direct cache checking
                if self.debrid_provider.supports_direct_cache_check:
                    # For providers like Torbox that support direct cache checking
                    cache_status = self.debrid_provider.is_cached(hash_value)
                    is_cached = cache_status.get(hash_value, False)

                    if is_cached:
                        logging.info("Result is cached, adding to debrid service")
                        add_result = self.debrid_provider.add_torrent(result['magnet'], temp_file)
                        logging.debug(f"Add result from debrid provider: {add_result}")
                        if add_result and isinstance(add_result, dict):
                            # Get files from add_result if available
                            files = add_result.get('files', [])
                            if not files:
                                files = add_result.get('data', {}).get('files', [])
                            
                            if not files:
                                logging.warning("No files found in add_result")
                                # Remove the torrent if it exists
                                torrent_id = add_result.get('torrent_id')
                                if torrent_id:
                                    try:
                                        self.debrid_provider.remove_torrent(torrent_id)
                                        logging.info(f"Removed torrent {torrent_id} from Real-Debrid - no files found")
                                    except Exception as e:
                                        logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
                                continue

                            # Process the files based on media type
                            media_type = item.get('type')
                            selected_file = None
                            
                            logging.debug(f"Processing files for media type: {media_type}")
                            logging.debug(f"Available files: {files}")
                            
                            if media_type == 'movie':
                                # For movies, get the largest video file
                                video_files = [f for f in files if f.get('path', '').lower().endswith(('.mkv', '.mp4', '.avi'))]
                                if video_files:
                                    selected_file = max(video_files, key=lambda x: x.get('bytes', 0))
                                    logging.debug(f"Selected movie file: {selected_file}")
                            elif media_type == 'episode':
                                # For episodes, try to match season/episode pattern
                                season = item.get('season_number')
                                episode = item.get('episode_number')
                                logging.debug(f"Looking for season {season} episode {episode}")
                                if season is not None and episode is not None:
                                    season_str = f"s{season:02d}"
                                    episode_str = f"e{episode:02d}"
                                    
                                    logging.debug(f"Looking for episode match: {season_str}{episode_str}")
                                    
                                    # First try exact season/episode match
                                    for file in files:
                                        filename = file.get('path', '').lower()
                                        logging.debug(f"Checking file: {filename}")
                                        if season_str in filename and episode_str in filename:
                                            selected_file = file
                                            logging.debug(f"Found exact episode match: {filename}")
                                            break
                                        else:
                                            logging.debug(f"No match: looking for {season_str}{episode_str} in {filename}")
                                    
                                    # If no exact match, fall back to largest video file
                                    if not selected_file:
                                        video_files = [f for f in files if f.get('path', '').lower().endswith(('.mkv', '.mp4', '.avi'))]
                                        if video_files:
                                            selected_file = max(video_files, key=lambda x: x.get('bytes', 0))
                                            logging.debug(f"No exact match, using largest file: {selected_file}")
                            
                            if selected_file:
                                file_path = selected_file.get('path', '')
                                file_name = os.path.basename(file_path)
                                
                                # Update media item with file info and state
                                queue_manager.move_to_checking(item, "Adding", result.get('title'), result['magnet'], file_name, add_result.get('torrent_id'))
                                logging.info(f"Updated current item with filled_by_file: {file_name}")
                                
                                # Mark the successful magnet/URL as not wanted to prevent reuse
                                if 'magnet' in result:
                                    from not_wanted_magnets import add_to_not_wanted
                                    magnet = result['magnet']
                                    hash_value = self._extract_hash_from_magnet(magnet)[0]
                                    if hash_value:
                                        add_to_not_wanted(hash_value, str(item.get('id')), item)
                                        logging.info(f"Added successful magnet hash {hash_value} to not wanted list")
                                elif 'url' in result:
                                    from not_wanted_magnets import add_to_not_wanted_urls
                                    url = result['url']
                                    add_to_not_wanted_urls(url, str(item.get('id')), item)
                                    logging.info(f"Added successful URL {url} to not wanted list")
                                
                                # For TV shows, check if this is a multi-pack that could fill other episodes
                                if item.get('type') == 'episode':
                                    # Get our series title
                                    series_title = item.get('series_title', '') or item.get('title', '')
                                    
                                    # Get all scraping queue items for the same show
                                    scraping_items = self._get_scraping_queue_items(None)
                                    if scraping_items:
                                        # Filter to only items from the same show and version
                                        matching_items = [
                                            i for i in scraping_items 
                                            if (i.get('series_title', '') or i.get('title', '')) == series_title
                                            and i.get('version') == item.get('version')
                                            and i['id'] != item['id']  # Exclude current item
                                        ]
                                        
                                        if matching_items:
                                            logging.info(f"Found {len(matching_items)} other episodes of '{series_title}' to check")
                                            
                                            # Try to match each file against each item
                                            for scraping_item in matching_items:
                                                matches = self.content_processor.media_matcher.match_content(files, scraping_item)
                                                if matches:
                                                    # Take the first matching file
                                                    matched_file = matches[0][0]
                                                    file_name = os.path.basename(matched_file)
                                                    
                                                    # Update the item state
                                                    queue_manager.move_to_checking(scraping_item, "Adding", result.get('title'), result['magnet'], file_name, add_result.get('torrent_id'))
                                                    logging.info(f"Updated matching item with filled_by_file: {file_name}")
                                else:
                                    logging.debug("Not a TV show, skipping multi-pack check")

                                # Stop processing more results since we found a valid cached one
                                self.remove_item(item)
                                return True
                            else:
                                logging.warning("No suitable file found for media item")
                            continue
                    else:
                        logging.info("Result is not cached, checking next result")
                        continue
                else:
                    # For providers like RealDebrid that need to add torrent to check cache
                    cache_status = self.debrid_provider.is_cached(hash_value)
                    cache_info = cache_status.get(hash_value, {})
                    is_cached = cache_info.get('cached', False)

                    if is_cached:
                        logging.info("Result is cached, torrent already added")
                        files = cache_info.get('files', [])
                        if files:
                            logging.debug(f"Processing current item: {item}")
                            matches = self.content_processor.media_matcher.match_content(files, item)
                            logging.debug(f"Found {len(matches)} matching files for current item")
                            
                            if matches:
                                # Get the first match (for episodes) or largest file (for movies)
                                if item.get('type') == 'movie':
                                    # For movies, get the largest matching file
                                    selected_file = max(
                                        (f for f, _ in matches), 
                                        key=lambda x: next((f['bytes'] for f in files if f['path'] == x), 0)
                                    )
                                else:
                                    # For episodes, take the first match
                                    selected_file = matches[0][0]
                                
                                # Find the file info from the original list
                                file_info = next(f for f in files if f['path'] == selected_file)
                                file_name = os.path.basename(file_info['path'])
                                logging.debug(f"Selected file for current item: {file_name}")
                                
                                queue_manager.move_to_checking(item, "Adding", result.get('title'), result['magnet'], file_name, cache_info.get('torrent_id'))
                                logging.info(f"Updated current item with filled_by_file: {file_name}")
                                
                                # Mark the successful magnet/URL as not wanted to prevent reuse
                                if 'magnet' in result:
                                    from not_wanted_magnets import add_to_not_wanted
                                    magnet = result['magnet']
                                    hash_value = self._extract_hash_from_magnet(magnet)[0]
                                    if hash_value:
                                        add_to_not_wanted(hash_value, str(item.get('id')), item)
                                        logging.info(f"Added successful magnet hash {hash_value} to not wanted list")
                                elif 'url' in result:
                                    from not_wanted_magnets import add_to_not_wanted_urls
                                    url = result['url']
                                    add_to_not_wanted_urls(url, str(item.get('id')), item)
                                    logging.info(f"Added successful URL {url} to not wanted list")
                                
                                # For TV shows, check if this is a multi-pack that could fill other episodes
                                if item.get('type') == 'episode':
                                    # Get our series title
                                    series_title = item.get('series_title', '') or item.get('title', '')
                                    
                                    # Get all scraping queue items for the same show
                                    scraping_items = self._get_scraping_queue_items(None)
                                    if scraping_items:
                                        # Filter to only items from the same show and version
                                        matching_items = [
                                            i for i in scraping_items 
                                            if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                                i.get('version') == item.get('version'))
                                        ]
                                        
                                        if matching_items:
                                            logging.info(f"Found {len(matching_items)} other episodes of '{series_title}' to check")
                                            
                                            # Try to match each file against each item
                                            for scraping_item in matching_items:
                                                matches = self.content_processor.media_matcher.match_content(files, scraping_item)
                                                if matches:
                                                    # Take the first matching file
                                                    matched_file = matches[0][0]
                                                    file_name = os.path.basename(matched_file)
                                                    
                                                    # Update the item state
                                                    queue_manager.move_to_checking(scraping_item, "Adding", result.get('title'), result['magnet'], file_name, cache_info.get('torrent_id'))
                                                    logging.info(f"Updated matching item with filled_by_file: {file_name}")

                                else:
                                    logging.debug("Not a TV show, skipping multi-pack check")

                                # Stop processing more results since we found a valid cached one
                                self.remove_item(item)
                                return True

                            else:
                                logging.warning("No suitable file found for current item")
                                # Remove the torrent if it exists
                                torrent_id = cache_info.get('torrent_id')
                                if torrent_id:
                                    try:
                                        self.debrid_provider.remove_torrent(torrent_id)
                                        logging.info(f"Removed torrent {torrent_id} from Real-Debrid - no suitable file found")
                                    except Exception as e:
                                        logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
                                continue
                        else:
                            logging.warning("No files found in cache response")
                            # Remove the torrent if it exists
                            torrent_id = add_result.get('data', {}).get('torrent_id')
                            if torrent_id:
                                try:
                                    self.debrid_provider.remove_torrent(torrent_id)
                                    logging.info(f"Removed torrent {torrent_id} from Real-Debrid - no files found")
                                except Exception as e:
                                    logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
                            continue
                        self.remove_item(item)
                        return
                    else:
                        logging.info("Result is not cached, checking next result")
                        # Remove the uncached torrent from Real-Debrid
                        torrent_id = cache_info.get('torrent_id')
                        if torrent_id:
                            try:
                                self.debrid_provider.remove_torrent(torrent_id)
                                logging.info(f"Removed uncached torrent {torrent_id} from Real-Debrid")
                            except Exception as e:
                                logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
            except Exception as e:
                logging.error(f"Error checking result: {str(e)}")
                continue

        # If we get here, no valid results were found
        logging.error("No valid results found")
        self._handle_failed_item(queue_manager, item)

    def _get_scrape_results(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get and validate scrape results for an item"""
        scrape_results = item.get('scrape_results', [])
        #logging.debug(f"Raw scrape results: {scrape_results}")
        
        if not scrape_results:
            logging.error("No scrape results found")
            return []

        # Handle string (JSON) format
        if isinstance(scrape_results, str):
            try:
                scrape_results = json.loads(scrape_results)
                #logging.debug(f"Parsed JSON scrape results: {scrape_results}")
            except json.JSONDecodeError:
                logging.error("Failed to decode scrape results JSON")
                return []

        # Ensure we have a list
        if not isinstance(scrape_results, list):
            logging.error(f"Scrape results is not a list, got type: {type(scrape_results)}")
            return []

        # Log the first result for debugging
        #if scrape_results:
            #logging.debug(f"First result structure: {scrape_results[0]}")

        return scrape_results

    def _extract_hash_from_magnet(self, magnet: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract hash from magnet link or download and parse torrent file
        
        Returns:
            Tuple of (hash, temp_file_path). temp_file_path will be None for magnet links
        """
        try:
            # Check if this is a magnet link
            if magnet.startswith('magnet:'):
                btih_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
                if btih_match:
                    return btih_match.group(1).lower(), None
            # Check if this is a Jackett URL
            elif 'jackett' in magnet.lower():
                logging.debug(f"Downloading torrent from Jackett URL: {magnet}")
                try:
                    # Download the torrent file to a temporary location
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp_file:
                        response = requests.get(magnet, timeout=10)
                        if response.status_code != 200:
                            logging.error(f"Failed to download torrent file: {response.status_code}")
                            return None, None
                            
                        # Check content type and headers
                        logging.debug(f"Response headers: {dict(response.headers)}")
                        content_type = response.headers.get('content-type', '')
                        logging.debug(f"Content type: {content_type}")
                        
                        # Get content and validate
                        content = response.content
                        logging.debug(f"Content first 100 bytes: {content[:100]}")
                        
                        if not content:
                            logging.error("Empty torrent file content")
                            return None, None
                            
                        # Try to decode as bencode to validate
                        try:
                            torrent_data = bencodepy.decode(content)
                            logging.debug(f"Decoded torrent data keys: {[k.decode() if isinstance(k, bytes) else k for k in torrent_data.keys()]}")
                            
                            if not isinstance(torrent_data, dict) or b'info' not in torrent_data:
                                logging.error("Invalid torrent file structure")
                                return None, None

                            # Extract hash from info dictionary
                            info = torrent_data[b'info']
                            info_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest()
                            logging.debug(f"Extracted info hash: {info_hash}")

                            # Write validated content to temp file
                            tmp_file.write(content)
                            tmp_file.flush()
                            return info_hash.lower(), tmp_file.name

                        except Exception as e:
                            logging.error(f"Failed to decode torrent data: {str(e)}")
                            return None, None

                except requests.exceptions.RequestException as e:
                    logging.error(f"Error downloading torrent file: {str(e)}")
                    return None, None
                except Exception as e:
                    logging.error(f"Unexpected error handling torrent file: {str(e)}")
                    return None, None

            logging.warning(f"Unrecognized magnet/URL format: {magnet}")
            return None, None

        except Exception as e:
            logging.error(f"Error extracting hash: {str(e)}")
            return None, None

    def _handle_failed_item(self, queue_manager: Any, item: Dict[str, Any]):
        """Handle a failed item by moving it to the appropriate queue"""
        from datetime import datetime, timedelta
        
        # Check if item is old (>7 days from release date)
        release_date_str = item.get('release_date')
        if release_date_str:
            try:
                release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
                days_old = (datetime.now() - release_date).days
                
                if days_old > 7:
                    logging.info(f"Item is {days_old} days old, blacklisting: {item.get('title')}")
                    blacklist_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    update_media_item_state(item['id'], 'Blacklisted', blacklisted_date=blacklist_date)
                    
                    # If this is a TV show, blacklist other episodes from same season in scraping queue
                    if item.get('type') == 'episode':
                        series_title = item.get('series_title', '') or item.get('title', '')
                        season = item.get('season') or item.get('season_number')
                        version = item.get('version')
                        
                        if series_title and season is not None and version:
                            scraping_items = self._get_scraping_queue_items(None)
                            if scraping_items:
                                # Find matching episodes from same show, season and version
                                matching_items = [
                                    i for i in scraping_items
                                    if ((i.get('series_title', '') or i.get('title', '')) == series_title and
                                        (i.get('season') or i.get('season_number')) == season and
                                        i.get('version') == version)
                                ]
                                
                                if matching_items:
                                    logging.info(f"Blacklisting {len(matching_items)} other episodes from '{series_title}' S{season} [{version}]")
                                    for match in matching_items:
                                        episode = match.get('episode') or match.get('episode_number')
                                        update_media_item_state(match['id'], 'Blacklisted', 
                                                             blacklisted_date=blacklist_date)
                                        logging.info(f"Blacklisted S{season}E{episode} of '{series_title}' [{version}]")
                else:
                    logging.info(f"Item is only {days_old} days old, sleeping: {item.get('title')}")
                    update_media_item_state(item['id'], 'Sleeping')
            except ValueError:
                logging.error(f"Invalid release date format: {release_date_str}")
                update_media_item_state(item['id'], 'Sleeping')
        else:
            logging.warning(f"No release date for item, sleeping: {item.get('title')}")
            update_media_item_state(item['id'], 'Sleeping')
            
        self.remove_item(item)

    def get_new_item_values(self, item: Dict[str, Any]) -> Dict[str, Any]:
        # Fetch the updated item from the database
        updated_item = get_media_item_by_id(item['id'])
        if updated_item:
            # Extract the new values that need to be updated
            new_values = {
                'filled_by_title': updated_item.get('filled_by_title'),
                'filled_by_magnet': updated_item.get('filled_by_magnet'),
                'filled_by_file': updated_item.get('filled_by_file'),
                'filled_by_torrent_id': updated_item.get('filled_by_torrent_id'),
                'version': updated_item.get('version'),
                # Include any other fields that were updated
            }
            return new_values
        else:
            logging.warning(f"Could not retrieve updated item for ID {item['id']}")
            return {}