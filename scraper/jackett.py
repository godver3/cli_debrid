import requests
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id
from metadata.metadata import get_year_from_imdb_id
from settings import get_setting, load_config as get_jackett_settings
from urllib.parse import quote, urlencode
import json

JACKETT_FILTER = "!status:failing,test:passed"

def scrape_jackett(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    jackett_results = []
    all_settings = get_jackett_settings()
    
    # Debug log to check the structure of all_settings
    logging.debug(f"All settings: {json.dumps(all_settings, indent=2)}")
    jackett_instances = all_settings.get('Scrapers', {})
    jackett_filter = "!status:failing,test:passed"
    for instance, settings in jackett_instances.items():
        if instance.startswith('Jackett'):
            if not settings.get('enabled', False):
                logging.debug(f"Jackett instance '{instance}' is disabled, skipping")
                continue
            logging.info(f"Scraping Jackett instance: {instance}")
            
            try:
                instance_results = scrape_jackett_instance(instance, settings, title, year, content_type, season, episode, jackett_filter)
                jackett_results.extend(instance_results)
            except Exception as e:
                logging.error(f"Error scraping Jackett instance '{instance}': {str(e)}", exc_info=True)
    return jackett_results

def scrape_jackett_instance(instance: str, settings: Dict[str, Any], title: str, year: str, content_type: str, season: int, episode: int, jackett_filter: str) -> List[Dict[str, Any]]:
    jackett_url = settings['url']
    jackett_api = settings['api']
    enabled_indexers = settings.get('enabled_indexers', '').lower()
    seeders_only = get_setting('Debug', 'jackett_seeders_only', False)
    logging.debug(f"Seeders only status: {seeders_only}")

    if content_type.lower() == 'movie':
        params = f"{title} {year}"
    else:
        params = f"{title}"
        if season is not None:
            params = f"{params}.s{season:02d}"
            if episode is not None:
                params = f"{params}.e{episode:02d}"

    search_endpoint = f"{jackett_url}/api/v2.0/indexers/{jackett_filter}/results?apikey={jackett_api}"
    query_params = {'Query': params}
    
    if enabled_indexers:
        query_params.update({f'Tracker[]': {enabled_indexers}})

    full_url = f"{search_endpoint}&{urlencode(query_params, doseq=True)}"
    logging.debug(f"Jackett instance '{instance}' URL: {full_url}")

    response = requests.get(full_url, headers={'accept': 'application/json'})
    logging.debug(f"Jackett instance '{instance}' API status code: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        return parse_jackett_results(data["Results"], instance, seeders_only)
    else:
        logging.error(f"Jackett instance '{instance}' API error: Status code {response.status_code}")
        return []

def parse_jackett_results(data: List[Dict[str, Any]], ins_name: str, seeders_only: bool) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        if item.get('MagnetUri'):
            magnet = item['MagnetUri']
        elif item.get('Link'):
            magnet = item['Link']
        else:
            continue

        seeders = item.get('Seeders', 0)
        if seeders_only and seeders == 0:
            logging.debug(f"Filtered out [item.get('Title')] due to no seeders")
            continue

        if item.get('Tracker') and item.get('Size'):
            result = {
                'title': item.get('Title', 'N/A'),
                'size': item.get('Size', 0) / (1024 * 1024 * 1024),  # Convert to GB
                'source': f"{ins_name} - {item.get('Tracker', 'N/A')}",
                'magnet': magnet,
                'seeders': seeders
            }
            results.append(result)
    return results