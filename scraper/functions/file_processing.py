import logging
import re
from typing import List, Dict, Any, Union
from database.database_reading import get_movie_runtime, get_episode_runtime, get_episode_count
from fuzzywuzzy import fuzz
from PTT import parse_title
from babelfish import Language
from scraper.functions import *
from scraper.functions.common import detect_season_episode_info
from functools import lru_cache
import time

# Pre-compile regex patterns
_SEASON_RANGE_PATTERN = re.compile(r's(\d+)-s(\d+)')
# The site prefix patterns are used multiple times during normalization
# Compile them once at module import time to avoid recompilation in tight loops
_SITE_PREFIX_PATTERN = re.compile(r'^(www\s+\S+\s+\S+|www\.\S+\.\S+|[a-zA-Z0-9]+\s*\.\s*[a-zA-Z]+)\s*-\s*', re.IGNORECASE)
# Re-use the same pattern for the second cleanup pass
_LEFTOVER_SITE_PREFIX_PATTERN = _SITE_PREFIX_PATTERN

@lru_cache(maxsize=1024)
def _parse_with_ptt(title: str) -> Dict[str, Any]:
    """Cached PTT parsing"""
    # Get the raw result from PTT
    raw_result = parse_title(title)
    
    # Create a copy to avoid modifying the original
    result = raw_result.copy()
    
    # Make sure the site is not included in the title
    if 'site' in result and result.get('title', ''):
        # Ensure we're using just the movie/show title without the site prefix
        result['original_site'] = result['site']
    
    # TEMPORARY OVERRIDE: Fix for Office Christmas Party parsing issue
    # PTT parser incorrectly parses "Office.Christmas.Party.2016..." as just "Office"
    # Override when the original title contains the full title but parsed title is incomplete
    if result.get('title', '').lower() == 'office':
        title_lower = title.lower()
        if 'office.christmas.party' in title_lower or 'office christmas party' in title_lower:
            result['title'] = 'Office Christmas Party'
            logging.info(f"Applied Office Christmas Party override: '{title}' -> '{result['title']}'")
    
    # TEMPORARY OVERRIDE: Fix for Dragon Ball series parsing issue
    # PTT parser incorrectly parses "Dragon Ball Z Complete Series..." as just "Dragon B"
    # Override when the parsed title is "Dragon B" and the original title contains Dragon Ball series names
    if result.get('title', '').lower() == 'dragon b':
        title_lower = title.lower()
        # Use regex to find Dragon Ball series patterns (with spaces or dots)
        dragon_ball_patterns = [
            r'dragon\.ball\.z\b',
            r'dragon ball z\b',
            r'dragon\.ball\.kai\b', 
            r'dragon ball kai\b',
            r'dragon\.ball\.gt\b',
            r'dragon ball gt\b',
            r'dragon\.ball\.daima\b',
            r'dragon ball daima\b'
        ]
        
        for pattern in dragon_ball_patterns:
            match = re.search(pattern, title_lower)
            if match:
                # Convert the matched text to the dot-separated format
                matched_text = match.group(0)
                corrected_title = matched_text.replace(' ', '.').replace('..', '.')
                result['title'] = corrected_title
                logging.info(f"Applied Dragon Ball series override: '{title}' -> '{result['title']}'")
                break
    
    '''
    # Special handling for shows where the title is a year (e.g. "1923")
    if result.get('year'):
        # Handle both space and dot-separated formats
        first_part = title.split('.')[0].split()[0].strip()
        
        # If the first part is a 4-digit number and it matches the detected year
        if first_part.isdigit() and len(first_part) == 4 and int(first_part) == result['year']:
            # Check if we have a season/episode pattern
            has_episode = bool(result.get('episodes')) or bool(result.get('seasons'))
            current_title = result.get('title', '')
            
            # If it's an episode and the current title contains episode-specific information
            if has_episode and current_title:
                # For titles that include episode title (e.g. "S02E01 The Killing Season")
                if 'S' in current_title and any(c.isdigit() for c in current_title):
                    # Set the show title to the year and clear the year field
                    result['title'] = first_part
                    result['year'] = None  # Clear the year since it's actually the title
                    # Store the episode title if needed
                    if ' ' in current_title:
                        _, episode_title = current_title.split(' ', 1)
                        result['episode_title'] = episode_title
            elif not current_title or current_title.startswith('S'):
                # If title is missing or just contains season info
                result['title'] = first_part
                result['year'] = None  # Clear the year since it's actually the title
    '''
    
    return result

def detect_hdr(parsed_info: Dict[str, Any]) -> bool:
    # Extract HDR information from PTT result
    hdr_info = parsed_info.get('hdr', False)
    if hdr_info:
        return True

    # No fallback method - removed to prevent false positives with titles like "Devil's Advocate"
    return False

def match_any_title(release_title: str, official_titles: List[str], threshold: float = 0.35) -> float:
    max_similarity = 0
    for title in official_titles:
        partial_score = partial_title_match(release_title, title)
        fuzzy_score = fuzzy_title_match(release_title, title) / 100
        similarity = max(partial_score, fuzzy_score)
        max_similarity = max(max_similarity, similarity)
        #logging.debug(f"Matching '{release_title}' with '{title}': Partial: {partial_score:.2f}, Fuzzy: {fuzzy_score:.2f}, Max: {similarity:.2f}")
    return max_similarity

def partial_title_match(title1: str, title2: str) -> float:
    words1 = set(title1.lower().split())
    words2 = set(title2.lower().split())
    common_words = words1.intersection(words2)
    return len(common_words) / max(len(words1), len(words2))

def fuzzy_title_match(title1: str, title2: str) -> float:
    return fuzz.partial_ratio(title1.lower(), title2.lower())

def calculate_bitrate(size_gb, runtime_minutes):
    if not size_gb or not runtime_minutes:
        return 0
    size_bits = size_gb * 8 * 1024 * 1024 * 1024  # Convert GB to bits
    runtime_seconds = runtime_minutes * 60
    bitrate_mbps = (size_bits / runtime_seconds) / 1000  # Convert to Mbps
    return round(bitrate_mbps, 2)

def detect_resolution(parsed_info: Dict[str, Any]) -> str:
    # First try to get resolution from PTT result
    resolution = parsed_info.get('resolution', 'Unknown')
    return resolution

def _process_single_title(args):
    """Helper for threaded execution inside batch_parse_torrent_info.

    This function contains the per-item parsing logic that was previously
    embedded in the for-loop of batch_parse_torrent_info. It is extracted so
    that we can easily run it in a ThreadPoolExecutor while maintaining the
    original behaviour.
    """
    title, size = args
    try:
        # Check for unreasonable season ranges early so we can skip expensive work
        season_range_match = _SEASON_RANGE_PATTERN.search(title.lower())
        if season_range_match:
            start_season, end_season = map(int, season_range_match.groups())
            if end_season - start_season > 50:
                return {'title': title, 'original_title': title, 'invalid_season_range': True}

        # Parse with PTT (cached)
        parsed_info_from_ptt = _parse_with_ptt(title)

        # Extract the clean title - if there's a site field, make sure it's not in the title
        clean_title = parsed_info_from_ptt.get('title', title)
        site = parsed_info_from_ptt.get('site')

        # If PTT didn't detect a site but the title looks like it contains site info, try to extract it
        if not site and isinstance(clean_title, str):
            site_match = _SITE_PREFIX_PATTERN.search(clean_title)
            if site_match:
                site = site_match.group(1).strip()
                clean_title = _SITE_PREFIX_PATTERN.sub('', clean_title)

        # If site exists in PTT result but the title still starts with it, clean it
        if site and isinstance(clean_title, str) and clean_title.lower().startswith(site.lower()):
            clean_title = re.sub(f"^{re.escape(site)}\\s*-?\\s*", "", clean_title, flags=re.IGNORECASE)

        # Further cleaning pass in case site-like prefixes are still present
        if isinstance(clean_title, str) and _LEFTOVER_SITE_PREFIX_PATTERN.search(clean_title):
            clean_title = _LEFTOVER_SITE_PREFIX_PATTERN.sub('', clean_title)

        # Convert PTT result to our standard format
        processed_info = {
            'title': clean_title,
            'original_title': title,
            'year': parsed_info_from_ptt.get('year'),
            'resolution': parsed_info_from_ptt.get('resolution', 'Unknown'),
            'source': parsed_info_from_ptt.get('source'),
            'audio': parsed_info_from_ptt.get('audio'),
            'codec': parsed_info_from_ptt.get('codec'),
            'group': parsed_info_from_ptt.get('group'),
            'season': parsed_info_from_ptt.get('season'),
            'seasons': parsed_info_from_ptt.get('seasons', []),
            'episode': parsed_info_from_ptt.get('episode'),
            'episodes': parsed_info_from_ptt.get('episodes', []),
            'type': parsed_info_from_ptt.get('type'),
            'country': parsed_info_from_ptt.get('country'),
            'date': parsed_info_from_ptt.get('date'),
            'documentary': parsed_info_from_ptt.get('documentary', False),
            'site': site,
            'trash': parsed_info_from_ptt.get('trash', False)
        }

        # Handle size if provided
        if size is not None:
            processed_info['size'] = parse_size(size)

        # Add additional information
        processed_info['resolution_rank'] = get_resolution_rank(processed_info['resolution'])
        processed_info['is_hdr'] = detect_hdr(parsed_info_from_ptt)

        # Extract season/episode info
        season_episode_info = detect_season_episode_info(processed_info.copy())
        processed_info['season_episode_info'] = season_episode_info

        return processed_info

    except Exception as e:
        logging.error(f"PTT parsing error for '{title}': {str(e)}", exc_info=True)
        return {'title': title, 'original_title': title, 'parsing_error': True}

def batch_parse_torrent_info(titles: List[str], sizes: List[Union[str, int, float]] = None) -> List[Dict[str, Any]]:
    """
    Parse multiple torrent titles efficiently.

    Optimisations:
    1. Regex patterns are pre-compiled at module load time.
    2. For large batches ( > 12 items ) the per-item work is executed in a
       ThreadPoolExecutor which can significantly reduce wall-clock time on
       multi-core systems without changing external behaviour.  A conservative
       threshold is chosen to avoid the overhead of thread creation for small
       inputs.
    """
    if sizes is None:
        sizes = [None] * len(titles)

    # Quick sanity to keep lists aligned
    if len(sizes) != len(titles):
        # Fallback to original behaviour by aligning sizes length
        sizes = list(sizes) + [None] * (len(titles) - len(sizes))

    # Decide whether to process in parallel or sequentially based on workload
    PARALLEL_THRESHOLD = 12  # Tunable – empirically the break-even point
    if len(titles) >= PARALLEL_THRESHOLD:
        try:
            from concurrent.futures import ThreadPoolExecutor
            import multiprocessing

            max_workers = min(32, (multiprocessing.cpu_count() or 1) * 2)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(_process_single_title, zip(titles, sizes)))
            return results
        except Exception as e:
            # If anything goes wrong, fall back to sequential processing and log once
            logging.error(f"Parallel batch_parse_torrent_info failed – falling back to sequential: {e}", exc_info=True)
            # continue to sequential section

    # Sequential fallback / small batch path
    return [_process_single_title(args) for args in zip(titles, sizes)]

def parse_torrent_info(title: str, size: Union[str, int, float] = None) -> Dict[str, Any]:
    """
    Parse a single torrent title. Uses the batch processing function for consistency.
    """
    results = batch_parse_torrent_info([title], [size])
    return results[0]

def get_media_info_for_bitrate(media_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from metadata.metadata import get_tmdb_id_and_media_type, get_metadata
    processed_items = []
    total_db_time = 0
    total_metadata_time = 0

    for item_idx, item in enumerate(media_items):
        item_start_time = time.time()
        try:
            if item['media_type'] == 'movie':
                db_start_time = time.time()
                db_runtime = get_movie_runtime(item['tmdb_id'])
                db_duration = time.time() - db_start_time
                total_db_time += db_duration
                #logging.debug(f"Item {item_idx} ('{item.get('title', 'N/A')}') get_movie_runtime took {db_duration:.4f}s")

                if db_runtime:
                    item['episode_count'] = 1
                    item['runtime'] = db_runtime
                else:
                    metadata_start_time = time.time()
                    metadata = get_metadata(tmdb_id=item['tmdb_id'], item_media_type='movie')
                    metadata_duration = time.time() - metadata_start_time
                    total_metadata_time += metadata_duration
                    #logging.debug(f"Item {item_idx} ('{item.get('title', 'N/A')}') get_metadata (movie) took {metadata_duration:.4f}s")
                    item['runtime'] = metadata.get('runtime', 100)
                    item['episode_count'] = 1
        
            elif item['media_type'] == 'episode':
                db_runtime_start_time = time.time()
                db_runtime = get_episode_runtime(item['tmdb_id'])
                db_runtime_duration = time.time() - db_runtime_start_time
                total_db_time += db_runtime_duration
                #logging.debug(f"Item {item_idx} ('{item.get('title', 'N/A')}') get_episode_runtime took {db_runtime_duration:.4f}s")

                if db_runtime:
                    item['runtime'] = db_runtime
                    db_episode_count_start_time = time.time()
                    item['episode_count'] = get_episode_count(item['tmdb_id'])
                    db_episode_count_duration = time.time() - db_episode_count_start_time
                    total_db_time += db_episode_count_duration
                    #logging.debug(f"Item {item_idx} ('{item.get('title', 'N/A')}') get_episode_count took {db_episode_count_duration:.4f}s")
                else:
                    tmdb_id_for_meta = item.get('tmdb_id')
                    media_type_for_meta = 'tv'
                    
                    # This part for fetching tmdb_id if only imdb_id is present seems less likely to be a bottleneck
                    # but we can log it if necessary. For now, focusing on get_metadata.
                    if 'imdb_id' in item and 'tmdb_id' not in item: # Check if tmdb_id is actually missing
                        # Potentially add timing for get_tmdb_id_and_media_type if it becomes relevant
                        tmdb_id_for_meta, media_type_for_meta = get_tmdb_id_and_media_type(item['imdb_id'])
                    
                    if tmdb_id_for_meta and media_type_for_meta == 'tv':
                        metadata_start_time = time.time()
                        metadata = get_metadata(tmdb_id=tmdb_id_for_meta, item_media_type='tv')
                        metadata_duration = time.time() - metadata_start_time
                        total_metadata_time += metadata_duration
                        #logging.debug(f"Item {item_idx} ('{item.get('title', 'N/A')}') get_metadata (tv) took {metadata_duration:.4f}s")
                        item['runtime'] = metadata.get('runtime', 30)
                        seasons = metadata.get('seasons', {})
                        item['episode_count'] = sum(season.get('episode_count', 0) for season in seasons.values())
                    else:
                        logging.warning(f"Could not fetch details for TV show: {item.get('title', 'N/A')}")
                        item['episode_count'] = 1
                        item['runtime'] = 30
            
            processed_items.append(item)
            #logging.debug(f"Item {item_idx} ('{item.get('title', 'N/A')}') processing took {time.time() - item_start_time:.4f}s")

        except Exception as e:
            logging.error(f"Error processing item {item.get('title', 'N/A')} in get_media_info_for_bitrate: {str(e)}", exc_info=True)
            item['episode_count'] = 1
            item['runtime'] = 30 if item.get('media_type') == 'episode' else 100
            processed_items.append(item)

    #logging.debug(f"get_media_info_for_bitrate: Total DB time: {total_db_time:.4f}s, Total Metadata time: {total_metadata_time:.4f}s for {len(media_items)} items.")
    return processed_items

def parse_size(size):
    if isinstance(size, (int, float)):
        return float(size)  # Assume it's already in GB
    elif isinstance(size, str):
        size = size.upper()
        if 'GB' in size:
            return float(size.replace('GB', '').strip())
        elif 'MB' in size:
            return float(size.replace('MB', '').strip()) / 1024
        elif 'KB' in size:
            return float(size.replace('KB', '').strip()) / (1024 * 1024)
    return 0  # Default to 0 if unable to parse

def get_resolution_rank(resolution: str) -> int:
    resolution = resolution.lower()
    
    if resolution in ['4k', '2160p']:
        return 4
    elif resolution in ['1080p', '1080i']:
        return 3
    elif resolution == '720p':
        return 2
    elif resolution in ['576p', '480p', 'sd']:
        return 1
    return 0  # For unknown resolutions

def compare_resolutions(res1: str, res2: str) -> int:
    resolution_order = {
        '2160p': 6, '4k': 6, 'uhd': 6,
        '1440p': 5, 'qhd': 5,
        '1080p': 4, 'fhd': 4,
        '720p': 3, 'hd': 3,
        '480p': 2, 'sd': 2,
        '360p': 1
    }

    val1 = resolution_order.get(res1.lower(), 0)
    val2 = resolution_order.get(res2.lower(), 0)

    return val1 - val2