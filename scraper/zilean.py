from api_tracker import api, requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id
from settings import load_config
from urllib.parse import urlencode

def scrape_zilean(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    all_results = []
    config = load_config()
    zilean_instances = config.get('Scrapers', {})
    
    #logging.debug(f"Zilean settings: {zilean_instances}")

    for instance, settings in zilean_instances.items():
        if instance.startswith('Zilean'):
            if not settings.get('enabled', False):
                logging.debug(f"Zilean instance '{instance}' is disabled, skipping")
                continue

            #logging.info(f"Scraping Zilean instance: {instance}")
            
            try:
                instance_results = scrape_zilean_instance(instance, settings, imdb_id, title, year, content_type, season, episode, multi)
                all_results.extend(instance_results)
            except api.exceptions.RequestException as e:
                logging.error(f"Error scraping Zilean instance '{instance}': {str(e)}", exc_info=True)

    return all_results

def scrape_zilean_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: str, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    zilean_url = settings.get('url', '')
    if not zilean_url:
        logging.warning(f"Zilean URL is not set or invalid for instance {instance}. Skipping.")
        return []
    
    # if imdb_id:
    #     params = {'ImdbId': imdb_id, 'Query': title}

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
    
    #logging.debug(f"Attempting to access Zilean API for {instance} with URL: {full_url}")
    
    try:
        response = api.get(full_url, headers={'accept': 'application/json'})
        #logging.debug(f"Zilean API status code for {instance}: {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_zilean_results(data, instance)
            except api.exceptions.JSONDecodeError as json_error:
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
        result = {
            'title': item.get('raw_title', 'N/A'),
            'size': item.get('size', 0) / (1024 * 1024 * 1024),  # Convert to GB
            'source': f'{instance}',
            'magnet': f"magnet:?xt=urn:btih:{item.get('info_hash', '')}"
        }
        results.append(result)
    return results