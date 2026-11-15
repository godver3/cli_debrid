"""Simkl (simkl.com) sign-in and the Simkl Watchlist content source.

Sign-in uses Simkl's AUTH V2 device flow (AUTH V1 / PIN is being retired around
April 2027): POST /oauth2/device gives a user code the user enters at
simkl.com/pin; POST /oauth2/token is polled until approved. Access tokens last
7 days and are refreshed with the (sliding, 180-day) refresh token.

The watchlist comes from GET /sync/all-items/{movies|shows|anime}/{status}.
Simkl asks clients not to re-download lists on a timer: each run first reads
/sync/activities and reuses the last result when nothing changed.
"""
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from routes.api_tracker import api
from utilities.settings import get_setting
from content_checkers.tracker_common import gate_and_merge, resolve_imdb

API = 'https://api.simkl.com'
REQUEST_TIMEOUT = 30
APP_NAME = 'cli-debrid'
STATUS_KEYS = {'Plan to Watch': 'plantowatch', 'Watching': 'watching', 'On Hold': 'hold',
               'Completed': 'completed', 'Dropped': 'dropped'}
MOVIE_STATUSES = {'plantowatch', 'completed', 'dropped'}
REFRESH_MARGIN = 24 * 3600

_lock = threading.Lock()
_device: Dict[str, Any] = {}     # pending device sign-in (device_code never leaves the server)


def _config_dir() -> str:
    return os.environ.get('USER_CONFIG', '/user/config')


def _auth_path() -> str:
    return os.path.join(_config_dir(), '.simkl_auth.json')


def _cache_path() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'simkl_watchlist_cache.json')


def _app_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'version.txt')) as f:
            return f.read().strip() or '1'
    except OSError:
        return '1'


def _client() -> Tuple[str, str]:
    return ((get_setting('Simkl', 'client_id', '') or '').strip(),
            (get_setting('Simkl', 'client_secret', '') or '').strip())


def _common_params(client_id: str) -> Dict[str, str]:
    return {'client_id': client_id, 'app-name': APP_NAME, 'app-version': _app_version()}


def _headers(token: Optional[str] = None) -> Dict[str, str]:
    h = {'User-Agent': f'cli_debrid/{_app_version()}', 'Accept': 'application/json'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _save_tokens(payload: Dict[str, Any]) -> None:
    auth = _read_json(_auth_path())
    auth['access_token'] = payload['access_token']
    if payload.get('refresh_token'):
        auth['refresh_token'] = payload['refresh_token']
    auth['expires_at'] = int(time.time()) + int(payload.get('expires_in') or 7 * 24 * 3600)
    _write_json(_auth_path(), auth)


def _token_post(data: Dict[str, str]):
    client_id, secret = _client()
    body = dict(data, client_id=client_id)
    if secret:
        body['client_secret'] = secret
    return api.post(f'{API}/oauth2/token', data=body, headers=_headers(), timeout=REQUEST_TIMEOUT)


# ---- sign-in -------------------------------------------------------------------

def start_device_auth() -> Dict[str, Any]:
    client_id, _ = _client()
    if not client_id:
        return {'success': False, 'error': 'Enter your Simkl Client ID first and save.'}
    try:
        r = api.post(f'{API}/oauth2/device', data={'client_id': client_id, 'scope': 'media:read'},
                     headers=_headers(), timeout=REQUEST_TIMEOUT)
        d = r.json() if r.content else {}
    except Exception as e:
        return {'success': False, 'error': f'Could not reach Simkl: {e}'}
    if r.status_code != 200 or not d.get('device_code'):
        return {'success': False, 'error': d.get('message') or d.get('error') or f'HTTP {r.status_code}'}
    with _lock:
        _device.clear()
        _device.update(device_code=d['device_code'], interval=int(d.get('interval') or 5),
                       expires_at=time.time() + int(d.get('expires_in') or 900), next_poll=0.0)
    return {'success': True, 'user_code': d.get('user_code'),
            'verification_uri': d.get('verification_uri_complete') or d.get('verification_uri') or 'https://simkl.com/pin',
            'interval': int(d.get('interval') or 5), 'expires_in': int(d.get('expires_in') or 900)}


def poll_device_auth() -> Dict[str, Any]:
    """One poll; returns status 'pending' | 'approved' | 'expired' | 'error'."""
    with _lock:
        if not _device:
            return {'status': 'error', 'error': 'No sign-in in progress'}
        if time.time() > _device['expires_at']:
            _device.clear()
            return {'status': 'expired'}
        if time.time() < _device['next_poll']:
            return {'status': 'pending'}
        device_code = _device['device_code']
        _device['next_poll'] = time.time() + _device['interval']
    try:
        r = _token_post({'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device_code})
        d = r.json() if r.content else {}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}
    if r.status_code == 200 and d.get('access_token'):
        _save_tokens(d)
        with _lock:
            _device.clear()
        _cache_clear()
        return {'status': 'approved'}
    err = d.get('error')
    if err == 'authorization_pending':
        return {'status': 'pending'}
    if err == 'slow_down':
        with _lock:
            if _device:
                _device['interval'] += 5
                _device['next_poll'] = time.time() + _device['interval']
        return {'status': 'pending'}
    if err == 'expired_token':
        with _lock:
            _device.clear()
        return {'status': 'expired'}
    return {'status': 'error', 'error': d.get('message') or err or f'HTTP {r.status_code}'}


def auth_status() -> Dict[str, Any]:
    auth = _read_json(_auth_path())
    return {'signed_in': bool(auth.get('access_token')), 'expires_at': auth.get('expires_at'),
            'client_id_set': bool(_client()[0])}


def sign_out() -> None:
    auth = _read_json(_auth_path())
    client_id, _ = _client()
    token = auth.get('refresh_token') or auth.get('access_token')
    if token and client_id:
        try:
            api.post(f'{API}/oauth2/revoke', data={'client_id': client_id, 'token': token},
                     headers=_headers(), timeout=REQUEST_TIMEOUT)
        except Exception as e:
            logging.debug(f"Simkl revoke failed: {e}")
    try:
        os.remove(_auth_path())
    except OSError:
        pass
    _cache_clear()


def _access_token() -> Optional[str]:
    """A valid access token, refreshed when it expires within a day."""
    with _lock:
        auth = _read_json(_auth_path())
        token = auth.get('access_token')
        if not token:
            return None
        if auth.get('refresh_token') and int(auth.get('expires_at') or 0) - time.time() < REFRESH_MARGIN:
            try:
                r = _token_post({'grant_type': 'refresh_token', 'refresh_token': auth['refresh_token']})
                d = r.json() if r.content else {}
                if r.status_code == 200 and d.get('access_token'):
                    _save_tokens(d)
                    token = d['access_token']
                    logging.info("Simkl access token refreshed")
                else:
                    logging.error(f"Simkl token refresh failed: {d.get('error') or r.status_code}; sign in again")
            except Exception as e:
                logging.error(f"Simkl token refresh failed: {e}")
        return token


# ---- data ----------------------------------------------------------------------

def _get(path: str, token: str, params=None) -> Optional[Any]:
    client_id, _ = _client()
    try:
        r = api.get(f'{API}{path}', params=dict(_common_params(client_id), **(params or {})),
                    headers=_headers(token), timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            logging.error("Simkl rejected the sign-in (401); sign in again under Additional Settings")
            return None
        r.raise_for_status()
        return r.json() if r.content else {}
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching from Simkl ({path}): {e}")
        return None
    except ValueError as e:
        logging.error(f"Simkl returned invalid JSON for {path}: {e}")
        return None


def _cache_clear() -> None:
    try:
        os.remove(_cache_path())
    except OSError:
        pass


def _entries(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows of a /sync/all-items response -> {'imdb_id', 'media_type'} entries."""
    out = []
    for bucket in ('movies', 'shows', 'anime'):
        for row in (data or {}).get(bucket) or []:
            media = row.get('movie') or row.get('show') or row.get('anime') or {}
            ids = media.get('ids') or row.get('ids') or {}
            if bucket == 'movies':
                media_type = 'movie'
            elif bucket == 'anime':
                anime_type = (row.get('anime_type') or media.get('anime_type') or '').lower()
                media_type = 'movie' if anime_type == 'movie' else 'tv'
            else:
                media_type = 'tv'
            imdb_id = resolve_imdb(ids, media_type)
            if not imdb_id:
                logging.warning(f"Skipping Simkl item without IMDb/TMDB/TVDB id: {media.get('title') or '?'} "
                                f"(simkl {ids.get('simkl')})")
                continue
            out.append({'imdb_id': imdb_id, 'media_type': media_type, 'season': None})
    return out


def get_wanted_from_simkl_watchlist(source_config: Dict[str, Any], versions: Dict[str, bool],
                                    unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    token = _access_token()
    if not token:
        logging.warning("Simkl Watchlist: not signed in (Additional Settings → Tracking accounts → Simkl)")
        return []
    statuses = source_config.get('simkl_statuses') or ['Plan to Watch']
    if isinstance(statuses, str):
        statuses = [s.strip() for s in statuses.split(',') if s.strip()]
    keys = [STATUS_KEYS[s] for s in statuses if s in STATUS_KEYS] or ['plantowatch']
    media_filter = (source_config.get('media_type') or 'All').lower()
    types = []
    if media_filter in ('all', 'movies'):
        types.append('movies')
    if media_filter in ('all', 'shows'):
        types.append('shows')
        if source_config.get('include_anime', True):
            types.append('anime')
    query_key = f"{','.join(sorted(types))}|{','.join(sorted(keys))}"

    activities = _get('/sync/activities', token)
    stamp = (activities or {}).get('all')
    cache = _read_json(_cache_path())
    cached = (cache.get('queries') or {}).get(query_key)
    if stamp and cached and cached.get('all') == stamp:
        entries = cached.get('entries') or []
        logging.info(f"Simkl unchanged since last run; reusing {len(entries)} entries")
    else:
        merged: Dict[str, List[Dict[str, Any]]] = {}
        for media_kind in types:
            for status in keys:
                if media_kind == 'movies' and status not in MOVIE_STATUSES:
                    continue
                data = _get(f'/sync/all-items/{media_kind}/{status}', token)
                if data is None:
                    return []
                for bucket, rows in (data or {}).items():
                    if isinstance(rows, list):
                        merged.setdefault(bucket, []).extend(rows)
        entries = _entries(merged)
        if stamp:
            cache.setdefault('queries', {})[query_key] = {'all': stamp, 'entries': entries}
            try:
                _write_json(_cache_path(), cache)
            except OSError as e:
                logging.debug(f"Could not write Simkl cache: {e}")
    items = gate_and_merge(entries, 'Simkl', unblacklist=unblacklist)
    logging.info(f"Found {len(items)} items in Simkl ({', '.join(statuses)})")
    return [(items, versions)]
