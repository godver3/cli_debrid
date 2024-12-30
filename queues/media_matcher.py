"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
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
        logging.debug(f"Matching content for item: {item}")
        if item.get('type') == 'movie':
            return self._match_movie_content(files, item)
        elif item.get('type') == 'episode':
            return self._match_tv_content(files, item)
        return []

    def _match_movie_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """Match movie files against a movie item"""
        matches = []
        for file in files:
            if not self.is_video_file(file['path']):
                continue
                
            parsed = parse_with_ptt(file['path'])
            logging.debug(f"Movie parse for {file['path']}: {parsed}")
            if self.match_movie(parsed, item, file['path']):
                matches.append((file['path'], item))
        return matches

    def _match_tv_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """Match TV show files against an episode item"""
        matches = []
        for file in files:
            if not self.is_video_file(file['path']):
                continue
                
            parsed = parse_with_ptt(file['path'])
            logging.debug(f"TV parse for {file['path']}: {parsed}")
            if self.match_episode(parsed, item):
                matches.append((file['path'], item))
            else:
                logging.debug(f"No match: title={parsed.get('title')}=={item.get('series_title')}, "
                            f"seasons={parsed.get('seasons')}=={item.get('season')}, "
                            f"episodes={parsed.get('episodes')}=={item.get('episode')}")
        return matches

    def match_movie(self, parsed: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        """
        Check if a movie file matches a movie item.
        Uses fuzzy matching for titles and is lenient about year matching
        since filenames can be inconsistent.
        """
        # Skip type check if we have no season/episode info
        if parsed.get('seasons') or parsed.get('episodes'):
            if parsed.get('type') != 'movie':
                logging.debug(f"Not a movie type: {parsed.get('type')}")
                return False
            
        parsed_title = self._normalize_title(parsed.get('title', ''))
        item_title = self._normalize_title(item.get('title', ''))
        
        # Use fuzzy matching for titles with a low threshold
        ratio = fuzz.ratio(parsed_title, item_title)
        title_match = ratio > 60  # Lower threshold for more lenient matching
        
        # Be lenient about year matching - only check if both years are present
        year_match = True
        if parsed.get('year') and item.get('year'):
            year_match = self._is_acceptable_year_mismatch(item, parsed)
        
        logging.debug(f"Title match: {title_match} ({parsed_title} == {item_title}, ratio: {ratio})")
        logging.debug(f"Year match: {year_match} ({parsed.get('year')} vs {item.get('year')})")
        
        # Match if either title or year matches
        return title_match or year_match

    def match_episode(self, parsed: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """
        Check if an episode file matches an episode item.
        Primarily matches on season and episode numbers, being lenient about title matching
        since filenames can be inconsistent.
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
            logging.debug(f"Skipping extras/special content: {original_title}")
            return False
            
        # Only check type if we have season/episode info
        if parsed.get('seasons') or parsed.get('episodes'):
            if parsed.get('type') != 'episode':
                logging.debug(f"Not an episode type: {parsed.get('type')}")
                return False
            
        # Get season/episode from various possible fields in the item
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')
        
        if item_season is None or item_episode is None:
            logging.debug(f"Missing season/episode in item: season={item_season}, episode={item_episode}")
            return False
            
        # Check if the requested season and episode are in the parsed seasons/episodes lists
        season_match = item_season in parsed.get('seasons', [])
        episode_match = item_episode in parsed.get('episodes', [])
        
        logging.debug(f"Season match: {season_match} ({item_season} in {parsed.get('seasons')})")
        logging.debug(f"Episode match: {episode_match} ({item_episode} in {parsed.get('episodes')})")
        
        # For TV shows, we primarily care about matching season and episode numbers
        return season_match and episode_match

    @staticmethod
    def is_video_file(filename: str) -> bool:
        """Check if a file is a video file based on extension"""
        video_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov'}
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for comparison"""
        if not title:
            return ''
        return title.lower().replace('.', ' ').replace('_', ' ').strip()

    @staticmethod
    def _is_acceptable_year_mismatch(item: Dict[str, Any], parsed: Dict[str, Any]) -> bool:
        """Check if year mismatch is acceptable (within 1 year)"""
        item_year = item.get('year')
        parsed_year = parsed.get('year')
        if not item_year or not parsed_year:
            return False
        return abs(int(item_year) - int(parsed_year)) <= 1
