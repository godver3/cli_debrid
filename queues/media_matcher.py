"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
import os
from typing import Dict, Any, List, Tuple, Optional
from scraper.functions.ptt_parser import parse_with_ptt
from fuzzywuzzy import fuzz

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
            
            parsed = parse_with_ptt(file['path'])
            logging.debug(f"Parsed file: {parsed}")
            
            # For anime, we only need to match the episode number
            if is_anime:
                episode_match = item_episode in parsed.get('episodes', [])
                # Only check season if it's present in both the parsed result and the item
                season_match = (not parsed.get('seasons') or  # If no season in filename, that's okay
                              not item_season or  # If no season in item, that's okay
                              item_season in parsed.get('seasons', []))  # If both have season, they should match
                
                if episode_match and season_match:
                    logging.debug(f"Matched anime: E{item_episode} in {os.path.basename(file['path'])}")
                    file_path = os.path.basename(file['path'])
                    matches.append((file_path, item))
                    logging.debug(f"Added match for anime: {file_path}")
            else:
                # Regular TV show matching requiring both season and episode
                if (item_season in parsed.get('seasons', []) and 
                    item_episode in parsed.get('episodes', [])):
                    logging.debug(f"Matched: S{item_season}E{item_episode} in {os.path.basename(file['path'])}")
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
