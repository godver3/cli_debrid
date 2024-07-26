import requests
import json
import re
import logging
from settings import get_setting
from types import SimpleNamespace
from time import sleep
from datetime import datetime

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
        # Step 1: Add magnet
        magnet_data = {'magnet': magnet_link}
        magnet_response = requests.post(f"{API_BASE_URL}/torrents/addMagnet", headers=headers, data=magnet_data)
        magnet_response.raise_for_status()
        torrent_id = magnet_response.json()['id']

        rate_limited()
        # Step 2: Get torrent info
        info_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers)
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
        select_response = requests.post(f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}", headers=headers, data=select_data)
        select_response.raise_for_status()

        # Step 4: Wait for the torrent to be processed
        max_attempts = 1
        for attempt in range(max_attempts):
            rate_limited()
            links_response = requests.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers)
            links_response.raise_for_status()
            links_info = links_response.json()

            if links_info['status'] == 'downloaded':
                logging.info(f"Successfully added magnet to Real-Debrid. Torrent ID: {torrent_id}")
                return links_info['links']
            elif links_info['status'] in ['magnet_error', 'error', 'virus', 'dead']:
                logging.error(f"Torrent processing failed. Status: {links_info['status']}")
                return None
            else:
                logging.debug(f"Torrent is still being processed. Current status: {links_info['status']}")
                sleep(10)

        logging.error("Torrent processing timed out")
        return None

    except requests.exceptions.RequestException as e:
        logging.error(f"Error adding magnet to Real-Debrid: {str(e)}")
        logging.debug(f"Error details: {e.response.text if e.response else 'No response text available'}")
        return None

    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        return None

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
