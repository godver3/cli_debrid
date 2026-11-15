"""Per-indexer daily API-hit and grab limits for Newznab indexers.

Indexers cap API hits (searches) and grabs (NZB downloads) per day, and none of
the ones tested report usage in their responses, so cli_debrid counts its own
calls. Each Newznab scraper can set:

  api_limit_per_day   searches allowed per day, 0 = no limit
  grab_limit_per_day  NZB downloads allowed per day, 0 = no limit
  limit_reset         when the day starts over: 'midnight_local' (this server's
                      midnight), 'midnight_utc', or 'rolling_24h' (the last 24
                      hours, never over the limit whatever the indexer uses)

Calls are recorded as timestamps (kept 49 h) in <db_content>/indexer_usage.json
so the count survives a restart. Calls made outside cli_debrid (another app on
the same API key) aren't seen; set the limit a little under the real one.
"""
import atexit
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from urllib.parse import urlparse

from utilities.settings import get_setting

DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
USAGE_FILE = os.path.join(DB_CONTENT_DIR, 'indexer_usage.json')

KINDS = ('api', 'grab')
RESET_MODES = ('midnight_local', 'midnight_utc', 'rolling_24h')
_KEEP_SECONDS = 49 * 3600
_SAVE_EVERY = 30.0

_lock = threading.RLock()
_usage: Optional[Dict[str, Dict[str, list]]] = None
_last_save = 0.0
_dirty = False
_warned: Dict[tuple, float] = {}
# NZB URL -> when its grab was counted. One release is often fetched more than
# once in a row (a pre-fetch that isn't usable, then cli_mount fetching the same
# URL on submit); count it once.
_recent_grabs: Dict[str, float] = {}
_GRAB_DEDUPE_SECONDS = 600


def _load() -> Dict[str, Dict[str, list]]:
    global _usage
    if _usage is None:
        try:
            with open(USAGE_FILE) as f:
                raw = json.load(f)
            _usage = {name: {k: [float(t) for t in (v.get(k) or [])] for k in KINDS}
                      for name, v in (raw or {}).items() if isinstance(v, dict)}
        except FileNotFoundError:
            _usage = {}
        except Exception as e:
            logging.warning(f'[IndexerQuota] Could not read {USAGE_FILE}: {e}; starting from zero')
            _usage = {}
    return _usage


def _save(force: bool = False) -> None:
    global _last_save, _dirty
    if not _dirty or (not force and time.time() - _last_save < _SAVE_EVERY):
        return
    try:
        tmp = USAGE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_usage, f)
        os.replace(tmp, USAGE_FILE)
        _last_save = time.time()
        _dirty = False
    except Exception as e:
        logging.debug(f'[IndexerQuota] Could not save usage: {e}')


def flush() -> None:
    with _lock:
        _save(force=True)


def _newznab_scrapers() -> Dict[str, dict]:
    scrapers = get_setting('Scrapers') or {}
    return {name: cfg for name, cfg in scrapers.items()
            if isinstance(cfg, dict) and cfg.get('type') == 'Newznab'}


def _limit(cfg: dict, kind: str) -> int:
    try:
        return max(0, int(cfg.get(f'{kind}_limit_per_day') or 0))
    except (TypeError, ValueError):
        return 0


def _reset_mode(cfg: dict) -> str:
    mode = (cfg.get('limit_reset') or 'midnight_local').strip()
    return mode if mode in RESET_MODES else 'midnight_local'


def window_start(mode: str, now: Optional[float] = None) -> float:
    """Epoch seconds where the current counting day began."""
    now = time.time() if now is None else now
    if mode == 'rolling_24h':
        return now - 86400
    if mode == 'midnight_utc':
        d = datetime.fromtimestamp(now, tz=timezone.utc)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def resets_at(mode: str, now: Optional[float] = None) -> float:
    """Epoch seconds when the count next drops (for rolling: when the oldest call ages out)."""
    now = time.time() if now is None else now
    if mode == 'rolling_24h':
        return now  # caller uses the oldest timestamp instead
    start = window_start(mode, now)
    if mode == 'midnight_utc':
        return start + 86400
    return (datetime.fromtimestamp(start) + timedelta(days=1)).timestamp()


_host_map_cache = (0.0, [])   # (built at, [(host, port, path, scraper name)])


def _split_url(url: str):
    """(host without api./www., effective port, path without trailing '/') or None."""
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower()
    if not host:
        return None
    for prefix in ('api.', 'www.'):
        if host.startswith(prefix):
            host = host[len(prefix):]
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if (parsed.scheme or '').lower() == 'https' else 80
    return host, port, (parsed.path or '').rstrip('/')


def _host_map() -> list:
    global _host_map_cache
    built, entries = _host_map_cache
    if time.time() - built < 60:
        return entries
    entries = []
    for name, cfg in _newznab_scrapers().items():
        try:
            parts = _split_url(cfg.get('url') or '')
        except Exception:
            parts = None
        if parts:
            entries.append((*parts, name))
    _host_map_cache = (time.time(), entries)
    return entries


def instance_for_url(url: str) -> Optional[str]:
    """The configured Newznab scraper an NZB / API URL belongs to.

    Matched by host (or a subdomain of it); when several scrapers share the
    host, by port too. When several still share a host and port (Prowlarr/Hydra serving many indexers), the one whose URL
    path is the longest prefix of this URL's path wins; if none or several tie,
    the URL is not attributed rather than charged to the wrong indexer.
    """
    try:
        parts = _split_url(url)
    except Exception:
        return None
    if not parts:
        return None
    host, port, path = parts
    on_host = [(pt, p, name) for h, pt, p, name in _host_map()
               if host == h or host.endswith('.' + h)]
    if len(on_host) <= 1:
        # One scraper on this host: it's that one whatever the scheme/port
        # (indexers often return https links for an http-configured URL).
        return on_host[0][2] if on_host else None
    cands = [(p, name) for pt, p, name in on_host if pt == port]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][1]
    matched = [(len(p), name) for p, name in cands
               if p and (path == p or path.startswith(p + '/'))]
    if matched:
        matched.sort(reverse=True)
        if len(matched) == 1 or matched[0][0] != matched[1][0]:
            return matched[0][1]
        return None
    bare = [name for p, name in cands if not p]
    return bare[0] if len(bare) == 1 else None


def request_kind(url: str, params=None) -> str:
    """'grab' for an NZB download, 'api' for any other request to an indexer's API."""
    t = ''
    if isinstance(params, dict):
        t = str(params.get('t') or '').lower()
    try:
        parsed = urlparse(url)
        if not t:
            from urllib.parse import parse_qs
            t = (parse_qs(parsed.query).get('t') or [''])[0].lower()
        path = (parsed.path or '').lower()
    except Exception:
        path = ''
    if t == 'get' or 'getnzb' in path or path.endswith('.nzb') or '/download' in path:
        return 'grab'
    return 'api'


def _grab_key(url: str, params=None) -> str:
    if isinstance(params, dict) and params:
        return url + '?' + '&'.join(f'{k}={params[k]}' for k in sorted(params, key=str))
    return url


def _record_for_url(url: str, kind: str, params=None) -> None:
    inst = instance_for_url(url)
    if not inst:
        return
    if kind == 'grab':
        key = _grab_key(url, params)
        now = time.time()
        with _lock:
            if now - _recent_grabs.get(key, 0) < _GRAB_DEDUPE_SECONDS:
                return
            if len(_recent_grabs) > 500:
                for k in [k for k, t in _recent_grabs.items() if now - t >= _GRAB_DEDUPE_SECONDS]:
                    del _recent_grabs[k]
            _recent_grabs[key] = now
    record(inst, kind)


def note_request(url: str, params=None) -> None:
    """Count one request if it goes to a configured Newznab indexer (called by api.get)."""
    try:
        _record_for_url(url, request_kind(url, params), params)
    except Exception:
        pass


def used(instance: str, kind: str, cfg: Optional[dict] = None, now: Optional[float] = None) -> int:
    cfg = cfg if cfg is not None else _newznab_scrapers().get(instance, {})
    start = window_start(_reset_mode(cfg), now)
    with _lock:
        return sum(1 for t in _load().get(instance, {}).get(kind, []) if t >= start)


def allow(instance: Optional[str], kind: str) -> bool:
    """False when this indexer has used its daily `kind` limit. Unknown/unlimited -> True."""
    if not instance:
        return True
    cfg = _newznab_scrapers().get(instance)
    if not cfg:
        return True
    lim = _limit(cfg, kind)
    if not lim:
        return True
    n = used(instance, kind, cfg)
    if n < lim:
        return True
    key = (instance, kind)
    if time.time() - _warned.get(key, 0) > 600:
        _warned[key] = time.time()
        what = 'API hits' if kind == 'api' else 'grabs'
        logging.warning(f"[IndexerQuota] '{instance}' has used {n}/{lim} {what} for today "
                        f"({_reset_mode(cfg)}); skipping its {what} until the limit resets")
    return False


def record(instance: Optional[str], kind: str) -> None:
    global _dirty
    if not instance or kind not in KINDS:
        return
    now = time.time()
    with _lock:
        entry = _load().setdefault(instance, {k: [] for k in KINDS})
        stamps = entry.setdefault(kind, [])
        stamps.append(now)
        if len(stamps) % 50 == 0 or (stamps and stamps[0] < now - _KEEP_SECONDS):
            entry[kind] = [t for t in stamps if t >= now - _KEEP_SECONDS]
        _dirty = True
        _save()


def allow_url(url: str, kind: str) -> bool:
    return allow(instance_for_url(url), kind)


def record_url(url: str, kind: str) -> None:
    """Count a call to `url`; a grab of a URL already counted in the last 10 min isn't counted again."""
    _record_for_url(url, kind)


def summary(now: Optional[float] = None) -> Dict[str, dict]:
    """{indexer: {'api': {'used','limit'}, 'grab': {...}, 'reset': mode, 'resets_at': epoch}}."""
    now = time.time() if now is None else now
    out = {}
    for name, cfg in _newznab_scrapers().items():
        mode = _reset_mode(cfg)
        info = {'reset': mode}
        for kind in KINDS:
            info[kind] = {'used': used(name, kind, cfg, now), 'limit': _limit(cfg, kind)}
        if mode == 'rolling_24h':
            start = window_start(mode, now)
            with _lock:
                live = [t for k in KINDS for t in _load().get(name, {}).get(k, []) if t >= start]
            info['resets_at'] = (min(live) + 86400) if live else None
        else:
            info['resets_at'] = resets_at(mode, now)
        out[name] = info
    return out


def usage_line(instance: str) -> str:
    """'API 123/5000 · Grabs 4/100 today' for the Connections page ('' if nothing to show)."""
    info = summary().get(instance)
    if not info:
        return ''
    parts = []
    for kind, label in (('api', 'API'), ('grab', 'Grabs')):
        used_n, lim = info[kind]['used'], info[kind]['limit']
        parts.append(f"{label} {used_n}/{lim}" if lim else f"{label} {used_n}")
    when = 'last 24 h' if info['reset'] == 'rolling_24h' else 'today'
    return ' · '.join(parts) + f' {when}'


def reset_for_tests() -> None:
    global _usage, _dirty, _last_save, _host_map_cache
    with _lock:
        _host_map_cache = (0.0, [])
        _recent_grabs.clear()
        _usage = {}
        _dirty = False
        _last_save = 0.0
        _warned.clear()


atexit.register(flush)
