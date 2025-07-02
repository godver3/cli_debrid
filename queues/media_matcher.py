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
        file_basename = os.path.basename(file_path)

        if not self.is_video_file(file_basename): # Check basename for extension
            return None
        if 'sample' in file_basename.lower(): # Check basename for 'sample'
            return None
        if 'specials' in file_basename.lower(): # Check basename for 'specials'
             return None

        ptt_result = parse_title(file_basename) # Parse only the basename
        
        # Ensure ptt_result is a dict, even if parse_title returns None or an unexpected type
        parsed_info = ptt_result if isinstance(ptt_result, dict) else {}

        parsed_info['original_filename'] = file_basename # Store basename

        # Attempt fallback episode extraction if PTT fails for episodes
        # PTT might return 'episodes': [] or no 'episodes' key at all.
        # We should trigger fallback if 'episodes' is empty or not present.
        if not parsed_info.get('episodes'): # This covers None or an empty list
             fallback_episode = self._extract_episode_from_filename(file_basename) # Use basename for fallback
             if fallback_episode is not None:
                  parsed_info['fallback_episode'] = fallback_episode
        
        return {
            'path': file_path, # Store original full path
            'bytes': file_dict.get('bytes', 0),
            'parsed_info': parsed_info
        }

    def _check_match(self, parsed_file_info: Dict[str, Any], item: Dict[str, Any], use_relaxed_matching: bool, xem_mapping: Optional[Dict[str, int]] = None) -> bool:
        """
        Checks if a pre-parsed file info dictionary matches a media item (TV Episode logic).

        Args:
            parsed_file_info: The dictionary returned by _parse_file_info.
            item: The media item (episode) to match against.
            use_relaxed_matching: Flag for relaxed matching rules.
            xem_mapping: Optional dictionary with 'season' and 'episode' keys from XEM.

        Returns:
            True if the file matches the item, False otherwise.
        """
        ptt_result = parsed_file_info['parsed_info'] # Get the PTT result stored earlier

        # Determine target season/episode: Use XEM if available, otherwise original item S/E
        target_season = item.get('season') or item.get('season_number')
        target_episode = item.get('episode') or item.get('episode_number')
        using_xem = False
        if xem_mapping and 'season' in xem_mapping and 'episode' in xem_mapping:
             # Validate XEM values are integers
             try:
                 xem_season = int(xem_mapping['season'])
                 xem_episode = int(xem_mapping['episode'])
                 logging.debug(f"Using XEM mapping for match check: S{xem_season}E{xem_episode} (Original: S{target_season}E{target_episode})")
                 target_season = xem_season
                 target_episode = xem_episode
                 using_xem = True
             except (ValueError, TypeError):
                  logging.warning(f"Invalid XEM mapping format encountered: {xem_mapping}. Falling back to original item S/E.")
                  # Fallback to original item S/E below
        # else: # No need for else, target_season/episode already hold original values
        #      logging.debug(f"Using original item S/E for match check: S{target_season}E{target_episode}")


        # Check required item fields (use target season/episode now)
        series_title = item.get('series_title', '') or item.get('title', '')
        if not all([series_title, target_episode is not None]):
            logging.debug(f"Match failed: Missing series title or target episode ({target_episode})")
            return False
        # Relaxed matching doesn't strictly require season, but strict does IF NOT using XEM
        if not use_relaxed_matching and target_season is None and not using_xem:
            logging.debug(f"Match failed: Strict matching requires season, but item season is None and not using XEM.")
            return False

        # --- Check if this is anime content ---
        genres = item.get('genres') or []
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)

        # --- Relaxed Matching Logic ---
        if use_relaxed_matching:
            episode_match = False
            # Check PTT episodes
            if ptt_result.get('episodes') and target_episode in ptt_result.get('episodes', []):
                episode_match = True
                logging.debug("Relaxed match: PTT episode matched target episode.")
            # Check fallback episode if PTT episodes are empty
            elif not ptt_result.get('episodes') and ptt_result.get('fallback_episode') == target_episode:
                episode_match = True
                logging.debug("Relaxed match: Fallback episode matched target episode.")

            # Season matching (allows missing season in filename or item, or season 0)
            season_match = (target_season is None or # If target season is None (only possible with XEM if mapping had None?)
                          not ptt_result.get('seasons') or # Filename has no season
                          target_season in ptt_result.get('seasons', []) or # Target season is in filename seasons
                          (0 in ptt_result.get('seasons', []))) # Filename has season 0

            # --- ANIME ABSOLUTE EPISODE MATCHING ---
            # Special handling for anime where season numbering might differ due to absolute episode formats
            if is_anime and not season_match and target_season is not None and target_episode is not None:
                try:
                    # Get season episode counts for absolute episode calculation
                    from database.database_reading import get_all_season_episode_counts
                    tmdb_id = item.get('tmdb_id')
                    if tmdb_id:
                        season_episode_counts = get_all_season_episode_counts(tmdb_id)
                        
                        # Calculate target absolute episode number
                        target_absolute_episode = 0
                        sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < target_season])
                        for s_num in sorted_seasons:
                            target_absolute_episode += season_episode_counts.get(s_num, 0)
                        target_absolute_episode += target_episode
                        
                        # Check if torrent uses absolute episode numbering (common patterns: S01E1134, E1134, 1134)
                        torrent_seasons = ptt_result.get('seasons', [])
                        torrent_episodes = ptt_result.get('episodes', [])
                        
                        # For anime, many releases use S01E{episode} where episode is actually the absolute episode number
                        # Pattern 1: S01E{episode} format where episode matches our target episode directly
                        if torrent_seasons == [1] and target_episode in torrent_episodes:
                            season_match = True
                            episode_match = True
                            logging.debug(f"Anime absolute match: S01E{target_episode} format matched (target S{target_season}E{target_episode}, treating as absolute numbering)")
                        
                        # Pattern 2: Check if calculated absolute episode number matches torrent episode
                        elif torrent_seasons == [1] and target_absolute_episode in torrent_episodes:
                            season_match = True
                            episode_match = True
                            logging.debug(f"Anime absolute match: S01E{target_absolute_episode} format matched (target S{target_season}E{target_episode} = abs {target_absolute_episode})")
                        
                        # Pattern 3: E{absolute} or {absolute} format (season might be empty)
                        elif not torrent_seasons and (target_episode in torrent_episodes or target_absolute_episode in torrent_episodes):
                            season_match = True  # Allow missing season for absolute format
                            episode_match = True
                            episode_used = target_episode if target_episode in torrent_episodes else target_absolute_episode
                            logging.debug(f"Anime absolute match: E{episode_used} or {episode_used} format matched (target S{target_season}E{target_episode})")
                        
                        # Pattern 4: Check if target episode or absolute episode appears in the original filename
                        original_filename = ptt_result.get('original_filename', '')
                        if original_filename:
                            import re
                            # Look for either the target episode or absolute episode as standalone numbers
                            target_patterns = [str(target_episode), str(target_absolute_episode)]
                            for pattern_num in target_patterns:
                                if re.search(rf'\b{pattern_num}\b', original_filename):
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Anime absolute match: Found episode {pattern_num} in filename '{original_filename}' (target S{target_season}E{target_episode})")
                                    break
                                
                except Exception as e:
                    logging.warning(f"Error during anime absolute episode matching: {e}")
                    # Continue with original season_match value
                    pass

            if season_match and episode_match:
                 logging.debug(f"Relaxed match successful: S:{season_match} E:{episode_match}")
                 return True
            else:
                 logging.debug(f"Relaxed match failed: S:{season_match} E:{episode_match}")
                 return False

        # --- Strict Matching Logic ---
        else:
            # Check date-based first (if file parsed as date-based)
            date_match = False
            has_date = 'date' in ptt_result and ptt_result['date'] is not None
            has_seasons = bool(ptt_result.get('seasons'))
            has_episodes = bool(ptt_result.get('episodes'))

            if has_date and not has_seasons and not has_episodes:
                 try:
                      # Use original item S/E for TMDB lookup as that identifies the actual episode
                      original_item_season = item.get('season') or item.get('season_number')
                      original_item_episode = item.get('episode') or item.get('episode_number')
                      if item.get('tmdb_id') and original_item_season is not None and original_item_episode is not None:
                           from utilities.web_scraper import get_tmdb_data
                           episode_data = get_tmdb_data(int(item['tmdb_id']), 'tv', original_item_season, original_item_episode)
                           if episode_data and episode_data.get('air_date') == ptt_result['date']:
                                logging.debug("Strict match: Date matched via TMDB lookup.")
                                date_match = True
                           else:
                                logging.debug(f"Strict match: Date mismatch (File: {ptt_result['date']}, TMDB: {episode_data.get('air_date') if episode_data else 'N/A'})")
                      else:
                           logging.debug("Strict match: Skipping date check (missing TMDB ID/S/E for lookup).")
                 except Exception as e:
                      logging.warning(f"Could not perform date check during match: {e}") # Warn instead of error

            # Season/Episode matching (using target_season/target_episode)
            season_episode_match = False
            if not date_match: # Only check if date didn't match
                # Determine item title for F1 check
                item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
                # Treat as Formula 1 motorsport event only if title includes "formula 1" **and** does NOT contain
                # "drive to survive" (which refers to the Netflix documentary series).
                is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)

                if is_formula_1_item and not using_xem: # Apply F1 logic if not overridden by XEM
                    # For F1, target_season IS the event year.
                    # We check if the file's PTT season is typical for F1 (empty or S1).
                    # The year from the filename's PTT is not reliable here.
                    season_match = (not ptt_result.get('seasons') or ptt_result.get('seasons') == [1])
                    
                    # Episode match for F1: item's event number should be in filename's PTT episodes,
                    # or filename has no PTT episodes (e.g. single file for the whole event part).
                    episode_match = (target_episode is None or not ptt_result.get('episodes') or target_episode in ptt_result.get('episodes', []))
                    
                    logging.debug(f"Strict F1 match: S/E check -> S:{season_match} E:{episode_match} (Item S{target_season}E{target_episode}, FilePTTSeason: {ptt_result.get('seasons')}, FilePTTEpisodes: {ptt_result.get('episodes')})")
                else: # Original logic for non-F1 or if using XEM
                    season_match = (target_season is not None and target_season in ptt_result.get('seasons', [])) or \
                                   (using_xem and target_season is None and not ptt_result.get('seasons')) # Allow None season match if XEM provided None and file has no season
                    episode_match = target_episode in ptt_result.get('episodes', [])
                    # Also check fallback episode if PTT episodes are empty
                    if not ptt_result.get('episodes') and ptt_result.get('fallback_episode') == target_episode:
                        episode_match = True
                    logging.debug(f"Strict non-F1/XEM match: S/E check -> S:{season_match} E:{episode_match} (Target S{target_season}E{target_episode})")

                    # --- ANIME ABSOLUTE EPISODE MATCHING FOR STRICT MODE ---
                    # Apply same logic as relaxed mode for anime content
                    if is_anime and not season_match and not episode_match and target_season is not None and target_episode is not None:
                        try:
                            # Get season episode counts for absolute episode calculation
                            from database.database_reading import get_all_season_episode_counts
                            tmdb_id = item.get('tmdb_id')
                            if tmdb_id:
                                season_episode_counts = get_all_season_episode_counts(tmdb_id)
                                
                                # Calculate target absolute episode number
                                target_absolute_episode = 0
                                sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < target_season])
                                for s_num in sorted_seasons:
                                    target_absolute_episode += season_episode_counts.get(s_num, 0)
                                target_absolute_episode += target_episode
                                
                                # Check if torrent uses absolute episode numbering
                                torrent_seasons = ptt_result.get('seasons', [])
                                torrent_episodes = ptt_result.get('episodes', [])
                                
                                # For anime, many releases use S01E{episode} where episode is actually the absolute episode number
                                # Pattern 1: S01E{episode} format where episode matches our target episode directly
                                if torrent_seasons == [1] and target_episode in torrent_episodes:
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Strict anime absolute match: S01E{target_episode} format matched (target S{target_season}E{target_episode}, treating as absolute numbering)")
                                
                                # Pattern 2: Check if calculated absolute episode number matches torrent episode
                                elif torrent_seasons == [1] and target_absolute_episode in torrent_episodes:
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Strict anime absolute match: S01E{target_absolute_episode} format matched (target S{target_season}E{target_episode} = abs {target_absolute_episode})")
                                
                                # Pattern 3: E{absolute} or {absolute} format (season might be empty)
                                elif not torrent_seasons and (target_episode in torrent_episodes or target_absolute_episode in torrent_episodes):
                                    season_match = True  # Allow missing season for absolute format
                                    episode_match = True
                                    episode_used = target_episode if target_episode in torrent_episodes else target_absolute_episode
                                    logging.debug(f"Strict anime absolute match: E{episode_used} or {episode_used} format matched (target S{target_season}E{target_episode})")
                                
                                # Pattern 4: Check if target episode or absolute episode appears in the original filename
                                original_filename = ptt_result.get('original_filename', '')
                                if original_filename:
                                    import re
                                    # Look for either the target episode or absolute episode as standalone numbers
                                    target_patterns = [str(target_episode), str(target_absolute_episode)]
                                    for pattern_num in target_patterns:
                                        if re.search(rf'\b{pattern_num}\b', original_filename):
                                            season_match = True
                                            episode_match = True
                                            logging.debug(f"Strict anime absolute match: Found episode {pattern_num} in filename '{original_filename}' (target S{target_season}E{target_episode})")
                                            break
                                        
                        except Exception as e:
                            logging.warning(f"Error during strict anime absolute episode matching: {e}")
                            # Continue with original season_match/episode_match values
                            pass

                season_episode_match = season_match and episode_match
                logging.debug(f"Strict match S/E component result: {season_episode_match}")


                # Last resort: check file date against TMDB date for season/episode files if S/E match failed
                if not season_episode_match and ptt_result.get('date'):
                    try:
                         # Use original item S/E for TMDB lookup
                         original_item_season = item.get('season') or item.get('season_number')
                         original_item_episode = item.get('episode') or item.get('episode_number')
                         if item.get('tmdb_id') and original_item_season is not None and original_item_episode is not None:
                             from utilities.web_scraper import get_tmdb_data
                             episode_data = get_tmdb_data(int(item['tmdb_id']), 'tv', original_item_season, original_item_episode)
                             if episode_data and episode_data.get('air_date') == ptt_result['date']:
                                 logging.debug("Strict match: Date matched via fallback TMDB lookup.")
                                 date_match = True # Consider it a date match if air dates align
                             else:
                                 logging.debug(f"Strict match: Fallback date mismatch (File: {ptt_result['date']}, TMDB: {episode_data.get('air_date') if episode_data else 'N/A'})")
                         else:
                              logging.debug("Strict match: Skipping fallback date check (missing TMDB ID/S/E).")
                    except Exception as e:
                         logging.warning(f"Could not perform fallback date check: {e}")

            final_match = season_episode_match or date_match
            logging.debug(f"Strict match final result: {final_match}")
            return final_match

    def find_best_match_from_parsed(self, parsed_files: List[Dict[str, Any]], item: Dict[str, Any], xem_mapping: Optional[Dict[str, int]] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Finds the best matching file for a single item from a list of pre-parsed file info.

        Args:
            parsed_files: List of dictionaries returned by _parse_file_info.
            item: The media item to match.
            xem_mapping: Optional dictionary with 'season' and 'episode' keys from XEM.

        Returns:
            A tuple (matching_filepath_basename, item) if a match is found, otherwise None.
            For movies, returns the largest video file path basename.
            For episodes, returns the first file that matches season/episode criteria (using XEM if provided).
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
            # Check for Formula 1
            item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
            # Same refined detection as above to avoid mis-classifying "Formula 1: Drive to Survive".
            is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)

            if is_formula_1_item:
                logging.debug(f"Formula 1 item detected: '{item_title_for_f1_check}'. Applying simplified 'session' file matching.")
                for parsed_file_info in parsed_files:
                    # Ensure 'parsed_info' and 'original_filename' exist
                    parsed_info_dict = parsed_file_info.get('parsed_info', {})
                    original_filename = parsed_info_dict.get('original_filename', '')
                    
                    if "session" in original_filename.lower():
                        logging.info(f"F1 Match (simplified): Found 'session' in filename '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                    elif "qualifying" in original_filename.lower():
                        logging.info(f"F1 Match (simplified): Found 'qualifying' in filename '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                    elif "race" in original_filename.lower():
                        logging.info(f"F1 Match (simplified): Found 'race' in filename '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                        return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                    
                
                logging.info(f"F1 Match (simplified): No file containing 'session'/'qualifying'/'race' found for item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')}. No match by this specific F1 rule.")
                return None # No file with "session" found for this F1 item by this rule

            # Determine if relaxed matching should be used (copied from original _match_tv_content)
            genres = item.get('genres') or []
            if isinstance(genres, str):
                genres = [genres]
            is_anime = any('anime' in genre.lower() for genre in genres)
            from utilities.settings import get_setting
            file_collection_management = get_setting('File Management', 'file_collection_management')
            using_plex = file_collection_management == 'Plex'
            use_relaxed_matching = not using_plex and (is_anime or self.relaxed_matching)
            logging.debug(f"Episode matching mode: {'Relaxed' if use_relaxed_matching else 'Strict'}")

            for parsed_file_info in parsed_files:
                # Pass xem_mapping down to _check_match
                if self._check_match(parsed_file_info, item, use_relaxed_matching, xem_mapping=xem_mapping):
                    # Return the first match found
                    logging.info(f"Match found for item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} (using XEM: {xem_mapping is not None}) -> File: {parsed_file_info['path']}")
                    return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item

            logging.debug(f"No matching file found for item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} (using XEM: {xem_mapping is not None}) in parsed files.")
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

    def find_related_items(self, parsed_torrent_files: List[Dict[str, Any]], scraping_items: List[Dict[str, Any]], wanted_items: List[Dict[str, Any]], original_item: Dict[str, Any], xem_mapping: Optional[Dict[str, int]] = None) -> List[Tuple[Dict[str, Any], str]]:
        """
        Find items in the scraping and wanted queues that match pre-parsed files in the torrent.

        Args:
            parsed_torrent_files: List of dictionaries from _parse_file_info for files in the torrent.
            scraping_items: List of items currently in scraping state.
            wanted_items: List of items currently in wanted state.
            original_item: The original item being processed, used to match version/title.
            xem_mapping: Optional dictionary with 'season' from PTT of the torrent title, to enforce season-matching for packs.

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

        # Allow multi-season matching if the user disables the restriction via settings.
        from utilities.settings import get_setting
        restrict_to_pack_season = get_setting('Matching', 'restrict_related_to_pack_season', False)

        # If the torrent was parsed as a specific season (pack), enforce matching only that season
        pack_season = xem_mapping.get('season') if xem_mapping else None
        if pack_season is not None and restrict_to_pack_season:
            logging.info(
                f"Torrent pack identified as Season {pack_season}. Related item matching will be restricted to this season (per setting)."
            )

        # Determine relaxed matching based on original item (assuming related items follow same logic)
        genres = original_item.get('genres') or []
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        file_collection_management = get_setting('File Management', 'file_collection_management')
        using_plex = file_collection_management == 'Plex'
        # Apply relaxed matching globally based on the original item context
        use_relaxed_matching_for_all = not using_plex and (is_anime or self.relaxed_matching)

        for item in all_candidate_items:
            item_id = item.get('id')
            if not item_id or item_id in processed_item_ids:
                continue

            # Optionally skip items from other seasons for season packs
            if pack_season is not None and restrict_to_pack_season:
                item_season = item.get('season') or item.get('season_number')
                if item_season != pack_season:
                    continue

            # --- Build per-candidate XEM mapping (season-offset only) ---
            candidate_xem_mapping = None
            if xem_mapping is not None:
                # Determine season delta based on original→scene mapping for the primary item
                original_item_season = original_item.get('season') or original_item.get('season_number')
                mapped_primary_season = xem_mapping.get('season')

                try:
                    if original_item_season is not None and mapped_primary_season is not None:
                        season_delta = int(mapped_primary_season) - int(original_item_season)

                        candidate_season = item.get('season') or item.get('season_number')
                        candidate_episode = item.get('episode') or item.get('episode_number')

                        if candidate_season is not None and candidate_episode is not None:
                            candidate_xem_mapping = {
                                'season': candidate_season + season_delta,
                                'episode': candidate_episode,  # assume episode number itself is unchanged
                            }
                except Exception as map_err:
                    logging.debug(f"Could not build candidate XEM mapping for item ID {item_id}: {map_err}")

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
                # Pass per-candidate mapping (if available) so scene numbering is considered correctly
                if self._check_match(
                    parsed_file_info,
                    item_for_matching,
                    use_relaxed_matching_for_all,
                    xem_mapping=candidate_xem_mapping,
                ):
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
