import requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id
from settings import get_setting
from urllib.parse import urlencode

ZILEAN_URL = get_setting('Zilean', 'url')

def scrape_zilean(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    if not ZILEAN_URL:
        return []

    title = get_title_by_imdb_id(imdb_id)
    if not title:
        return []

    params = {'Query': title}
    
    if content_type.lower() == 'tv' and season is not None:
        params['Season'] = season
        if episode is not None:
            params['Episode'] = episode
    

    search_endpoint = f"{ZILEAN_URL}/dmm/filtered"
    encoded_params = urlencode(params)
    full_url = f"{search_endpoint}?{encoded_params}"
    
    logging.debug(f"Attempting to access Zilean API with URL: {full_url}")
    
    try:
        response = requests.get(full_url, headers={'accept': 'application/json'})
        
        logging.debug(f"Zilean API status code: {response.status_code}")
        #logging.info(f"Zilean API raw response: {response.text}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_zilean_results(data)
            except requests.exceptions.JSONDecodeError as json_error:
                logging.error(f"Failed to parse JSON response: {str(json_error)}")
                return []
        else:
            logging.error(f"Zilean API error: Status code {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error in scrape_zilean: {str(e)}", exc_info=True)
        return []

def parse_zilean_results(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        result = {
            'title': item.get('rawTitle', 'N/A'),  # Using rawTitle instead of title for more specificity
            'size': item.get('size', 0) / (1024 * 1024 * 1024),  # Convert to GB
            'source': 'Zilean',
            'magnet': f"magnet:?xt=urn:btih:{item.get('infoHash', '')}"
        }
        results.append(result)
    return results
