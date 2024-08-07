import requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id
from metadata.metadata import get_year_from_imdb_id
from settings import get_setting
from urllib.parse import quote, urlencode

JACKETT_URL = get_setting('Jackett', 'url')
JACKETT_API = get_setting('Jackett', 'api')
JACKETT_FILTER = "!status:failing,test:passed"
ENABLED_INDEXERS = get_setting('Jackett', 'enabled_indexers')

def scrape_jackett(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    if not JACKETT_URL:
        logging.info("Jackett disabled")
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

    search_endpoint = f"{JACKETT_URL}/api/v2.0/indexers/{JACKETT_FILTER}/results?apikey={JACKETT_API}"
    
    query_params = {'Query': params}
    
    if ENABLED_INDEXERS:
        indexers = [indexer.strip().lower() for indexer in ENABLED_INDEXERS.split(',')]
        query_params.update({f'Tracker[]': indexer for indexer in indexers})

    full_url = f"{search_endpoint}&{urlencode(query_params, doseq=True)}"
    
    logging.debug(f"Attempting to access Jackett API with URL: {full_url}")

    try:
        response = requests.get(full_url, headers={'accept': 'application/json'})
        logging.debug(f"Jackett API status code: {response.status_code}")
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_jackett_results(data["Results"])
            except requests.exceptions.JSONDecodeError as json_error:
                logging.error(f"Failed to parse JSON response: {str(json_error)}")
                return []
        else:
            logging.error(f"Jackett API error: Status code {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error in scrape_jackett: {str(e)}", exc_info=True)
        return []

def parse_jackett_results(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        if item.get('MagnetUri'):
            magnet = item['MagnetUri']
        elif item.get('Link'):
            magnet = item['Link']
        else:
            continue

        if item.get('Tracker') and item.get('Size'):
            result = {
                'title': item.get('Title', 'N/A'),
                'size': item.get('Size', 0) / (1024 * 1024 * 1024),  # Convert to GB
                'source': f"Jackett - {item.get('Tracker', 'N/A')}",
                'magnet': magnet
            }
            results.append(result)

    return results
