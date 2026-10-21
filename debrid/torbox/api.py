"""Torbox API client implementation."""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import TorboxAPIError, TorboxAuthError
from utilities.settings import get_setting
from routes.api_tracker import api


_api_rate_limiter = {
    'last_request_time': 0.0,
    'min_interval': 0.25,
    'lock': threading.Lock(),
}


def _wait_for_rate_limit() -> None:
    with _api_rate_limiter['lock']:
        current_time = time.time()
        time_since_last = current_time - _api_rate_limiter['last_request_time']
        min_interval = _api_rate_limiter['min_interval']
        if time_since_last < min_interval:
            time.sleep(min_interval - time_since_last)
        _api_rate_limiter['last_request_time'] = time.time()


def _decrease_rate_limit_on_success() -> None:
    with _api_rate_limiter['lock']:
        if _api_rate_limiter['min_interval'] > 0.25:
            _api_rate_limiter['min_interval'] = max(0.25, _api_rate_limiter['min_interval'] * 0.95)


def get_api_key() -> str:
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise TorboxAuthError("No API key found in settings. Please configure in settings.")
    return api_key


def should_retry_error(exception: Exception) -> bool:
    if isinstance(exception, api.exceptions.HTTPError):
        return exception.response.status_code in [429, 500, 502, 503, 504]
    return isinstance(exception, (api.exceptions.Timeout, api.exceptions.ConnectionError))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((api.exceptions.RequestException, TorboxAPIError, RateLimitError, api.exceptions.HTTPError)),
    retry_error_callback=lambda rs: rs.outcome.result(),
)
def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    json_data: Optional[Dict] = None,
    **kwargs,
) -> Any:
    url = f"https://api.torbox.app/v1/api{endpoint}"
    headers = kwargs.get('headers', {})
    headers['Authorization'] = f'Bearer {api_key}'
    kwargs['headers'] = headers

    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30

    _wait_for_rate_limit()

    try:
        if method.upper() == 'GET':
            response = api.get(url, **kwargs)
        elif method.upper() == 'POST':
            response = api.post(url, data=data, files=files, json=json_data, **kwargs)
        elif method.upper() == 'DELETE':
            response = api.delete(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if response.status_code >= 400:
            if response.status_code == 401:
                raise TorboxAuthError("Invalid API key")
            if response.status_code == 403:
                raise TorboxAuthError("Access denied")
            if response.status_code == 429:
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    try:
                        time.sleep(int(retry_after))
                    except (TypeError, ValueError):
                        pass
                with _api_rate_limiter['lock']:
                    _api_rate_limiter['min_interval'] = min(5.0, _api_rate_limiter['min_interval'] * 2)
                raise RateLimitError("Torbox rate limit exceeded")
            if response.status_code in [500, 502, 503, 504]:
                raise TorboxAPIError(f"Torbox service temporarily unavailable (HTTP {response.status_code})")
            try:
                err_body = response.json()
            except Exception:
                err_body = response.text[:500]
            # Torbox returns 400 for rate limiting on createtorrent — treat as retryable
            if response.status_code == 400 and isinstance(err_body, dict):
                err_code = (err_body.get('error') or '').upper()
                if 'RATE_LIMIT' in err_code or 'TOO_MANY' in err_code:
                    with _api_rate_limiter['lock']:
                        _api_rate_limiter['min_interval'] = min(30.0, _api_rate_limiter['min_interval'] * 2)
                    logging.warning(f"Torbox 400 rate limit on {endpoint}: sleeping 30s")
                    time.sleep(30)
                    raise RateLimitError(f"Torbox rate limit (400): {err_body.get('detail', err_code)}")
            logging.error(f"Torbox {response.status_code} on {endpoint}: {err_body}")
            raise ProviderUnavailableError(f"Torbox request failed: {response.status_code} - {err_body}")

        if response.status_code == 204:
            _decrease_rate_limit_on_success()
            return {"success": True, "status_code": 204}

        try:
            result = response.json()
        except ValueError:
            _decrease_rate_limit_on_success()
            return response.content

        if isinstance(result, dict) and result.get('success') is False:
            error_code = result.get('error') or 'UNKNOWN'
            detail = result.get('detail') or 'Unknown error'
            if error_code in {'BAD_TOKEN', 'AUTH_ERROR'}:
                raise TorboxAuthError(detail)
            raise TorboxAPIError(f"Torbox API error {error_code}: {detail}")

        _decrease_rate_limit_on_success()
        return result
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("Torbox request timed out")
    except api.exceptions.RequestException as e:
        if should_retry_error(e):
            raise TorboxAPIError(f"Temporary Torbox service error: {str(e)}")
        raise ProviderUnavailableError(f"Torbox request failed: {str(e)}")


def get_data_payload(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get('data')
    return result


def get_all_torrents(api_key: str) -> List[Dict]:
    result = make_request('GET', '/torrents/mylist', api_key, params={'bypass_cache': 'true'})
    data = get_data_payload(result)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []
