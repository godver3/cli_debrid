"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
import os
from typing import Dict, Any, List, Tuple, Optional
from fuzzywuzzy import fuzz
from PTT import parse_title

class MediaMatcher:
    """Handles media content matching and validation"""
    
    def __init__(self):
        self.episode_count_cache: Dict[str, Dict[int, int]] = {}

    def match_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Match content files against a media item.
        
        Args:
            files: List of files to match
            item: Media item to match against
            
        Returns:
            List of matched files with their corresponding items
        """
        logging.debug(f"Matching content for {item.get('title')} ({item.get('type')})")
        if item.get('type') == 'movie':
            return self._match_movie_content(files, item)
        elif item.get('type') == 'episode':
            return self._match_tv_content(files, item)
        return []

    def _match_movie_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """Match movie files against a movie item by taking the largest video file"""
        video_files = []
        
        # Get all video files with their sizes
        for file in files:
            if not self.is_video_file(file['path']):
                continue
                
            # Skip sample files
            if 'sample' in file['path'].lower():
                continue
                
            video_files.append(file)
        
        if not video_files:
            logging.debug("No video files found")
            return []
            
        # Sort by size descending and take the largest
        largest_file = max(video_files, key=lambda x: x.get('bytes', 0))
        logging.debug(f"Selected largest video: {largest_file['path']}")
        
        # Get just the filename using os.path.basename
        file_path = os.path.basename(largest_file['path'])
        
        return [(file_path, item)]

    def _extract_episode_from_filename(self, filename: str) -> Optional[int]:
        """
        Fallback method to extract episode numbers from filenames when PTT fails.
        Handles cases like '999 1.mp4' or 'ep1.mp4'
        """
        import re
        
        # Remove the file extension
        basename = os.path.splitext(os.path.basename(filename))[0]
        
        # Try various patterns
        patterns = [
            r'(?:^|\D)(\d{1,4})(?:\D|$)',  # Matches standalone numbers like "1" or "001"
            r'(?:ep|episode)[.\s-]*(\d{1,4})',  # Matches "ep1" or "episode 1"
            r'[eE](\d{1,4})',  # Matches "E1" or "e01"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, basename)
            if match:
                try:
                    episode_num = int(match.group(1))
                    if 0 < episode_num < 2000:  # Sanity check for reasonable episode numbers
                        return episode_num
                except ValueError:
                    continue
        
        return None

    def _match_tv_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Match TV show files against an episode item.
        Returns all matching files with their season/episode info.
        Special handling for anime which may not include season numbers.
        """
        matches = []
        series_title = item.get('series_title', '') or item.get('title', '')
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')
        # Handle both string and list genres
        genres = item.get('genres', [])
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        
        if not all([series_title, item_episode is not None]):
            logging.debug(f"Missing required TV info: title='{series_title}', E{item_episode}")
            return []
        
        if is_anime:
            logging.debug(f"Matching anime: '{series_title}' E{item_episode}")
        else:
            if item_season is None:
                logging.debug(f"Missing season number for non-anime content")
                return []
            logging.debug(f"Matching TV: '{series_title}' S{item_season}E{item_episode}")
        
        for file in files:
            if not self.is_video_file(file['path']):
                continue
            
            # Skip sample files
            if 'sample' in file['path'].lower():
                continue

            if 'specials' in file['path'].lower():
                continue
            
            # Get the raw PTT parse result
            ptt_result = parse_title(file['path'])
            logging.debug(f"PTT parsed result: {ptt_result}")
            
            # Create our result info, preserving all PTT fields
            result_info = {
                'title': ptt_result.get('title'),
                'original_title': file['path'],
                'type': 'movie',
                'year': None,
                'resolution': ptt_result.get('resolution'),
                'source': ptt_result.get('quality'),
                'audio': None,
                'codec': ptt_result.get('codec'),
                'group': ptt_result.get('group'),
                'seasons': ptt_result.get('seasons', []),
                'episodes': ptt_result.get('episodes', []),
                'date': ptt_result.get('date'),  # Explicitly include the date field
                'bit_depth': ptt_result.get('bit_depth'),  # Include other useful fields
                'container': ptt_result.get('container')
            }
            logging.debug(f"Combined result info: {result_info}")
            
            # For anime, we only need to match the episode number
            if is_anime:
                episode_match = False
                
                # First try PTT parsed episodes
                if item_episode in result_info.get('episodes', []):
                    episode_match = True
                
                # If PTT failed to find episodes, try our fallback method
                if not episode_match and not result_info.get('episodes'):
                    fallback_episode = self._extract_episode_from_filename(file['path'])
                    if fallback_episode == item_episode:
                        episode_match = True
                        logging.debug(f"Matched episode {item_episode} using fallback parser for {file['path']}")
                
                # Only check season if it's present in both the parsed result and the item
                season_match = (not result_info.get('seasons') or  # If no season in filename, that's okay
                              not item_season or  # If no season in item, that's okay
                              item_season in result_info.get('seasons', []) or  # If both have season, they should match
                              (is_anime and 0 in result_info.get('seasons', [])))  # For anime, season 0 matches any season
                
                if episode_match and season_match:
                    logging.debug(f"Matched anime: E{item_episode} in {os.path.basename(file['path'])}")
                    file_path = os.path.basename(file['path'])
                    matches.append((file_path, item))
                    logging.debug(f"Added match for anime: {file_path}")
            else:
                # Check if this is a date-based episode first
                date_match = False
                has_date = 'date' in result_info and result_info['date'] is not None
                has_seasons = bool(result_info.get('seasons'))
                has_episodes = bool(result_info.get('episodes'))
                logging.debug(f"Checking if date-based episode - Has date: {has_date}, Has seasons: {has_seasons}, Has episodes: {has_episodes}")
                
                if has_date and not has_seasons and not has_episodes:
                    try:
                        logging.debug(f"Attempting date match for date-based episode")

                        from web_scraper import get_tmdb_data
                        episode_data = get_tmdb_data(int(item.get('tmdb_id')), 'tv', item.get('season_number'), item.get('episode_number'))
                        
                        if episode_data:
                            expected_air_date = episode_data.get('air_date')
                            logging.debug(f"Date comparison - File date: {result_info['date']}, Expected air date: {expected_air_date}")
                            
                            if expected_air_date and result_info['date'] == expected_air_date:
                                date_match = True
                                logging.debug(f"Matched date-based episode by air date: {expected_air_date} == {result_info['date']}")
                            else:
                                logging.debug(f"Date match failed - Missing air_date: {not expected_air_date}, Dates don't match: {expected_air_date != result_info.get('date')}")
                    except Exception as e:
                        logging.error(f"Error checking air date: {str(e)}")
                
                # Only try season/episode matching if date matching failed
                season_episode_match = False
                if not date_match:
                    logging.debug("Date match failed or not applicable, trying season/episode matching")
                    season_episode_match = (item_season in result_info.get('seasons', []) and 
                                          item_episode in result_info.get('episodes', []))
                    
                    # Try TMDB date lookup as last resort for season/episode files
                    if not season_episode_match and result_info.get('date'):
                        try:
                            if item_season is not None and item_episode is not None:
                                logging.debug(f"Attempting TMDB lookup for S{item_season}E{item_episode}")

                                from web_scraper import get_tmdb_data
                                episode_data = get_tmdb_data(int(item.get('tmdb_id')), 'tv', item_season, item_episode)
                                
                                if episode_data:
                                    air_date = episode_data.get('air_date')
                                    logging.debug(f"TMDB lookup - File date: {result_info.get('date')}, TMDB air date: {air_date}")
                                    if air_date and result_info['date'] == air_date:
                                        date_match = True
                                        logging.debug(f"Matched by air date: {air_date} == {result_info['date']}")
                                    else:
                                        logging.debug(f"TMDB date match failed - Missing air_date: {not air_date}, Dates don't match: {air_date != result_info.get('date')}")
                                else:
                                    logging.debug("Failed to get episode data from TMDB")
                        except Exception as e:
                            logging.error(f"Error checking air date: {str(e)}")

                if season_episode_match or date_match:
                    logging.debug(f"Matched: {'by date' if date_match else f'S{item_season}E{item_episode}'} in {os.path.basename(file['path'])}")
                    file_path = os.path.basename(file['path'])
                    matches.append((file_path, item))
            
        if not matches:
            if is_anime:
                logging.debug(f"No matches found for anime: {series_title} E{item_episode}")
            else:
                logging.debug(f"No matches found for TV: {series_title} S{item_season}E{item_episode}")
        
        return matches

    def match_movie(self, parsed: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        """
        Check if a movie file matches a movie item.
        Matches based on the queue item title and year.
        """
        # Get the parsed title from the file
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('title', ''))
        if not parsed_title or not queue_title:
            return False
            
        # Match based on normalized title match
        title_match = parsed_title == queue_title
        
        # Be lenient about year matching - only check if both years are present
        year_match = True
        if parsed.get('year') and item.get('year'):
            year_match = self._is_acceptable_year_mismatch(item, parsed)
        
        logging.debug(f"Movie match: '{parsed.get('title')}' ({parsed.get('year')}) -> {title_match and year_match}")
        
        # Match only if both title and year match
        return title_match and year_match

    def match_episode(self, parsed: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """
        Check if an episode file matches an episode item.
        Matches based on the queue item title, season, and episode numbers.
        """
        # Skip files that are likely extras/specials
        original_title = parsed.get('original_title', '').lower()
        if any(extra in original_title for extra in [
            'deleted scene', 'deleted scenes',
            'extra', 'extras',
            'special', 'specials',
            'behind the scene', 'behind the scenes',
            'bonus', 'interview',
            'featurette', 'making of',
            'alternate'
        ]):
            logging.debug(f"Skipping extras/special: {original_title}")
            return False
            
        # Get parsed title and queue title
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('series_title', '') or item.get('title', ''))
        if not parsed_title or not queue_title:
            return False
            
        # Match based on normalized title match
        title_match = parsed_title == queue_title
            
        # Get season/episode from various possible fields in the item
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')
        
        if item_season is None or item_episode is None:
            return False
            
        # Check if the requested season and episode are in the parsed seasons/episodes lists
        season_match = item_season in parsed.get('seasons', [])
        episode_match = item_episode in parsed.get('episodes', [])
        
        match_result = title_match and season_match and episode_match
        if match_result:
            logging.debug(f"Matched episode: '{parsed.get('title')}' S{item_season}E{item_episode}")
        
        # Match only if title, season, and episode all match
        return match_result

    @staticmethod
    def is_video_file(filename: str) -> bool:
        """Check if a file is a video file based on extension"""
        video_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov'}
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for comparison by removing special characters and whitespace"""
        if not title:
            return ''
        # Remove special characters and normalize spaces
        normalized = title.lower()
        normalized = ''.join(c for c in normalized if c.isalnum() or c.isspace())
        normalized = ' '.join(normalized.split())  # Normalize whitespace
        return normalized

    @staticmethod
    def _is_acceptable_year_mismatch(item: Dict[str, Any], parsed: Dict[str, Any]) -> bool:
        """Check if year mismatch is acceptable (within 1 year)"""
        item_year = item.get('year')
        parsed_year = parsed.get('year')
        if not item_year or not parsed_year:
            return False
        return abs(int(item_year) - int(parsed_year)) <= 1

    def find_related_items(self, files: List[Dict[str, Any]], scraping_items: List[Dict[str, Any]], original_item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Find items in the scraping queue that match files in the torrent.
        
        Args:
            files: List of files from the torrent
            scraping_items: List of items currently in scraping state
            original_item: The original item being processed, used to match version
            
        Returns:
            List of items from scraping_items that match files in the torrent
        """
        related_items = []
        original_version = original_item.get('version')
        original_title = original_item.get('title')
        
        for item in scraping_items:
            # Skip if not an episode, different version, or different title
            if (item.get('type') != 'episode' or 
                item.get('version') != original_version or
                item.get('title') != original_title):
                continue
                
            # Try to match this item against the files
            matches = self._match_tv_content(files, item)
            if matches:
                logging.debug(f"Found related: S{item.get('season_number')}E{item.get('episode_number')} ({item.get('version')})")
                related_items.append(item)
                
        return related_items
