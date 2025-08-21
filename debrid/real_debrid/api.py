"""Real-Debrid API client implementation"""

import os
import logging
import time
import threading
from typing import Optional, Dict, Any, Union, List
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import RealDebridAPIError, RealDebridAuthError
from utilities.settings import get_setting
from routes.api_tracker import api
import asyncio

# Global rate limiter for Real-Debrid API calls
_api_rate_limiter = {
    'last_request_time': 0,
    'min_interval': 0.5,  # Minimum 500ms between API calls
    'lock': threading.Lock()
}

def _wait_for_rate_limit():
    """Wait if necessary to respect rate limits"""
    with _api_rate_limiter['lock']:
        current_time = time.time()
        time_since_last = current_time - _api_rate_limiter['last_request_time']
        min_interval = _api_rate_limiter['min_interval']
        
        if time_since_last < min_interval:
            sleep_time = min_interval - time_since_last
            time.sleep(sleep_time)
        
        _api_rate_limiter['last_request_time'] = time.time()

def _decrease_rate_limit_on_success():
    """Gradually decrease rate limiting interval on successful requests"""
    with _api_rate_limiter['lock']:
        if _api_rate_limiter['min_interval'] > 0.5:  # Don't go below 500ms
            _api_rate_limiter['min_interval'] = max(0.5, _api_rate_limiter['min_interval'] * 0.95)

def get_api_key() -> str:
    """Get Real-Debrid API key from settings"""
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise RealDebridAuthError("No API key found in settings. Please configure in settings.")
    return api_key

def should_retry_error(exception: Exception) -> bool:
    """Determine if we should retry the request based on the error"""
    if isinstance(exception, api.exceptions.HTTPError):
        return exception.response.status_code in [503, 504]  # Service Unavailable, Gateway Timeout
    return isinstance(exception, (api.exceptions.Timeout, api.exceptions.ConnectionError))

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((api.exceptions.RequestException, RealDebridAPIError, RateLimitError, api.exceptions.HTTPError)),
    retry_error_callback=lambda retry_state: None  # Return None on final failure
)
def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    **kwargs
) -> Any:
    """
    Make a request to the Real-Debrid API
    
    Args:
        method: HTTP method (GET, POST, etc)
        endpoint: API endpoint (e.g. /torrents/info)
        api_key: Real-Debrid API key
        data: Optional data for POST requests
        files: Optional files for upload
        **kwargs: Additional arguments for requests
        
    Returns:
        Response data from the API
        
    Raises:
        RealDebridAPIError: If the API returns an error
        RealDebridAuthError: If authentication fails
        ProviderUnavailableError: If the service is unavailable
        RateLimitError: If rate limit is exceeded
    """
    url = f"https://api.real-debrid.com/rest/1.0{endpoint}"
    headers = {'Authorization': f'Bearer {api_key}'}
    kwargs['headers'] = headers
    
    # Apply rate limiting
    _wait_for_rate_limit()
    
    try:
        if method.upper() == 'GET':
            response = api.get(url, **kwargs)
        elif method.upper() == 'POST':
            response = api.post(url, data=data, files=files, **kwargs)
        elif method.upper() == 'PUT':
            response = api.put(url, data=data, files=files, **kwargs)
        elif method.upper() == 'DELETE':
            response = api.delete(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
            
        # Handle HTTP errors
        if response.status_code >= 400:
            if response.status_code == 401:
                raise RealDebridAuthError("Invalid API key")
            elif response.status_code == 403:
                raise RealDebridAuthError("Access denied")
            elif response.status_code == 429:
                # Convert to RateLimitError which will be caught by the retry decorator
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    try:
                        # If Retry-After is provided, use it as the wait time
                        retry_after_seconds = int(retry_after)
                        logging.warning(f"Rate limit exceeded. Server requested wait of {retry_after_seconds}s.")
                        time.sleep(retry_after_seconds)
                    except (ValueError, TypeError):
                        pass  # If we can't parse the Retry-After header, just continue with default retry
                
                # Increase rate limiting interval when we hit 429
                with _api_rate_limiter['lock']:
                    _api_rate_limiter['min_interval'] = min(5.0, _api_rate_limiter['min_interval'] * 2)
                    logging.warning(f"Increased API rate limit interval to {_api_rate_limiter['min_interval']}s due to 429 error")
                
                raise RateLimitError("Rate limit exceeded")
            elif response.status_code == 404:
                # Check if this is a duplicate torrent add attempt
                if method == 'POST' and endpoint == '/torrents/addMagnet':
                    return None
                response.raise_for_status()
            elif response.status_code in [503, 504]:
                raise RealDebridAPIError(f"Service temporarily unavailable (HTTP {response.status_code})")
            else:
                response.raise_for_status()
        
        # Some endpoints return no content
        if response.status_code == 204:
            _decrease_rate_limit_on_success()
            return {"success": True, "status_code": 204}
            
        # Parse JSON response
        try:
            result = response.json()
            _decrease_rate_limit_on_success()
            return result
        except ValueError:
            result = response.content
            _decrease_rate_limit_on_success()
            return result
            
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("Request timed out")
        
    except api.exceptions.RequestException as e:
        if should_retry_error(e):
            raise RealDebridAPIError(f"Temporary service error: {str(e)}")
        raise ProviderUnavailableError(f"Request failed: {str(e)}")

async def get_all_items(endpoint: str, api_key: str, limit: int = 500, extra_params: Optional[Dict] = None) -> Optional[List[Dict]]:
    """
    Fetches all items from a paginated Real-Debrid endpoint asynchronously using page-based pagination.

    Args:
        endpoint: The API endpoint (e.g., '/torrents').
        api_key: The Real-Debrid API key.
        limit: The number of items to fetch per request (max 5000 according to docs, use a reasonable default).
        extra_params: Optional dictionary of additional query parameters.

    Returns:
        A list of all items, or None if an error occurs.
    """
    all_items = []
    current_page = 1 # Start with page 1
    # RD docs say limit max 5000, but let's use 500 as a safer default per page
    limit = min(limit, 500)

    while True:
        try:
            # Use 'page' and 'limit' for pagination
            params = {'limit': limit, 'page': current_page}
            if extra_params:
                params.update(extra_params)

            logging.debug(f"Fetching items from {endpoint} with params: {params}")
            page_items = await asyncio.to_thread(
                make_request, 'GET', endpoint, api_key, params=params
            )

            # Check response: RD might return empty list on last page OR None on error/404
            if page_items is None:
                 # If make_request returns None (e.g. 404 on a page > 1), assume we're done
                 logging.warning(f"Received None response for {endpoint} at page {current_page}. Assuming end of list.")
                 break # Stop pagination

            if not isinstance(page_items, list):
                logging.error(f"Expected list response from {endpoint}, got {type(page_items)}. Stopping pagination.")
                return all_items # Return what we have

            if not page_items:
                # Empty list means no more items on this or subsequent pages
                logging.debug(f"Received empty list for {endpoint} at page {current_page}. End of list.")
                break # Stop pagination

            all_items.extend(page_items)
            logging.debug(f"Fetched {len(page_items)} items from page {current_page}. Total items: {len(all_items)}")

            # According to RD docs, X-Total-Count header *might* exist, but relying on empty list is safer
            # Stop if the number fetched is less than the limit (usually indicates last page)
            if len(page_items) < limit:
                 logging.debug("Fetched less items than limit, assuming last page.")
                 break

            current_page += 1 # Go to the next page

        except (RealDebridAPIError, ProviderUnavailableError, RateLimitError) as e:
            logging.error(f"API error fetching items from {endpoint} at page {current_page}: {e}")
            return None # Signal failure

        except Exception as e:
            logging.error(f"An unexpected error occurred during pagination for {endpoint}: {e}", exc_info=True)
            return None # Signal failure

    logging.info(f"Successfully fetched {len(all_items)} items from {endpoint} using page pagination.")
    return all_items

async def get_all_torrents(api_key: str) -> Optional[List[Dict]]:
    """Fetches all torrents from the user's Real-Debrid account asynchronously."""
    return await get_all_items('/torrents', api_key, limit=500, extra_params=None) # Use page pagination default

async def get_all_downloads(api_key: str) -> Optional[List[Dict]]:
    """Fetches all downloads from the user's Real-Debrid account asynchronously."""
    return await get_all_items('/downloads', api_key, limit=500, extra_params=None) # Use page pagination default
