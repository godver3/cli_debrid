"""
Media matching module for handling content validation and file matching logic.
Separates the media matching concerns from queue management.
"""

import logging
import os
import re
from typing import Dict, Any, List, Tuple, Optional
from collections import defaultdict
from fuzzywuzzy import fuzz
from PTT import parse_title
from scraper.functions.anime_utils import detect_absolute_numbering

class MediaMatcher:
    """Handles media content matching and validation"""
    
    def __init__(self, relaxed_matching: bool = False):
        self.episode_count_cache: Dict[str, Dict[int, int]] = {}
        self.relaxed_matching = relaxed_matching

    def _get_season_episode_counts_cached(self, tmdb_id: Optional[int]) -> Optional[Dict[int, int]]:
        """Return season→episode-count map cached per tmdb_id."""
        if not tmdb_id:
            return None
        key = str(tmdb_id)
        if key in self.episode_count_cache:
            return self.episode_count_cache[key]
        try:
            from database.database_reading import get_all_season_episode_counts
            counts = get_all_season_episode_counts(tmdb_id)
            if counts:
                self.episode_count_cache[key] = counts
            return counts
        except Exception as e:
            logging.debug(f"Could not fetch season episode counts for tmdb_id={tmdb_id}: {e}")
            return None

    def _compute_absolute_episode_for_item(self, item: Dict[str, Any]) -> Optional[int]:
        """Compute the absolute episode number for an item using cached counts or detect_absolute_numbering."""
        try:
            tmdb_id = item.get('tmdb_id')
            target_season = item.get('season') or item.get('season_number')
            target_episode = item.get('episode') or item.get('episode_number')
            if tmdb_id is None or target_season is None or target_episode is None:
                return None

            series_title = item.get('series_title') or item.get('title')
            uses_absolute, detected_absolute = detect_absolute_numbering(series_title, target_season, target_episode, tmdb_id)
            if uses_absolute and detected_absolute:
                return detected_absolute

            season_episode_counts = self._get_season_episode_counts_cached(tmdb_id)
            if not season_episode_counts:
                return None

            target_absolute_episode = 0
            sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < target_season])
            for s_num in sorted_seasons:
                target_absolute_episode += season_episode_counts.get(s_num, 0)
            target_absolute_episode += target_episode
            return target_absolute_episode
        except Exception as e:
            logging.debug(f"Could not compute absolute episode for item: {e}")
            return None

    def _build_parsed_file_indexes(self, parsed_files: List[Dict[str, Any]]):
        """Build fast lookups for parsed files to avoid scanning all files for every item."""
        by_season_episode = defaultdict(list)  # key: (season or None, episode) -> [parsed_file_info]
        by_episode_only = defaultdict(list)    # key: episode -> [parsed_file_info]
        f1_candidates = {
            'session': [],
            'qualifying': [],
            'race': [],
        }
        date_only_files = []  # Files parsed only as date (no seasons/episodes)

        for parsed_file_info in parsed_files:
            parsed_info = parsed_file_info.get('parsed_info', {})
            if parsed_info.get('is_anime_special_content', False):
                continue

            filename = parsed_info.get('original_filename', '')
            filename_lower = filename.lower()
            if 'session' in filename_lower:
                f1_candidates['session'].append(parsed_file_info)
            if 'qualifying' in filename_lower:
                f1_candidates['qualifying'].append(parsed_file_info)
            if 'race' in filename_lower:
                f1_candidates['race'].append(parsed_file_info)

            seasons = parsed_info.get('seasons') or []
            episodes = parsed_info.get('episodes') or []

            if parsed_info.get('date') and not seasons and not episodes:
                date_only_files.append(parsed_file_info)

            if not episodes:
                continue

            # Index by episode regardless of season
            for ep in episodes:
                by_episode_only[ep].append(parsed_file_info)

            # Index by (season, episode) with season or None
            if seasons:
                for s in seasons:
                    for ep in episodes:
                        by_season_episode[(s, ep)].append(parsed_file_info)
            else:
                for ep in episodes:
                    by_season_episode[(None, ep)].append(parsed_file_info)

        return {
            'by_season_episode': by_season_episode,
            'by_episode_only': by_episode_only,
            'f1_candidates': f1_candidates,
            'date_only_files': date_only_files,
        }

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
        
        # --- Anime Special Content Detection ---
        # Instead of filtering here, we'll tag it and decide later
        is_anime_special_content = False
        basename_lower = file_basename.lower()
        anime_special_patterns = [
            r'(?<![a-zA-Z0-9])ncop(?=[._-]|$)', r'(?<![a-zA-Z0-9])nced(?=[._-]|$)',  # No Credit Opening/Ending
            r'(?<![a-zA-Z0-9])opening(?=[._-]|$)', r'(?<![a-zA-Z0-9])ending(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])ova(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])blooper(?=[._-]|$)', r'(?<![a-zA-Z0-9])bloopers(?=[._-]|$)',  # Blooper content
            r'(?<![a-zA-Z0-9])special(?=[._-]|$)', r'(?<![a-zA-Z0-9])specials(?=[._-]|$)',  # Special content
            r'(?<![a-zA-Z0-9])omake(?=[._-]|$)', r'(?<![a-zA-Z0-9])omakes(?=[._-]|$)',  # Omake (bonus content)
            r'(?<![a-zA-Z0-9])extra(?=[._-]|$)', r'(?<![a-zA-Z0-9])extras(?=[._-]|$)',  # Extra content
            r'(?<![a-zA-Z0-9])bonus(?=[._-]|$)', r'(?<![a-zA-Z0-9])bonuses(?=[._-]|$)'  # Bonus content
        ]
        
        for i, pattern in enumerate(anime_special_patterns):
            match = re.search(pattern, basename_lower)
            if match:
                is_anime_special_content = True
                logging.debug(f"Tagged as potential anime special content: '{file_basename}' (matched pattern: {pattern})")
                break # Found a match, no need to check others
        

        ptt_result = parse_title(file_basename) # Parse only the basename
        
        # Ensure ptt_result is a dict, even if parse_title returns None or an unexpected type
        parsed_info = ptt_result if isinstance(ptt_result, dict) else {}

        parsed_info['is_anime_special_content'] = is_anime_special_content

        parsed_info['original_filename'] = file_basename # Store basename

        # Attempt fallback episode extraction if PTT fails for episodes
        # PTT might return 'episodes': [] or no 'episodes' key at all.
        # We should trigger fallback if 'episodes' is empty or not present.
        if not parsed_info.get('episodes'): # This covers None or an empty list
             fallback_episode = self._extract_episode_from_filename(file_basename) # Use basename for fallback
             if fallback_episode is not None:
                  parsed_info['fallback_episode'] = fallback_episode
        
        # Additional check for anime openings/endings that might have been parsed as episodes
        # Look for patterns like NCOP1, NCED2, OP1, ED1, etc.
        if parsed_info.get('episodes'):
            episode_list = parsed_info.get('episodes', [])
            original_filename = parsed_info.get('original_filename', '')
            
            # Check if any "episode" numbers are actually opening/ending identifiers
            anime_op_ed_patterns = [
                r'\b(ncop|nced|op|ed)\d+\b',  # NCOP1, NCED2, OP1, ED1, etc.
                r'\bopening\s*\d+\b',         # Opening 1, etc.
                r'\bending\s*\d+\b',          # Ending 1, etc.
            ]
            
            for pattern in anime_op_ed_patterns:
                if re.search(pattern, original_filename.lower()):
                    logging.debug(f"Filtered out anime opening/ending with episode-like numbering: '{original_filename}' (matched pattern: {pattern})")
                    return None
        
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
        # Always reject anime special content files (bloopers, openings, endings, etc.)
        is_special_content = parsed_file_info.get('parsed_info', {}).get('is_anime_special_content', False)
        if is_special_content:
            logging.debug(f"Rejecting anime special content file: '{parsed_file_info.get('path', 'unknown')}'")
            return False

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

            # Season matching - apply more restrictive logic for anime, similar to filter_results.py
            season_match = False
            lenient_season_pass = False
            explicit_season_mismatch = False
            
            # Determine if parsed season info is missing or defaulted
            parsed_season_is_missing_or_default = not ptt_result.get('seasons') or ptt_result.get('seasons') == [1]

            # Fallback season detection for anime when PTT fails to parse season
            fallback_season = None
            if is_anime and parsed_season_is_missing_or_default:
                filename_for_season_check = ptt_result.get('original_filename', '')
                if filename_for_season_check:
                    # Look for patterns like "Title 2 - 01" or "Title Season 2 - 01"
                    season_patterns = [
                        r'(?<=\w)\s+(\d{1,2})\s*-\s*\d+',  # "Title 2 - 01"
                        r'Season\s+(\d{1,2})',              # "Season 2"
                        r'S(\d{1,2})',                      # "S2"
                    ]
                    for pattern in season_patterns:
                        match = re.search(pattern, filename_for_season_check, re.IGNORECASE)
                        if match:
                            try:
                                detected_season = int(match.group(1))
                                if 1 <= detected_season <= 50:  # Reasonable season range
                                    fallback_season = detected_season
                                    logging.debug(f"Fallback season detection found season {fallback_season} in filename '{filename_for_season_check}'")
                                    break
                            except (ValueError, IndexError):
                                continue
            
            if target_season in ptt_result.get('seasons', []):
                # Parsed season explicitly matches the target season
                season_match = True
            elif fallback_season and target_season == fallback_season:
                # Fallback season detection matches the target season
                season_match = True
                lenient_season_pass = True
                logging.debug(f"Season match via fallback detection: target S{target_season} matches fallback season {fallback_season}")
            elif is_anime and parsed_season_is_missing_or_default and target_season > 1:
                # For anime S2+, if the file is parsed as S1/None, we need to be more restrictive
                # Only allow if we have strong evidence this is the right season
                filename_for_check = ptt_result.get('original_filename', '')
                
                # Check if we have absolute episode evidence
                has_absolute_episode = False
                try:
                    tmdb_id = item.get('tmdb_id')
                    if tmdb_id:
                        season_episode_counts = self._get_season_episode_counts_cached(tmdb_id)
                        if season_episode_counts:
                            # Calculate target absolute episode number
                            abs_target = 0
                            sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < target_season])
                            for s_num in sorted_seasons:
                                abs_target += season_episode_counts.get(s_num, 0)
                            abs_target += target_episode
                            
                            # Check if absolute episode appears in the original filename
                            if filename_for_check and re.search(rf'\b{abs_target}\b', filename_for_check):
                                has_absolute_episode = True
                except Exception as e:
                    logging.debug(f"Could not calculate absolute episode for anime matching: {e}")
                
                # Only allow if we have absolute episode evidence
                if has_absolute_episode:
                    season_match = True
                    lenient_season_pass = True
                    logging.debug(f"Allowing anime result ({filename_for_check}) parsed as S1/None when target is S{target_season} (absolute episode evidence found)")
                elif ptt_result.get('episodes') and not ptt_result.get('seasons') and not fallback_season:
                    # This is likely an absolute numbered episode. We can't confirm the season here,
                    # but we shouldn't reject it either. We'll let it pass the season check
                    # and rely on the comprehensive absolute episode matching logic later.
                    season_match = False # Keep false to trigger absolute matching block
                    logging.debug(f"Anime result ({filename_for_check}) has episode but no season. Deferring to absolute matching logic.")
                elif ptt_result.get('episodes') and not ptt_result.get('seasons') and fallback_season and fallback_season != target_season:
                    # We detected a fallback season but it doesn't match the target season
                    season_match = False
                    logging.debug(f"Anime result ({filename_for_check}) fallback season {fallback_season} doesn't match target S{target_season}")
                else:
                    # Reject if no strong evidence this is the right season
                    season_match = False
                    logging.debug(f"Rejecting anime result ({filename_for_check}) parsed as S1/None when target is S{target_season} (no absolute episode evidence)")
            elif not ptt_result.get('seasons'):
                # Allow titles with NO season info at all (might be absolute)
                # BUT: For anime with XEM mapping, be more restrictive
                # If we're searching for a specific season (not S1) and the torrent has no season info,
                # we should be more cautious to avoid grabbing episodes from wrong seasons
                if is_anime and target_season > 1:
                    # For anime S2+, if no season info, be more restrictive
                    # This prevents grabbing "Episode 07" from any season when we want S02E07
                    # Only allow if we have absolute episode numbers or other strong indicators
                    has_absolute_episode = False
                    # Calculate absolute episode number on the fly (similar to filter_results.py)
                    try:
                        tmdb_id = item.get('tmdb_id')
                        if tmdb_id:
                            season_episode_counts = self._get_season_episode_counts_cached(tmdb_id)
                            if season_episode_counts:
                                # Calculate target absolute episode number
                                abs_target = 0
                                sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < target_season])
                                for s_num in sorted_seasons:
                                    abs_target += season_episode_counts.get(s_num, 0)
                                abs_target += target_episode
                                
                                # Check if absolute episode appears in the original filename
                                filename_for_abs_check = ptt_result.get('original_filename', '')
                                if filename_for_abs_check and re.search(rf'\b{abs_target}\b', filename_for_abs_check):
                                    has_absolute_episode = True
                    except Exception as e:
                        logging.debug(f"Could not calculate absolute episode for anime matching: {e}")
                    
                    original_filename = ptt_result.get('original_filename', '')
                    has_episode_in_title = bool(re.search(rf'\b{target_episode}\b', original_filename))
                    
                    # Additional check: if the title only contains episode number without season,
                    # and we're searching for a specific season (not S1), be more restrictive
                    # This catches cases like "Dandadan - 07" when we want S02E07
                    title_has_only_episode = (
                        has_episode_in_title and 
                        not re.search(r'[Ss]\d+', original_filename) and  # No season info in title
                        not has_absolute_episode  # No absolute episode number
                    )
                    
                    if title_has_only_episode:
                        season_match = False
                        logging.info(f"Rejecting anime result with only episode number (no season/absolute) when searching for S{target_season}E{target_episode}: '{original_filename}'")
                    elif not has_absolute_episode and not has_episode_in_title:
                        season_match = False
                        logging.info(f"Rejecting anime result with no season info when searching for S{target_season}E{target_episode}: '{original_filename}' (no absolute episode or strong episode indicator)")
                    else:
                        # Even if we have episode indicator, be more restrictive for S2+
                        # Only allow if we have absolute episode numbers that provide proper context
                        if has_absolute_episode:
                            season_match = True
                            lenient_season_pass = True
                            logging.info(f"Allowing anime result with no season info but with absolute episode evidence for S{target_season}E{target_episode}: '{original_filename}'")
                        else:
                            season_match = False
                            logging.info(f"Rejecting anime result with episode indicator but no absolute episode evidence for S{target_season}E{target_episode}: '{original_filename}'")
                else:
                    season_match = True
                    lenient_season_pass = True # Mark as lenient pass
                    logging.debug(f"Allowing result ({ptt_result.get('original_filename', '')}) with no season info to pass season check")
            else:
                # Original fallback logic for other cases
                season_match = (target_season is None or # If target season is None (only possible with XEM if mapping had None?)
                              target_season in ptt_result.get('seasons', []) or # Target season is in filename seasons
                              (0 in ptt_result.get('seasons', []))) # Filename has season 0

            # --- ANIME ABSOLUTE EPISODE MATCHING ---
            # Special handling for anime where season numbering might differ due to absolute episode formats
            if is_anime and not season_match and target_season is not None and target_episode is not None:
                try:
                    # Get season episode counts for absolute episode calculation
                    tmdb_id = item.get('tmdb_id')
                    series_title = item.get('series_title') or item.get('title')
                    if tmdb_id:
                        # Initialize base_season and base_episode before conditional logic
                        base_season = target_season  # This is already XEM-mapped if using_xem=True
                        base_episode = target_episode  # This is already XEM-mapped if using_xem=True
                        
                        # First check if this anime uses absolute numbering
                        uses_absolute, detected_absolute = detect_absolute_numbering(series_title, target_season, target_episode, tmdb_id)
                        
                        if uses_absolute and detected_absolute:
                            # For absolute numbered anime, the episode number IS the absolute episode
                            target_absolute_episode = detected_absolute
                        else:
                            # Traditional calculation for non-absolute numbered anime
                            season_episode_counts = self._get_season_episode_counts_cached(tmdb_id)
                            
                            # Calculate target absolute episode number using the same logic as convert_anime_episode_format
                            # Use XEM-mapped S/E if available, otherwise use original item S/E
                            # base_season and base_episode already initialized above
                            
                            target_absolute_episode = 0
                            # Sort seasons to ensure correct order and handle potential non-integer keys from bad metadata
                            sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < base_season])
                            for s_num in sorted_seasons:
                                target_absolute_episode += season_episode_counts.get(s_num, 0)
                            target_absolute_episode += base_episode
                        
                        # Check if torrent uses absolute episode numbering (common patterns: S01E1134, E1134, 1134)
                        torrent_seasons = ptt_result.get('seasons', [])
                        torrent_episodes = ptt_result.get('episodes', [])
                        original_filename = ptt_result.get('original_filename', '')
                        
                        # Pattern 1: Check if calculated absolute episode number matches torrent episode
                        if torrent_seasons == [1] and target_absolute_episode in torrent_episodes:
                            season_match = True
                            episode_match = True
                            logging.debug(f"Anime absolute match: S01E{target_absolute_episode} format matched (target S{base_season}E{base_episode} = abs {target_absolute_episode})")
                        
                        # Pattern 2: E{absolute} or {absolute} format (season might be empty)
                        elif not torrent_seasons and target_absolute_episode in torrent_episodes:
                            season_match = True  # Allow missing season for absolute format
                            episode_match = True
                            logging.debug(f"Anime absolute match: E{target_absolute_episode} or {target_absolute_episode} format matched (target S{base_season}E{base_episode})")
                        
                        # Pattern 3: Check if absolute episode appears in the original filename
                        elif original_filename and re.search(rf'\b{target_absolute_episode}\b', original_filename):
                            season_match = True
                            episode_match = True
                            logging.debug(f"Anime absolute match: Found episode {target_absolute_episode} in filename '{original_filename}' (target S{base_season}E{base_episode})")
                        
                        # Pattern 4: Check for padded absolute episode formats (E001, E0001, etc.)
                        else:
                            # Determine padding for absolute number (same logic as convert_anime_episode_format)
                            total_show_episodes = sum(season_episode_counts.values()) if season_episode_counts else 0
                            padding = 4 if total_show_episodes > 999 else 3
                            
                            # Check padded formats
                            padded_absolute = f"{target_absolute_episode:0{padding}d}"
                            if original_filename:
                                # Check E{padded} format
                                if re.search(rf'\bE{padded_absolute}\b', original_filename):
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Anime absolute match: Found padded E{padded_absolute} in filename '{original_filename}' (target S{base_season}E{base_episode})")
                                # Check standalone {padded} format
                                elif re.search(rf'\b{padded_absolute}\b', original_filename):
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Anime absolute match: Found padded {padded_absolute} in filename '{original_filename}' (target S{base_season}E{base_episode})")
                                
                except Exception as e:
                    logging.warning(f"Error during anime absolute episode matching: {e}")
                    # Continue with original season_match value
                    pass

            # --- ORIGINAL EPISODE FALLBACK (similar to filter_results.py) ---
            # If we're using XEM mapping and the episode number changed, try the original episode as fallback
            if not episode_match and using_xem:
                original_item_season = item.get('season') or item.get('season_number')
                original_item_episode = item.get('episode') or item.get('episode_number')
                
                # Only try fallback if original episode is different from target episode
                if original_item_episode is not None and original_item_episode != target_episode:
                    logging.debug(f"Trying original episode fallback: original_episode={original_item_episode}, xem_episode={target_episode}, torrent_episodes={ptt_result.get('episodes', [])}")
                    
                    # Try matching against the original episode number
                    if original_item_episode in ptt_result.get('episodes', []):
                        episode_match = True
                        logging.info(f"Episode matched via original episode number {original_item_episode} for '{ptt_result.get('original_filename', '')}'")
                    elif ptt_result.get('original_filename') and re.search(rf'\b{original_item_episode}\b', ptt_result.get('original_filename')):
                        episode_match = True
                        logging.info(f"Episode matched via original episode number {original_item_episode} found in filename for '{ptt_result.get('original_filename', '')}'")
            # --- End original episode fallback ---

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
                            tmdb_id = item.get('tmdb_id')
                            series_title = item.get('series_title') or item.get('title')
                            if tmdb_id:
                                # First check if this anime uses absolute numbering
                                uses_absolute, detected_absolute = detect_absolute_numbering(series_title, target_season, target_episode, tmdb_id)
                                
                                if uses_absolute and detected_absolute:
                                    # For absolute numbered anime, the episode number IS the absolute episode
                                    target_absolute_episode = detected_absolute
                                else:
                                    # Traditional calculation for non-absolute numbered anime
                                    season_episode_counts = self._get_season_episode_counts_cached(tmdb_id)
                                    
                                    # Calculate target absolute episode number using the same logic as convert_anime_episode_format
                                    # Use XEM-mapped S/E if available, otherwise use original item S/E
                                    base_season = target_season  # This is already XEM-mapped if using_xem=True
                                    base_episode = target_episode  # This is already XEM-mapped if using_xem=True
                                    
                                    target_absolute_episode = 0
                                    # Sort seasons to ensure correct order and handle potential non-integer keys from bad metadata
                                    sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < base_season])
                                    for s_num in sorted_seasons:
                                        target_absolute_episode += season_episode_counts.get(s_num, 0)
                                    target_absolute_episode += base_episode
                                
                                # Check if torrent uses absolute episode numbering
                                torrent_seasons = ptt_result.get('seasons', [])
                                torrent_episodes = ptt_result.get('episodes', [])
                                original_filename = ptt_result.get('original_filename', '')
                                
                                # Pattern 1: Check if calculated absolute episode number matches torrent episode
                                if torrent_seasons == [1] and target_absolute_episode in torrent_episodes:
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Strict anime absolute match: S01E{target_absolute_episode} format matched (target S{base_season}E{base_episode} = abs {target_absolute_episode})")
                                
                                # Pattern 2: E{absolute} or {absolute} format (season might be empty)
                                elif not torrent_seasons and target_absolute_episode in torrent_episodes:
                                    season_match = True  # Allow missing season for absolute format
                                    episode_match = True
                                    logging.debug(f"Strict anime absolute match: E{target_absolute_episode} or {target_absolute_episode} format matched (target S{base_season}E{base_episode})")
                                
                                # Pattern 3: Check if absolute episode appears in the original filename
                                elif original_filename and re.search(rf'\b{target_absolute_episode}\b', original_filename):
                                    season_match = True
                                    episode_match = True
                                    logging.debug(f"Strict anime absolute match: Found episode {target_absolute_episode} in filename '{original_filename}' (target S{base_season}E{base_episode})")
                                
                                # Pattern 4: Check for padded absolute episode formats (E001, E0001, etc.)
                                else:
                                    # Determine padding for absolute number (same logic as convert_anime_episode_format)
                                    total_show_episodes = sum(season_episode_counts.values()) if season_episode_counts else 0
                                    padding = 4 if total_show_episodes > 999 else 3
                                    
                                    # Check padded formats
                                    padded_absolute = f"{target_absolute_episode:0{padding}d}"
                                    if original_filename:
                                        # Check E{padded} format
                                        if re.search(rf'\bE{padded_absolute}\b', original_filename):
                                            season_match = True
                                            episode_match = True
                                            logging.debug(f"Strict anime absolute match: Found padded E{padded_absolute} in filename '{original_filename}' (target S{base_season}E{base_episode})")
                                        # Check standalone {padded} format
                                        elif re.search(rf'\b{padded_absolute}\b', original_filename):
                                            season_match = True
                                            episode_match = True
                                            logging.debug(f"Strict anime absolute match: Found padded {padded_absolute} in filename '{original_filename}' (target S{base_season}E{base_episode})")
                                        
                        except Exception as e:
                            logging.warning(f"Error during strict anime absolute episode matching: {e}")
                            # Continue with original season_match/episode_match values
                            pass

                    # --- ORIGINAL EPISODE FALLBACK FOR STRICT MODE (similar to filter_results.py) ---
                    # If we're using XEM mapping and the episode number changed, try the original episode as fallback
                    if not episode_match and using_xem:
                        original_item_season = item.get('season') or item.get('season_number')
                        original_item_episode = item.get('episode') or item.get('episode_number')
                        
                        # Only try fallback if original episode is different from target episode
                        if original_item_episode is not None and original_item_episode != target_episode:
                            logging.debug(f"Trying original episode fallback (strict): original_episode={original_item_episode}, xem_episode={target_episode}, torrent_episodes={ptt_result.get('episodes', [])}")
                            
                            # Try matching against the original episode number
                            if original_item_episode in ptt_result.get('episodes', []):
                                episode_match = True
                                logging.info(f"Episode matched via original episode number {original_item_episode} for '{ptt_result.get('original_filename', '')}' (strict mode)")
                            elif ptt_result.get('original_filename') and re.search(rf'\b{original_item_episode}\b', ptt_result.get('original_filename')):
                                episode_match = True
                                logging.info(f"Episode matched via original episode number {original_item_episode} found in filename for '{ptt_result.get('original_filename', '')}' (strict mode)")
                    # --- End original episode fallback for strict mode ---

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
            # Build indexes once for faster candidate selection
            indexes = self._build_parsed_file_indexes(parsed_files)
            by_season_episode = indexes['by_season_episode']
            by_episode_only = indexes['by_episode_only']
            f1_candidates = indexes['f1_candidates']
            # Check for Formula 1
            item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
            # Same refined detection as above to avoid mis-classifying "Formula 1: Drive to Survive".
            is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)

            if is_formula_1_item:
                logging.debug(f"Formula 1 item detected: '{item_title_for_f1_check}'. Applying simplified 'session' file matching.")
                # Prefer pre-indexed F1 candidates first
                candidate_sets = [
                    f1_candidates.get('session', []),
                    f1_candidates.get('qualifying', []),
                    f1_candidates.get('race', []),
                ]
                # Iterate candidates in priority order
                for candidate_list in candidate_sets:
                    for parsed_file_info in candidate_list:
                        parsed_info_dict = parsed_file_info.get('parsed_info', {})
                        original_filename = parsed_info_dict.get('original_filename', '')
                        if "session" in original_filename.lower() or "qualifying" in original_filename.lower() or "race" in original_filename.lower():
                            logging.info(f"F1 Match (simplified): Found candidate '{original_filename}'. Matching item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} to file: {parsed_file_info['path']}")
                            return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item
                # Fallback: scan remaining parsed files for any missed F1 indicators
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

            # Narrow candidates by season/episode indexes to avoid scanning all files
            target_season = item.get('season') or item.get('season_number')
            target_episode = item.get('episode') or item.get('episode_number')

            # Apply XEM mapping from item if available (similar to how scraper.py does it)
            if xem_mapping:
                try:
                    xem_season = int(xem_mapping.get('season'))
                    xem_episode = int(xem_mapping.get('episode'))
                    logging.debug(f"Using XEM mapping for media matching: S{xem_season}E{xem_episode} (Original: S{target_season}E{target_episode})")
                    target_season = xem_season
                    target_episode = xem_episode
                except (ValueError, TypeError):
                    logging.warning(f"Invalid XEM mapping format in media matcher: {xem_mapping}. Using original S/E.")

            candidate_files: List[Dict[str, Any]] = []
            seen_ids = set()
            if target_episode is not None:
                # Index hits: (season, episode)
                for pf in by_season_episode.get((target_season, target_episode), []):
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
                # Index hits: (None, episode)
                for pf in by_season_episode.get((None, target_episode), []):
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
                # Index hits: episode-only
                for pf in by_episode_only.get(target_episode, []):
                    if id(pf) not in seen_ids:
                        seen_ids.add(id(pf)); candidate_files.append(pf)
            else:
                candidate_files = parsed_files  # Fallback if no episode available

            for parsed_file_info in candidate_files:
                # Always skip files that are tagged as anime special content
                is_special = parsed_file_info.get('parsed_info', {}).get('is_anime_special_content', False)
                if is_special:
                    logging.debug(f"Skipping anime special file '{parsed_file_info['path']}' for episode matching.")
                    continue

                # Pass xem_mapping down to _check_match
                if self._check_match(parsed_file_info, item, use_relaxed_matching, xem_mapping=xem_mapping):
                    # Return the first match found
                    logging.info(f"Match found for item '{item.get('title')}' S{target_season}E{target_episode} (using XEM: {xem_mapping is not None}) -> File: {parsed_file_info['path']}")
                    return (os.path.basename(parsed_file_info['path']), item) # Return basename path and item

            logging.debug(f"No matching file found for item '{item.get('title')}' S{target_season}E{target_episode} (using XEM: {xem_mapping is not None}) in parsed files.")
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
        
        # Skip special content files to avoid false episode extraction
        basename_lower = basename.lower()
        special_content_patterns = [
            r'(?<![a-zA-Z0-9])ncop(?=[._-]|$)', r'(?<![a-zA-Z0-9])nced(?=[._-]|$)', r'(?<![a-zA-Z0-9])opening(?=[._-]|$)', r'(?<![a-zA-Z0-9])ending(?=[._-]|$)', r'(?<![a-zA-Z0-9])ova(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])blooper(?=[._-]|$)', r'(?<![a-zA-Z0-9])bloopers(?=[._-]|$)', r'(?<![a-zA-Z0-9])special(?=[._-]|$)', r'(?<![a-zA-Z0-9])specials(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])omake(?=[._-]|$)', r'(?<![a-zA-Z0-9])omakes(?=[._-]|$)', r'(?<![a-zA-Z0-9])extra(?=[._-]|$)', r'(?<![a-zA-Z0-9])extras(?=[._-]|$)',
            r'(?<![a-zA-Z0-9])bonus(?=[._-]|$)', r'(?<![a-zA-Z0-9])bonuses(?=[._-]|$)'
        ]
        
        for pattern in special_content_patterns:
            if re.search(pattern, basename_lower):
                logging.debug(f"Skipping episode extraction for special content file: '{filename}' (matched pattern: {pattern})")
                return None

        # Try various patterns, but be more specific to avoid false positives
        patterns = [
            # Most specific patterns first
            r'(?:ep|episode)[.\s-]*(\d{1,4})(?:\D|$)',  # Matches "ep1" or "episode 1" (most reliable)
            r'[eE](\d{1,4})(?:\D|$)',  # Matches "E1" or "e01" (word boundary)
            
            # Standalone numbers - be more careful to avoid season numbers
            r'(?:^|\D)(\d{1,4})(?:\D|$)',  # Matches standalone numbers like "1" or "001"
        ]

        for pattern in patterns:
            match = re.search(pattern, basename)
            if match:
                try:
                    episode_num = int(match.group(1))
                    if 0 < episode_num < 2000:  # Sanity check for reasonable episode numbers
                        
                        # Additional context check for standalone numbers to avoid season conflicts
                        if pattern == r'(?:^|\D)(\d{1,4})(?:\D|$)':
                            # For standalone numbers, check if it's likely part of a season number
                            start_pos = match.start()
                            end_pos = match.end()
                            
                            # Check if preceded by 's' or 'season' (likely a season number)
                            if start_pos > 0:
                                before_match = basename[start_pos-1:start_pos+1]
                                if before_match.startswith('s') or before_match.startswith('season'):
                                    continue
                            
                            # Check if followed by 'e' (likely part of SxxExx format)
                            if end_pos < len(basename):
                                after_match = basename[end_pos-1:end_pos+1]
                                if after_match.endswith('e'):
                                    continue
                        
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

    def find_related_items(self, parsed_torrent_files: List[Dict[str, Any]], scraping_items: List[Dict[str, Any]], wanted_items: List[Dict[str, Any]], original_item: Dict[str, Any], xem_mapping: Optional[Dict[str, int]] = None, torrent_title: Optional[str] = None) -> List[Tuple[Dict[str, Any], str]]:
        """
        Find items in the scraping and wanted queues that match pre-parsed files in the torrent.

        Args:
            parsed_torrent_files: List of dictionaries from _parse_file_info for files in the torrent.
            scraping_items: List of items currently in scraping state.
            wanted_items: List of items currently in wanted state.
            original_item: The original item being processed, used to match version/title.
            xem_mapping: Optional dictionary with 'season' from PTT of the torrent title, to enforce season-matching for packs.
            torrent_title: Optional torrent title string to parse for season information.

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

        # --- ENHANCED SEASON DETECTION ---
        # Determine the pack season from multiple sources in order of priority:
        pack_season = None
        
        # 1. First try XEM mapping (existing logic)
        if xem_mapping and 'season' in xem_mapping:
            pack_season = xem_mapping.get('season')
            logging.debug(f"Using XEM mapping for pack season: {pack_season}")
        
        # 2. If no XEM mapping, try parsing the torrent title directly
        if pack_season is None and torrent_title:
            try:
                from scraper.functions.ptt_parser import parse_with_ptt
                torrent_parsed = parse_with_ptt(torrent_title)
                torrent_seasons = torrent_parsed.get('seasons', [])
                
                if torrent_seasons:
                    # If multiple seasons, this is a multi-season pack
                    if len(torrent_seasons) > 1:
                        logging.info(f"Torrent title indicates multi-season pack: {torrent_seasons}")
                        # For multi-season packs, we might want to allow all seasons
                        # But for now, let's be conservative and only allow if user setting is disabled
                        if not restrict_to_pack_season:
                            pack_season = None  # Allow all seasons
                        else:
                            pack_season = torrent_seasons[0]  # Restrict to first season
                    else:
                        pack_season = torrent_seasons[0]
                        logging.info(f"Torrent title indicates single season pack: S{pack_season}")
            except Exception as e:
                logging.debug(f"Could not parse torrent title for season info: {e}")
        
        # 3. Fallback to original item's season if still no pack season
        if pack_season is None:
            pack_season = original_item.get('season') or original_item.get('season_number')
            if pack_season:
                logging.debug(f"Using original item season as pack season: {pack_season}")

        # Apply season restriction if we have a pack season and the setting is enabled
        if pack_season is not None and restrict_to_pack_season:
            logging.info(f"Torrent pack identified as Season {pack_season}. Related item matching will be restricted to this season (per setting).")

        # Determine relaxed matching based on original item (assuming related items follow same logic)
        genres = original_item.get('genres') or []
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        file_collection_management = get_setting('File Management', 'file_collection_management')
        using_plex = file_collection_management == 'Plex'
        # Apply relaxed matching globally based on the original item context
        use_relaxed_matching_for_all = not using_plex and (is_anime or self.relaxed_matching)

        # Build indexes once for this batch to avoid O(Files × Items)
        indexes = self._build_parsed_file_indexes(parsed_torrent_files)
        by_season_episode = indexes['by_season_episode']
        by_episode_only = indexes['by_episode_only']
        f1_candidates = indexes['f1_candidates']

        for item in all_candidate_items:
            item_id = item.get('id')
            if not item_id or item_id in processed_item_ids:
                continue

            # Optionally skip items from other seasons for season packs
            if pack_season is not None and restrict_to_pack_season:
                item_season = item.get('season') or item.get('season_number')
                if item_season != pack_season:
                    continue
            
            # Check if this specific candidate item is anime
            candidate_genres = item.get('genres', [])
            if isinstance(candidate_genres, str):
                candidate_genres = [candidate_genres]
            candidate_is_anime = any('anime' in g.lower() for g in candidate_genres)

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
            # Pre-select candidate files using indexes to drastically reduce comparisons
            candidate_files: List[Dict[str, Any]] = []
            seen_ids = set()

            # Formula 1 special case: use f1 keyword buckets
            item_title_for_f1_check = (item.get('series_title', '') or item.get('title', '')).lower()
            is_formula_1_item = ("formula 1" in item_title_for_f1_check) and ("drive to survive" not in item_title_for_f1_check)
            if is_formula_1_item:
                for key in ('session', 'qualifying', 'race'):
                    for pf in f1_candidates.get(key, []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
            else:
                # Determine target season/episode (respect per-candidate XEM mapping if any)
                target_season = item.get('season') or item.get('season_number')
                target_episode = item.get('episode') or item.get('episode_number')

                if candidate_xem_mapping is not None:
                    try:
                        mapped_season = candidate_xem_mapping.get('season')
                        mapped_episode = candidate_xem_mapping.get('episode', target_episode)
                        if mapped_season is not None:
                            target_season = int(mapped_season)
                        if mapped_episode is not None:
                            target_episode = int(mapped_episode)
                    except Exception:
                        pass

                if target_episode is not None:
                    # Exact (season, episode)
                    for pf in by_season_episode.get((target_season, target_episode), []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                    # (None, episode)
                    for pf in by_season_episode.get((None, target_episode), []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)
                    # Episode-only
                    for pf in by_episode_only.get(target_episode, []):
                        if id(pf) not in seen_ids:
                            seen_ids.add(id(pf)); candidate_files.append(pf)

                    # Anime: include absolute-episode candidates
                    if candidate_is_anime:
                        # Build a minimal item clone with mapped S/E for absolute computation
                        item_clone_for_abs = dict(item)
                        if target_season is not None:
                            item_clone_for_abs['season'] = target_season
                            item_clone_for_abs['season_number'] = target_season
                        if target_episode is not None:
                            item_clone_for_abs['episode'] = target_episode
                            item_clone_for_abs['episode_number'] = target_episode
                        abs_ep = self._compute_absolute_episode_for_item(item_clone_for_abs)
                        if abs_ep is not None:
                            for pf in by_episode_only.get(abs_ep, []):
                                if id(pf) not in seen_ids:
                                    seen_ids.add(id(pf)); candidate_files.append(pf)
                            for pf in by_season_episode.get((1, abs_ep), []):
                                if id(pf) not in seen_ids:
                                    seen_ids.add(id(pf)); candidate_files.append(pf)
                else:
                    # Fallback: if we somehow lack episode number, consider all files (rare)
                    candidate_files = parsed_torrent_files

            for parsed_file_info in candidate_files:
                # Always skip files tagged as anime special content
                if parsed_file_info.get('parsed_info', {}).get('is_anime_special_content', False):
                    logging.debug(f"Skipping anime special file '{parsed_file_info['path']}' for related item matching.")
                    continue
                
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
