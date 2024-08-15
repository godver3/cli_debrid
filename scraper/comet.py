import requests
import logging
import re
from typing import List, Dict, Any
from settings import load_config

def scrape_comet(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    all_results = []
    config = load_config()
    comet_instances = config.get('Scrapers', {})
    
    logging.debug(f"Comet settings: {comet_instances}")

    for instance, settings in comet_instances.items():
        if instance.startswith('Comet'):
            if not settings.get('enabled', False):
                logging.debug(f"Comet instance '{instance}' is disabled, skipping")
                continue

            logging.info(f"Scraping Comet instance: {instance}")
            
            try:
                instance_results = scrape_comet_instance(instance, settings, imdb_id, content_type, season, episode)
                all_results.extend(instance_results)
            except Exception as e:
                logging.error(f"Error scraping Comet instance '{instance}': {str(e)}", exc_info=True)

    return all_results

def scrape_comet_instance(instance: str, settings: Dict[str, Any], imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    comet_base_url = settings.get('url', '').rstrip('manifest.json')
    
    try:
        url = construct_url(comet_base_url, imdb_id, content_type, season, episode)
        logging.debug(f"Constructed URL for {instance}: {url}")
        response = fetch_data(url)
        logging.debug(f"Response from URL for {instance}: {response}")
        if not response or 'streams' not in response:
            logging.warning(f"No valid response received for IMDb ID: {imdb_id} from {instance}")
            return []
        parsed_results = parse_results(response['streams'], instance)
        logging.debug(f"Parsed results from {instance}: {parsed_results}")
        return parsed_results
    except Exception as e:
        logging.error(f"Error in scrape_comet_instance for {instance}: {str(e)}", exc_info=True)
        return []

def construct_url(base_url: str, imdb_id: str, content_type: str, season: int = None, episode: int = None) -> str:
    if content_type == "movie":
        return f"{base_url}stream/movies/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{base_url}stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{base_url}stream/series/{imdb_id}.json"
    else:
        logging.error("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

def fetch_data(url: str) -> Dict:
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Failed to fetch data from URL: {url} with status {response.status_code}")
    except requests.RequestException as e:
        logging.error(f"Request failed: {str(e)}")
    return {}

def parse_size(title: str) -> float:
    size_match = re.search(r'💾\s*([\d.]+)\s*(\w+)', title)
    if size_match:
        size, unit = size_match.groups()
        size = float(size)
        if unit.lower() == 'gb':
            return size
        elif unit.lower() == 'mb':
            return size / 1024
        elif unit.lower() == 'tb':
            return size * 1024
    logging.warning(f"Could not parse size from: {title}")
    return 0.0

def parse_results(streams: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    for stream in streams:
        try:
            title = stream.get('title', '')
            torrent_title = stream.get('torrentTitle', '')
            name = title.split('\n')[0] if '\n' in title else title
            size = parse_size(title)
            info_hash_match = re.search(r'/([a-f0-9]{40})/', stream.get('url', ''))
            if info_hash_match:
                info_hash = info_hash_match.group(1)
                magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
                results.append({
                    'title': torrent_title,
                    'size': size,
                    'source': f'Comet - {instance}',
                    'magnet': magnet_link
                })
            else:
                logging.warning(f"Could not extract info hash from URL: {stream.get('url', '')}")
        except Exception as e:
            logging.error(f"Error parsing result: {str(e)}")
            continue
    return results
