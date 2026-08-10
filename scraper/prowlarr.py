from routes.api_tracker import api
import hashlib
import logging
import threading
import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from utilities.settings import get_setting
from urllib.parse import urlencode, quote_plus
import re
import json
from scraper.functions.common import trim_magnet

# Query result cache — reduces API hits for duplicate NZB searches within TTL window
_NZB_CACHE: Dict[str, tuple] = {}   # key -> (timestamp, results)
_NZB_CACHE_LOCK = threading.Lock()
_NZB_CACHE_TTL = 600  # 10 minutes


def _cache_key(endpoint: str, params: dict) -> str:
    stable = f"{endpoint}|{sorted(params.items())}"
    return hashlib.sha256(stable.encode()).hexdigest()


def _cache_get(key: str) -> Optional[List]:
    with _NZB_CACHE_LOCK:
        entry = _NZB_CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _NZB_CACHE_TTL:
            return entry[1]
        if entry:
            del _NZB_CACHE[key]
    return None


def _cache_set(key: str, results: List) -> None:
    with _NZB_CACHE_LOCK:
        _NZB_CACHE[key] = (time.monotonic(), results)


def _build_prowlarr_params_list(
    title: str,
    year: int,
    content_type: str,
    imdb_id: Optional[str],
    tmdb_id: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    multi: bool,
    tags_setting: str,
) -> List[Dict[str, Any]]:
    """Always build both ID-based and title-based queries to run simultaneously."""
    base = {'limit': 1000, 'offset': 0}
    if tags_setting:
        try:
            ids = [int(t.strip()) for t in tags_setting.split(',') if t.strip().isdigit()]
            if ids:
                base['indexerIds'] = ids
        except ValueError:
            pass

    params_list = []
    clean_title = rename_special_characters(title)

    if content_type.lower() == 'movie':
        # 1. ID search
        if imdb_id or tmdb_id:
            p = {**base, 'type': 'movie', 'query': ''}
            if imdb_id:
                p['imdbId'] = imdb_id.replace('tt', '')
            elif tmdb_id:
                p['tmdbId'] = tmdb_id
            params_list.append(p)
        # 2. Title search
        q = f"{clean_title} {year or ''}".strip()
        params_list.append({**base, 'type': 'movie', 'query': q})

    elif content_type.lower() == 'episode':
        season_ep: Dict[str, Any] = {}
        if season is not None:
            season_ep['season'] = season
            if episode is not None and not multi:
                season_ep['episode'] = episode
        # 1. ID search (empty query, structured season/ep)
        if imdb_id or tmdb_id:
            p = {**base, 'type': 'tvsearch', 'query': '', **season_ep}
            if imdb_id:
                p['imdbId'] = imdb_id.replace('tt', '')
            elif tmdb_id:
                p['tmdbId'] = tmdb_id
            params_list.append(p)
        # 2. Title text search
        q_parts = [clean_title]
        if season is not None:
            if episode is not None and not multi:
                q_parts.append(f'S{season:02d}E{episode:02d}')
            else:
                q_parts.append(f'S{season:02d}')
        params_list.append({**base, 'type': 'tvsearch', 'query': ' '.join(q_parts), **season_ep})

    else:
        q = f"{clean_title} {year or ''}".strip()
        params_list.append({**base, 'type': 'search', 'query': q})

    return params_list


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

    tags_setting = settings.get('tags', '')
    headers = {'X-Api-Key': prowlarr_api_key, 'accept': 'application/json'}
    search_endpoint = f"{prowlarr_url}/api/v1/search"
    # scraper_timeout's own setting description says "0 to disable" — requests
    # treats timeout=None (not 0) as no timeout, so 0/falsy must map to None.
    timeout = get_setting('Scraping', 'scraper_timeout', 30) or None
    seeders_only = get_setting('Scraping', 'prowlarr_seeders_only', get_setting('Scraping', 'jackett_seeders_only', True))

    params_list = _build_prowlarr_params_list(
        title, year, content_type, imdb_id, tmdb_id, season, episode, multi, tags_setting
    )

    def _fetch(query_params):
        ck = _cache_key(search_endpoint, query_params)
        cached = _cache_get(ck)
        if cached is not None:
            logging.info(f"Prowlarr '{instance}' cache hit for {query_params.get('type')} query")
            return cached
        try:
            logging.debug(f"Prowlarr '{instance}' query: {query_params}")
            response = api.get(search_endpoint, headers=headers, params=query_params, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    results = parse_prowlarr_results(data, instance, seeders_only)
                    if results:
                        _cache_set(ck, results)
                    return results
                logging.error(f"Prowlarr '{instance}' unexpected response type: {type(data)}")
            else:
                logging.error(f"Prowlarr '{instance}' HTTP {response.status_code}: {response.text[:300]}")
        except api.exceptions.Timeout:
            logging.error(f"Prowlarr '{instance}' timed out")
        except Exception as e:
            logging.error(f"Prowlarr '{instance}' error: {e}", exc_info=True)
        return []

    # Run all queries in parallel
    all_instance_results: List[Dict[str, Any]] = []
    if len(params_list) == 1:
        all_instance_results = _fetch(params_list[0])
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(_fetch, p) for p in params_list]
            for f in as_completed(futures):
                all_instance_results.extend(f.result())

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

    logging.info(f"Found {len(unique_results)} unique results from Prowlarr instance {instance} for '{title}' ({len(all_instance_results)} total before dedup)")
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

        guid = item.get('guid')
        
        item_protocol = item.get('protocol', 'torrent').lower()
        is_nzb = (item_protocol == 'nzb')

        if magnet_url and magnet_url.startswith('magnet:'):
            primary_link = trim_magnet(magnet_url)
        elif download_url:
            primary_link = download_url
            is_torrent_url = not is_nzb
        # Check if the guid field contains a magnet link
        elif guid and guid.startswith('magnet:'):
            primary_link = trim_magnet(guid)
            logging.debug(f"Using magnet link from guid field for '{title}' from {ins_name}")
        else:
            filtered_no_link += 1
            item_debug = {
                'title': title,
                'indexer': item.get('indexer', 'Unknown'),
                'guid': guid,
                'protocol': item_protocol,
                'categories': item.get('categories', []),
                'has_magnet': magnet_url is not None,
                'has_download': download_url is not None
            }
            logging.debug(f"Skipping Prowlarr result '{title}' from {ins_name} - No magnetUrl or downloadUrl found. Item details: {json.dumps(item_debug, indent=2)}")
            continue

        seeders = item.get('seeders', 0)
        # Don't filter NZB results by seeders — usenet doesn't have seeders
        if seeders_only and seeders == 0 and not is_nzb:
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
            'protocol': item_protocol,
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
            'magnet_link': None,
            'protocol': item_protocol,
            'nzb_url': primary_link if is_nzb else None,
        }

        if is_nzb:
            # NZB result — primary_link is the NZB download URL
            result_dict['magnet'] = primary_link
        else:
            result_dict['magnet'] = primary_link
            if is_torrent_url:
                result_dict['torrent_url'] = primary_link
                if info_hash:
                    constructed_magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(str(title))}"
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
