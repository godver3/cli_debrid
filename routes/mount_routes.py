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
import threading

from flask import Blueprint, jsonify, render_template, request

from routes.models import admin_required
from utilities.settings import get_setting, set_setting
from utilities.settings_schema import SETTINGS_SCHEMA

mount_bp = Blueprint('mount', __name__)

SECTION = 'Built-in Mount'
SECRET_KEYS = ('primary_password', 'backup_password', 'api_token')
GROUPS = [
    ('Mount', ['mount_path', 'cache_dir', 'cache_size_gb', 'read_ahead_segments', 'allow_other']),
    ('Main usenet server', ['primary_host', 'primary_port', 'primary_ssl', 'primary_username',
                            'primary_password', 'primary_connections']),
    ('Backup server (optional)', ['backup_host', 'backup_port', 'backup_ssl', 'backup_username',
                                  'backup_password', 'backup_connections']),
    ('Advanced', ['api_port', 'api_token']),
]

_sync_lock = threading.Lock()
_last_sync = {'result': '', 'running': False}


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
        if key == 'additional_servers':
            continue
        val = cur.get(key, spec.get('default'))
        if key in SECRET_KEYS:
            out[key] = ''
            out[key + '_set'] = bool(val)
        else:
            out[key] = val
    out['additional_servers'] = [
        {**{k: v for k, v in s.items() if k != 'password'}, 'password': '', 'password_set': bool(s.get('password'))}
        for s in _servers(cur.get('additional_servers')) if isinstance(s, dict)]
    return out


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
    return '' if value is None else str(value).strip()


def _merge_servers(new_list, old_list) -> list:
    """Validate the edited server rows; an empty password keeps the old one
    (matched by host + username, else by position)."""
    from utilities.builtin_engine import additional_servers
    old = [s for s in _servers(old_list) if isinstance(s, dict)]
    out = []
    for i, s in enumerate(new_list or []):
        if not isinstance(s, dict):
            raise ValueError(f'additional server {i + 1} is not valid')
        host = str(s.get('host') or '').strip()
        if not host:
            continue  # blank row
        row = {'host': host, 'port': s.get('port') or 563, 'ssl': bool(s.get('ssl', True)),
               'username': str(s.get('username') or '').strip(), 'connections': s.get('connections') or 5,
               'backup': bool(s.get('backup', False)), 'password': str(s.get('password') or '')}
        if not row['password']:
            match = next((o for o in old if o.get('host') == host and (o.get('username') or '') == row['username']), None)
            if match is None and i < len(old) and old[i].get('host') == host:
                match = old[i]
            row['password'] = (match or {}).get('password', '')
        out.append(row)
    additional_servers({'additional_servers': out})  # raises ValueError on bad rows
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
    groups = [(title, [(k, schema[k]) for k in keys if k in schema]) for title, keys in GROUPS]
    return render_template('mount.html', groups=groups, selected=builtin_selected(),
                           servers_help=schema.get('additional_servers', {}).get('description', ''))


@mount_bp.route('/api/status')
@admin_required
def status():
    from utilities import builtin_engine
    out = {'selected': builtin_selected(), 'state': builtin_engine.state(),
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
            if key == 'additional_servers':
                updates[key] = json.dumps(_merge_servers(value, cur.get(key)))
                continue
            spec = schema.get(key)
            if spec is None:
                continue  # ignore unknown keys
            v = _coerce(key, spec, value)
            if key in SECRET_KEYS and v == '':
                continue  # empty secret: keep current
            if key == 'mount_path' and not str(v).startswith('/'):
                raise ValueError('mount path must be an absolute path')
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


@mount_bp.route('/api/restart', methods=['POST'])
@admin_required
def restart():
    if not builtin_selected():
        return jsonify({'success': False, 'error': 'The built-in mount is not the selected usenet backend.'}), 400
    from utilities import builtin_engine
    issues = builtin_engine.problems()
    if issues:
        return jsonify({'success': False, 'error': '; '.join(issues)}), 400
    builtin_engine.write_config(builtin_engine.build_config(builtin_engine._load_settings()))
    out = builtin_engine._supervisorctl('restart', builtin_engine.PROGRAM)
    return jsonify({'success': 'ERROR' not in out.upper(), 'output': out.strip()})
