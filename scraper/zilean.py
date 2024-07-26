import requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id  # Import the function from database.py
from settings import get_setting

ZILEAN_URL = get_setting('Zilean', 'url')

def scrape_zilean(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    if not ZILEAN_URL:
        return []

    title = get_title_by_imdb_id(imdb_id)
    if not title:
        return []

    try:
        if season is not None and episode is not None:
            season = int(season)
            episode = int(episode)
            query = f"{title} S{season:02d}E{episode:02d}"
        elif season is not None:
            season = int(season)
            query = f"{title} S{season:02d}"
        else:
            query = title
    except ValueError:
        return []

    search_endpoint = f"{ZILEAN_URL}/dmm/search"

    try:
        response = requests.post(search_endpoint, json={"queryText": query})
        if response.status_code == 200:
            data = response.json()
            return parse_zilean_results(data)
        else:
            error_data = response.json()
            logging.error(f"Zilean API error: {error_data.get('detail', 'Unknown error')}")
            return []
    except Exception as e:
        logging.error(f"Error in scrape_zilean: {str(e)}", exc_info=True)
        return []

def parse_zilean_results(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        result = {
            'title': item.get('filename', 'N/A'),
            'size': item.get('filesize', 0) / (1024 * 1024 * 1024),  # Convert to GB
            'source': 'Zilean',
            'magnet': f"magnet:?xt=urn:btih:{item.get('infoHash', '')}"
        }
        results.append(result)
    return results
