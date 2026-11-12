"""HighWay transfer API client.

HighWay (https://high-way.me) exposes torrents through a bearer-token transfer API,
documented at https://cloud.high-way.me/docs/openapi/highway-transfers.yaml:

- GET  ?stats=1                              account, plan and transfer slots
- GET  ?service=torrent&check=<h1>,<h2>      cache check, up to 100 hashes
- GET  ?service=torrent                      list of the account's torrents
- GET  ?id=<id>&service=torrent              one torrent with its files
- POST magnet=<magnet> or file=<.torrent>    add (known content is linked at once)
- DELETE ?id=<id>&service=torrent            remove from the account
"""

import threading
import time
from typing import Any, Dict, List, Optional

from ..base import ProviderUnavailableError, RateLimitError
from .exceptions import HighwayAPIError, HighwayAuthError
from utilities.settings import get_setting
from routes.api_tracker import api

API_URL = "https://cloud.high-way.me/center-api/transfers.php"
CHECK_BATCH = 100

_rate_lock = threading.Lock()
_last_request = [0.0]
_MIN_INTERVAL = 0.2


def _wait_for_rate_limit() -> None:
    with _rate_lock:
        wait = _MIN_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def get_api_key() -> str:
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise HighwayAuthError("No API key found in settings. Please configure in settings.")
    return api_key


def make_request(method: str, api_key: str, params: Optional[Dict] = None, data: Optional[Dict] = None,
                 files: Optional[Dict] = None, timeout: int = 30) -> Any:
    """Call the transfer API and return the decoded JSON body."""
    _wait_for_rate_limit()
    headers = {'Authorization': f'Bearer {api_key}'}
    try:
        if method == 'GET':
            response = api.get(API_URL, params=params, headers=headers, timeout=timeout)
        elif method == 'POST':
            response = api.post(API_URL, params=params, data=data, files=files, headers=headers, timeout=timeout)
        elif method == 'DELETE':
            response = api.delete(API_URL, params=params, headers=headers, timeout=timeout)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("HighWay request timed out")
    except api.exceptions.RequestException as e:
        raise ProviderUnavailableError(f"HighWay request failed: {str(e)}")

    try:
        body = response.json()
    except ValueError:
        body = None

    if response.status_code >= 400:
        code = body.get('error', '') if isinstance(body, dict) else ''
        message = body.get('message', '') if isinstance(body, dict) else response.text[:200]
        if response.status_code in (401, 403):
            raise HighwayAuthError(f"HighWay {response.status_code} {code}: {message}")
        if response.status_code == 429:
            raise RateLimitError("HighWay rate limit exceeded (429)")
        if response.status_code >= 500:
            raise ProviderUnavailableError(f"HighWay service temporarily unavailable (HTTP {response.status_code})")
        raise HighwayAPIError(f"HighWay request failed: {response.status_code} {code}: {message}")

    if not isinstance(body, dict):
        raise HighwayAPIError(f"Unexpected HighWay response (HTTP {response.status_code})")
    return body


def get_stats(api_key: str) -> Dict:
    return make_request('GET', api_key, params={'stats': '1'})


def check_cached(api_key: str, hashes: List[str]) -> Dict[str, Dict]:
    """Cache state per lower-case hash: {'known', 'ready', 'id', 'name', 'size', 'progress'}."""
    results: Dict[str, Dict] = {}
    for start in range(0, len(hashes), CHECK_BATCH):
        batch = hashes[start:start + CHECK_BATCH]
        body = make_request('GET', api_key, params={'service': 'torrent', 'check': ','.join(batch)})
        for hash_value, result in (body.get('results') or {}).items():
            results[hash_value.lower()] = result
    return results


def get_all_torrents(api_key: str) -> List[Dict]:
    body = make_request('GET', api_key, params={'service': 'torrent'})
    return [t for t in (body.get('transfers') or []) if t.get('service', 'torrent') == 'torrent']


def get_torrent(api_key: str, transfer_id: str) -> Dict:
    return make_request('GET', api_key, params={'id': transfer_id, 'service': 'torrent'})


def add_magnet(api_key: str, magnet: str) -> Dict:
    return make_request('POST', api_key, data={'magnet': magnet})


def add_torrent_file(api_key: str, filename: str, content: bytes) -> Dict:
    return make_request('POST', api_key, files={'file': (filename, content, 'application/x-bittorrent')})


def delete_torrent(api_key: str, transfer_id: str) -> Dict:
    return make_request('DELETE', api_key, params={'id': transfer_id, 'service': 'torrent'})
