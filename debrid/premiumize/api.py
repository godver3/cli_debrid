"""Premiumize API client with rate limiting"""

import logging
import time
import threading
from typing import Optional, Dict, Any, List

from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import PremiumizeAPIError, PremiumizeAuthError
from utilities.settings import get_setting
from routes.api_tracker import api

# Premiumize does not publish explicit rate limits.
# Using a conservative 2 req/sec default.
_api_rate_limiter = {
    'last_request_time': 0,
    'min_interval': 0.5,  # 2 req/sec
    'lock': threading.Lock()
}

PREMIUMIZE_BASE_URL = 'https://www.premiumize.me/api'

# Transfer status values returned by Premiumize
TRANSFER_STATUS_MAP = {
    'waiting':    'queued',
    'queued':     'queued',
    'running':    'downloading',
    'finished':   'downloaded',
    'error':      'error',
    'deleted':    'error',
    'timeout':    'error',
    'seeding':    'downloaded',
}


def _wait_for_rate_limit():
    with _api_rate_limiter['lock']:
        current_time = time.time()
        time_since_last = current_time - _api_rate_limiter['last_request_time']
        min_interval = _api_rate_limiter['min_interval']
        if time_since_last < min_interval:
            time.sleep(min_interval - time_since_last)
        _api_rate_limiter['last_request_time'] = time.time()


def get_api_key() -> str:
    """Get Premiumize API key from settings."""
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise PremiumizeAuthError("No API key configured. Please set your Premiumize API key in settings.")
    return api_key


def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    params: Optional[Dict] = None,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    **kwargs
) -> Any:
    """
    Make a request to the Premiumize API.

    Authentication: ?apikey=X query parameter on every request.
    Base URL: https://www.premiumize.me/api

    Args:
        method:   HTTP method (GET, POST)
        endpoint: API endpoint e.g. /transfer/list
        api_key:  Premiumize API key
        params:   Additional query parameters
        data:     POST body data
        files:    File uploads
        **kwargs: Extra arguments for requests

    Returns:
        Parsed JSON response dict, or None on failure.

    Raises:
        PremiumizeAuthError:    On 401 / bad API key.
        PremiumizeAPIError:     On API-level error responses.
        ProviderUnavailableError: On network / server errors.
    """
    url = f"{PREMIUMIZE_BASE_URL}{endpoint}"

    # Inject API key as query param (Premiumize's primary auth method)
    qp = params.copy() if params else {}
    qp['apikey'] = api_key

    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30

    _wait_for_rate_limit()

    try:
        if method.upper() == 'GET':
            response = api.get(url, params=qp, **kwargs)
        elif method.upper() == 'POST':
            response = api.post(url, params=qp, data=data, files=files, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if response.status_code == 401:
            raise PremiumizeAuthError("Invalid Premiumize API key")
        if response.status_code == 403:
            raise PremiumizeAuthError("Premiumize access denied — check account status")
        if response.status_code == 429:
            with _api_rate_limiter['lock']:
                _api_rate_limiter['min_interval'] = min(5.0, _api_rate_limiter['min_interval'] * 2)
            raise RateLimitError("Premiumize rate limit exceeded")
        if response.status_code >= 500:
            raise PremiumizeAPIError(f"Premiumize server error (HTTP {response.status_code})")
        if response.status_code >= 400:
            response.raise_for_status()

        try:
            result = response.json()
        except ValueError:
            return response.content

        # Premiumize returns {"status": "error", "message": "..."} on failures
        if isinstance(result, dict) and result.get('status') == 'error':
            msg = result.get('message', 'Unknown Premiumize error')
            if 'api key' in msg.lower() or 'apikey' in msg.lower():
                raise PremiumizeAuthError(f"Premiumize auth error: {msg}")
            raise PremiumizeAPIError(f"Premiumize API error: {msg}")

        return result

    except (PremiumizeAuthError, PremiumizeAPIError, RateLimitError):
        raise
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("Premiumize request timed out")
    except api.exceptions.RequestException as e:
        raise ProviderUnavailableError(f"Premiumize request failed: {e}")
