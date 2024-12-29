"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
from typing import Dict, Any, List, Tuple, Optional
from guessit import guessit

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
                
            guess = guessit(file['path'])
            if self.match_movie(guess, item, file['path']):
                matches.append((file['path'], item))
        return matches

    def _match_tv_content(self, files: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """Match TV show files against an episode item"""
        matches = []
        for file in files:
            if not self.is_video_file(file['path']):
                continue
                
            guess = guessit(file['path'])
            if self.match_episode(guess, item):
                matches.append((file['path'], item))
        return matches

    def match_movie(self, guess: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        """Check if a movie file matches a movie item"""
        if guess.get('type') != 'movie':
            return False
            
        title_match = self._normalize_title(guess.get('title', '')) == self._normalize_title(item.get('title', ''))
        year_match = guess.get('year') == item.get('year')
        
        return title_match and (year_match or self._is_acceptable_year_mismatch(item, guess))

    def match_episode(self, guess: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """Check if an episode file matches an episode item"""
        if guess.get('type') != 'episode':
            return False
            
        title_match = self._normalize_title(guess.get('title', '')) == self._normalize_title(item.get('series_title', ''))
        season_match = guess.get('season') == item.get('season')
        episode_match = guess.get('episode') == item.get('episode')
        
        return title_match and season_match and episode_match

    def handle_multi_episode_file(self, file_path: str, season: int, episodes: List[int], 
                                items: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
        """Handle files containing multiple episodes"""
        matches = []
        for item in items:
            if (item.get('season') == season and 
                item.get('episode') in episodes):
                matches.append((file_path, item))
        return matches

    @staticmethod
    def is_video_file(filename: str) -> bool:
        """Check if a file is a video file based on extension"""
        video_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.wmv', '.mov', '.flv'}
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for comparison"""
        if not title:
            return ''
        return ''.join(c.lower() for c in title if c.isalnum())

    @staticmethod
    def _is_acceptable_year_mismatch(item: Dict[str, Any], guess: Dict[str, Any]) -> bool:
        """Check if year mismatch is acceptable (within 1 year)"""
        if not (item.get('year') and guess.get('year')):
            return True
        return abs(item['year'] - guess['year']) <= 1
