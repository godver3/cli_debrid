import requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id
from metadata.metadata import get_year_from_imdb_id
from settings import load_config
from urllib.parse import quote

def scrape_prowlarr(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    all_results = []
    config = load_config()
    prowlarr_instances = config.get('Scrapers', {})
    
    logging.debug(f"Prowlarr settings: {prowlarr_instances}")

    title = get_title_by_imdb_id(imdb_id)
    year = get_year_from_imdb_id(imdb_id)
    if not title or not year:
        logging.error(f"Failed to get title or year for IMDB ID: {imdb_id}")
        return []

    for instance, settings in prowlarr_instances.items():
        if instance.startswith('Prowlarr'):
            if not settings.get('enabled', False):
                logging.debug(f"Prowlarr instance '{instance}' is disabled, skipping")
                continue

            logging.info(f"Scraping Prowlarr instance: {instance}")
            
            try:
                instance_results = scrape_prowlarr_instance(instance, settings, title, year, content_type, season, episode)
                all_results.extend(instance_results)
            except Exception as e:
                logging.error(f"Error scraping Prowlarr instance '{instance}': {str(e)}", exc_info=True)

    return all_results

def scrape_prowlarr_instance(instance: str, settings: Dict[str, Any], title: str, year: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    prowlarr_url = settings.get('url', '')
    prowlarr_api = settings.get('api', '')

    if content_type.lower() == 'movie':
        params = f"{title} {year}"
    else:
        params = f"{title}"
    
    if content_type.lower() == 'tv' and season is not None:
        params = f"{params}.s{season:02d}"
        if episode is not None:
            params = f"{params}.e{episode:02d}"

    headers = {'X-Api-Key': prowlarr_api, 'accept': 'application/json'}
    encoded_params = quote(params)
    search_endpoint = f"{prowlarr_url}/api/v1/search?query={encoded_params}&type=search&limit=1000&offset=0"    
    full_url = f"{search_endpoint}"
    
    logging.debug(f"Attempting to access Prowlarr API for {instance} with URL: {full_url}")
    
    try:
        response = requests.get(full_url, headers=headers, timeout=60)
        
        logging.debug(f"Prowlarr API status code for {instance}: {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_prowlarr_results(data[:], instance)
            except requests.exceptions.JSONDecodeError as json_error:
                logging.error(f"Failed to parse JSON response for {instance}: {str(json_error)}")
                return []
        else:
            logging.error(f"Prowlarr API error for {instance}: Status code {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error in scrape_prowlarr_instance for {instance}: {str(e)}", exc_info=True)
        return []

def parse_prowlarr_results(data: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        if item.get('indexer') is not None and item.get('size') is not None:
            if 'infoHash' in item:
                result = {
                    'title': item.get('title', 'N/A'),  
                    'size': item.get('size', 0) / (1024 * 1024 * 1024),  # Convert to GB
                    'source': f"Prowlarr - {instance} - {item.get('indexer', 'N/A')}",
                    'magnet': f"magnet:?xt=urn:btih:{item.get('infoHash', '')}"
                }       
                results.append(result)
    return results
