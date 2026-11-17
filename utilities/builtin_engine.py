"""
Runs the optional built-in mount engine (engine/, `clid-engine`).

Standard library only: utilities/set_supervisor_env.py imports it before
cli_debrid itself is up. It turns cli_debrid's settings into the engine's
JSON config and starts, restarts or stops supervisord's `mount_engine`
program to match:

  - Usenet Provider is enabled with provider = builtin -> engine running,
    restarted only when its generated config changed;
  - anything else -> engine stopped (it unmounts on SIGTERM).

The engine is never required: without the binary or /dev/fuse it simply
isn't started and onboarding/connection checks report why.
"""

import json
import os
import secrets
import subprocess
import tempfile

ENGINE_BIN = os.environ.get('CLID_ENGINE_BIN', '/app/bin/clid-engine')
PROGRAM = 'mount_engine'


def _config_dir() -> str:
    return os.environ.get('USER_CONFIG', '/user/config')


def config_path() -> str:
    return os.path.join(_config_dir(), 'engine.json')


def _load_settings() -> dict:
    try:
        with open(os.path.join(_config_dir(), 'config.json')) as f:
            return json.load(f)
    except Exception:
        return {}


def selected(settings: dict = None) -> bool:
    """True when the user picked the built-in mount as the usenet backend."""
    up = (settings if settings is not None else _load_settings()).get('Usenet Provider') or {}
    return bool(up.get('enabled')) and (up.get('provider') or '').strip().lower() == 'builtin'


def problems(settings: dict = None) -> list:
    """Reasons the engine can't run here (empty when it can)."""
    s = settings if settings is not None else _load_settings()
    out = []
    if not os.access(ENGINE_BIN, os.X_OK):
        out.append(f'engine binary missing ({ENGINE_BIN}); this image has no built-in mount')
    if not os.path.exists('/dev/fuse'):
        out.append('/dev/fuse is not available: add `devices: [/dev/fuse]` and `cap_add: [SYS_ADMIN]` to the container')
    bm = s.get('Built-in Mount') or {}
    if not (bm.get('primary_host') or '').strip():
        out.append('no usenet server set (Built-in Mount → primary host)')
    try:
        additional_servers(bm)
    except ValueError as exc:
        out.append(f'Built-in Mount → additional servers: {exc}')
    return out


def _provider(bm: dict, prefix: str, backup: bool) -> dict:
    host = (bm.get(f'{prefix}_host') or '').strip()
    return {
        'name': host or prefix,
        'host': host,
        'port': int(bm.get(f'{prefix}_port') or 563),
        'tls': bool(bm.get(f'{prefix}_ssl', True)),
        'username': bm.get(f'{prefix}_username') or '',
        'password': bm.get(f'{prefix}_password') or '',
        'max_conns': max(1, int(bm.get(f'{prefix}_connections') or (5 if backup else 20))),
        'backup': backup,
    }


def additional_servers(bm: dict) -> list:
    """Engine providers from the `additional_servers` JSON list. Raises
    ValueError describing the first bad entry."""
    raw = bm.get('additional_servers')
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f'not valid JSON ({exc})') from None
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError('must be a JSON list of servers')
    out = []
    for i, e in enumerate(raw, 1):
        if not isinstance(e, dict) or not str(e.get('host') or '').strip():
            raise ValueError(f'entry {i} needs a "host"')
        try:
            port = int(e.get('port') or 563)
            conns = max(1, int(e.get('connections') or e.get('max_connections') or 5))
        except (TypeError, ValueError):
            raise ValueError(f'entry {i}: port and connections must be numbers') from None
        ssl = e.get('ssl', e.get('tls', True))
        out.append({
            'name': str(e.get('name') or e['host']).strip(),
            'host': str(e['host']).strip(),
            'port': port,
            'tls': ssl if isinstance(ssl, bool) else str(ssl).lower() not in ('false', '0', 'no', ''),
            'username': str(e.get('username') or ''),
            'password': str(e.get('password') or ''),
            'max_conns': conns,
            'backup': bool(e.get('backup', False)),
        })
    return out


def build_config(settings: dict) -> dict:
    """The engine config for these settings (token must already be set)."""
    bm = settings.get('Built-in Mount') or {}
    providers = [_provider(bm, 'primary', False)]
    if (bm.get('backup_host') or '').strip():
        providers.append(_provider(bm, 'backup', True))
    try:
        providers += additional_servers(bm)
    except ValueError:
        pass  # problems() reports it; sync() never gets here with it
    cache_gb = int(bm.get('cache_size_gb') or 0)
    return {
        'data_dir': os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'engine'),
        'cache_dir': bm.get('cache_dir') or '/user/cache/engine',
        'cache_bytes': cache_gb * (1 << 30),
        'mount_path': (bm.get('mount_path') or '/mnt/clid').rstrip('/'),
        'listen': f"127.0.0.1:{int(bm.get('api_port') or 8283)}",
        'token': resolve_token(settings),
        'read_ahead': int(bm.get('read_ahead_segments') or 8),
        'allow_other': bool(bm.get('allow_other', True)),
        'uid': int(os.environ.get('PUID', '0') or 0),
        'gid': int(os.environ.get('PGID', '0') or 0),
        'providers': providers,
    }


def current_token() -> str:
    """Token in the existing engine config, if any."""
    try:
        with open(config_path()) as f:
            return json.load(f).get('token') or ''
    except Exception:
        return ''


def resolve_token(settings: dict) -> str:
    """The setting if set, else the token already in use, else a new one.
    Kept in engine.json only, so config.json is never rewritten here."""
    bm = settings.get('Built-in Mount') or {}
    return bm.get('api_token') or current_token() or secrets.token_urlsafe(24)


def write_config(cfg: dict, path: str = None) -> bool:
    """Write the engine config atomically (mode 0600). Returns True when
    the content changed."""
    path = path or config_path()
    data = json.dumps(cfg, indent=1, sort_keys=True)
    try:
        with open(path) as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.engine-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def _supervisorctl(*args) -> str:
    try:
        r = subprocess.run(['supervisorctl', *args], capture_output=True, text=True, timeout=60)
        return (r.stdout or '') + (r.stderr or '')
    except Exception as exc:
        return f'supervisorctl failed: {exc}'


def state() -> str:
    """RUNNING, STOPPED, STARTING, FATAL, ... or UNKNOWN."""
    out = _supervisorctl('status', PROGRAM)
    for word in ('RUNNING', 'STARTING', 'STOPPED', 'FATAL', 'BACKOFF', 'EXITED'):
        if word in out:
            return word
    return 'UNKNOWN'


def _stop_flag() -> str:
    return os.path.join(_config_dir(), 'engine.stopped')


def stopped_by_user() -> bool:
    """True after the user pressed Stop on the mount; startup and settings
    saves then leave it stopped until they press Start."""
    return os.path.exists(_stop_flag())


def user_stop() -> str:
    os.makedirs(_config_dir(), exist_ok=True)
    with open(_stop_flag(), 'w') as f:
        f.write('stopped from the cli_debrid UI\n')
    if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
        _supervisorctl('stop', PROGRAM)
    return 'stopped'


def user_start(settings: dict = None) -> str:
    try:
        os.unlink(_stop_flag())
    except FileNotFoundError:
        pass
    return sync(settings)


def sync(settings: dict = None) -> str:
    """Bring the engine in line with the settings. Returns what happened.

    Only the mount's own settings (and selecting/deselecting it) restart or
    stop it: stopping the program, saving other settings or restarting the
    web app never touch the mount."""
    s = settings if settings is not None else _load_settings()
    if not selected(s):
        if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
            _supervisorctl('stop', PROGRAM)
            return 'stopped'
        return 'not selected'
    issues = problems(s)
    if issues:
        if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
            _supervisorctl('stop', PROGRAM)
        return 'cannot start: ' + '; '.join(issues)
    changed = write_config(build_config(s))
    if stopped_by_user():
        if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
            _supervisorctl('stop', PROGRAM)
        return 'stopped by user'
    st = state()
    if st not in ('RUNNING', 'STARTING'):
        _supervisorctl('start', PROGRAM)
        return 'started'
    if changed:
        _supervisorctl('restart', PROGRAM)
        return 'restarted'
    return 'running'
