"""
Mount page: status and settings of the optional built-in mount engine
(Usenet Provider = builtin). Shown in the top nav only when it is selected.

Settings saved here are only the "Built-in Mount" section, so saving does
not reinitialise the rest of cli_debrid: the engine is re-synced (restarted
only when its generated config changed) and the client reset.

Passwords and the API token are never sent to the browser. An empty
password field on save means "keep the current one".
"""

import json
import logging
import os
import threading
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request

from routes.models import admin_required
from utilities.settings import get_setting, set_setting
from utilities.settings_schema import SETTINGS_SCHEMA

mount_bp = Blueprint('mount', __name__)


@mount_bp.record_once
def _start_background(state):
    """Plex next-episode prewarm poller (idle unless the mount is in use)."""
    try:
        from utilities import mount_prewarm
        mount_prewarm.start(get_setting)
    except Exception as exc:
        logging.warning(f'Mount prewarm poller not started: {exc}')
    try:
        from utilities import mount_debrid
        mount_debrid.start(get_setting)
    except Exception as exc:
        logging.warning(f'Mount debrid sync not started: {exc}')

SECTION = 'Built-in Mount'
SECRET_KEYS = ('api_token',)
GROUPS = [
    ('Mount', ['mount_path', 'allow_other', 'uid', 'gid', 'umask']),
    ('Debrid', ['debrid_streaming']),
    ('Files', ['allowed_extensions', 'deobfuscate_mode']),
    ('Streaming', ['read_ahead_mb', 'max_connections_per_file', 'precache_mb', 'warm_connections',
                   'playback_reserve_percent', 'plex_playback_protection', 'buffer_memory_mb', 'idle_timeout_minutes']),
    ('Plex next-episode prewarm', ['plex_prewarm', 'prewarm_trigger_percent', 'prewarm_percent', 'prewarm_max_mb']),
    ('Disk cache', ['cache_dir', 'cache_size_gb', 'cache_expiry_hours', 'cache_cleanup_minutes']),
    ('Importing & health checks', ['processing_connections', 'processing_timeout_minutes',
                                   'import_sample_percent', 'health_sample_percent']),
    ('Advanced', ['debug_logging', 'api_port', 'api_token']),
]

LABELS = {
    'mount_path': 'Mount path', 'allow_other': 'Let other users (Plex) read the mount', 'uid': 'User ID (UID)',
    'gid': 'Group ID (GID)', 'umask': 'UMASK', 'read_ahead_mb': 'Read ahead (MB)',
    'max_connections_per_file': 'Max connections per file', 'buffer_memory_mb': 'Buffer memory (MB)',
    'idle_timeout_minutes': 'Connection idle timeout (minutes)', 'precache_mb': 'Pre-cache on open (MB)',
    'warm_connections': 'Warm connections per server', 'debrid_streaming': 'Stream debrid torrents through this mount', 'plex_prewarm': 'Prewarm the next episode while one plays',
    'prewarm_trigger_percent': 'Start at (% watched)', 'prewarm_percent': 'Fetch (% of next episode)',
    'prewarm_max_mb': 'Fetch at most (MB)', 'playback_reserve_percent': 'Playback connection reserve %',
    'plex_playback_protection': 'Plex playback protection (reserve only for what Plex is playing)', 'cache_dir': 'Cache directory',
    'cache_size_gb': 'Disk cache size (GB)', 'cache_expiry_hours': 'Cache expiry (hours)',
    'cache_cleanup_minutes': 'Cache cleanup interval (minutes)', 'processing_connections': 'Processing connections per file',
    'processing_timeout_minutes': 'Processing timeout (minutes)', 'import_sample_percent': 'NZB import availability sample %',
    'health_sample_percent': 'Repair availability sample %', 'debug_logging': 'Debug logging',
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


def builtin_selected() -> bool:
    try:
        up = get_setting('Usenet Provider') or {}
        return bool(up.get('enabled')) and (up.get('provider') or '').strip().lower() == 'builtin'
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
    from utilities.builtin_engine import servers
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
               'backup': bool(s.get('backup', False)), 'password': str(s.get('password') or '')}
        if not row['password']:
            match = next((o for o in old if o.get('host') == host and (o.get('username') or '') == row['username']), None)
            if match is None and i < len(old) and old[i].get('host') == host:
                match = old[i]
            row['password'] = (match or {}).get('password', '')
        out.append(row)
    servers({'servers': out})  # raises ValueError on bad rows
    return out


def _sync_in_background():
    def run():
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
    threading.Thread(target=run, name='builtin-engine-sync', daemon=True).start()


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
    out = {'selected': builtin_selected(), 'state': builtin_engine.state(),
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


@mount_bp.route('/api/now_playing')
@admin_required
def now_playing():
    """Plex sessions streaming from the built-in mount (others left out)."""
    if not builtin_selected():
        return jsonify({'selected': False, 'sessions': []})
    from usenet.builtin_client import BuiltinClient
    from utilities import mount_now_playing as np
    client = BuiltinClient()
    streams = client.get_streams()
    sessions = []
    if streams:  # nothing open on the mount: nothing to ask Plex about
        url, token = np.plex_server(get_setting)
        sessions = np.now_playing(np.plex_sessions(url, token), streams, client.mount_path,
                                  np.metadata_lookup(url, token))
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

@mount_bp.route('/internal/link', methods=['POST'])
def internal_link():
    """The engine's link callback: a fresh download URL for one debrid file.
    Only the local engine (its token, from localhost) may call it."""
    from utilities import builtin_engine, mount_debrid
    token = builtin_engine.current_token()
    auth = request.headers.get('Authorization', '')
    if not token or auth != f'Bearer {token}' or request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': 'forbidden'}), 403
    ref = str((request.get_json(silent=True) or {}).get('ref') or '')
    try:
        url, ttl = mount_debrid.link(ref)
    except Exception as exc:
        logging.warning(f'[mount debrid] no link for {ref}: {exc}')
        return jsonify({'error': str(exc)}), 502
    return jsonify({'url': url, 'expires_in': int(ttl)})


# ---- Migrate from cli_mount -------------------------------------------------

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
    return jsonify({
        'provider': (up.get('provider') or 'climount'),
        'climount_url': up.get('url') or '',
        'alongside': bool((get_setting('Built-in Mount') or {}).get('run_alongside')),
        'engine_state': builtin_engine.state(),
        'problems': builtin_engine.problems(),
        'meta_dir': mdir, 'metas': metas,
        'climount_root': root,
        'climount_root_mounted': bool(root) and os.path.ismount(root),
        'builtin_root': _engine().mount_path,
        'pre_migration_mount_path': (get_setting('Built-in Mount') or {}).get('pre_migration_mount_path') or '',
    })


@mount_bp.route('/api/migrate/alongside', methods=['POST'])
@admin_required
def migrate_alongside():
    on = bool((request.get_json(silent=True) or {}).get('on'))
    set_setting(SECTION, 'run_alongside', on)
    _sync_in_background()
    return jsonify({'success': True, 'alongside': on})


@mount_bp.route('/api/migrate/import', methods=['POST'])
@admin_required
def migrate_import():
    from utilities import builtin_migrate as bm
    limit = int((request.get_json(silent=True) or {}).get('limit') or 0)
    data_path = (get_setting('Usenet Provider') or {}).get('data_path') or ''
    job = bm.start_import(_climount_source(), _engine(), data_path, limit=limit)
    return jsonify({'success': True, 'job': job})


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
    if os.path.ismount(root):
        return jsonify({'success': False, 'error': f'{root} is still mounted: stop the cli_mount container first, then switch.'}), 409
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
    """Back to cli_mount: the engine stops (unmounting cli_mount's path)."""
    prev = (get_setting('Built-in Mount') or {}).get('pre_migration_mount_path') or '/mnt/clid'
    set_setting('Usenet Provider', 'provider', 'climount')
    set_setting(SECTION, 'mount_path', prev)
    _sync_in_background()
    return jsonify({'success': True, 'message': 'Switched back to cli_mount. Start the cli_mount container again.'})


@mount_bp.route('/api/state')
@admin_required
def engine_state():
    """Cheap state for the nav button."""
    from utilities import builtin_engine
    return jsonify({'selected': builtin_selected(), 'state': builtin_engine.state(),
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
