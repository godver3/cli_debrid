import logging
import requests
from settings import get_setting
from logging_config import get_logger

logger = get_logger()

REQUEST_TIMEOUT = 10  # seconds
PAGINATION_TAKE = 100  # Number of items to fetch per pagination request

def get_overseerr_headers(api_key):
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url, endpoint):
    return f"{base_url}{endpoint}"

def fetch_all_overseerr_requests(overseerr_url, overseerr_api_key):
    headers = get_overseerr_headers(overseerr_api_key)
    all_requests = []
    skip = 0

    while True:
        endpoint = f"/api/v1/request?take={PAGINATION_TAKE}&skip={skip}&sort=added&requestedBy=1"
        url = get_url(overseerr_url, endpoint)

        try:
            logger.debug(f"Fetching Overseerr requests with URL: {url}")
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            requests_batch = data.get('results', [])
            logger.debug(f"Fetched {len(requests_batch)} requests from Overseerr")

            if not requests_batch:
                break

            all_requests.extend(requests_batch)
            skip += PAGINATION_TAKE

        except requests.RequestException as e:
            logger.error(f"Error fetching requests from Overseerr: {e}")
            logger.error(f"Response content: {e.response.content if e.response else 'No response content'}")
            break

    logger.debug(f"Total requests fetched: {len(all_requests)}")
    return all_requests

def delete_request(overseerr_url, overseerr_api_key, request_id):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/request/{request_id}")

    try:
        logger.debug(f"Deleting request with ID {request_id}, URL: {url}")
        response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info(f"Successfully deleted request with ID {request_id} from Overseerr.")
    except requests.RequestException as e:
        logger.error(f"Error deleting request with ID {request_id} from Overseerr: {e}")
        logger.error(f"Response content: {e.response.content if e.response else 'No response content'}")

def delete_all_requests():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logger.error("Overseerr URL or API key not set. Please configure in settings.")
        return

    all_requests = fetch_all_overseerr_requests(overseerr_url, overseerr_api_key)

    for request in all_requests:
        request_id = request.get('id')
        if request_id:
            delete_request(overseerr_url, overseerr_api_key, request_id)

# Example usage
if __name__ == "__main__":
    delete_all_requests()
