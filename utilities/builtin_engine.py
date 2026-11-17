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
    return out


def _provider(bm: dict, prefix: str, backup: bool) -> dict:
    return {
        'name': prefix,
        'host': (bm.get(f'{prefix}_host') or '').strip(),
        'port': int(bm.get(f'{prefix}_port') or 563),
        'tls': bool(bm.get(f'{prefix}_ssl', True)),
        'username': bm.get(f'{prefix}_username') or '',
        'password': bm.get(f'{prefix}_password') or '',
        'max_conns': max(1, int(bm.get(f'{prefix}_connections') or (5 if backup else 20))),
        'backup': backup,
    }


def build_config(settings: dict) -> dict:
    """The engine config for these settings (token must already be set)."""
    bm = settings.get('Built-in Mount') or {}
    providers = [_provider(bm, 'primary', False)]
    if (bm.get('backup_host') or '').strip():
        providers.append(_provider(bm, 'backup', True))
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


def sync(settings: dict = None) -> str:
    """Bring the engine in line with the settings. Returns what happened."""
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
    st = state()
    if st not in ('RUNNING', 'STARTING'):
        _supervisorctl('start', PROGRAM)
        return 'started'
    if changed:
        _supervisorctl('restart', PROGRAM)
        return 'restarted'
    return 'running'
