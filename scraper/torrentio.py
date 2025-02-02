import logging
from api_tracker import api
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus
from settings import load_config, get_setting
from database.database_reading import get_imdb_aliases

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
            logging.debug(f"Constructed Torrentio URL for ID {current_imdb_id}: {url}")
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
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = api.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching data: {str(e)}")
    return {}

def parse_results(streams: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    skipped_count = 0
    no_title_count = 0
    no_info_hash_count = 0
    parse_error_count = 0
    
    for stream in streams:
        try:
            title = stream.get('title', '')
            if not title:
                no_title_count += 1
                continue
                
            #logging.debug(f"Processing stream with raw title: {title}")
            title_parts = title.split('\n')
            
            # First line is always the main title
            name = title_parts[0].strip()
            size = 0.0
            seeders = 0
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

            if not size_found:
                logging.error(f"No size information found in any part of title: {title}")

            info_hash = stream.get("infoHash", "")
            if not info_hash:
                no_info_hash_count += 1
                continue
                
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            if stream.get('fileIdx') is not None:
                magnet_link += f'&dn={quote_plus(name)}&so={stream["fileIdx"]}'
            
            result = {
                'title': name,
                'size': size,
                'source': f'{instance}',
                'magnet': magnet_link,
                'seeders': seeders,
                'info_hash': info_hash
            }
            results.append(result)
            #logging.debug(f"Successfully parsed result: {result}")
            
        except Exception as e:
            parse_error_count += 1
            logging.error(f"Error parsing stream: {str(e)}")
            if 'title' in stream:
                logging.error(f"Failed stream title: {stream['title']}")
            continue
    
    #skipped_count = no_title_count + no_info_hash_count + parse_error_count
    #if skipped_count > 0:
    #    logging.debug(f"Torrentio parsing summary:")
    #    logging.debug(f"- Total streams: {len(streams)}")
    #    logging.debug(f"- Successfully parsed: {len(results)}")
    #    logging.debug(f"- Skipped {skipped_count} results:")
    #    logging.debug(f"  - No title: {no_title_count}")
    #    logging.debug(f"  - No info hash: {no_info_hash_count}")
    #    logging.debug(f"  - Parse errors: {parse_error_count}")
    
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
                logging.debug(f"Invalid size string '{size_str}' in '{size_info}'")
                return 0.0
                
            try:
                size = float(size_str)
            except ValueError:
                logging.debug(f"Could not convert '{size_str}' to float in '{size_info}'")
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
                logging.debug(f"Unknown size unit '{unit}' in '{size_info}'")
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