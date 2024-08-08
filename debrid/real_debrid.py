import requests
from requests.exceptions import HTTPError
import json
import re
import logging
from settings import get_setting
from types import SimpleNamespace
from time import sleep

class RealDebridUnavailableError(Exception):
    pass

API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
api_key = get_setting('RealDebrid', 'api_key')

# Common video file extensions
VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm', 'mpg', 'mpeg', 'm2ts', 'ts'
]

def is_video_file(filename):
    return any(filename.lower().endswith(f'.{ext}') for ext in VIDEO_EXTENSIONS)

def is_unwanted_file(filename):
    return 'sample' in filename.lower()

def rate_limited():
    sleep(1)  # Simple rate limiting, adjust as needed

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
        if 'magnet:?xt=urn:btih:' in magnet_link:
            magnet_data = {'magnet': magnet_link}
            torrent_response = requests.post(f"{API_BASE_URL}/torrents/addMagnet", headers=headers, data=magnet_data)
        else:
            torrent = requests.get(magnet_link, allow_redirects=False, timeout=60)
            if torrent.status_code != 200:
                sleep(1)
                torrent = requests.get(magnet_link, allow_redirects=False, timeout=60)
                if torrent.status_code != 200:
                    torrent.raise_for_status()
                    return False
            torrent_response = requests.put(f"{API_BASE_URL}/torrents/addTorrent", headers=headers, data=torrent, timeout=60)
            if not torrent_response:
                sleep(1)
                torrent_response = requests.put(f"{API_BASE_URL}/torrents/addTorrent", headers=headers, data=torrent, timeout=60)
            sleep(0.1)

        torrent_response.raise_for_status()
        torrent_id = torrent_response.json()['id']

        rate_limited()
        # Step 2: Get torrent info
        info_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
        info_response.raise_for_status()
        torrent_info = info_response.json()

        # Step 3: Select files based on video extensions and exclude samples
        files_to_select = [
            str(f['id']) for f in torrent_info['files']
            if is_video_file(f['path']) and not is_unwanted_file(f['path'])
        ]

        if not files_to_select:
            logging.warning("No suitable video files found in the torrent.")
            return None

        rate_limited()
        select_data = {'files': ','.join(files_to_select)}
        select_response = requests.post(f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}", headers=headers, data=select_data, timeout=60)
        select_response.raise_for_status()

        # Step 4: Wait for the torrent to be processed
        max_attempts = 1
        for attempt in range(max_attempts):
            rate_limited()
            links_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers, timeout=60)
            links_response.raise_for_status()
            links_info = links_response.json()

            if links_info['status'] == 'downloaded':
                logging.info(f"Successfully added cached torrent to Real-Debrid. Torrent ID: {torrent_id}")
                return links_info['links']
            elif links_info['status'] == 'downloading' or links_info['status'] == 'queued':
                logging.info(f"Successfully added uncached torrent to Real-Debrid. Torrent ID: {torrent_id}")
                return links_info['status']
            elif links_info['status'] in ['magnet_error', 'error', 'virus', 'dead']:
                logging.error(f"Torrent processing failed. Status: {links_info['status']}")
                return None
            else:
                logging.debug(f"Torrent is still being processed. Current status: {links_info['status']}")
                sleep(10)

        logging.error("Torrent processing timed out")
        return None

    except requests.exceptions.RequestException as e:
        if isinstance(e, HTTPError) and e.response.status_code == 503:
            logging.error("Real-Debrid service is unavailable (503 error)")
            raise RealDebridUnavailableError("Real-Debrid service is unavailable") from e
        logging.error(f"Error adding magnet to Real-Debrid: {str(e)}")
        logging.debug(f"Error details: {e.response.text if e.response else 'No response text available'}")
        raise

    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        raise

def logerror(response):
    errors = [
        [202, " action already done"],
        [400, " bad Request (see error message)"],
        [403, " permission denied (infringing torrent or account locked or not premium)"],
        [503, " service unavailable (see error message)"],
        [404, " wrong parameter (invalid file id(s)) / unknown resource (invalid id)"],
    ]
    if response.status_code not in [200, 201, 204]:
        desc = ""
        for error in errors:
            if response.status_code == error[0]:
                desc = error[1]
        logging.error(f"[realdebrid] error: ({response.status_code}{desc}) {response.content}")

def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Authorization': f'Bearer {api_key}'
    }
    try:
        rate_limited()
        response = requests.get(url, headers=headers)
        logerror(response)
        response_data = response.json()
        return json.loads(json.dumps(response_data), object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        logging.error(f"[realdebrid] error: (json exception): {e}")
        return None

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

def is_cached_on_rd(hashes):
    if isinstance(hashes, str):
        hashes = [hashes]
    url = f'{API_BASE_URL}/torrents/instantAvailability/{"/".join(hashes)}'
    response = get(url)
    if not response:
        return {hash_: False for hash_ in hashes}
    
    # Convert entire response to dictionary, including nested SimpleNamespace objects
    response = namespace_to_dict(response)
    
    cache_status = {}
    for hash_ in hashes:
        hash_key = hash_.lower()
        logging.debug(f"Checking availability for hash: {hash_key}")
        if hash_key in response:
            hash_data = response[hash_key]
            if isinstance(hash_data, list):
                # If the response is a list, check if it's non-empty
                cache_status[hash_] = len(hash_data) > 0
            elif isinstance(hash_data, dict):
                # If it's a dictionary, check for the 'rd' key
                rd_info = hash_data.get('rd', [])
                cache_status[hash_] = len(rd_info) > 0
            else:
                logging.warning(f"Unexpected data type for hash {hash_key}: {type(hash_data)}")
                cache_status[hash_] = False
        else:
            logging.debug(f"No response data for {hash_key}")
            cache_status[hash_] = False
    logging.debug(f"Cache status: {cache_status}")
    return cache_status

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
        # Flatten the list of dictionaries into a single dictionary
        all_files = {k: v for file_dict in files for k, v in file_dict.items()}
    elif isinstance(files, dict):
        all_files = files
    else:
        return None

    # Extract only the filenames
    return {'cached_files': [file_info['filename'] for file_info in all_files.values()]}

def get_magnet_files(magnet_link):
    hash_ = extract_hash_from_magnet(magnet_link)
    if not hash_:
        logging.error(f"Invalid magnet link: {magnet_link}")
        return None

    cache_status = is_cached_on_rd(hash_)
    if cache_status.get(hash_, False):
        return get_cached_files(hash_)
    
    return None
