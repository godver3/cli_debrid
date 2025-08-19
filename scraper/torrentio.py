import logging
from routes.api_tracker import api
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus
from utilities.settings import load_config, get_setting
from database.database_reading import get_imdb_aliases
import time
import random
from http.client import RemoteDisconnected

DEFAULT_OPTS = "sort=qualitysize|qualityfilter=480p,scr,cam"
TORRENTIO_BASE_URL = "https://torrentio.strem.fun"

def scrape_torrentio_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    opts = settings.get('opts', '').strip()
    if not opts:
        opts = DEFAULT_OPTS
    
    try:
        # Get all IMDB aliases for this ID
        imdb_ids = get_imdb_aliases(imdb_id)
        all_results = []
        
        # Scrape for each IMDB ID (original + aliases)
        for current_imdb_id in imdb_ids:
            url = construct_url(current_imdb_id, content_type, season, episode, opts)
            #logging.debug(f"Constructed Torrentio URL for ID {current_imdb_id}: {url}")
            response = fetch_data(url)
            if not response or 'streams' not in response:
                logging.warning(f"No streams found for IMDb ID: {current_imdb_id} in instance {instance}")
                continue
                
            parsed_results = parse_results(response['streams'], instance)
            all_results.extend(parsed_results)
            
        # Remove duplicates based on info_hash
        seen_hashes = set()
        unique_results = []
        for result in all_results:
            if result['info_hash'] not in seen_hashes:
                seen_hashes.add(result['info_hash'])
                unique_results.append(result)
                
        logging.debug(f"Found {len(unique_results)} unique results after checking {len(imdb_ids)} IMDB IDs")
        return unique_results
    except Exception as e:
        logging.error(f"Error in scrape_torrentio_instance for {instance}: {str(e)}", exc_info=True)
        return []

def construct_url(imdb_id: str, content_type: str, season: int = None, episode: int = None, opts: str = DEFAULT_OPTS) -> str:
    #logging.info(f"Constructing Torrentio URL for {imdb_id} with content_type: {content_type}, season: {season}, episode: {episode}")
    if season is not None and episode is None:
        #logging.info(f"Multi-episode mode detected. Setting episode to 1 for {imdb_id}")
        episode = 1
    if content_type == "movie":
        return f"{TORRENTIO_BASE_URL}/{opts}/stream/movie/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{TORRENTIO_BASE_URL}/{opts}/stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{TORRENTIO_BASE_URL}/{opts}/stream/series/{imdb_id}.json"
    else:
        logging.error("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

def fetch_data(url: str) -> Dict:
    max_retries = 4
    base_backoff_seconds = 0.5
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        for attempt in range(max_retries):
            try:
                response = api.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    return data
                # Retry on server errors (5xx)
                if 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        sleep_seconds = base_backoff_seconds * (2 ** attempt) + random.uniform(0, 0.25)
                        logging.warning(
                            f"Server error {response.status_code} while fetching {url} (attempt {attempt + 1}/{max_retries}). "
                            f"Retrying in {sleep_seconds:.2f}s"
                        )
                        time.sleep(sleep_seconds)
                        continue
                # Non-retriable status codes
                logging.warning(f"Non-200 status ({response.status_code}) for URL {url}")
                return {}
            except (api.exceptions.RequestException, RemoteDisconnected) as e:
                if attempt < max_retries - 1:
                    sleep_seconds = base_backoff_seconds * (2 ** attempt) + random.uniform(0, 0.25)
                    logging.warning(
                        f"Error fetching data: {e} (attempt {attempt + 1}/{max_retries}) for {url}. "
                        f"Retrying in {sleep_seconds:.2f}s"
                    )
                    time.sleep(sleep_seconds)
                    continue
                logging.error(f"Error fetching data: {str(e)}")
                return {}
    except Exception as e:
        logging.error(f"Unexpected error in fetch_data: {str(e)}", exc_info=True)
    return {}

def parse_results(streams: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    skipped_count = 0
    no_title_count = 0
    no_info_hash_count = 0
    parse_error_count = 0
    
    for stream in streams:
        parsed_info = {}
        try:
            raw_title = stream.get('title', '')
            if not raw_title:
                no_title_count += 1
                continue
                
            behaviorHints = stream.get('behaviorHints', {})
            parsed_info['filename'] = behaviorHints.get('filename')
            parsed_info['bingeGroup'] = behaviorHints.get('bingeGroup')

            title_parts = raw_title.split('\n')
            
            # First line is always the main title
            name = title_parts[0].strip()
            size = 0.0
            seeders = 0
            languages = []
            source_site = None
            size_found = False
            
            # Look through all lines after the title for metadata
            for metadata_line in title_parts[1:]:
                metadata_line = metadata_line.strip()
                
                # Try to find size and seeders in each line
                size_info = parse_size(metadata_line)
                if size_info > 0:
                    size = size_info
                    size_found = True
                
                seeder_info = parse_seeder(metadata_line)
                if seeder_info > 0:
                    seeders = seeder_info

                # Extract source site (e.g., Rutor, ThePirateBay)
                source_match = re.search(r'⚙️\s*(\S+)', metadata_line)
                if source_match:
                    source_site = source_match.group(1)
                    parsed_info['source_site'] = source_site

                # Extract language flags
                lang_match = re.findall(r'[🇬🇧🇷🇺🇺🇦🇫🇷🇩🇪🇪🇸🇲🇽]', metadata_line)
                if lang_match:
                    languages.extend(lang_match)
                    parsed_info['languages'] = list(set(languages)) # Store unique languages

            if not size_found:
                # Attempt to parse size from filename if not found in title lines
                if parsed_info.get('filename'):
                     # Basic regex for size in filename (e.g., [2.5GB]) - might need refinement
                     size_match_fname = re.search(r'\[([\d.]+)\s*([GTKM]B)\]', parsed_info['filename'], re.IGNORECASE)
                     if size_match_fname:
                         size_str, unit = size_match_fname.groups()
                         try:
                             size_val = float(size_str)
                             unit = unit.upper()
                             if unit == 'GB': size = size_val
                             elif unit == 'MB': size = size_val / 1024
                             elif unit == 'TB': size = size_val * 1024
                             elif unit == 'KB': size = size_val / (1024 * 1024)
                             if size > 0: size_found = True
                         except ValueError:
                              pass # Ignore conversion errors

                if not size_found:
                    logging.warning(f"No size information found in title or filename: {raw_title}")


            info_hash = stream.get("infoHash", "")
            if not info_hash:
                no_info_hash_count += 1
                continue
                
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            fileIdx = stream.get('fileIdx')
            if fileIdx is not None:
                 # Use filename from behaviorHints if available for dn, otherwise use parsed name
                 dn_name = parsed_info.get('filename', name) 
                 # Ensure dn_name is not empty before adding
                 if dn_name:
                     magnet_link += f'&dn={quote_plus(dn_name)}&so={fileIdx}'
                 else:
                      # Fallback if filename is also empty
                      magnet_link += f'&so={fileIdx}'


            result = {
                'title': name,
                'size': round(size, 2),
                'source': f'{instance}{f" - {source_site}" if source_site else ""}', # Append source site if found
                'magnet': magnet_link,
                'seeders': seeders,
                'info_hash': info_hash,
                'parsed_info': parsed_info # Store extra parsed details
            }
            # Add languages directly if found
            if languages:
                 result['languages'] = list(set(languages))

            results.append(result)
            
        except Exception as e:
            parse_error_count += 1
            logging.error(f"Error parsing stream: {str(e)}", exc_info=True)
            if 'title' in stream:
                logging.error(f"Failed stream title: {stream['title']}")
            continue
    
    # Log summary (optional, uncomment if needed)
    # skipped_count = no_title_count + no_info_hash_count + parse_error_count
    # if skipped_count > 0 or parse_error_count > 0 or no_title_count > 0 or no_info_hash_count > 0:
    #     logging.debug(f"Torrentio parsing summary for {instance}:")
    #     logging.debug(f"- Total streams processed: {len(streams)}")
    #     logging.debug(f"- Successfully parsed: {len(results)}")
    #     logging.debug(f"- Skipped (no title): {no_title_count}")
    #     logging.debug(f"- Skipped (no info hash): {no_info_hash_count}")
    #     logging.debug(f"- Parse errors: {parse_error_count}")
    
    return results

def parse_size(size_info: str) -> float:
    try:
        # Try the original pattern first (emoji version)
        size_match = re.search(r'💾\s*([\d.]+)\s*(\w+)', size_info)
        if not size_match:
            # If the original pattern fails, try a more lenient pattern
            size_match = re.search(r'([\d.]+)\s*(\w+)', size_info)
        
        if size_match:
            size_str, unit = size_match.groups()
            # Clean the size string
            size_str = size_str.strip()
            if not size_str or size_str == '.':
                return 0.0
                
            try:
                size = float(size_str)
            except ValueError:
                return 0.0
                
            unit = unit.lower()
            if unit.startswith(('g', 'г')):  # GB, GiB (including Cyrillic г for Russian)
                return size
            elif unit.startswith(('m', 'м')):  # MB, MiB
                return size / 1024
            elif unit.startswith(('t', 'т')):  # TB, TiB
                return size * 1024
            elif unit.startswith(('k', 'к')):  # KB, KiB
                return size / (1024 * 1024)
            else:
                return size
    except Exception as e:
        logging.error(f"Error parsing size from '{size_info}': {str(e)}")
        return 0.0
    
    return 0.0

def parse_seeder(seeder_info: str) -> int:
    seeder_match = re.search(r'👤\s*(\d+)', seeder_info)
    if seeder_match:
        return int(seeder_match.group(1))
    return 0