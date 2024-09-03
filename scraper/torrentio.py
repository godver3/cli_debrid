import logging
from api_tracker import api
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus
from settings import load_config, get_setting

DEFAULT_OPTS = "sort=qualitysize|qualityfilter=480p,scr,cam"
TORRENTIO_BASE_URL = "https://torrentio.strem.fun"

def scrape_torrentio_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    opts = settings.get('opts', '').strip()
    if not opts:
        opts = DEFAULT_OPTS
    
    try:
        url = construct_url(imdb_id, content_type, season, episode, opts)
        response = fetch_data(url)
        if not response or 'streams' not in response:
            logging.warning(f"No streams found for IMDb ID: {imdb_id} in instance {instance}")
            return []
        parsed_results = parse_results(response['streams'], instance)

        return parsed_results
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
        response = api.get(url)
        if response.status_code == 200:
            return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching data: {str(e)}")
    return {}

def parse_results(streams: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    for stream in streams:
        try:
            title = stream.get('title', '')
            logging.debug(f"Raw title: {title}")
            title_parts = title.split('\n')
            name = title_parts[0].strip()
            size = 0.0
            seeders = 0
            
            # Look for size and seeder info in all parts
            for part in title_parts:
                size_info = parse_size(part)
                if size_info > 0:
                    size = size_info
                seeder_info = parse_seeder(part)
                if seeder_info > 0:
                    seeders = seeder_info

            info_hash = stream.get("infoHash", "")
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            if stream.get('fileIdx') is not None:
                magnet_link += f'&dn={quote_plus(name)}&so={stream["fileIdx"]}'
            results.append({
                'title': name,
                'size': size,
                'source': f'{instance}',
                'magnet': magnet_link,
                'seeders': seeders
            })
            logging.debug(f"Parsed result: title={name}, size={size}, seeders={seeders}")
        except Exception as e:
            continue
    return results

def parse_size(size_info: str) -> float:
    # Try the original pattern first
    size_match = re.search(r'💾\s*([\d.]+)\s*(\w+)', size_info)
    if not size_match:
        # If the original pattern fails, try a more lenient pattern
        size_match = re.search(r'([\d.]+)\s*(\w+)', size_info)
    
    if size_match:
        size, unit = size_match.groups()
        size = float(size)
        if unit.lower() == 'gb':
            return size
        elif unit.lower() == 'mb':
            converted_size = size / 1024
            return converted_size
        else:
            return size
    
    logging.debug("Returning 0.0 as fallback")
    return 0.0

def parse_seeder(seeder_info: str) -> int:
    seeder_match = re.search(r'👤\s*(\d+)', seeder_info)
    if seeder_match:
        return int(seeder_match.group(1))
    return 0