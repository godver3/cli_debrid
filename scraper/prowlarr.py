from routes.api_tracker import api
import logging
from typing import List, Dict, Any, Optional
from utilities.settings import get_setting
from urllib.parse import urlencode
import re
import json # Import json for pretty printing

def scrape_prowlarr_instance(
    instance: str,
    settings: Dict[str, Any],
    imdb_id: Optional[str],
    title: str,
    year: int,
    content_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    multi: bool = False,
    tmdb_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    logging.info(f"Scraping Prowlarr instance: {instance} for '{title}' ({year})")
    prowlarr_url = settings.get('url', '').rstrip('/')
    prowlarr_api_key = settings.get('api_key', '')

    if not prowlarr_url or not prowlarr_api_key:
        logging.error(f"Prowlarr instance '{instance}' is missing URL or API key.")
        return []

    query_params = {
        'limit': 1000,
        'offset': 0
    }
    
    search_query_parts = [rename_special_characters(title)]

    if content_type.lower() == 'movie':
        query_params['type'] = 'movie'
        if year:
            search_query_parts.append(str(year))
        if imdb_id:
            query_params['imdbId'] = imdb_id.replace('tt', '')
        elif tmdb_id:
             query_params['tmdbId'] = tmdb_id
    elif content_type.lower() == 'episode':
        query_params['type'] = 'tvsearch'
        if season is not None:
            search_query_parts.append(f"S{season:02d}")
            query_params['season'] = season
            if episode is not None and not multi:
                search_query_parts.append(f"E{episode:02d}")
                query_params['episode'] = episode
        if imdb_id:
            query_params['imdbId'] = imdb_id.replace('tt', '')
        elif tmdb_id:
             query_params['tmdbId'] = tmdb_id
    else:
        query_params['type'] = 'search'
        if year:
            search_query_parts.append(str(year))

    query_params['query'] = " ".join(search_query_parts)
    
    # Handle tags (Indexer IDs)
    tags_setting = settings.get('tags', '')
    if tags_setting:
        try:
            indexer_ids = [int(tag.strip()) for tag in tags_setting.split(',') if tag.strip().isdigit()]
            if indexer_ids:
                query_params['indexerIds'] = indexer_ids
                logging.debug(f"Prowlarr instance '{instance}' will use specific indexer IDs: {indexer_ids}")
        except ValueError:
            logging.warning(f"Could not parse Prowlarr tags (Indexer IDs) for instance '{instance}'. Expected comma-separated numbers. Value: '{tags_setting}'")

    headers = {'X-Api-Key': prowlarr_api_key, 'accept': 'application/json'}
    search_endpoint = f"{prowlarr_url}/api/v1/search"
    
    logging.debug(f"Prowlarr query for {instance}: URL: {search_endpoint}, Params: {query_params}")
    
    all_instance_results = []
    try:
        response = api.get(search_endpoint, headers=headers, params=query_params, timeout=get_setting('Scraping', 'scraper_timeout', 30))
        
        logging.debug(f"Prowlarr API response status for {instance} ({query_params.get('query')}): {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()

                if not isinstance(data, list):
                    logging.error(f"Prowlarr response for {instance} was not a list: {type(data)}. Full response: {data}")
                    return []
                
                seeders_only = get_setting('Scraping', 'prowlarr_seeders_only', get_setting('Scraping', 'jackett_seeders_only', True))
                
                all_instance_results = parse_prowlarr_results(data, instance, seeders_only)
            except ValueError as json_error:
                logging.error(f"Failed to parse JSON response for {instance} searching '{query_params.get('query')}': {str(json_error)}. Response text: {response.text[:500]}")
                return []
        else:
            logging.error(f"Prowlarr API error for {instance} searching '{query_params.get('query')}': Status {response.status_code}. Response: {response.text[:500]}")
            return []
    except api.exceptions.Timeout:
        logging.error(f"Prowlarr request for {instance} timed out for query '{query_params.get('query')}'.")
        return []
    except Exception as e:
        logging.error(f"Error querying Prowlarr API for {instance} ('{query_params.get('query')}'): {str(e)}", exc_info=True)
        return []

    seen_keys = set()
    unique_results = []
    for result in all_instance_results:
        unique_key = result.get('parsed_info', {}).get('guid') or result.get('magnet')
        
        if unique_key and unique_key not in seen_keys:
            seen_keys.add(unique_key)
            unique_results.append(result)
        elif not unique_key:
            logging.warning(f"Prowlarr result for '{result.get('title')}' has no GUID or magnet for deduplication. Adding it anyway.")
            unique_results.append(result)

    logging.info(f"Found {len(unique_results)} unique results from Prowlarr instance {instance} for '{query_params.get('query')}'")
    return unique_results

def parse_prowlarr_results(data: List[Dict[str, Any]], ins_name: str, seeders_only: bool) -> List[Dict[str, Any]]:
    results = []
    if not isinstance(data, list):
        logging.error(f"Prowlarr parsing error: Expected a list, got {type(data)}")
        return results

    filtered_no_link = 0
    filtered_no_seeders = 0

    logging.debug(f"Parsing {len(data)} items from Prowlarr instance {ins_name}") # Added count log
    for idx, item in enumerate(data): # Added index for logging

        title = item.get('title', 'N/A')
        
        magnet_url = item.get('magnetUrl')
        download_url = item.get('downloadUrl')
        info_hash = item.get('infoHash', '').lower()

        primary_link = None
        is_torrent_url = False

        if magnet_url and magnet_url.startswith('magnet:'):
            primary_link = magnet_url
        elif download_url:
            primary_link = download_url
            is_torrent_url = True
        else:
            filtered_no_link += 1
            logging.debug(f"Skipping Prowlarr result '{title}' from {ins_name} - No magnetUrl or downloadUrl found.")
            continue

        seeders = item.get('seeders', 0)
        if seeders_only and seeders == 0:
            filtered_no_seeders += 1
            continue
            
        size_bytes = item.get('size', 0)
        size_gb = round(size_bytes / (1024 * 1024 * 1024), 2) if size_bytes else 0.0

        indexer_name = item.get('indexer', 'Unknown Indexer')
        source_name = f"{ins_name} - {indexer_name}"

        if not info_hash and magnet_url and magnet_url.startswith('magnet:'):
            match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet_url, re.IGNORECASE)
            if match:
                info_hash = match.group(1).lower()
        
        if not title or not primary_link:
            logging.warning(f"Skipping Prowlarr item due to missing title or link: {item}")
            continue

        parsed_info = {
            'guid': item.get('guid'),
            'indexer_id_prowlarr': item.get('indexerId'),
            'protocol': item.get('protocol', 'torrent').lower(),
            'publish_date': item.get('publishDate'),
            'leechers': item.get('leechers'),
            'peers': item.get('peers', seeders + item.get('leechers', 0)),
            'grabs': item.get('grabs') or item.get('snatches'),
            'categories_prowlarr': item.get('categories', []),
            'imdb_id_prowlarr': item.get('imdbId'),
            'tmdb_id_prowlarr': item.get('tmdbId'),
            'tvdb_id_prowlarr': item.get('tvdbId'),
            'indexer_raw_name': item.get('indexer'),
            'rejections': item.get('rejections') 
        }

        result_dict = {
            'title': title,
            'size': size_gb,
            'source': source_name,
            'seeders': seeders,
            'hash': info_hash,
            'parsed_info': parsed_info,
            'magnet': None,
            'torrent_url': None,
            'magnet_link': None
        }
        
        result_dict['magnet'] = primary_link
        
        if is_torrent_url:
            result_dict['torrent_url'] = primary_link
            if info_hash:
                constructed_magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urlencode(title)}"
                result_dict['magnet_link'] = constructed_magnet
        else:
            result_dict['magnet_link'] = primary_link

        results.append(result_dict)

    if filtered_no_link > 0 or filtered_no_seeders > 0:
        logging.debug(f"Prowlarr parsing summary for {ins_name}: Total items: {len(data)}, Parsed: {len(results)}, Filtered (no link): {filtered_no_link}, Filtered (no seeders): {filtered_no_seeders}")

    return results

def rename_special_characters(text: str) -> str:
    '''
    replacements = [
        ("&", ""), ("\u00fc", "ue"), ("\u00e4", "ae"), ("\u00e2", "a"),
        ("\u00e1", "a"), ("\u00e0", "a"), ("\u00f6", "oe"), ("\u00f4", "o"),
        ("\u00e8", "e"), (":", ""), ("(", ""), (")", ""), ("`", ""),
        (",", ""), ("!", ""), ("?", ""), (" - ", " "), ("'", ""),
        ("*", ""), (".", " "),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    text = text.replace("'", "")
    '''
    return text
