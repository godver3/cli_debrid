"""Debrid-Link API v2 low-level client."""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import DebridLinkAPIError, DebridLinkAuthError
from utilities.settings import get_setting
from routes.api_tracker import api

BASE_URL = 'https://debrid-link.com/api/v2'

_rate_limiter = {
    'last_request_time': 0.0,
    'min_interval': 0.25,
    'lock': threading.Lock(),
}


def _wait_for_rate_limit() -> None:
    with _rate_limiter['lock']:
        elapsed = time.time() - _rate_limiter['last_request_time']
        if elapsed < _rate_limiter['min_interval']:
            time.sleep(_rate_limiter['min_interval'] - elapsed)
        _rate_limiter['last_request_time'] = time.time()


def get_api_key() -> str:
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise DebridLinkAuthError("No API key configured for Debrid-Link.")
    return api_key


def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    params: Optional[Dict] = None,
    json_data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    form_data: Optional[Dict] = None,
    **kwargs,
) -> Any:
    """
    Make a request to the Debrid-Link API v2.

    Auth: Authorization: Bearer <api_key>
    Success: {"success": true, "value": <data>}
    Error:   {"error": "<code>", "error_description": "<text>"}
    """
    url = f"{BASE_URL}{endpoint}"
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = f'Bearer {api_key}'

    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30

    _wait_for_rate_limit()

    try:
        if method.upper() == 'GET':
            response = api.get(url, params=params, headers=headers, **kwargs)
        elif method.upper() == 'POST':
            if files:
                response = api.post(url, params=params, files=files, headers=headers, **kwargs)
            elif form_data is not None:
                response = api.post(url, params=params, data=form_data, headers=headers, **kwargs)
            else:
                response = api.post(url, params=params, json=json_data, headers=headers, **kwargs)
        elif method.upper() == 'DELETE':
            response = api.delete(url, params=params, headers=headers, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if response.status_code == 401:
            raise DebridLinkAuthError("Invalid or expired Debrid-Link API key")
        if response.status_code == 403:
            raise DebridLinkAuthError("Access denied — check your Debrid-Link API key")
        if response.status_code == 429:
            with _rate_limiter['lock']:
                _rate_limiter['min_interval'] = min(5.0, _rate_limiter['min_interval'] * 2)
            raise RateLimitError("Debrid-Link rate limit exceeded")
        if response.status_code >= 500:
            raise DebridLinkAPIError(f"Debrid-Link server error (HTTP {response.status_code})")

        try:
            result = response.json()
        except ValueError:
            raise DebridLinkAPIError(f"Non-JSON response from Debrid-Link (HTTP {response.status_code})")

        if isinstance(result, dict) and not result.get('success', True):
            error_code = result.get('error', 'UNKNOWN')
            desc = result.get('error_description', 'Unknown error')
            if error_code in ('badToken', 'unauthorized'):
                raise DebridLinkAuthError(desc)
            raise DebridLinkAPIError(f"Debrid-Link API error [{error_code}]: {desc}")

        # Decay rate limit on success
        with _rate_limiter['lock']:
            if _rate_limiter['min_interval'] > 0.25:
                _rate_limiter['min_interval'] = max(0.25, _rate_limiter['min_interval'] * 0.95)

        return result

    except (DebridLinkAPIError, DebridLinkAuthError, RateLimitError):
        raise
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("Debrid-Link request timed out")
    except api.exceptions.HTTPError as e:
        # Log the response body so we can see the actual API error reason
        body = ''
        try:
            body = e.response.text[:500] if e.response is not None else ''
        except Exception:
            pass
        logging.error(f"Debrid-Link HTTP error {e} — response body: {body}")
        raise ProviderUnavailableError(f"Debrid-Link request failed: {e}")
    except api.exceptions.RequestException as e:
        raise ProviderUnavailableError(f"Debrid-Link request failed: {e}")


def get_all_torrents(api_key: str) -> List[Dict]:
    """Fetch all seedbox torrents, handling pagination."""
    torrents = []
    page = 0
    per_page = 100
    while True:
        result = make_request('GET', '/seedbox/list', api_key,
                              params={'page': page, 'perPage': per_page})
        value = result.get('value', []) if isinstance(result, dict) else []
        if not isinstance(value, list):
            break
        torrents.extend(value)
        pagination = result.get('pagination', {})
        next_page = pagination.get('next', -1)
        if next_page == -1 or len(value) < per_page:
            break
        page = next_page
    return torrents
