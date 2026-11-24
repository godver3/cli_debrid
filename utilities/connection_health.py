"""Cached connection health for the home page's Connections tile.

Runs the same checks as the Connections page in a background thread and keeps
the last result, so the home page reads a cache instead of calling every
service on each load.

- System services (Plex/Jellyfin, mount, cli_mount, cli_battery, TVDB, TMDB,
  Trakt, Scrob, PhalanxDB) are re-checked every 5 minutes; these checks are cheap.
- Scrapers and content sources are re-checked every 6 hours. Their checks spend
  indexer API hits (Newznab t=caps), run a real Torrentio search, or fetch a
  sample list, so checking them often would eat into API limits.
- Opening the Connections page records its results here too (record()).

The thread starts on the first summary() call, so nothing runs until a page
asks for it.
"""
import logging
import threading
import time
from datetime import datetime, timezone

SYSTEM_EVERY = 5 * 60
SLOW_EVERY = 6 * 60 * 60
_TICK = 30

_lock = threading.Lock()
_results = {}       # kind -> list of {'name', 'connected', 'error'}
_checked_at = {}    # kind -> epoch seconds of the last completed check
_running = set()    # kinds being checked right now
_thread = None


def _normalize(kind, payload):
    """Turn a check endpoint's payload into a list of {'name','connected','error'}."""
    if kind == 'system':
        items = [v for v in (payload or {}).values() if v]
    else:
        items = [v for v in (payload or []) if v]
    out = []
    for item in items:
        out.append({
            'name': item.get('name') or 'Unknown',
            'connected': bool(item.get('connected')),
            'error': item.get('error'),
        })
    return out


def record(kind, payload):
    """Store the result of one check kind ('system', 'scrapers', 'content_sources')."""
    with _lock:
        _results[kind] = _normalize(kind, payload)
        _checked_at[kind] = time.time()


def _run_system():
    from routes import connections_routes as cr
    from utilities.silo_functions import is_silo_selected
    jellyfin = cr.get_cached_setting('Debug', 'emby_jellyfin_url') and cr.get_cached_setting('Debug', 'emby_jellyfin_token')
    from utilities.direct_mount import is_direct_mount_jellyfin
    if is_silo_selected():
        media_check = cr.check_silo_connection
    elif is_direct_mount_jellyfin():
        media_check = cr.check_jellyfin_connection
    else:
        media_check = cr.check_jellyfin_connection if jellyfin else cr.check_plex_connection
    checks = [cr.check_cli_battery_connection, cr.check_mounted_files_connection, cr.check_phalanx_db_connection,
              media_check,
              cr.check_tvdb_connection, cr.check_tmdb_connection, cr.check_climount_connection,
              cr.check_scrob_connection, cr.check_trakt_connection]
    results = {}
    for fn in checks:
        try:
            results[fn.__name__] = fn()
        except Exception as e:
            results[fn.__name__] = {'name': fn.__name__.replace('check_', '').replace('_connection', ''),
                                    'connected': False, 'error': str(e)}
    return results


def _run_scrapers():
    from routes import connections_routes as cr
    return cr.check_nyaa_scrapers_only() + cr.check_non_nyaa_scrapers()


def _run_content_sources():
    from routes import connections_routes as cr
    return cr.check_content_sources_connections()


_KINDS = (
    ('system', SYSTEM_EVERY, _run_system),
    ('scrapers', SLOW_EVERY, _run_scrapers),
    ('content_sources', SLOW_EVERY, _run_content_sources),
)


def _due(kind, every):
    with _lock:
        if kind in _running:
            return False
        last = _checked_at.get(kind)
        if last is not None and time.time() - last < every:
            return False
        _running.add(kind)
        return True


def _check(kind, fn):
    try:
        record(kind, fn())
    except Exception as e:
        logging.warning(f"Connection health: {kind} check failed: {e}")
        with _lock:
            _checked_at[kind] = time.time()  # retry on the normal schedule, not every tick
    finally:
        with _lock:
            _running.discard(kind)


def _loop():
    while True:
        for kind, every, fn in _KINDS:
            if _due(kind, every):
                _check(kind, fn)
        time.sleep(_TICK)


def _ensure_started():
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name='connection-health', daemon=True)
        _thread.start()


def summary():
    """What the tile shows: counts, what's down, and when it was last checked."""
    _ensure_started()
    with _lock:
        items = [i for kind in ('system', 'scrapers', 'content_sources') for i in _results.get(kind, [])]
        checked = 'system' in _checked_at
        last = max(_checked_at.values()) if _checked_at else None
        checking = bool(_running)
    down = [{'name': i['name'], 'error': i['error']} for i in items if not i['connected']]
    return {
        'checked': checked,
        'checking': checking,
        'total': len(items),
        'ok': len(items) - len(down),
        'down': down,
        'checked_at': datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
    }
