"""AllDebrid API client implementation with improved rate limiting"""

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

# AllDebrid API rate limits (from official docs):
# - 12 requests per second
# - 600 requests per minute

# Global rate limiter with sliding window approach (similar to Trakt implementation)
_api_rate_limiter = {
    'last_request_time': 0,
    'min_interval': 0.083,  # ~12 req/sec (1000ms / 12 = 83ms)
    'request_times': [],  # Sliding window for 600/min limit
    'window_seconds': 60,
    'requests_per_window': 600,
    'lock': threading.Lock()
}

# AllDebrid error code mapping
ALLDEBRID_ERROR_CODES = {
    'AUTH_MISSING_APIKEY': AllDebridAuthError,
    'AUTH_BAD_APIKEY': AllDebridAuthError,
    'AUTH_USER_BANNED': AllDebridAuthError,
    'MAGNET_INVALID_URI': AllDebridAPIError,
    'MAGNET_MUST_BE_PREMIUM': AllDebridAuthError,
    'MAGNET_TOO_MANY': AllDebridAPIError,
    'MAGNET_TOO_MANY_ACTIVE': AllDebridAPIError,
    'LINK_HOST_NOT_SUPPORTED': AllDebridAPIError,
    'LINK_DOWN': AllDebridAPIError,
    'LINK_PASS_PROTECTED': AllDebridAPIError,
    'LINK_ERROR': AllDebridAPIError,
}

# Magnet status codes (0-15)
MAGNET_STATUS_CODES = {
    0: 'In Queue',
    1: 'Downloading',
    2: 'Compressing',
    3: 'Uploading',
    4: 'Ready',
    5: 'Error',
    6: 'Virus',
    7: 'Dead',
    8: 'Error - No peer',
    9: 'Error - Internal',
    10: 'Error - Limit reached',
    11: 'Magnet conversion error',
    15: 'Unavailable - No peer',
}


def _wait_for_rate_limit():
    """
    Wait if necessary to respect AllDebrid rate limits using sliding window approach.
    Enforces both per-second (12 req/sec) and per-minute (600 req/min) limits.
    """
    with _api_rate_limiter['lock']:
        current_time = time.time()

        # Per-request delay (12 req/sec = 83ms interval)
        time_since_last = current_time - _api_rate_limiter['last_request_time']
        min_interval = _api_rate_limiter['min_interval']

        if time_since_last < min_interval:
            sleep_time = min_interval - time_since_last
            time.sleep(sleep_time)
            current_time = time.time()

        # Sliding window check for 600 req/min limit
        window_start = current_time - _api_rate_limiter['window_seconds']
        _api_rate_limiter['request_times'] = [
            t for t in _api_rate_limiter['request_times'] if t > window_start
        ]

        if len(_api_rate_limiter['request_times']) >= _api_rate_limiter['requests_per_window']:
            # Need to wait until oldest request falls out of window
            oldest_request = _api_rate_limiter['request_times'][0]
            wait_time = _api_rate_limiter['window_seconds'] - (current_time - oldest_request)
            if wait_time > 0:
                logging.warning(f"AllDebrid sliding window limit reached. Waiting {wait_time:.2f}s")
                time.sleep(wait_time)
                current_time = time.time()

        # Record this request
        _api_rate_limiter['request_times'].append(current_time)
        _api_rate_limiter['last_request_time'] = current_time


def _decrease_rate_limit_on_success():
    """Gradually decrease rate limiting interval on successful requests"""
    with _api_rate_limiter['lock']:
        # Don't go below 83ms (12 req/sec)
        if _api_rate_limiter['min_interval'] > 0.083:
            _api_rate_limiter['min_interval'] = max(0.083, _api_rate_limiter['min_interval'] * 0.95)


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
    Make a request to the AllDebrid API with improved rate limiting

    Args:
        method: HTTP method (GET, POST, etc)
        endpoint: API endpoint (e.g. /v4.1/magnet/status)
        api_key: AllDebrid API key
        data: Optional data for POST requests
        files: Optional files for upload
        use_query_auth: If True, use query param auth instead of Bearer token
        **kwargs: Additional arguments for requests

    Returns:
        Response data from the API

    Raises:
        AllDebridAPIError: If the API returns an error
        AllDebridAuthError: If authentication fails
        ProviderUnavailableError: If the service is unavailable
        RateLimitError: If rate limit is exceeded
    """
    # AllDebrid supports both v4 and v4.1 endpoints
    if not endpoint.startswith('/v4'):
        endpoint = f'/v4.1{endpoint}'

    url = f"https://api.alldebrid.com{endpoint}"

    # Authentication: AllDebrid supports both Bearer token and query param
    if use_query_auth:
        # Add API key as query parameter
        params = kwargs.get('params', {})
        params['agent'] = 'cli_debrid'
        params['apikey'] = api_key
        kwargs['params'] = params
    else:
        # Use Bearer token in header (preferred)
        headers = kwargs.get('headers', {})
        headers['Authorization'] = f'Bearer {api_key}'
        kwargs['headers'] = headers

    # Add timeout if not already specified
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30  # 30 second timeout

    # Apply rate limiting (12 req/sec + 600 req/min sliding window)
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
                raise AllDebridAuthError("Access denied - check your API key or account status")
            elif response.status_code == 429:
                # AllDebrid rate limit exceeded - use Retry-After header if provided
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    try:
                        retry_after_seconds = int(retry_after)
                        logging.warning(f"AllDebrid rate limit exceeded. Server requested wait of {retry_after_seconds}s")
                        time.sleep(retry_after_seconds)
                    except (ValueError, TypeError):
                        pass  # If we can't parse, continue with default retry

                # Increase rate limiting interval when we hit 429 (cap at 5.0s)
                with _api_rate_limiter['lock']:
                    _api_rate_limiter['min_interval'] = min(5.0, _api_rate_limiter['min_interval'] * 2)
                    logging.warning(f"Increased AllDebrid API rate limit interval to {_api_rate_limiter['min_interval']}s due to 429 error")

                raise RateLimitError("AllDebrid rate limit exceeded")
            elif response.status_code == 404:
                # Some endpoints may return 404 for "not found" scenarios
                if method == 'POST' and 'magnet/upload' in endpoint:
                    return None
                response.raise_for_status()
            elif response.status_code in [503, 504]:
                raise AllDebridAPIError(f"AllDebrid service temporarily unavailable (HTTP {response.status_code})")
            else:
                response.raise_for_status()

        # Some endpoints return no content
        if response.status_code == 204:
            _decrease_rate_limit_on_success()
            return {"success": True, "status_code": 204}

        # Parse JSON response
        try:
            result = response.json()

            # Check for API-level errors in response
            if isinstance(result, dict):
                status = result.get('status')
                if status == 'error':
                    error_code = result.get('error', {}).get('code', 'UNKNOWN')
                    error_message = result.get('error', {}).get('message', 'Unknown error')

                    # Map to specific exception type if known
                    exception_class = ALLDEBRID_ERROR_CODES.get(error_code, AllDebridAPIError)
                    raise exception_class(f"AllDebrid API error {error_code}: {error_message}")

            _decrease_rate_limit_on_success()
            return result
        except ValueError:
            result = response.content
            _decrease_rate_limit_on_success()
            return result

    except api.exceptions.Timeout:
        raise ProviderUnavailableError("AllDebrid request timed out")

    except api.exceptions.RequestException as e:
        if should_retry_error(e):
            raise AllDebridAPIError(f"Temporary AllDebrid service error: {str(e)}")
        raise ProviderUnavailableError(f"AllDebrid request failed: {str(e)}")


async def get_all_magnets(api_key: str) -> Optional[List[Dict]]:
    """
    Fetches all user's magnets from AllDebrid

    Args:
        api_key: The AllDebrid API key

    Returns:
        A list of all magnet items, or None if an error occurs
    """
    try:
        logging.debug("Fetching all magnets from AllDebrid")
        result = await asyncio.to_thread(
            make_request, 'GET', '/v4.1/magnet/status', api_key
        )

        if result and isinstance(result, dict):
            # AllDebrid returns magnets in data.magnets array
            data = result.get('data', {})
            magnets = data.get('magnets', [])
            logging.debug(f"Retrieved {len(magnets)} magnets from AllDebrid")
            return magnets

        return []

    except Exception as e:
        logging.error(f"Error fetching AllDebrid magnets: {str(e)}")
        return None
