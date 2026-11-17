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
import sys
import tempfile
import time

IS_WINDOWS = sys.platform == 'win32'
PROGRAM = 'mount_engine'


def _default_bin() -> str:
    if not IS_WINDOWS:
        return '/app/bin/clid-engine'
    # Windows build: shipped next to cli_debrid.exe; from source: engine/.
    here = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else \
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'engine')
    return os.path.join(here, 'clid-engine.exe')


ENGINE_BIN = os.environ.get('CLID_ENGINE_BIN') or _default_bin()

# WinFsp (https://winfsp.dev) provides FUSE on Windows.
_WINFSP_DLLS = [os.path.join(os.environ.get(v, ''), 'WinFsp', 'bin', 'winfsp-x64.dll')
                for v in ('ProgramFiles(x86)', 'ProgramFiles') if os.environ.get(v)]


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


def alongside(settings: dict = None) -> bool:
    """True while migrating: the engine runs next to cli_mount (on its own
    mount path) even though cli_mount is still the usenet backend."""
    s = settings if settings is not None else _load_settings()
    return bool((s.get('Built-in Mount') or {}).get('run_alongside'))


def wanted(settings: dict = None) -> bool:
    """Should the engine run: it is the backend, or a migration is staged."""
    s = settings if settings is not None else _load_settings()
    return selected(s) or alongside(s)


def problems(settings: dict = None) -> list:
    """Reasons the engine can't run here (empty when it can)."""
    s = settings if settings is not None else _load_settings()
    out = []
    if not (os.path.isfile(ENGINE_BIN) if IS_WINDOWS else os.access(ENGINE_BIN, os.X_OK)):
        out.append(f'engine binary missing ({ENGINE_BIN}); this build has no built-in mount')
    if IS_WINDOWS:
        if not any(os.path.exists(p) for p in _WINFSP_DLLS):
            out.append('WinFsp is not installed: get it from https://winfsp.dev (the built-in mount uses it on Windows)')
    elif not os.path.exists('/dev/fuse'):
        out.append('/dev/fuse is not available: add `devices: [/dev/fuse]` and `cap_add: [SYS_ADMIN]` to the container')
    bm = s.get('Built-in Mount') or {}
    try:
        if not servers(bm):
            out.append('no usenet server set (Mount page → Usenet servers)')
    except ValueError as exc:
        out.append(f'usenet servers: {exc}')
    return out


def servers(bm: dict) -> list:
    """Engine providers from the `servers` JSON list (order kept; the engine
    asks backups last). Raises ValueError describing the first bad entry."""
    raw = bm.get('servers')
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
            raise ValueError(f'server {i} needs a host')
        try:
            port = int(e.get('port') or 563)
            conns = max(1, int(e.get('connections') or e.get('max_connections') or 10))
        except (TypeError, ValueError):
            raise ValueError(f'server {i}: port and connections must be numbers') from None
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


def mount_path(bm: dict) -> str:
    """The configured mount path; on Windows a Linux-style path (the default
    /mnt/clid) becomes drive M:."""
    p = (bm.get('mount_path') or '/mnt/clid').strip()
    if IS_WINDOWS and p.startswith('/'):
        return 'M:'
    return p.rstrip('/\\') if len(p) > 3 else p


def _int(v, default: int) -> int:
    try:
        return default if v in (None, '') else int(v)
    except (TypeError, ValueError):
        return default


def build_config(settings: dict) -> dict:
    """The engine config for these settings (token must already be set)."""
    bm = settings.get('Built-in Mount') or {}
    try:
        providers = servers(bm)
    except ValueError:
        providers = []  # problems() reports it; sync() never gets here with it
    warm = _int(bm.get('warm_connections'), 2)
    reserve_pct = _int(bm.get('playback_reserve_percent'), 10)
    for p in providers:
        p['min_idle'] = 0 if p['backup'] else min(max(warm, 0), p['max_conns'])
        p['reserve'] = 0 if reserve_pct <= 0 or p['max_conns'] < 2 else max(1, round(p['max_conns'] * reserve_pct / 100))
    cache_gb = int(bm.get('cache_size_gb') or 0)

    def num(key, default):
        try:
            v = bm.get(key)
            return default if v in (None, '') else int(v)
        except (TypeError, ValueError):
            return default

    def ident(key, env):
        v = str(bm.get(key) or '').strip() or os.environ.get(env, '0') or '0'
        try:
            return int(v)
        except ValueError:
            return 0
    try:
        umask = int(str(bm.get('umask') or '0').strip() or '0', 8) & 0o777
    except ValueError:
        umask = 0
    return {
        'data_dir': os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'engine'),
        'cache_dir': bm.get('cache_dir') or '/user/cache/engine',
        'cache_bytes': cache_gb * (1 << 30),
        'mem_cache_bytes': max(64, num('buffer_memory_mb', 512)) << 20,
        'cache_expiry_seconds': num('cache_expiry_hours', 0) * 3600,
        'cache_cleanup_seconds': max(1, num('cache_cleanup_minutes', 1)) * 60,
        'mount_path': mount_path(bm),
        'listen': f"127.0.0.1:{int(bm.get('api_port') or 8283)}",
        'token': resolve_token(settings),
        'read_ahead_mb': max(1, num('read_ahead_mb', 16)),
        'precache_mb': max(0, num('precache_mb', 2)),
        'max_conns_per_file': num('max_connections_per_file', 15),
        'parallel': max(1, num('processing_connections', 8)),
        'import_timeout_seconds': num('processing_timeout_minutes', 5) * 60,
        'import_sample_percent': min(100, num('import_sample_percent', 1)),
        'health_sample_percent': min(100, num('health_sample_percent', 0)),
        'idle_timeout_seconds': max(1, num('idle_timeout_minutes', 5)) * 60,
        'allow_other': bool(bm.get('allow_other', True)),
        'uid': ident('uid', 'PUID'),
        'gid': ident('gid', 'PGID'),
        'umask': umask,
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
    if IS_WINDOWS:
        return _proc_state()
    out = _supervisorctl('status', PROGRAM)
    for word in ('RUNNING', 'STARTING', 'STOPPED', 'FATAL', 'BACKOFF', 'EXITED'):
        if word in out:
            return word
    return 'UNKNOWN'


def control(action: str) -> str:
    """start, stop or restart the engine process (supervisord in Docker; a
    detached process on Windows, which keeps running across cli_debrid
    restarts just like the supervised one)."""
    if IS_WINDOWS:
        return {'start': _proc_start, 'stop': _proc_stop, 'restart': _proc_restart}[action]()
    return _supervisorctl(action, PROGRAM)


# ---- Windows: detached process -------------------------------------------

def _pid_path() -> str:
    return os.path.join(_config_dir(), 'engine.pid')


def _proc():
    """The running engine process, or None."""
    try:
        import psutil
        with open(_pid_path()) as f:
            p = psutil.Process(int(f.read().strip()))
        if p.is_running() and 'clid-engine' in (p.name() or '').lower():
            return p
    except Exception:
        pass
    return None


def _proc_state() -> str:
    return 'RUNNING' if _proc() is not None else 'STOPPED'


def _log_path() -> str:
    return os.path.join(os.environ.get('USER_LOGS', '/user/logs'), 'mount_engine.log')


def _proc_start() -> str:
    if _proc() is not None:
        return 'already running'
    log = _log_path()
    os.makedirs(os.path.dirname(log), exist_ok=True)
    try:
        if os.path.getsize(log) > 50 << 20:
            os.replace(log, log + '.1')
    except OSError:
        pass
    flags = 0
    for name in ('DETACHED_PROCESS', 'CREATE_NEW_PROCESS_GROUP', 'CREATE_NO_WINDOW'):
        flags |= getattr(subprocess, name, 0)
    with open(log, 'ab') as out:
        p = subprocess.Popen([ENGINE_BIN, '--config', config_path()], stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    with open(_pid_path(), 'w') as f:
        f.write(str(p.pid))
    return 'started'


def _proc_stop() -> str:
    p = _proc()
    if p is None:
        return 'not running'
    try:  # graceful: the engine unmounts, then exits
        import urllib.request
        with open(config_path()) as f:
            cfg = json.load(f)
        req = urllib.request.Request(f"http://{cfg['listen']}/shutdown", method='POST',
                                     headers={'Authorization': f"Bearer {cfg['token']}"})
        urllib.request.urlopen(req, timeout=5).read()
        p.wait(timeout=20)
    except Exception:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            pass
    return 'stopped'


def _proc_restart() -> str:
    _proc_stop()
    time.sleep(1)
    return _proc_start()


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
        control('stop')
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
    if not wanted(s):
        if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
            control('stop')
            return 'stopped'
        return 'not selected'
    issues = problems(s)
    if issues:
        if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
            control('stop')
        return 'cannot start: ' + '; '.join(issues)
    changed = write_config(build_config(s))
    if stopped_by_user():
        if state() in ('RUNNING', 'STARTING', 'BACKOFF'):
            control('stop')
        return 'stopped by user'
    st = state()
    if st not in ('RUNNING', 'STARTING'):
        control('start')
        return 'started'
    if changed:
        control('restart')
        return 'restarted'
    return 'running'
