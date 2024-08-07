import requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id
from metadata.metadata import get_year_from_imdb_id
from settings import get_setting
from urllib.parse import quote

PROWLARR_URL = get_setting('Prowlarr', 'url')
PROWLARR_API = get_setting('Prowlarr', 'api')
PROWLARR_ENABLED = get_setting('Prowlarr', 'enabled')

def scrape_prowlarr(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    if not PROWLARR_ENABLED:
        logging.info("Prowlarr disabled")
        return []

    title = get_title_by_imdb_id(imdb_id)
    if not title:
        return []
    year = get_year_from_imdb_id(imdb_id)
    if not year:
        return []
    
    if content_type.lower() == 'movie':
        params = f"{title} {year}"
    else:
        params = f"{title}"
    
    if content_type.lower() == 'tv' and season is not None:
        params = f"{params}.s{season:02d}"
        if episode is not None:
            params = f"{params}.e{episode:02d}"

    headers = {'X-Api-Key': PROWLARR_API,'accept': 'application/json'}
    encoded_params = quote(params)
    search_endpoint = f"{PROWLARR_URL}/api/v1/search?query={encoded_params}&type=search&limit=1000&offset=0"    
    full_url = f"{search_endpoint}"

    logging.debug(f"Attempting to access Prowlarr API with URL: {full_url}")
    
    try:
        response = requests.get(full_url, headers=headers, timeout=60)
        
        logging.debug(f"Prowlarr API status code: {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_prowlarr_results(data[:])
            except requests.exceptions.JSONDecodeError as json_error:
                logging.error(f"Failed to parse JSON response: {str(json_error)}")
                return []
        else:
            logging.error(f"Prowlarr API error: Status code {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error in scrape_prowlarr: {str(e)}", exc_info=True)
        return []

def parse_prowlarr_results(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        if not item['indexer'] == None and not item['size'] == None:
            result = {
                'title': item.get('title', 'N/A'),  
                'size': item.get('size', 0) / (1024 * 1024 * 1024),  # Convert to GB
                'source': f"Prowlarr - {item.get('indexer', 'N/A')}",
                'magnet': f"magnet:?xt=urn:btih:{item.get('infoHash', '')}"
            }
        results.append(result)
    return (results)
