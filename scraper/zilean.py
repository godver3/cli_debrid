from routes.api_tracker import api, requests
import logging
from typing import List, Dict, Any, Tuple
from utilities.settings import load_config
from urllib.parse import urlencode

# Timeout for API requests in seconds
TIMEOUT = 10

def scrape_zilean_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    zilean_url = settings.get('url', '')
    if not zilean_url:
        logging.warning(f"Zilean URL is not set or invalid for instance {instance}. Skipping.")
        return []
    
    params = {'Query': title}
        
    if content_type.lower() == 'movie':
        params['Year'] = year
    else:
        if season is not None:
            params['Season'] = season
            if episode is not None and not multi:
                params['Episode'] = episode

    search_endpoint = f"{zilean_url}/dmm/filtered"
    encoded_params = urlencode(params)
    full_url = f"{search_endpoint}?{encoded_params}"
    
    try:
        response = api.get(full_url, headers={'accept': 'application/json'}, timeout=TIMEOUT)
        
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_zilean_results(data, instance)
            except requests.exceptions.JSONDecodeError as json_error:
                logging.error(f"Failed to parse JSON response for {instance}: {str(json_error)}")
                return []
        else:
            logging.error(f"Zilean API error for {instance}: Status code {response.status_code}")
            return []
    except api.exceptions.RequestException as e:
        logging.error(f"Error in scrape_zilean_instance for {instance}: {str(e)}", exc_info=True)
        return []

def parse_zilean_results(data: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        size = item.get('size', 0)
        # Convert size to float if it's a string, otherwise use as is
        # Handle potential errors during conversion
        try:
            size_gb = float(size) / (1024 * 1024 * 1024) if isinstance(size, (str, int, float)) else 0.0
        except (ValueError, TypeError):
             logging.warning(f"Could not convert size '{size}' to float for item: {item.get('raw_title')}")
             size_gb = 0.0
        
        info_hash = item.get('info_hash', '')
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}" if info_hash else ''

        result = {
            'title': item.get('raw_title', 'N/A'),
            'size': round(size_gb, 2),  # Round to 2 decimal places
            'source': f'{instance}',
            'magnet': magnet_link,
            'info_hash': info_hash,
            'resolution': item.get('resolution'),
            'quality': item.get('quality'),
            'codec': item.get('codec'),
            'audio': item.get('audio'),
            'channels': item.get('channels'),
            'hdr': item.get('hdr'),
            'languages': item.get('languages'),
             # Add other potentially useful fields if needed
             'year': item.get('year'), 
             'parsed_info': item # Store the full parsed details for potential future use/filtering
        }
        # Filter out results with no magnet link
        if magnet_link:
             results.append(result)
        else:
             logging.debug(f"Skipping Zilean result with no info_hash: {item.get('raw_title')}")

    return results