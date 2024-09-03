from api_tracker import api, requests
from api_tracker import api, requests
import json
import re
import logging
from settings import get_setting
from types import SimpleNamespace
from time import sleep
from functools import lru_cache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import time
import requests

class RealDebridUnavailableError(Exception):
    pass

API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
api_key = get_setting('RealDebrid', 'api_key')

# Common video file extensions
VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm', 'mpg', 'mpeg', 'm2ts', 'ts'
]

def is_video_file(filename):
    result = any(filename.lower().endswith(f'.{ext}') for ext in VIDEO_EXTENSIONS)
    logging.debug(f"is_video_file check for {filename}: {result}")
    return result

def is_unwanted_file(filename):
    return 'sample' in filename.lower()

def rate_limited(max_per_second=1):
    min_interval = 1.0 / max_per_second
    def decorate(func):
        last_time_called = [0.0]
        def rate_limited_function(*args, **kwargs):
            elapsed = time.time() - last_time_called[0]
            left_to_wait = min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            ret = func(*args, **kwargs)
            last_time_called[0] = time.time()
            return ret
        return rate_limited_function
    return decorate

def add_to_real_debrid(magnet_link):
    if not api_key:
        logging.error("Real-Debrid API token not found in settings")
        return

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    try:
        rate_limited()
        # Step 1: Add magnet or torrent file
        if magnet_link.startswith('magnet:'):
            magnet_data = {'magnet': magnet_link}
            torrent_response = api.post(f"{API_BASE_URL}/torrents/addMagnet", headers=headers, data=magnet_data)
        else:
            torrent = api.get(magnet_link, allow_redirects=False, timeout=60)
            if torrent.status_code != 200:
                sleep(1)
                torrent = api.get(magnet_link, allow_redirects=False, timeout=60)
                if torrent.status_code != 200:
                    torrent.raise_for_status()
                    return False
            torrent_response = api.put(f"{API_BASE_URL}/torrents/addTorrent", headers=headers, data=torrent, timeout=60)
            if not torrent_response:
                sleep(1)
                torrent_response = api.put(f"{API_BASE_URL}/torrents/addTorrent", headers=headers, data=torrent, timeout=60)
            sleep(0.1)

        torrent_response.raise_for_status()
        torrent_id = torrent_response.json()['id']

        rate_limited()
        # Step 2: Get torrent info
        info_response = api.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
        info_response.raise_for_status()
        torrent_info = info_response.json()

        # Add debug logging for all files in the torrent
        logging.debug(f"All files in torrent: {[f['path'] for f in torrent_info['files']]}")

        # Step 3: Select files based on video extensions and exclude samples
        files_to_select = [
            str(f['id']) for f in torrent_info['files']
            if is_video_file(f['path']) and not is_unwanted_file(f['path'])
        ]

        # Add debug logging for selected files
        logging.debug(f"Files selected as video files: {files_to_select}")
        logging.debug(f"Video files paths: {[f['path'] for f in torrent_info['files'] if str(f['id']) in files_to_select]}")

        if not files_to_select:
            logging.warning("No suitable video files found in the torrent.")
            return None

        rate_limited()
        select_data = {'files': ','.join(files_to_select)}
        select_response = api.post(f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}", headers=headers, data=select_data, timeout=60)
        select_response.raise_for_status()

        # Step 4: Wait for the torrent to be processed
        rate_limited()
        links_response = api.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
        links_response.raise_for_status()
        links_info = links_response.json()

        return {
            'status': links_info['status'],
            'links': links_info.get('links'),
            'files': [f['path'] for f in torrent_info['files'] if is_video_file(f['path']) and not is_unwanted_file(f['path'])],
            'torrent_id': torrent_id
        }

    except api.exceptions.RequestException as e:
        if isinstance(e, api.exceptions.HTTPError) and e.response.status_code == 503:
            logging.error("Real-Debrid service is unavailable (503 error)")
            raise RealDebridUnavailableError("Real-Debrid service is unavailable") from e
        logging.error(f"Error adding magnet to Real-Debrid: {str(e)}")
        logging.debug(f"Error details: {e.response.text if e.response else 'No response text available'}")
        raise

    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        raise

@lru_cache(maxsize=1000)
def extract_hash_from_magnet(magnet_link):
    match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet_link)
    if match:
        return match.group(1).lower()
    else:
        return None

def namespace_to_dict(obj):
    if isinstance(obj, SimpleNamespace):
        return {k: namespace_to_dict(v) for k, v in vars(obj).items()}
    elif isinstance(obj, list):
        return [namespace_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: namespace_to_dict(v) for k, v in obj.items()}
    else:
        return obj

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import requests
import logging

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception))
)
def is_cached_on_rd(hashes):
    if isinstance(hashes, str):
        hashes = [hashes]
    url = f'{API_BASE_URL}/torrents/instantAvailability/{"/".join(hashes)}'
    
    try:
        response = get(url)
        if not response:
            logging.warning(f"No response from Real-Debrid for hashes: {hashes}")
            return {hash_: False for hash_ in hashes}  # Consider all as not cached

        response = namespace_to_dict(response)

        cache_status = {}
        for hash_ in hashes:
            hash_key = hash_.lower()
            if hash_key in response and response[hash_key]:
                # Process as before
                hash_data = response[hash_key]
                has_video_files = False
                if isinstance(hash_data, list) and hash_data:
                    files = hash_data[0].get('rd', [])
                elif isinstance(hash_data, dict):
                    files = hash_data.get('rd', [])
                else:
                    files = []

                if files:
                    for file_info in files:
                        for filename in file_info.values():
                            if is_video_file(filename['filename']):
                                has_video_files = True
                                break
                        if has_video_files:
                            break

                cache_status[hash_] = has_video_files
            else:
                cache_status[hash_] = False  # Consider as not cached
        
        logging.debug(f"Cache status for hashes {hashes}: {cache_status}")
        return cache_status

    except Exception as e:
        logging.error(f"Error in is_cached_on_rd: {str(e)}")
        return {hash_: False for hash_ in hashes}  # Consider all as not cached in case of any error

def get_cached_files(hash_):
    url = f'{API_BASE_URL}/torrents/instantAvailability/{hash_}'
    response = get(url)
    if not response:
        return None

    response = namespace_to_dict(response)
    hash_data = response.get(hash_.lower(), [])

    if isinstance(hash_data, list) and hash_data:
        files = hash_data[0].get('rd', [])
    elif isinstance(hash_data, dict):
        files = hash_data.get('rd', [])
    else:
        return None

    if isinstance(files, list) and files:
        all_files = {k: v for file_dict in files for k, v in file_dict.items()}
    elif isinstance(files, dict):
        all_files = files
    else:
        return None

    return {'cached_files': [file_info['filename'] for file_info in all_files.values()]}

def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Authorization': f'Bearer {api_key}'
    }
    try:
        response = api.get(url, headers=headers, timeout=10)  # Add a timeout
        response.raise_for_status()  # Raise an exception for bad status codes
        return json.loads(json.dumps(response.json()), object_hook=lambda d: SimpleNamespace(**d))
    except requests.exceptions.Timeout:
        logging.error(f"Timeout error accessing {url}")
        return None
    except requests.exceptions.ConnectionError:
        logging.error(f"Connection error accessing {url}")
        return None
    except Exception as e:
        logging.error(f"[realdebrid] error: {e}")
        return None

def get_magnet_files(magnet_link):
    torrent_id = add_to_real_debrid(magnet_link)
    if not torrent_id:
        return None

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    try:
        rate_limited()
        info_response = api.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
        info_response.raise_for_status()
        torrent_info = info_response.json()

        return [f['path'] for f in torrent_info['files'] if is_video_file(f['path']) and not is_unwanted_file(f['path'])]

    except api.exceptions.RequestException as e:
        logging.error(f"Error getting magnet files from Real-Debrid: {str(e)}")
        return None

    except Exception as e:
        logging.error(f"Unexpected error in get_magnet_files: {str(e)}")
        return None

def process_hashes(hashes):
    cache_status = is_cached_on_rd(hashes)
    results = {}
    for hash_ in hashes:
        if cache_status.get(hash_, False):
            results[hash_] = get_cached_files(hash_)
        else:
            results[hash_] = None
    return results

def file_matches_item(filename, item):
    filename = filename.lower()
    
    if 'sample' in filename:
        return False

    quality = item['version'].lower()

    if item['type'] == 'movie':
        title = item['title'].lower().replace(' ', '.')
        year = item['year']
        movie_pattern = rf"(?i){re.escape(title)}.*{year}"
        return bool(re.search(movie_pattern, filename))
    elif item['type'] == 'episode':
        season_number = int(item['season_number'])
        episode_number = int(item['episode_number'])

        # Regular episode matching
        single_ep_pattern = rf"(?i)s0*{season_number}(?:[.-]?|\s*)e0*{episode_number}(?!\d)"
        range_pattern = rf"(?i)s0*{season_number}(?:[.-]?|\s*)e0*(\d+)(?:-|-)0*(\d+)"

        single_match = re.search(single_ep_pattern, filename)
        range_match = re.search(range_pattern, filename)

        if single_match:
            return True
        elif range_match:
            start_ep, end_ep = map(int, range_match.groups())
            return start_ep <= episode_number <= end_ep

        # Anime-specific matching
        if 'anime' in item.get('genres', []):
            absolute_pattern = rf"(?i)(?:[-_.\s]|^)(?:e0*)?{episode_number}(?:[-_.\s]|$)"
            return bool(re.search(absolute_pattern, filename))

    return False