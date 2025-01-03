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
        logging.info(f"Attempting to match content for item: {item.get('title')} (type: {item.get('type')})")
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
                logging.info(f"Skipping sample file: {file['path']}")
                continue
                
            video_files.append(file)
        
        if not video_files:
            logging.info("No video files found")
            return []
            
        # Sort by size descending and take the largest
        largest_file = max(video_files, key=lambda x: x.get('bytes', 0))
        logging.info(f"Selected largest video file: {largest_file['path']} ({largest_file.get('bytes', 0)} bytes)")
        
        # Get just the filename using os.path.basename
        file_path = os.path.basename(largest_file['path'])
        
        return [(file_path, item)]

    def _match_tv_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Match TV show files against an episode item.
        Returns all matching files with their season/episode info.
        """
        matches = []
        series_title = item.get('series_title', '') or item.get('title', '')
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')
        
        if not all([series_title, item_season is not None, item_episode is not None]):
            logging.info(f"Missing required TV info: title='{series_title}', S{item_season}E{item_episode}")
            return []
            
        logging.info(f"Matching TV show: '{series_title}' S{item_season}E{item_episode}")
        
        for file in files:
            if not self.is_video_file(file['path']):
                continue
                
            # Skip sample files
            if 'sample' in file['path'].lower():
                logging.info(f"Skipping sample file: {file['path']}")
                continue
                
            parsed = parse_with_ptt(file['path'])
            logging.info(f"Parsed file: {file['path']}")
            logging.info(f"Season/Episode info: seasons={parsed.get('seasons')}, episodes={parsed.get('episodes')}")
            
            # Check if this file matches our season/episode
            if (item_season in parsed.get('seasons', []) and 
                item_episode in parsed.get('episodes', [])):
                logging.info(f"✓ Matched TV episode: S{item_season}E{item_episode} in {file['path']}")
                # Get just the filename using os.path.basename
                file_path = os.path.basename(file['path'])
                matches.append((file_path, item))
            else:
                logging.info(f"✗ No match: S{item_season}E{item_episode} not in file")
                
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
            logging.info("Missing parsed title or queue title")
            return False
            
        # Match based on normalized title match
        title_match = parsed_title == queue_title
        
        # Be lenient about year matching - only check if both years are present
        year_match = True
        if parsed.get('year') and item.get('year'):
            year_match = self._is_acceptable_year_mismatch(item, parsed)
        
        logging.info(f"Movie match results:")
        logging.info(f"- Title: {title_match} (normalized: '{parsed_title}' == '{queue_title}')")
        logging.info(f"- Original titles: '{parsed.get('title', '')}' vs '{item.get('title', '')}'")
        logging.info(f"- Year: {year_match} ({parsed.get('year')} vs {item.get('year')})")
        
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
            logging.info(f"Skipping extras/special content: {original_title}")
            return False
            
        # Get parsed title and queue title
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('series_title', '') or item.get('title', ''))
        if not parsed_title or not queue_title:
            logging.info("Missing parsed title or queue title")
            return False
            
        # Match based on normalized title match
        title_match = parsed_title == queue_title
            
        # Get season/episode from various possible fields in the item
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')
        
        if item_season is None or item_episode is None:
            logging.info(f"Missing season/episode in item: season={item_season}, episode={item_episode}")
            return False
            
        # Check if the requested season and episode are in the parsed seasons/episodes lists
        season_match = item_season in parsed.get('seasons', [])
        episode_match = item_episode in parsed.get('episodes', [])
        
        logging.info(f"TV episode match results:")
        logging.info(f"- Title: {title_match} ('{parsed_title}' == '{queue_title}')")
        logging.info(f"- Season: {season_match} ({item_season} in {parsed.get('seasons', [])})")
        logging.info(f"- Episode: {episode_match} ({item_episode} in {parsed.get('episodes', [])})")
        
        # Match only if title, season, and episode all match
        return title_match and season_match and episode_match

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
        
        for item in scraping_items:
            # Skip if not an episode or different version
            if item.get('type') != 'episode' or item.get('version') != original_version:
                continue
                
            # Try to match this item against the files
            matches = self._match_tv_content(files, item)
            if matches:
                logging.info(f"Found related episode: {item.get('title')} S{item.get('season_number')}E{item.get('episode_number')} (version: {item.get('version')})")
                related_items.append(item)
                
        return related_items
