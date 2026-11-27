"""Torbox API client implementation."""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from urllib3.util import Timeout

from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import TorboxAPIError, TorboxAuthError
from utilities.settings import get_setting
from routes.api_tracker import api


_api_rate_limiter = {
    'last_request_time': 0.0,
    'min_interval': 0.25,
    'lock': threading.Lock(),
}

_create_limit = {
    'paused_until': 0.0,
    'lock': threading.Lock(),
}


def _create_pause_remaining() -> float:
    with _create_limit['lock']:
        return max(0.0, _create_limit['paused_until'] - time.time())


def _pause_creates(response=None) -> float:
    """Pause create calls locally after TorBox rejects the add quota.

    createtorrent is non-idempotent and has a separate uncached-add quota.
    Retrying it can duplicate work and turns one rejected grab into several API
    calls, so later adds fail locally until Retry-After (or five minutes).
    """
    wait = 300
    if response is not None:
        try:
            wait = int(response.headers.get('Retry-After') or wait)
        except (TypeError, ValueError):
            wait = 300
    wait = min(max(wait, 1), 3600)
    with _create_limit['lock']:
        _create_limit['paused_until'] = max(
            _create_limit['paused_until'], time.time() + wait)
        return _create_limit['paused_until']


def _acquire_rate_limit_lock(deadline: Optional[float]) -> bool:
    lock = _api_rate_limiter['lock']
    if deadline is None:
        return lock.acquire()
    return lock.acquire(timeout=max(0, deadline - time.monotonic()))


def _wait_for_rate_limit(deadline: Optional[float] = None) -> None:
    if not _acquire_rate_limit_lock(deadline):
        raise ProviderUnavailableError("Torbox request timed out waiting for rate limit")
    try:
        delay = _api_rate_limiter['min_interval'] - (time.time() - _api_rate_limiter['last_request_time'])
        if deadline is not None and max(0, delay) >= deadline - time.monotonic():
            raise ProviderUnavailableError("Torbox request timed out waiting for rate limit")
        if delay > 0:
            time.sleep(delay)
        _api_rate_limiter['last_request_time'] = time.time()
    finally:
        _api_rate_limiter['lock'].release()


def _decrease_rate_limit_on_success(deadline: Optional[float]) -> None:
    if _acquire_rate_limit_lock(deadline):
        try:
            if _api_rate_limiter['min_interval'] > 0.25:
                _api_rate_limiter['min_interval'] = max(0.25, _api_rate_limiter['min_interval'] * 0.95)
        finally:
            _api_rate_limiter['lock'].release()


def _raise_rate_limit(response, deadline: Optional[float], honor_retry_after: bool) -> None:
    if _acquire_rate_limit_lock(deadline):
        try:
            _api_rate_limiter['min_interval'] = min(5.0, _api_rate_limiter['min_interval'] * 2)
        finally:
            _api_rate_limiter['lock'].release()
    if honor_retry_after:
        try:
            delay = max(0, int(response.headers.get('Retry-After') or 0))
        except (TypeError, ValueError):
            delay = 0
        if deadline is not None and delay >= deadline - time.monotonic():
            raise ProviderUnavailableError("Torbox Retry-After exceeds the request budget")
        if delay:
            time.sleep(delay)
    raise RateLimitError("Torbox rate limit exceeded")


def get_api_key() -> str:
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise TorboxAuthError("No API key found in settings. Please configure in settings.")
    return api_key


def should_retry_error(exception: Exception) -> bool:
    if isinstance(exception, api.exceptions.HTTPError):
        return exception.response.status_code in [429, 500, 502, 503, 504]
    return isinstance(exception, (api.exceptions.Timeout, api.exceptions.ConnectionError))


def make_request_once(
    method: str,
    endpoint: str,
    api_key: str,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    json_data: Optional[Dict] = None,
    *,
    deadline: Optional[float] = None,
    honor_retry_after: bool = True,
    **kwargs,
) -> Any:
    url = f"https://api.torbox.app/v1/api{endpoint}"
    is_create = endpoint.rstrip('/').endswith('/torrents/createtorrent')
    if is_create:
        remaining = _create_pause_remaining()
        if remaining > 0:
            raise ProviderUnavailableError(
                f"Torbox torrent adds paused for another {int(remaining) + 1}s after rate limiting")
        # TorBox's allow_zip defaults to true: a torrent with 100+ files
        # (big anime / complete-series packs) is then stored as one .zip,
        # which can't be symlinked or streamed per episode. Every add path
        # (magnet, .torrent upload, URL) goes through here.
        data = dict(data or {})
        data.setdefault('allow_zip', 'false')
    headers = kwargs.get('headers', {})
    headers['Authorization'] = f'Bearer {api_key}'
    kwargs['headers'] = headers

    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30

    if deadline is None:
        _wait_for_rate_limit()
    else:
        _wait_for_rate_limit(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUnavailableError("Torbox request timed out")
        kwargs['timeout'] = Timeout(total=remaining)

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
                if is_create:
                    until = _pause_creates(response)
                    raise ProviderUnavailableError(
                        f"Torbox torrent add limit reached; adds paused until {time.ctime(until)}")
                _raise_rate_limit(response, deadline, honor_retry_after)
            if response.status_code in [500, 502, 503, 504]:
                if is_create:
                    # A create may have reached TorBox even when its response
                    # failed. Do not replay this non-idempotent request.
                    raise ProviderUnavailableError(
                        f"Torbox create request failed (HTTP {response.status_code}); not retrying")
                raise TorboxAPIError(f"Torbox service temporarily unavailable (HTTP {response.status_code})")
            try:
                err_body = response.json()
            except Exception:
                err_body = response.text[:500]
            # TorBox sometimes reports the create quota as HTTP 400. Pause
            # locally; retrying the same add only spends more quota.
            if is_create and response.status_code == 400 and isinstance(err_body, dict):
                err_code = (err_body.get('error') or '').upper()
                if 'RATE_LIMIT' in err_code or 'TOO_MANY' in err_code:
                    with _api_rate_limiter['lock']:
                        _api_rate_limiter['min_interval'] = min(30.0, _api_rate_limiter['min_interval'] * 2)
                    until = _pause_creates(response)
                    logging.warning(f"Torbox 400 rate limit on {endpoint}; adds paused until {time.ctime(until)}")
                    raise ProviderUnavailableError(
                        f"Torbox torrent add limit reached: {err_body.get('detail', err_code)}")
            logging.error(f"Torbox {response.status_code} on {endpoint}: {err_body}")
            raise ProviderUnavailableError(f"Torbox request failed: {response.status_code} - {err_body}")

        if response.status_code == 204:
            _decrease_rate_limit_on_success(deadline)
            return {"success": True, "status_code": 204}

        try:
            result = response.json()
        except ValueError:
            _decrease_rate_limit_on_success(deadline)
            return response.content

        if isinstance(result, dict) and result.get('success') is False:
            error_code = result.get('error') or 'UNKNOWN'
            detail = result.get('detail') or 'Unknown error'
            if error_code in {'BAD_TOKEN', 'AUTH_ERROR'}:
                raise TorboxAuthError(detail)
            if is_create and any(marker in str(error_code).upper() for marker in ('RATE_LIMIT', 'TOO_MANY')):
                until = _pause_creates(response)
                raise ProviderUnavailableError(
                    f"Torbox torrent add limit reached; adds paused until {time.ctime(until)}")
            raise TorboxAPIError(f"Torbox API error {error_code}: {detail}")

        _decrease_rate_limit_on_success(deadline)
        return result
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("Torbox request timed out")
    except api.exceptions.RequestException as e:
        # APITracker raises on HTTP errors before returning a response.
        if (deadline is not None and isinstance(e, api.exceptions.HTTPError)
                and e.response is not None and e.response.status_code == 429):
            _raise_rate_limit(e.response, deadline, honor_retry_after)
        if should_retry_error(e):
            raise TorboxAPIError(f"Temporary Torbox service error: {str(e)}")
        raise ProviderUnavailableError(f"Torbox request failed: {str(e)}")


_stop_after_three_attempts = stop_after_attempt(3)


def _stop_request_retry(state) -> bool:
    deadline = state.kwargs.get('deadline')
    return _stop_after_three_attempts(state) or (
        deadline is not None and time.monotonic() + state.upcoming_sleep >= deadline
    )


make_request = retry(
    stop=_stop_request_retry,
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((api.exceptions.RequestException, TorboxAPIError, RateLimitError, api.exceptions.HTTPError)),
    retry_error_callback=lambda rs: rs.outcome.result(),
)(make_request_once)


def get_data_payload(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get('data')
    return result


MYLIST_PAGE = 1000  # TorBox's mylist answers at most this many per call
MYLIST_MAX_PAGES = 100


def get_all_torrents(api_key: str) -> List[Dict]:
    """Every torrent on the account. mylist returns up to 1000 per call, so
    larger accounts are read page by page (offset/limit)."""
    out: List[Dict] = []
    seen = set()
    for page in range(MYLIST_MAX_PAGES):
        result = make_request('GET', '/torrents/mylist', api_key,
                              params={'bypass_cache': 'true', 'offset': page * MYLIST_PAGE, 'limit': MYLIST_PAGE})
        data = get_data_payload(result)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            break
        new = [t for t in data if not isinstance(t, dict) or t.get('id') not in seen]
        if not new:
            break  # offset ignored: the same page again
        seen.update(t.get('id') for t in new if isinstance(t, dict))
        out.extend(new)
        if len(data) < MYLIST_PAGE:
            break
    return out
