"""AllDebrid API client implementation"""

import logging
import time
import threading
from typing import Optional, Dict, Any, List
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import AllDebridAPIError, AllDebridAuthError
from utilities.settings import get_setting
from routes.api_tracker import api
import asyncio

# Application agent identifier for AllDebrid API
AGENT = "cli_debrid"

# Global rate limiter for AllDebrid API calls
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
    """Get AllDebrid API key from settings"""
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise AllDebridAuthError("No API key found in settings. Please configure in settings.")
    return api_key

def should_retry_error(exception: Exception) -> bool:
    """Determine if we should retry the request based on the error"""
    if isinstance(exception, api.exceptions.HTTPError):
        return exception.response.status_code in [503, 504]  # Service Unavailable, Gateway Timeout
    return isinstance(exception, (api.exceptions.Timeout, api.exceptions.ConnectionError))

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((api.exceptions.RequestException, AllDebridAPIError, RateLimitError, api.exceptions.HTTPError)),
    retry_error_callback=lambda retry_state: None  # Return None on final failure
)
def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    use_query_auth: bool = False,
    **kwargs
) -> Any:
    """
    Make a request to the AllDebrid API

    Args:
        method: HTTP method (GET, POST, etc)
        endpoint: API endpoint (e.g. /magnet/status)
        api_key: AllDebrid API key
        data: Optional data for POST requests
        files: Optional files for upload
        use_query_auth: If True, use query parameter auth instead of header (for /user endpoint)
        **kwargs: Additional arguments for requests

    Returns:
        Response data from the API

    Raises:
        AllDebridAPIError: If the API returns an error
        AllDebridAuthError: If authentication fails
        ProviderUnavailableError: If the service is unavailable
        RateLimitError: If rate limit is exceeded
    """
    # Determine API version from endpoint (some endpoints use v4.1)
    if endpoint.startswith('/v4.1/'):
        base_url = "https://api.alldebrid.com"
        url = f"{base_url}{endpoint}"
    else:
        url = f"https://api.alldebrid.com/v4{endpoint}"

    # Set up headers and params
    headers = {}
    params = kwargs.pop('params', {})

    # Add agent to all requests
    params['agent'] = AGENT

    # Authentication - either via header or query param
    if use_query_auth:
        params['apikey'] = api_key
    else:
        headers['Authorization'] = f'Bearer {api_key}'

    kwargs['headers'] = headers
    kwargs['params'] = params

    # Add timeout if not already specified
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30  # 30 second timeout

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
                raise AllDebridAuthError("Invalid API key")
            elif response.status_code == 403:
                raise AllDebridAuthError("Access denied")
            elif response.status_code == 429:
                # Increase rate limiting interval when we hit 429
                with _api_rate_limiter['lock']:
                    _api_rate_limiter['min_interval'] = min(5.0, _api_rate_limiter['min_interval'] * 2)
                    logging.warning(f"Increased API rate limit interval to {_api_rate_limiter['min_interval']}s due to 429 error")
                raise RateLimitError("Rate limit exceeded")
            elif response.status_code == 404:
                response.raise_for_status()
            elif response.status_code in [503, 504]:
                raise AllDebridAPIError(f"Service temporarily unavailable (HTTP {response.status_code})")
            else:
                response.raise_for_status()

        # Some endpoints return no content
        if response.status_code == 204:
            _decrease_rate_limit_on_success()
            return {"success": True, "status_code": 204}

        # Parse JSON response
        try:
            result = response.json()

            # AllDebrid wraps all responses with status field
            if result.get('status') == 'error':
                error_info = result.get('error', {})
                error_code = error_info.get('code', 'UNKNOWN')
                error_message = error_info.get('message', 'Unknown error')

                if error_code in ['AUTH_BAD_APIKEY', 'AUTH_MISSING_APIKEY', 'AUTH_BLOCKED']:
                    raise AllDebridAuthError(f"Authentication error: {error_message}")
                else:
                    raise AllDebridAPIError(f"AllDebrid API error ({error_code}): {error_message}")

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
            raise AllDebridAPIError(f"Temporary service error: {str(e)}")
        raise ProviderUnavailableError(f"Request failed: {str(e)}")


async def get_all_magnets(api_key: str) -> Optional[List[Dict]]:
    """
    Fetches all magnets from the user's AllDebrid account asynchronously.
    AllDebrid returns all magnets in a single request (no pagination needed).

    Args:
        api_key: The AllDebrid API key.

    Returns:
        A list of all magnets, or None if an error occurs.
    """
    try:
        result = await asyncio.to_thread(
            make_request, 'GET', '/magnet/status', api_key
        )

        if result is None:
            return None

        # AllDebrid returns data.magnets array
        data = result.get('data', {})
        magnets = data.get('magnets', [])

        if not isinstance(magnets, list):
            logging.error(f"Expected list of magnets, got {type(magnets)}")
            return None

        logging.info(f"Successfully fetched {len(magnets)} magnets from AllDebrid")
        return magnets

    except (AllDebridAPIError, ProviderUnavailableError, RateLimitError) as e:
        logging.error(f"API error fetching magnets: {e}")
        return None

    except Exception as e:
        logging.error(f"Unexpected error fetching magnets: {e}", exc_info=True)
        return None
