"""
Mount page: status and settings of the optional built-in mount engine
(Usenet Provider = builtin). Shown in the top nav only when it is selected.

Settings saved here are only the "Built-in Mount" section, so saving does
not reinitialise the rest of cli_debrid: the engine is re-synced (restarted
only when its generated config changed) and the client reset.

Passwords and the API token are never sent to the browser. An empty
password field on save means "keep the current one".
"""

import datetime
import json
import logging
import os
import threading
import time
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request

from routes.models import admin_required
from utilities.settings import get_setting, set_setting
from utilities.settings_schema import SETTINGS_SCHEMA

mount_bp = Blueprint('mount', __name__)


@mount_bp.record_once
def _start_background(state):
    """Show precache + stream protection poller (idle unless the mount is in use)."""
    try:
        from utilities import mount_prewarm
        mount_prewarm.start(get_setting)
    except Exception as exc:
        logging.warning(f'Stream protection / show precache poller not started: {exc}')
    try:
        from utilities import mount_debrid
        mount_debrid.start(get_setting)
    except Exception as exc:
        logging.warning(f'Mount debrid sync not started: {exc}')

SECTION = 'Built-in Mount'
SECRET_KEYS = ('api_token', 'webdav_password', 'plex_token', 'jellyfin_token', 'debrid_download_api_keys', 'debrid_proxy')
GROUPS = [
    ('Mount', ['mount_path', 'allow_other', 'uid', 'gid', 'umask']),
    ('WebDAV share', ['webdav_enabled', 'webdav_port', 'webdav_username', 'webdav_password']),
    ('Files', ['allowed_extensions', 'deobfuscate_mode']),
    ('Media servers', ['plex_url', 'plex_token', 'jellyfin_url', 'jellyfin_token']),
    ('Streaming', ['read_ahead_mb', 'max_connections_per_file', 'precache_mb', 'warm_connections', 'primary_load_mode',
                   'playback_reserve_percent', 'playback_reserve_min', 'playback_reserve_max', 'plex_playback_protection', 'buffer_memory_mb', 'idle_timeout_minutes',
                   'plex_prewarm', 'prewarm_trigger_percent', 'prewarm_percent', 'prewarm_max_mb']),
    ('Debrid streaming', ['debrid_chunk_mb', 'debrid_connections_per_file', 'debrid_read_ahead_mb',
                          'debrid_sync_seconds', 'debrid_link_lifetime_hours', 'debrid_download_api_keys',
                          'debrid_proxy', 'debrid_user_agent']),
    ('Disk cache', ['cache_dir', 'cache_size_gb', 'cache_expiry_hours', 'cache_cleanup_minutes']),
    ('Importing & health checks', ['processing_connections', 'processing_timeout_minutes',
                                   'import_sample_percent', 'health_sample_percent', 'media_probe']),
    ('Advanced', ['debug_logging', 'api_port', 'api_token']),
]

LABELS = {
    'mount_path': 'Mount path', 'allow_other': 'Let other users (Plex) read the mount', 'uid': 'User ID (UID)',
    'gid': 'Group ID (GID)', 'umask': 'UMASK', 'read_ahead_mb': 'Read ahead (MB)',
    'max_connections_per_file': 'Max connections per file', 'buffer_memory_mb': 'Buffer memory (MB)',
    'idle_timeout_minutes': 'Connection idle timeout (minutes)', 'precache_mb': 'Pre-cache on open (MB)',
    'warm_connections': 'Warm connections per server', 'primary_load_mode': 'Primary server load', 'webdav_enabled': 'Share the mount over WebDAV',
    'webdav_port': 'WebDAV port', 'webdav_username': 'WebDAV username', 'webdav_password': 'WebDAV password', 'plex_prewarm': 'Show precache (fetch the start of the next episode while one plays)',
    'prewarm_trigger_percent': 'Start at (% watched)', 'prewarm_percent': 'Fetch (% of next episode)',
    'prewarm_max_mb': 'Fetch at most (MB)', 'playback_reserve_percent': 'Playback connection reserve %',
    'playback_reserve_min': 'Reserve at least (connections)', 'playback_reserve_max': 'Reserve at most (connections)',
    'plex_playback_protection': 'Stream protection (reserve only for what your media server is playing)', 'cache_dir': 'Cache directory',
    'cache_size_gb': 'Disk cache size (GB)', 'cache_expiry_hours': 'Cache expiry (hours)',
    'cache_cleanup_minutes': 'Cache cleanup interval (minutes)', 'processing_connections': 'Processing connections per file',
    'processing_timeout_minutes': 'Processing timeout (minutes)', 'import_sample_percent': 'NZB import availability sample %',
    'health_sample_percent': 'Repair availability sample %', 'media_probe': 'Check playability with ffprobe',
    'debug_logging': 'Debug logging', 'plex_url': 'Plex URL', 'plex_token': 'Plex token',
    'jellyfin_url': 'Jellyfin / Emby URL', 'jellyfin_token': 'Jellyfin / Emby API key',
    'debrid_chunk_mb': 'Range size (MB)', 'debrid_connections_per_file': 'Connections per file',
    'debrid_read_ahead_mb': 'Read ahead (MB)', 'debrid_sync_seconds': 'Account sync interval (seconds)',
    'debrid_link_lifetime_hours': 'Link lifetime (hours)', 'debrid_download_api_keys': 'Extra download API keys',
    'debrid_proxy': 'Proxy', 'debrid_user_agent': 'User-Agent',
    'allowed_extensions': 'Allowed file extensions', 'deobfuscate_mode': 'Deobfuscate mode', 'api_port': 'Engine API port', 'api_token': 'Engine API token',
}

_sync_lock = threading.Lock()
_last_sync = {'result': '', 'running': False}


def mount_page_visible() -> bool:
    """Nav shows Mount when the built-in mount is the backend or a
    migration from cli_mount is running alongside."""
    try:
        return builtin_selected() or bool((get_setting('Built-in Mount') or {}).get('run_alongside'))
    except Exception:
        return False



def _fuse_mounted(path: str) -> bool:
    from utilities import builtin_engine
    return builtin_engine.fuse_mounted(path)


def _dead_mount(path: str) -> bool:
    """A FUSE mount whose program has stopped ("Transport endpoint is not
    connected"). Docker leaves one in a container that was created while
    cli_mount's mount was up: the container's bind is that mount itself, so
    stopping cli_mount can't take it away. Only recreating the container does."""
    import errno
    try:
        with os.scandir(path):  # stat still succeeds on a dead mount; opening it doesn't
            pass
    except OSError as exc:
        return exc.errno == errno.ENOTCONN
    return False

def builtin_selected() -> bool:
    """The built-in mount is in use (usenet backend, or debrid-only)."""
    try:
        from utilities import builtin_engine
        return builtin_engine.selected({'Usenet Provider': get_setting('Usenet Provider') or {},
                                        'Built-in Mount': get_setting('Built-in Mount') or {}})
    except Exception:
        return False


def _schema():
    return {k: v for k, v in SETTINGS_SCHEMA.get(SECTION, {}).items() if isinstance(v, dict)}


def _current() -> dict:
    return dict(get_setting(SECTION) or {})


def _servers(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        out = json.loads(raw) if isinstance(raw, str) and raw.strip() else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _public_settings() -> dict:
    """Settings for the browser, with secrets replaced by "is set" flags."""
    cur, schema = _current(), _schema()
    out = {}
    for key, spec in schema.items():
        if key in ('servers', 'virtual_folders'):
            continue
        val = cur.get(key, spec.get('default'))
        if key in SECRET_KEYS:
            out[key] = ''
            out[key + '_set'] = bool(val)
        else:
            if spec.get('choices') and val not in spec['choices']:
                val = spec.get('default')
            out[key] = val
    out['virtual_folders'] = [{'name': n, 'filters': [{'type': k, 'value': v} for k, v in f.items()]}
                              for n, f in _virtual_folders(cur).items()]
    from utilities import mount_now_playing as np
    url, token, url_src, token_src = np.plex_server_source(get_setting)
    out['plex_effective'] = {'url': url, 'url_source': url_src, 'token_set': bool(token), 'token_source': token_src}
    from utilities import media_sessions as ms
    url, token, url_src, token_src = ms.jellyfin_server_source(get_setting)
    out['jellyfin_effective'] = {'url': url, 'url_source': url_src, 'token_set': bool(token), 'token_source': token_src}
    silo_url, silo_key = ms.silo_server(get_setting)
    out['silo_effective'] = {'url': silo_url, 'key_set': bool(silo_key)}
    out['servers'] = [
        {**{k: v for k, v in s.items() if k != 'password'}, 'password': '', 'password_set': bool(s.get('password'))}
        for s in _servers(cur.get('servers')) if isinstance(s, dict)]
    return out


def _virtual_folders(cur, strict=False):
    from utilities.builtin_engine import virtual_folders
    return virtual_folders(cur, strict=strict)


def _coerce(key, spec, value):
    kind = spec.get('type')
    if kind == 'boolean':
        return value if isinstance(value, bool) else str(value).lower() in ('true', '1', 'on', 'yes')
    if kind == 'integer':
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'{key} must be a whole number')
        if v < 0:
            raise ValueError(f'{key} cannot be negative')
        return v
    v = '' if value is None else str(value).strip()
    if spec.get('choices') and v not in spec['choices']:
        raise ValueError(f'{key} must be one of: ' + ', '.join(spec['choices']))
    return v


def _merge_servers(new_list, old_list) -> list:
    """Validate the edited server rows; an empty password keeps the old one
    (matched by host + username, else by position)."""
    from utilities.builtin_engine import servers, server_enabled
    old = [s for s in _servers(old_list) if isinstance(s, dict)]
    out = []
    for i, s in enumerate(new_list or []):
        if not isinstance(s, dict):
            raise ValueError(f'server {i + 1} is not valid')
        host = str(s.get('host') or '').strip()
        if not host:
            continue  # blank row
        row = {'host': host, 'port': s.get('port') or 563, 'ssl': bool(s.get('ssl', True)),
               'username': str(s.get('username') or '').strip(), 'connections': s.get('connections') or 10,
               'backup': bool(s.get('backup', False)), 'password': str(s.get('password') or ''),
               # Off: kept with its settings but not given to the engine.
               'enabled': server_enabled(s),
               # Servers sharing an article store (same backbone name) answer
               # "missing" for each other: the engine asks only one of them.
               'backbone': str(s.get('backbone') or '').strip()}
        # Plan end + auto-renew are notes for the Mount page only; the
        # engine never sees them (builtin_engine.servers drops them).
        expires = str(s.get('expires') or '').strip()
        if expires:
            try:
                expires = datetime.date.fromisoformat(expires).isoformat()
            except ValueError:
                raise ValueError(f'server {i + 1} ({host}): plan end must be a date (YYYY-MM-DD)') from None
        row['expires'] = expires
        row['auto_renew'] = bool(s.get('auto_renew', False))
        if not row['password']:
            match = next((o for o in old if o.get('host') == host and (o.get('username') or '') == row['username']), None)
            if match is None and i < len(old) and old[i].get('host') == host:
                match = old[i]
            row['password'] = (match or {}).get('password', '')
        out.append(row)
    servers({'servers': out})  # raises ValueError on bad rows
    return out


_plans = {'at': 0.0, 'data': {}, 'running': False}
_plans_lock = threading.Lock()
PLAN_REFRESH_SECONDS = 3600


def _debrid_plans() -> dict:
    """Plan end per debrid provider key (rd, tb, ...), from the providers'
    own account APIs. Cached an hour and refreshed in the background, so the
    Mount page never waits on a provider."""
    import time
    with _plans_lock:
        stale = time.time() - _plans['at'] > PLAN_REFRESH_SECONDS
        if stale and not _plans['running']:
            _plans['running'] = True
            threading.Thread(target=_refresh_debrid_plans, daemon=True, name='mount-debrid-plans').start()
        return dict(_plans['data'])


def _refresh_debrid_plans():
    import time
    data = {}
    try:
        from utilities import mount_debrid
        for p in mount_debrid.providers():
            key = mount_debrid.provider_key(p)
            try:
                sub = p.get_subscription_status() or {}
            except Exception as exc:
                logging.debug(f'Mount page: {key} subscription status failed: {exc}')
                continue
            if sub.get('error'):
                continue
            data[key] = {'expiration': sub.get('expiration'), 'days_remaining': sub.get('days_remaining'),
                         'premium': sub.get('premium')}
    finally:
        with _plans_lock:
            _plans.update(at=time.time(), data=data, running=False)


def _sync_now():
    """Apply the saved settings to the engine (restarts it when its config
    changed) and wait for that to finish."""
    with _sync_lock:
        _last_sync['running'] = True
        try:
            from usenet import reset_usenet_client
            from usenet.builtin_client import reset_builtin_client
            from utilities import builtin_engine
            reset_builtin_client()
            reset_usenet_client()
            _last_sync['result'] = builtin_engine.sync()
            logging.info(f"Built-in mount engine (Mount page save): {_last_sync['result']}")
        except Exception as exc:
            _last_sync['result'] = f'sync failed: {exc}'
            logging.warning(f'Built-in mount engine sync failed: {exc}')
        finally:
            _last_sync['running'] = False


def _sync_in_background():
    threading.Thread(target=_sync_now, name='builtin-engine-sync', daemon=True).start()


@mount_bp.route('/')
@admin_required
def index():
    schema = _schema()
    groups = [(title, [(k, schema[k], LABELS.get(k) or k.split('_', 1)[-1].replace('_', ' ').capitalize())
                       for k in keys if k in schema]) for title, keys in GROUPS]
    return render_template('mount.html', groups=groups, selected=builtin_selected(),
                           servers_help=schema.get('servers', {}).get('description', ''),
                           default_vfolders=_default_vfolders())


def _default_vfolders():
    from utilities.content_routing import DEFAULT_VIRTUAL_FOLDERS
    return DEFAULT_VIRTUAL_FOLDERS


@mount_bp.route('/api/status')
@admin_required
def status():
    from utilities import builtin_engine
    out = {'selected': builtin_selected(), 'state': builtin_engine.cached_state(),
           'stopped_by_user': builtin_engine.stopped_by_user(),
           'problems': builtin_engine.problems(), 'last_sync': dict(_last_sync),
           'settings': _public_settings(), 'engine': None, 'stats': None}
    if out['selected']:
        try:
            from usenet.builtin_client import BuiltinClient
            c = BuiltinClient()
            code, body = c._call('GET', '/status', timeout=5)
            if code == 200:
                out['engine'] = body
            code, body = c._call('GET', '/stats', timeout=5)
            if code == 200:
                out['stats'] = body
            out['streams'] = c.get_streams()
            out['debrid_plans'] = _debrid_plans()
            from utilities import mount_debrid as md
            out['debrid_keys'] = [md.provider_key(p) for p in md.providers()]
        except Exception as exc:
            logging.debug(f'Mount page: engine status failed: {exc}')
    return jsonify(out)


@mount_bp.route('/api/settings', methods=['POST'])
@admin_required
def save_settings():
    data = request.get_json(silent=True) or {}
    schema, cur = _schema(), _current()
    updates = {}
    try:
        for key, value in data.items():
            if key == 'servers':
                updates[key] = json.dumps(_merge_servers(value, cur.get(key)))
                continue
            if key == 'virtual_folders':
                rows = value if isinstance(value, list) else []
                checked = _virtual_folders({'virtual_folders': rows}, strict=True)  # raises on bad rows
                updates[key] = json.dumps([{'name': n, 'filters': [{'type': k, 'value': v} for k, v in f.items()]}
                                           for n, f in checked.items()]) if checked else ''
                continue
            spec = schema.get(key)
            if spec is None:
                continue  # ignore unknown keys
            v = _coerce(key, spec, value)
            if key in SECRET_KEYS and v == '':
                continue  # empty secret: keep current
            if key == 'mount_path' and not (str(v).startswith('/') or (len(str(v)) >= 2 and str(v)[1] == ':')):
                raise ValueError('mount path must be an absolute path (or a drive letter like M: on Windows)')
            if key in ('uid', 'gid') and v and not str(v).isdigit():
                raise ValueError(f'{key} must be a number or empty')
            if key == 'umask' and v and not all(ch in '01234567' for ch in str(v)):
                raise ValueError('umask must be octal digits, e.g. 022')
            if key.endswith('_percent') and v > 100:
                raise ValueError(f'{key.replace("_", " ")} must be 0-100')
            updates[key] = v
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    changed = {k: v for k, v in updates.items() if cur.get(k) != v}
    for k, v in changed.items():
        set_setting(SECTION, k, v)
    if changed and builtin_selected():
        _sync_in_background()
    return jsonify({'success': True, 'changed': sorted(changed),
                    'applying': bool(changed and builtin_selected())})


@mount_bp.route('/api/speedtest', methods=['POST'])
@admin_required
def speedtest():
    """Manual speed test of one usenet server ({kind: usenet, provider:
    name}) or debrid service ({kind: debrid, provider: rd|tb|...})."""
    if not builtin_selected():
        return jsonify({'ok': False, 'error': 'the built-in mount is not in use'}), 409
    from utilities import mount_speedtest as mst
    data = request.get_json(silent=True) or {}
    name = str(data.get('provider') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'provider is required'}), 400
    client = _engine()
    if data.get('kind') == 'debrid':
        from utilities import mount_debrid as md
        out = mst.debrid(client, name, md.providers, md.provider_key, md.link)
    else:
        out = mst.usenet(client, name)
    out['tested_at'] = time.strftime('%H:%M')
    return jsonify(out)


@mount_bp.route('/api/plex_test', methods=['POST'])
@admin_required
def plex_test():
    """Can the mount reach Plex? Uses the URL/token typed on the page (not
    saved yet) or else the ones in effect."""
    from utilities import mount_now_playing as np
    data = request.get_json(silent=True) or {}
    url, token, url_src, token_src = np.plex_server_source(get_setting)
    if (data.get('url') or '').strip():
        url, url_src = data['url'].strip().rstrip('/'), 'Mount page (unsaved)'
    if (data.get('token') or '').strip():
        token, token_src = data['token'].strip(), 'Mount page (unsaved)'
    if not url or not token:
        return jsonify({'ok': False, 'error': 'No Plex URL/token: enter them here, or set Plex in Settings.'})
    import requests as rq
    try:
        r = rq.get(f'{url}/', headers={'X-Plex-Token': token, 'Accept': 'application/json'}, timeout=8)
        if r.status_code == 401:
            return jsonify({'ok': False, 'error': 'Plex refused the token (401).', 'url': url})
        r.raise_for_status()
        name = (r.json().get('MediaContainer') or {}).get('friendlyName') or ''
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'Plex not reachable at {url}: {exc}', 'url': url})
    sessions, err = np.plex_sessions_checked(url, token)
    return jsonify({'ok': not err, 'error': err, 'server': name, 'url': url, 'url_source': url_src,
                    'token_source': token_src, 'sessions': len(sessions)})


@mount_bp.route('/api/jellyfin_test', methods=['POST'])
@admin_required
def jellyfin_test():
    """Can the mount reach Jellyfin/Emby? Uses the URL/key typed on the page
    (not saved yet) or else the ones in effect."""
    from utilities import media_sessions as ms
    data = request.get_json(silent=True) or {}
    url, token, url_src, token_src = ms.jellyfin_server_source(get_setting)
    if (data.get('url') or '').strip():
        url, url_src = data['url'].strip().rstrip('/'), 'Mount page (unsaved)'
    if (data.get('token') or '').strip():
        token, token_src = data['token'].strip(), 'Mount page (unsaved)'
    return jsonify({**ms.jellyfin_test(url, token), 'url_source': url_src, 'token_source': token_src})


@mount_bp.route('/api/silo_test', methods=['POST'])
@admin_required
def silo_test():
    """Can the mount read Silo's playback sessions? Silo's URL and key are
    set in Settings (File Management), not on this page."""
    from utilities import media_sessions as ms
    from utilities.silo_functions import test_connection
    url, key = ms.silo_server(get_setting)
    if not url:
        return jsonify({'ok': False, 'error': 'Silo is not the media server, or has no URL/API key (Settings → File Management).'})
    ok, message = test_connection(url, key)
    if not ok:
        return jsonify({'ok': False, 'error': message, 'url': url})
    sessions, err = ms.silo_sessions_checked(get_setting)
    return jsonify({'ok': not err, 'error': err, 'url': url, 'server': 'Silo', 'sessions': len(sessions)})


@mount_bp.route('/api/now_playing')
@admin_required
def now_playing():
    """Media server sessions (Plex, Jellyfin/Emby, Silo) streaming from the
    built-in mount (others left out)."""
    if not builtin_selected():
        return jsonify({'selected': False, 'sessions': []})
    from usenet.builtin_client import BuiltinClient
    from utilities import mount_now_playing as np
    client = BuiltinClient()
    streams = client.get_streams()
    sessions = []
    # Nothing open and nothing remembered (a fully cached file is closed by
    # the player while it still plays): nothing to ask the servers about.
    if streams or np.has_remembered():
        from utilities import media_sessions as ms
        sessions = np.now_playing(ms.sessions(get_setting), streams, client.mount_path, ms.lookup(get_setting))
    return jsonify({'selected': True, 'sessions': sessions, 'open_files': len(streams)})


# ---- Queue / Browse tabs (engine jobs, through cli_debrid) -----------------

def _engine():
    from usenet.builtin_client import BuiltinClient
    return BuiltinClient()


def _engine_unavailable():
    return jsonify({'success': False, 'error': 'The mount engine is not running.'}), 503


@mount_bp.route('/api/jobs')
@admin_required
def jobs_list():
    code, body = _engine()._call('GET', '/jobs', timeout=10)
    if code is None:
        return _engine_unavailable()
    return jsonify({'success': code == 200, 'jobs': body if isinstance(body, list) else []})


@mount_bp.route('/api/tree')
@mount_bp.route('/api/tree/<top>')
@admin_required
def tree(top=None):
    """The mount's folders as the engine serves them (__all__, nzbs, torrents...)."""
    path = '/tree' if top is None else f'/tree/{quote(top, safe="")}'
    code, body = _engine()._call('GET', path, timeout=10)
    if code is None:
        return _engine_unavailable()
    if code != 200:
        return jsonify({'success': False, 'error': (body or {}).get('error', f'HTTP {code}')}), code
    return jsonify({'success': True, ('tops' if top is None else 'folders'): body if isinstance(body, list) else []})


@mount_bp.route('/api/jobs/<job_id>')
@admin_required
def job_detail(job_id):
    code, body = _engine()._call('GET', f'/jobs/{job_id}', timeout=10)
    if code is None:
        return _engine_unavailable()
    if code != 200:
        return jsonify({'success': False, 'error': (body or {}).get('error', f'HTTP {code}')}), code
    return jsonify({'success': True, 'job': body})


@mount_bp.route('/api/jobs/delete', methods=['POST'])
@admin_required
def jobs_delete():
    """Delete jobs from the mount (their files disappear). Like cli_mount's
    queue delete: cli_debrid notices through the sync feed."""
    ids = [str(i) for i in (request.get_json(silent=True) or {}).get('ids') or [] if i]
    client = _engine()
    failed = [i for i in ids if not client.remove_nzb_exact(i)]
    return jsonify({'success': not failed, 'deleted': len(ids) - len(failed), 'failed': failed})


@mount_bp.route('/api/jobs/<job_id>/<action>', methods=['POST'])
@admin_required
def job_action(job_id, action):
    paths = {'retry': f'/jobs/{job_id}/retry', 'check': f'/jobs/{job_id}/health'}
    if action not in paths:
        return jsonify({'success': False, 'error': 'unknown action'}), 404
    code, body = _engine()._call('POST', paths[action], timeout=15)
    if code is None:
        return _engine_unavailable()
    ok = code in (200, 202)
    return jsonify({'success': ok, **({} if ok else {'error': (body or {}).get('error', f'HTTP {code}')})}), (200 if ok else 400)


# ---- Debrid links for the engine (not a user page) ---------------------------

def _engine_caller_ok() -> bool:
    """Only the local engine (its token, from localhost) may call /internal."""
    from utilities import builtin_engine
    token = builtin_engine.current_token()
    auth = request.headers.get('Authorization', '')
    return bool(token) and auth == f'Bearer {token}' and request.remote_addr in ('127.0.0.1', '::1')


@mount_bp.route('/internal/link', methods=['POST'])
def internal_link():
    """The engine's link callback: a fresh download URL for one debrid file.
    410 + dead=true when the provider proved the file gone (the engine then
    marks it broken for repair); any other failure is 502 and proves nothing."""
    from utilities import mount_debrid
    from debrid.mount_proof import MountFileGone
    if not _engine_caller_ok():
        return jsonify({'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    ref = str(body.get('ref') or '')
    try:
        url, ttl = mount_debrid.link(ref, refresh=bool(body.get('refresh')))
    except MountFileGone as gone:
        logging.warning(f'[mount debrid] {ref} is gone on the provider: {gone}')
        return jsonify({'dead': True, 'reason': gone.reason, 'error': gone.detail or str(gone)}), 410
    except Exception as exc:
        logging.warning(f'[mount debrid] no link for {ref}: {exc}')
        return jsonify({'error': str(exc)}), 502
    return jsonify({'url': url, 'expires_in': int(ttl)})


@mount_bp.route('/internal/check', methods=['POST'])
def internal_check():
    """The engine's repair check for one debrid file, by cli_mount's rules:
    {state: healthy|broken|unknown, reason}."""
    from utilities import mount_debrid
    if not _engine_caller_ok():
        return jsonify({'error': 'forbidden'}), 403
    ref = str((request.get_json(silent=True) or {}).get('ref') or '')
    state, reason = mount_debrid.check(ref)
    return jsonify({'state': state, 'reason': reason})


# ---- Migrate from cli_mount -------------------------------------------------

def _import_space() -> dict:
    """Where imported jobs go, and how much more the disk cache may still
    grow (it can share that disk)."""
    from utilities import builtin_engine
    cfg = builtin_engine.build_config(builtin_engine._load_settings())
    used = 0
    try:
        code, st = _engine()._call('GET', '/stats', timeout=5)
        used = int(((st or {}).get('disk_cache') or {}).get('bytes') or 0) if code == 200 else 0
    except Exception:
        pass
    return {'data_dir': cfg['data_dir'], 'cache_dir': cfg['cache_dir'],
            'cache_left': max(0, int(cfg.get('cache_bytes') or 0) - used)}


def _climount_source():
    from usenet.climount_client import CliMountClient
    return CliMountClient()


@mount_bp.route('/api/migrate/status')
@admin_required
def migrate_status():
    from utilities import builtin_engine, builtin_migrate as bm
    up = get_setting('Usenet Provider') or {}
    data_path = up.get('data_path') or ''
    mdir = bm.meta_dir(data_path)
    try:
        metas = sum(1 for n in os.listdir(mdir) if n.endswith('.meta')) if os.path.isdir(mdir) else 0
    except OSError:
        metas = 0
    root = bm.climount_mount_root(get_setting)
    space = None
    listed = None  # jobs cli_mount lists now: what Import imports
    if metas:
        try:
            listed = [e.get('info_hash') or '' for e in _climount_source().iter_library_nzbs()]
        except Exception as exc:
            logging.debug(f'[mount] cli_mount job list failed: {exc}')
        try:
            sp = _import_space()
            sizes = bm.meta_sizes(mdir)
            # The metadata folder keeps maps of jobs cli_mount has since
            # removed; Import skips those, so only listed jobs are counted.
            ids = [i for i in listed if i in sizes] if listed is not None else sorted(sizes)
            space = bm.space_plan(ids, sizes, sp['data_dir'], sp['cache_dir'], sp['cache_left'])
        except Exception as exc:
            logging.debug(f'[mount] import space estimate failed: {exc}')
    return jsonify({
        'space': space,
        'provider': (up.get('provider') or 'climount'),
        'climount_url': up.get('url') or '',
        'alongside': bool((get_setting('Built-in Mount') or {}).get('run_alongside')),
        'engine_state': builtin_engine.state(),
        'problems': builtin_engine.problems(),
        'meta_dir': mdir, 'metas': metas, 'listed': None if listed is None else len(listed),
        'climount_root': root,
        'climount_root_mounted': bool(root) and _fuse_mounted(root),
        'builtin_root': _engine().mount_path,
        'pre_migration_mount_path': (get_setting('Built-in Mount') or {}).get('pre_migration_mount_path') or '',
        'debrid_needed': _debrid_needed(),
        'debrid': _debrid_status(),
        'program_running': _program_running(),
    })


def _debrid_needed() -> bool:
    try:
        from utilities import mount_debrid
        return mount_debrid.needed(get_setting)
    except Exception:
        return False


def _debrid_status() -> dict:
    from utilities import mount_debrid
    return mount_debrid.status()


def _program_running() -> bool:
    try:
        from routes.program_operation_routes import program_is_running
        return bool(program_is_running())
    except Exception:
        return False


@mount_bp.route('/api/migrate/alongside', methods=['POST'])
@admin_required
def migrate_alongside():
    on = bool((request.get_json(silent=True) or {}).get('on'))
    set_setting(SECTION, 'run_alongside', on)
    _sync_in_background()
    return jsonify({'success': True, 'alongside': on})


def _engine_why_down() -> str:
    """Why the engine won't come up ('' while it may): what stops it from
    starting here, Stop pressed on the Mount page, or supervisord gave up."""
    from utilities import builtin_engine
    issues = builtin_engine.problems()
    if issues:
        return "it can't start: " + '; '.join(issues)
    if builtin_engine.stopped_by_user():
        return 'it was stopped from the Mount page (press Start)'
    if builtin_engine.state() == 'FATAL':
        return 'it keeps crashing on start (see the cli_debrid log)'
    return ''


@mount_bp.route('/api/migrate/import', methods=['POST'])
@admin_required
def migrate_import():
    from utilities import builtin_migrate as bm
    try:
        limit = max(0, int((request.get_json(silent=True) or {}).get('limit') or 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Jobs to import must be a whole number (0 = all).'}), 400
    from utilities import builtin_engine
    if not builtin_engine.wanted():
        return jsonify({'success': False, 'error': "The built-in mount engine isn't running: start it with step 1 "
                                                   "(Start alongside) first, then Import."}), 400
    why = _engine_why_down()
    if why:
        return jsonify({'success': False, 'error': f"The built-in mount engine isn't running: {why}. "
                                                   f"Fix that on the Mount page, then Import."}), 400
    data_path = (get_setting('Usenet Provider') or {}).get('data_path') or ''
    # Same repair standards as cli_mount: carry its repair settings over.
    copied = bm.climount_repair_settings(data_path)
    # cli_mount's Plex URL/token only when cli_debrid has none of its own
    from utilities import mount_now_playing as np
    have_url, have_token = np.plex_server(get_setting)
    if have_url and have_token:
        copied.pop('plex_url', None)
        copied.pop('plex_token', None)
    backbones = copied.pop(bm.BACKBONES_KEY, None) or {}
    if backbones:
        # cli_mount's backbone names go onto the servers with the same host.
        current = _servers((get_setting(SECTION) or {}).get('servers'))
        updated, changed = bm.apply_backbones(current, backbones)
        if changed:
            set_setting(SECTION, 'servers', json.dumps(updated))
            copied['server_backbones'] = f'{changed} server(s)'
    for key, value in copied.items():
        if key == 'server_backbones':
            continue
        set_setting(SECTION, key, value)
    # The sync may restart the engine: it runs in the import thread, which
    # then waits for the engine to answer before importing anything.
    job = bm.start_import(_climount_source(), _engine(), data_path, limit=limit, space=_import_space(),
                          prepare=_sync_now if copied else None, why_down=_engine_why_down)
    shown = {k: ('(set)' if k in SECRET_KEYS else v) for k, v in copied.items()}
    return jsonify({'success': True, 'job': job, 'copied_settings': shown})


@mount_bp.route('/api/migrate/job/<job_id>')
@admin_required
def migrate_job(job_id):
    from utilities import builtin_migrate as bm
    st = bm.status(job_id)
    return (jsonify(st), 200) if st else (jsonify({'error': 'unknown job'}), 404)


@mount_bp.route('/api/migrate/job/<job_id>/cancel', methods=['POST'])
@admin_required
def migrate_cancel(job_id):
    from utilities import builtin_migrate as bm
    return jsonify({'success': bm.cancel(job_id)})


@mount_bp.route('/api/migrate/compare', methods=['POST'])
@admin_required
def migrate_compare():
    from utilities import builtin_migrate as bm
    sample = max(1, min(int((request.get_json(silent=True) or {}).get('sample') or 10), 50))
    root = bm.climount_mount_root(get_setting)
    client = _engine()
    if not root:
        return jsonify({'success': False, 'error': "cli_mount's mount path isn't known (File Management → original files path)."}), 400
    out = bm.compare(client, os.path.join(root, '__all__'), os.path.join(client.mount_path, '__all__'), sample)
    return jsonify({'success': True, **out})


@mount_bp.route('/api/migrate/switch', methods=['POST'])
@admin_required
def migrate_switch():
    """Make the built-in mount the backend on cli_mount's mount path."""
    from utilities import builtin_migrate as bm
    root = bm.climount_mount_root(get_setting)
    if not root:
        return jsonify({'success': False, 'error': "cli_mount's mount path isn't known (File Management → original files path)."}), 400
    if _fuse_mounted(root):
        if _dead_mount(root):
            return jsonify({'success': False, 'error': (
                f"cli_mount is stopped, but {root} is still held by this container: it was created while "
                f"cli_mount's mount was up. With cli_mount still stopped, recreate Clyde's container "
                f"(docker compose up -d --force-recreate <clyde service>), then switch.")}), 409
        return jsonify({'success': False, 'error': f'{root} is still mounted: stop the cli_mount container first, then switch.'}), 409
    if _debrid_needed():
        st = _debrid_status()
        if not st.get('synced_at') or not st.get('complete'):
            left = st.get('pending') or 0
            why = (f'{left} debrid torrent(s) are still being added to the built-in mount'
                   if st.get('synced_at') else 'the built-in mount has not listed your debrid account(s) yet')
            return jsonify({'success': False, 'error': f'Not yet: {why}. Their folders would be missing after the '
                            'switch. Keep it running alongside and try again in a few minutes.'}), 409
    cur = (get_setting('Built-in Mount') or {}).get('mount_path') or '/mnt/clid'
    if cur.rstrip('/') != root.rstrip('/'):
        set_setting(SECTION, 'pre_migration_mount_path', cur)
    set_setting(SECTION, 'mount_path', root)
    set_setting(SECTION, 'run_alongside', False)
    set_setting('Usenet Provider', 'provider', 'builtin')  # URL/token kept for switching back
    _sync_in_background()
    return jsonify({'success': True, 'mount_path': root})


@mount_bp.route('/api/migrate/rollback', methods=['POST'])
@admin_required
def migrate_rollback():
    """Back to cli_mount. The engine stays up on its own path (alongside)
    so the jobs added since the switch can be copied back."""
    if _program_running():
        return jsonify({'success': False, 'error': 'Stop the program first (the mount keeps running). Its queue '
                        "checks would take jobs that aren't in cli_mount yet for dead ones."}), 409
    prev = (get_setting('Built-in Mount') or {}).get('pre_migration_mount_path') or '/mnt/clid'
    set_setting('Usenet Provider', 'provider', 'climount')
    set_setting(SECTION, 'mount_path', prev)
    set_setting(SECTION, 'run_alongside', True)
    _sync_in_background()
    return jsonify({'success': True, 'message': f'Switched back. The built-in mount moves to {prev}. Start the '
                    'cli_mount container, press Copy back, and start the program when it has finished.'})


@mount_bp.route('/api/migrate/export', methods=['POST'])
@admin_required
def migrate_export():
    """Copy the engine's usenet jobs cli_mount lacks back to cli_mount."""
    from utilities import builtin_migrate as bm
    if (get_setting('Usenet Provider') or {}).get('provider') == 'builtin':
        return jsonify({'success': False, 'error': 'Switch back to cli_mount first.'}), 409
    limit = int((request.get_json(silent=True) or {}).get('limit') or 0)
    job = bm.start_export(_engine(), _climount_source(), limit=limit)
    return jsonify({'success': True, 'job': job})


@mount_bp.route('/api/state')
@admin_required
def engine_state():
    """Cheap state for the nav button."""
    from utilities import builtin_engine
    return jsonify({'selected': builtin_selected(), 'state': builtin_engine.cached_state(),
                    'stopped_by_user': builtin_engine.stopped_by_user()})


@mount_bp.route('/api/start', methods=['POST'])
@admin_required
def start():
    from utilities import builtin_engine
    if not mount_page_visible():
        return jsonify({'success': False, 'error': 'The built-in mount is not the selected usenet backend.'}), 400
    result = builtin_engine.user_start()
    ok = not result.startswith('cannot start')
    return jsonify({'success': ok, 'result': result, **({} if ok else {'error': result})}), (200 if ok else 400)


@mount_bp.route('/api/stop', methods=['POST'])
@admin_required
def stop():
    from utilities import builtin_engine
    return jsonify({'success': True, 'result': builtin_engine.user_stop()})


@mount_bp.route('/api/cache/<action>', methods=['POST'])
@admin_required
def cache_action(action):
    """Run cleanup now, or purge the engine's caches (cli_mount's buttons)."""
    if action not in ('cleanup', 'purge'):
        return jsonify({'success': False, 'error': 'unknown action'}), 404
    code, body = _engine()._call('POST', f'/cache/{action}', timeout=120)
    if code is None:
        return _engine_unavailable()
    return jsonify({'success': code == 200, 'result': body}), (200 if code == 200 else 502)


@mount_bp.route('/api/restart', methods=['POST'])
@admin_required
def restart():
    if not mount_page_visible():
        return jsonify({'success': False, 'error': 'The built-in mount is not the selected usenet backend.'}), 400
    from utilities import builtin_engine
    issues = builtin_engine.problems()
    if issues:
        return jsonify({'success': False, 'error': '; '.join(issues)}), 400
    try:  # an explicit restart also undoes an earlier Stop
        import os
        os.unlink(builtin_engine._stop_flag())
    except FileNotFoundError:
        pass
    builtin_engine.write_config(builtin_engine.build_config(builtin_engine._load_settings()))
    out = builtin_engine.control('restart')
    return jsonify({'success': 'ERROR' not in out.upper(), 'output': out.strip()})
