"""Real-Debrid API client implementation"""

import os
import logging
import time
from typing import Optional, Dict, Any, Union, List
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import RealDebridAPIError, RealDebridAuthError
from utilities.settings import get_setting
from routes.api_tracker import api
import asyncio

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
            return None
            
        # Parse JSON response
        try:
            return response.json()
        except ValueError:
            return response.content
            
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
