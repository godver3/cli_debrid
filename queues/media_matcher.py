"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
import os
import re
from typing import Dict, Any, List, Tuple, Optional
from fuzzywuzzy import fuzz
from PTT import parse_title

class MediaMatcher:
    """Handles media content matching and validation"""
    
    def __init__(self, relaxed_matching: bool = False):
        self.episode_count_cache: Dict[str, Dict[int, int]] = {}
        self.relaxed_matching = relaxed_matching

    def _parse_file_info(self, file_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parses a single file's path, checks validity, and returns structured info.

        Args:
            file_dict: Dictionary representing the file {'path': str, 'bytes': int}

        Returns:
            A dictionary with 'path', 'bytes', and 'parsed_info' if valid, otherwise None.
        """
        file_path = file_dict['path']
        if not self.is_video_file(file_path):
            return None
        if 'sample' in file_path.lower():
            return None
        if 'specials' in file_path.lower(): # Keep filtering specials here
             return None

        ptt_result = parse_title(file_path)
        # Use PTT result directly as parsed_info for now
        # Potentially add more structured fields later if needed
        parsed_info = ptt_result
        parsed_info['original_filename'] = os.path.basename(file_path) # Store basename for potential fallback use

        # Attempt fallback episode extraction if PTT fails for episodes
        if not parsed_info.get('episodes'):
             fallback_episode = self._extract_episode_from_filename(file_path)
             if fallback_episode is not None:
                  # Store this separately to avoid overriding PTT potentially empty list
                  parsed_info['fallback_episode'] = fallback_episode

        return {
            'path': file_path,
            'bytes': file_dict.get('bytes', 0),
            'parsed_info': parsed_info
        }

    def _check_match(self, parsed_file_info: Dict[str, Any], item: Dict[str, Any], use_relaxed_matching: bool) -> bool:
        """
        Checks if a pre-parsed file info dictionary matches a media item (TV Episode logic).

        Args:
            parsed_file_info: The dictionary returned by _parse_file_info.
            item: The media item (episode) to match against.
            use_relaxed_matching: Flag for relaxed matching rules.

        Returns:
            True if the file matches the item, False otherwise.
        """
        ptt_result = parsed_file_info['parsed_info'] # Get the PTT result stored earlier
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')

        # Check required item fields (already done in original caller, but good safeguard)
        series_title = item.get('series_title', '') or item.get('title', '')
        if not all([series_title, item_episode is not None]):
            return False
        if not use_relaxed_matching and item_season is None:
            return False

        # --- Relaxed Matching Logic ---
        if use_relaxed_matching:
            episode_match = False
            # Check PTT episodes
            if item_episode in ptt_result.get('episodes', []):
                episode_match = True
            # Check fallback episode if PTT episodes are empty
            elif not ptt_result.get('episodes') and ptt_result.get('fallback_episode') == item_episode:
                episode_match = True

            # Season matching (allows missing season in filename or item)
            season_match = (not ptt_result.get('seasons') or
                          not item_season or
                          item_season in ptt_result.get('seasons', []) or
                          (use_relaxed_matching and 0 in ptt_result.get('seasons', []))) # Season 0 relaxed match

            return episode_match and season_match

        # --- Strict Matching Logic ---
        else:
            # Check date-based first (if file parsed as date-based)
            date_match = False
            has_date = 'date' in ptt_result and ptt_result['date'] is not None
            has_seasons = bool(ptt_result.get('seasons'))
            has_episodes = bool(ptt_result.get('episodes'))

            if has_date and not has_seasons and not has_episodes:
                # Date matching logic (TMDB lookup) - requires item TMDB ID etc.
                # This might be better placed outside the pure check, or require more info passed in.
                # For now, keep the original logic flow structure. We assume TMDB lookup happens later if needed.
                # Let's replicate the original check structure approximately.
                 try:
                      if item.get('tmdb_id') and item.get('season_number') is not None and item.get('episode_number') is not None:
                           from utilities.web_scraper import get_tmdb_data
                           episode_data = get_tmdb_data(int(item['tmdb_id']), 'tv', item['season_number'], item['episode_number'])
                           if episode_data and episode_data.get('air_date') == ptt_result['date']:
                                date_match = True
                 except Exception as e:
                      logging.warning(f"Could not perform date check during match: {e}") # Warn instead of error

            # Season/Episode matching
            season_episode_match = False
            if not date_match: # Only check if date didn't match
                season_match = item_season in ptt_result.get('seasons', [])
                episode_match = item_episode in ptt_result.get('episodes', [])
                # Also check fallback episode if PTT episodes are empty
                if not ptt_result.get('episodes') and ptt_result.get('fallback_episode') == item_episode:
                    episode_match = True
                season_episode_match = season_match and episode_match

                # Last resort: check file date against TMDB date for season/episode files
                if not season_episode_match and ptt_result.get('date'):
                    try:
                         if item.get('tmdb_id') and item_season is not None and item_episode is not None:
                             from utilities.web_scraper import get_tmdb_data
                             episode_data = get_tmdb_data(int(item['tmdb_id']), 'tv', item_season, item_episode)
                             if episode_data and episode_data.get('air_date') == ptt_result['date']:
                                 date_match = True # Consider it a date match if air dates align
                    except Exception as e:
                         logging.warning(f"Could not perform fallback date check: {e}")

            return season_episode_match or date_match

    def find_best_match_from_parsed(self, parsed_files: List[Dict[str, Any]], item: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Finds the best matching file for a single item from a list of pre-parsed file info.

        Args:
            parsed_files: List of dictionaries returned by _parse_file_info.
            item: The media item to match.

        Returns:
            A tuple (matching_filepath, item) if a match is found, otherwise None.
            For movies, returns the largest video file path.
            For episodes, returns the first file that matches season/episode criteria.
        """
        item_type = item.get('type')

        # --- Movie Logic (Find largest video file) ---
        if item_type == 'movie':
            video_files = []
            for parsed_file in parsed_files:
                 # _parse_file_info already filtered non-video/samples
                 video_files.append(parsed_file)

            if not video_files:
                return None

            # Sort by size descending and take the largest
            largest_file_info = max(video_files, key=lambda x: x.get('bytes', 0))
            return (os.path.basename(largest_file_info['path']), item) # Return basename path and item

        # --- TV Episode Logic ---
        elif item_type == 'episode':
            # Determine if relaxed matching should be used (copied from original _match_tv_content)
            genres = item.get('genres', [])
            if isinstance(genres, str):
                genres = [genres]
            is_anime = any('anime' in genre.lower() for genre in genres)
            from utilities.settings import get_setting
            file_collection_management = get_setting('File Management', 'file_collection_management')
            using_plex = file_collection_management == 'Plex'
            use_relaxed_matching = not using_plex and (is_anime or self.relaxed_matching)

            for parsed_file_info in parsed_files:
                if self._check_match(parsed_file_info, item, use_relaxed_matching):
                    # Return the first match found
                    return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
            return None # No match found

        # --- Unknown Type ---
        else:
            logging.warning(f"Unknown item type '{item_type}' in find_best_match_from_parsed")
            return None

    def _extract_episode_from_filename(self, filename: str) -> Optional[int]:
        """
        Fallback method to extract episode numbers from filenames when PTT fails.
        Handles cases like '999 1.mp4' or 'ep1.mp4'
        """
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

    def match_movie(self, parsed: Dict[str, Any], item: Dict[str, Any], filename: str) -> bool:
        """
        Check if a movie file matches a movie item.
        Matches based on the queue item title and year.
        NOTE: This uses PTT parsed info, assumes 'parsed' is the result of PTT.
              It's likely NOT used directly by AddingQueue flow anymore.
        """
        # Get the parsed title from the file
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('title', ''))
        if not parsed_title or not queue_title:
            return False

        # Match based on normalized title match using fuzzy matching for robustness
        # title_match = parsed_title == queue_title
        title_ratio = fuzz.ratio(parsed_title, queue_title)
        title_match = title_ratio > 85 # Use a threshold (e.g., 85%) for title match

        # Be lenient about year matching - only check if both years are present
        year_match = self._is_acceptable_year_mismatch(item, parsed)

        # Match only if both title and year match
        logging.debug(f"Movie match check: '{parsed_title}' vs '{queue_title}' (Ratio: {title_ratio}), Year Match: {year_match} -> Result: {title_match and year_match}")
        return title_match and year_match

    def match_episode(self, parsed: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """
        Check if an episode file matches an episode item.
        Matches based on the queue item title, season, and episode numbers.
        NOTE: This uses PTT parsed info, assumes 'parsed' is the result of PTT.
              It's likely NOT used directly by AddingQueue flow anymore.
        """
        # Skip files that are likely extras/specials based on filename from PTT result
        original_filename = parsed.get('original_filename', '').lower() # Use stored basename
        if any(extra in original_filename for extra in [
            'deleted scene', 'deleted scenes',
            'extra', 'extras',
            'special', 'specials',
            'behind the scene', 'behind the scenes',
            'bonus', 'interview',
            'featurette', 'making of',
            'alternate'
        ]):
            return False

        # Traditional matching
        parsed_title = self._normalize_title(parsed.get('title', ''))
        queue_title = self._normalize_title(item.get('series_title', '') or item.get('title', ''))
        if not parsed_title or not queue_title:
            return False

        # Title match using fuzzy matching
        # title_match = parsed_title == queue_title
        title_ratio = fuzz.ratio(parsed_title, queue_title)
        title_match = title_ratio > 85 # Use a threshold

        # Get season/episode from item
        item_season = item.get('season') or item.get('season_number')
        item_episode = item.get('episode') or item.get('episode_number')

        if item_season is None or item_episode is None:
            return False

        # Check if the requested season and episode are in the parsed seasons/episodes lists
        season_match = item_season in parsed.get('seasons', [])
        episode_match = item_episode in parsed.get('episodes', [])

        # Check fallback episode if PTT episodes are empty
        if not parsed.get('episodes') and parsed.get('fallback_episode') == item_episode:
             episode_match = True

        # Match only if title, season, and episode all match
        match_result = title_match and season_match and episode_match
        #logging.debug(f"Episode match check: Title Match: {title_match} (Ratio: {title_ratio}), S: {season_match}, E: {episode_match} -> Result: {match_result}")
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
            return False # Changed to False if either year is missing for stricter check maybe? Or True? Let's keep original logic: True if one is missing.
        # Revert to original logic: If one year is missing, it's acceptable. Only compare if both exist.
        if item_year and parsed_year:
             return abs(int(item_year) - int(parsed_year)) <= 1
        return True # Acceptable if one or both years are missing

    def find_related_items(self, parsed_torrent_files: List[Dict[str, Any]], scraping_items: List[Dict[str, Any]], wanted_items: List[Dict[str, Any]], original_item: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str]]:
        """
        Find items in the scraping and wanted queues that match pre-parsed files in the torrent.

        Args:
            parsed_torrent_files: List of dictionaries from _parse_file_info for files in the torrent.
            scraping_items: List of items currently in scraping state.
            wanted_items: List of items currently in wanted state.
            original_item: The original item being processed, used to match version/title.

        Returns:
            List of tuples, where each tuple contains (related_item_dict, matching_filepath_basename).
        """
        related_matches = []
        original_version = original_item.get('version')
        # Ensure consistent title check (e.g., using series_title if available)
        original_title_to_check = original_item.get('series_title') or original_item.get('title')

        all_candidate_items = scraping_items + wanted_items
        processed_item_ids = set() # Prevent adding the same item ID twice

        logging.debug(f"Checking {len(all_candidate_items)} candidate items against {len(parsed_torrent_files)} parsed files.")

        # Determine relaxed matching based on original item (assuming related items follow same logic)
        genres = original_item.get('genres', [])
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        from utilities.settings import get_setting
        file_collection_management = get_setting('File Management', 'file_collection_management')
        using_plex = file_collection_management == 'Plex'
        # Apply relaxed matching globally based on the original item context
        use_relaxed_matching_for_all = not using_plex and (is_anime or self.relaxed_matching)

        for item in all_candidate_items:
            item_id = item.get('id')
            if not item_id or item_id in processed_item_ids:
                continue

            # Basic filtering for relevance
            item_title_to_check = item.get('series_title') or item.get('title')
            if (item.get('type') != 'episode' or
                item.get('version') != original_version or
                item_title_to_check != original_title_to_check):
                continue

            # --- Apply XEM mapping logic directly to the candidate item for matching ---
            # This part is complex as XEM was applied based on the *chosen scrape result* before.
            # We don't have that context easily here.
            # Simplification: Assume related items use their absolute S/E for matching for now.
            # A more robust solution would require passing XEM context differently.
            item_for_matching = item # Use the item directly

            # --- Find Match in Parsed Files ---
            found_match_for_this_item = False
            for parsed_file_info in parsed_torrent_files:
                if self._check_match(parsed_file_info, item_for_matching, use_relaxed_matching_for_all):
                    logging.info(f"Found related item ID {item_id} (State: {item.get('state', 'Unknown')}) matching file '{parsed_file_info['path']}'")
                    # Store the item and the *basename* of the matched file path
                    related_matches.append((item, os.path.basename(parsed_file_info['path'])))
                    processed_item_ids.add(item_id) # Mark as processed
                    found_match_for_this_item = True
                    break # Move to the next candidate item once a match is found for this one

            # Optional: Debug log for non-matches
            # if not found_match_for_this_item:
            #     logging.debug(f"No file match found for candidate item ID {item_id}")

        logging.debug(f"Found {len(related_matches)} related items matching files in total.")
        return related_matches
