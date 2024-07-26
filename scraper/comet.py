import requests
import logging
import re
from typing import List, Dict, Any, Tuple
from settings import get_setting

COMET_BASE_URL = get_setting('Comet', 'url').rstrip('manifest.json')
DEBRID_API_KEY = get_setting('Debrid', 'api_key')

def scrape_comet(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        url = construct_url(imdb_id, content_type, season, episode)
        logging.debug(f"Constructed URL: {url}")  # Debug log for constructed URL
        response = fetch_data(url)
        logging.debug(f"Response from URL: {response}")  # Debug log for response
        if not response:
            logging.warning(f"No response received for IMDb ID: {imdb_id}")
            return url, []
        if 'streams' not in response:
            logging.warning(f"No 'streams' key in response for IMDb ID: {imdb_id}")
            return url, []
        parsed_results = parse_results(response['streams'])
        logging.debug(f"Parsed results: {parsed_results}")  # Debug log for parsed results
        return url, parsed_results
    except Exception as e:
        logging.error(f"Error in scrape_comet: {str(e)}", exc_info=True)
        return "", []

def construct_url(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> str:
    if content_type == "movie":
        return f"{COMET_BASE_URL}stream/movies/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{COMET_BASE_URL}stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{COMET_BASE_URL}stream/series/{imdb_id}.json"
    else:
        logging.error("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

def fetch_data(url: str) -> Dict:
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Failed to fetch data from URL: {url} with status {response.status_code}")  # Debug log for fetch failure
    except requests.RequestException as e:
        logging.error(f"Request failed: {str(e)}")
    return {}

def parse_results(streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for stream in streams:
        try:
            title = stream.get('torrentTitle', '')
            size = stream.get('torrentSize', 0) / (1024 ** 3)  # Convert to GB
            info_hash = re.search(r'/([a-f0-9]{40})/', stream.get('url', '')).group(1)
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            results.append({
                'title': title,
                'size': size,
                'source': 'Comet',
                'magnet': magnet_link
            })
        except Exception as e:
            logging.error(f"Error parsing result: {str(e)}")
            continue
    return results
