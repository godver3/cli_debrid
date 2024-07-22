import logging
import requests
from settings import get_setting
from logging_config import get_logger
from database import get_media_item_status

logger = get_logger()

REQUEST_TIMEOUT = 10  # seconds

def get_mdblists():
    mdblist_api_key = get_setting('MDBList', 'api_key')
    mdblist_urls = get_setting('MDBList', 'urls')

    if not mdblist_api_key or not mdblist_urls:
        logger.error("MDBList API key or URLs not set. Please configure in settings.")
        return []

    url_list = [url.strip() + '/json' for url in mdblist_urls.split(',')]
    headers = {
        'Authorization': f'Bearer {mdblist_api_key}',
        'Accept': 'application/json'
    }

    all_mdblist_content = []
    for url in url_list:
        try:
            logger.info(f"Fetching MDBList content from URL: {url}")
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            all_mdblist_content.extend(data)
            logger.debug(f"Successfully fetched content from URL: {url}")
        except requests.RequestException as e:
            logger.error(f"Error fetching content from MDBList: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while processing MDBList response: {e}")

    return all_mdblist_content

def get_overseerr_headers(api_key):
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url, endpoint):
    return f"{base_url}{endpoint}"

def fetch_data(url, headers, cookies=None):
    response = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

def fetch_existing_overseerr_requests(overseerr_url, overseerr_api_key, imdb_id):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/request?filter=all&imdb_id={imdb_id}")

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data.get('results', [])
    except requests.RequestException as e:
        logger.error(f"Error fetching existing requests from Overseerr: {e}")
        return []

def add_to_overseerr(overseerr_url, overseerr_api_key, imdb_id, media_type):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/request")
    payload = {
        'mediaType': media_type,
        'tmdbId': imdb_id
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info(f"Successfully added {media_type} with IMDb ID {imdb_id} to Overseerr.")
    except requests.RequestException as e:
        logger.error(f"Error adding {media_type} with IMDb ID {imdb_id} to Overseerr: {e}")

def get_wanted_from_mdblists():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logger.error("Overseerr URL or API key not set. Please configure in settings.")
        return

    mdblist_content = get_mdblists()
    if not mdblist_content:
        logger.error("No content fetched from MDBList.")
        return

    for item in mdblist_content:
        media_type = item.get('mediatype')
        imdb_id = item.get('imdb_id')
        title = item.get('title', 'Unknown Title')
        year = item.get('release_year', 'Unknown Year')

        if not imdb_id or not media_type:
            logger.warning(f"Skipping item with missing data: {item}")
            continue

        status = get_media_item_status(imdb_id=imdb_id)
        if status == "Missing":
            existing_requests = fetch_existing_overseerr_requests(overseerr_url, overseerr_api_key, imdb_id)
            if existing_requests:
                logger.info(f"{media_type.capitalize()} with IMDb ID {imdb_id} already exists in Overseerr: {title} ({year})")
            else:
                logger.info(f"{media_type.capitalize()} missing: {title} ({year}), adding to Overseerr.")
                add_to_overseerr(overseerr_url, overseerr_api_key, imdb_id, media_type)
        else:
            logger.info(f"{media_type.capitalize()} already collected: {title} ({year})")

# Example usage
if __name__ == "__main__":
    get_wanted_from_mdblists()
