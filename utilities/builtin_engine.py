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
        with open(os.path.join(_config_dir(), 'config.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def usenet_selected(settings: dict = None) -> bool:
    """True when the user picked the built-in mount as the usenet backend."""
    up = (settings if settings is not None else _load_settings()).get('Usenet Provider') or {}
    return bool(up.get('enabled')) and (up.get('provider') or '').strip().lower() == 'builtin'


def selected(settings: dict = None) -> bool:
    """True when the built-in mount is in use: the usenet backend, or turned
    on for debrid alone (Built-in Mount.enabled)."""
    s = settings if settings is not None else _load_settings()
    return usenet_selected(s) or bool((s.get('Built-in Mount') or {}).get('enabled'))


def alongside(settings: dict = None) -> bool:
    """True while migrating: the engine runs next to cli_mount (on its own
    mount path) even though cli_mount is still the usenet backend."""
    s = settings if settings is not None else _load_settings()
    return bool((s.get('Built-in Mount') or {}).get('run_alongside'))


def wanted(settings: dict = None) -> bool:
    """Should the engine run: it is the backend, or a migration is staged."""
    s = settings if settings is not None else _load_settings()
    return selected(s) or alongside(s)


def host_problems() -> list:
    """Reasons this container/machine can't run the engine at all (no
    settings involved): missing binary, /dev/fuse or WinFsp."""
    out = []
    if not (os.path.isfile(ENGINE_BIN) if IS_WINDOWS else os.access(ENGINE_BIN, os.X_OK)):
        out.append(f'engine binary missing ({ENGINE_BIN}); this build has no built-in mount')
    if IS_WINDOWS:
        if not any(os.path.exists(p) for p in _WINFSP_DLLS):
            out.append('WinFsp is not installed: get it from https://winfsp.dev (the built-in mount uses it on Windows)')
    elif not os.path.exists('/dev/fuse'):
        out.append('/dev/fuse is not available: add `devices: [/dev/fuse]` and `cap_add: [SYS_ADMIN]` to the container')
    return out


def problems(settings: dict = None) -> list:
    """Reasons the engine can't run here (empty when it can)."""
    s = settings if settings is not None else _load_settings()
    out = host_problems()
    bm = s.get('Built-in Mount') or {}
    if bm.get('webdav_enabled') and not (str(bm.get('webdav_username') or '').strip() and bm.get('webdav_password')):
        out.append('WebDAV share is on but has no username/password (Mount page → WebDAV share); it stays off')
    try:
        if not servers(bm) and (usenet_selected(s) or alongside(s)):
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
            'backbone': str(e.get('backbone') or '').strip(),
        })
    return out


def mount_path(bm: dict) -> str:
    """The configured mount path; on Windows a Linux-style path (the default
    /mnt/clid) becomes drive M:."""
    p = (bm.get('mount_path') or '/mnt/clid').strip()
    if IS_WINDOWS and p.startswith('/'):
        return 'M:'
    return p.rstrip('/\\') if len(p) > 3 else p


def all_path(bm: dict) -> str:
    """<mount>/__all__, with the platform's separator (M:\\__all__ on
    Windows, so it matches the paths os.path.join builds elsewhere)."""
    m = mount_path(bm)
    if IS_WINDOWS:
        return m.rstrip('/\\') + '\\__all__'
    return m.rstrip('/') + '/__all__'


def cache_dir(bm: dict) -> str:
    """The engine's disk cache folder. On Windows the Linux default
    (/user/cache/engine) would land in C:\\user\\cache, so it goes next to
    the other data folders instead."""
    p = (bm.get('cache_dir') or '').strip()
    if IS_WINDOWS and (not p or p.startswith('/')):
        base = os.path.dirname(os.environ.get('USER_DB_CONTENT') or os.path.join(os.getcwd(), 'db_content'))
        return os.path.join(base, 'cache', 'engine')
    return p or '/user/cache/engine'


def _int(v, default: int) -> int:
    try:
        return default if v in (None, '') else int(v)
    except (TypeError, ValueError):
        return default


def reserve(max_conns: int, pct: int, lo: int, hi: int) -> int:
    """Playback-only connections of one server: pct of its connections,
    within lo..hi, always leaving one for everything else (0 = off)."""
    if pct <= 0 or max_conns < 2:
        return 0
    return min(max(round(max_conns * pct / 100), lo), hi, max_conns - 1)


def build_config(settings: dict) -> dict:
    """The engine config for these settings (token must already be set)."""
    bm = settings.get('Built-in Mount') or {}
    try:
        providers = servers(bm)
    except ValueError:
        providers = []  # problems() reports it; sync() never gets here with it
    warm = _int(bm.get('warm_connections'), 2)
    reserve_pct = _int(bm.get('playback_reserve_percent'), 11)
    reserve_min = max(1, _int(bm.get('playback_reserve_min'), 6))
    reserve_max = max(reserve_min, _int(bm.get('playback_reserve_max'), 100))
    for p in providers:
        p['min_idle'] = 0 if p['backup'] else min(max(warm, 0), p['max_conns'])
        p['reserve'] = reserve(p['max_conns'], reserve_pct, reserve_min, reserve_max)
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
        'cache_dir': cache_dir(bm),
        'cache_bytes': cache_gb * (1 << 30),
        'mem_cache_bytes': max(64, num('buffer_memory_mb', 512)) << 20,
        'cache_expiry_seconds': num('cache_expiry_hours', 0) * 3600,
        'cache_cleanup_seconds': max(1, num('cache_cleanup_minutes', 1)) * 60,
        'mount_path': mount_path(bm),
        'listen': f"127.0.0.1:{int(bm.get('api_port') or 8283)}",
        'token': resolve_token(settings),
        'read_ahead_mb': max(1, num('read_ahead_mb', 16)),
        # Debrid streaming: the engine asks cli_debrid (this host) for links.
        'link_callback': f"http://127.0.0.1:{os.environ.get('CLI_DEBRID_PORT', '5000')}/mount/internal/link",
        'precache_mb': max(0, num('precache_mb', 2)),
        'debrid_chunk_mb': min(64, max(1, num('debrid_chunk_mb', 8))),
        'debrid_conns_per_file': min(16, max(1, num('debrid_connections_per_file', 4))),
        'debrid_read_ahead_mb': max(0, num('debrid_read_ahead_mb', 32)),
        'debrid_user_agent': str(bm.get('debrid_user_agent') or '').strip(),
        'debrid_proxy': str(bm.get('debrid_proxy') or '').strip(),
        'log_level': 'debug' if bm.get('debug_logging') else 'info',
        'allowed_extensions': allowed_extensions(bm),
        'deobfuscate_mode': {'season_ep': 'season_ep', 'index': 'index'}.get(str(bm.get('deobfuscate_mode', 'season_ep') or '').strip(), ''),
        'virtual_folders': virtual_folders(bm),
        'plex_protection': bool(bm.get('plex_playback_protection', True)) and reserve_pct > 0,
        'max_conns_per_file': num('max_connections_per_file', 15),
        'parallel': max(1, num('processing_connections', 8)),
        'import_timeout_seconds': num('processing_timeout_minutes', 5) * 60,
        'import_sample_percent': min(100, num('import_sample_percent', 1)),
        'health_sample_percent': min(100, num('health_sample_percent', 0)),
        'media_probe': bool(bm.get('media_probe', False)),
        'idle_timeout_seconds': max(1, num('idle_timeout_minutes', 5)) * 60,
        'allow_other': bool(bm.get('allow_other', True)),
        'uid': ident('uid', 'PUID'),
        'gid': ident('gid', 'PGID'),
        'umask': umask,
        'providers': providers,
        **webdav_config(bm),
    }


def webdav_config(bm: dict) -> dict:
    """The engine's WebDAV share settings (off unless enabled with a login)."""
    user, pw = str(bm.get('webdav_username') or '').strip(), str(bm.get('webdav_password') or '')
    if not bm.get('webdav_enabled') or not user or not pw:
        return {'webdav_listen': ''}
    return {'webdav_listen': f"0.0.0.0:{_int(bm.get('webdav_port'), 8284)}", 'webdav_user': user, 'webdav_password': pw}


DEFAULT_EXTENSIONS = 'mkv,mp4,m4v,avi,mov,mpg,mpeg'
VIRTUAL_FILTER_TYPES = ['include', 'exclude', 'starts_with', 'ends_with', 'not_starts_with', 'not_ends_with',
                        'regex', 'not_regex', 'exact_match', 'not_exact_match', 'size_gt', 'size_lt', 'last_added',
                        'file_count_gt', 'file_count_lt', 'files_regex', 'not_files_regex', 'tags', 'not_tags',
                        'category', 'not_category', 'protocol']
RESERVED_FOLDERS = {'__all__', '__bad__', 'nzbs', 'torrents'}


def allowed_extensions(bm: dict) -> list:
    raw = bm.get('allowed_extensions', DEFAULT_EXTENSIONS)
    raw = DEFAULT_EXTENSIONS if raw is None else str(raw)
    out = []
    for part in raw.replace(';', ',').replace(' ', ',').split(','):
        ext = part.strip().lstrip('.').lower()
        if ext and ext not in out:
            out.append(ext)
    return out


def virtual_folders(bm: dict, strict: bool = False) -> dict:
    """{folder: {filter: value}} from the stored JSON list
    [{"name": .., "filters": [{"type": .., "value": ..}]}]. strict raises
    ValueError on the first bad entry (saving); otherwise bad ones are dropped."""
    import re
    raw = bm.get('virtual_folders') or ''
    try:
        rows = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, list) else [])
    except ValueError:
        if strict:
            raise ValueError('virtual folders are not valid JSON')
        rows = []
    out = {}
    for i, row in enumerate(rows if isinstance(rows, list) else []):
        try:
            name = str((row or {}).get('name') or '').strip()
            if not name or '/' in name or '\\' in name or name in ('.', '..'):
                raise ValueError(f'virtual folder {i + 1}: needs a plain folder name')
            if name.lower() in RESERVED_FOLDERS:
                raise ValueError(f'virtual folder "{name}": that name is used by the mount itself')
            if name in out:
                raise ValueError(f'virtual folder "{name}" is listed twice')
            filters = {}
            for f in (row.get('filters') or []):
                kind, value = str(f.get('type') or ''), str(f.get('value') or '').strip()
                if not kind and not value:
                    continue
                if kind not in VIRTUAL_FILTER_TYPES:
                    raise ValueError(f'virtual folder "{name}": unknown filter "{kind}"')
                if not value:
                    raise ValueError(f'virtual folder "{name}": filter "{kind}" needs a value')
                if 'regex' in kind:
                    try:
                        re.compile(value)
                    except re.error as exc:
                        raise ValueError(f'virtual folder "{name}": bad regex {value!r}: {exc}')
                filters[kind] = value
            if not filters:
                raise ValueError(f'virtual folder "{name}": add at least one filter')
            out[name] = filters
        except (ValueError, AttributeError, TypeError) as exc:
            if strict:
                raise ValueError(str(exc))
    return out


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
        with open(path, encoding='utf-8') as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.engine-')
    try:
        if hasattr(os, 'fchmod'):  # POSIX only; on Windows the file inherits the user-only ACL
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
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


def _windows_fuse_mounted(path: str) -> bool:
    """WinFsp drive letter (M:) or folder mount: GetVolumeInformationW on its
    root names the filesystem. os.path.realpath raises WinError 1005 there
    and os.path.ismount is True for every folder on the drive, so neither
    can be used."""
    root = (path or '').rstrip('/\\')
    if not root:
        return False
    root += '\\'
    try:
        import ctypes
        fs_name = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, None, None, None, fs_name, len(fs_name))
    except Exception:
        return False
    return bool(ok) and fs_name.value.upper().startswith('FUSE')


def fuse_mounted(path: str) -> bool:
    """A FUSE filesystem (cli_mount's, live or dead) sits on path. Inside
    Docker the folder is a bind mount, so os.path.ismount() is always True
    there and can't tell cli_mount apart from the bind; /proc/self/mountinfo
    names the filesystem type. On Windows ask the volume for its
    filesystem name (WinFsp mounts report FUSE-<name>)."""
    if IS_WINDOWS:
        return _windows_fuse_mounted(path)
    target = os.path.realpath(path).rstrip('/') or '/'
    try:
        with open('/proc/self/mountinfo') as f:
            lines = f.read().splitlines()
    except OSError:
        return os.path.ismount(path)
    for line in lines:
        left, _, right = line.partition(' - ')
        parts = left.split()
        if len(parts) < 5:
            continue
        mnt = parts[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')
        if mnt.rstrip('/') == target and (right.split() or [''])[0].startswith('fuse'):
            return True
    return False



def wait_until_mounted(settings: dict = None, timeout: float = 180, poll: float = 2) -> bool:
    """Block until the built-in mount is up, for program start: after a
    container start the engine loads every job before it mounts (about a
    minute for ~8k jobs), and a program started meanwhile failed its
    connectivity check on the original files path. Returns at once when the
    built-in mount isn't in use, was stopped by hand, crashed or can't run;
    True when mounted, False on timeout."""
    import time
    s = settings if settings is not None else _load_settings()
    if not wanted(s):
        return True
    path = mount_path(s.get('Built-in Mount') or {})
    deadline = time.monotonic() + timeout
    while True:
        if fuse_mounted(path):
            return True
        # Not started yet is normal at container start: supervisord starts
        # the program before the start hook starts the engine. Give up early
        # only when it won't come: stopped by hand, crashed, or can't run.
        if (time.monotonic() >= deadline or stopped_by_user() or state() == 'FATAL'
                or problems(s)):
            return fuse_mounted(path)
        time.sleep(poll)

def startup(settings: dict = None) -> str:
    """Container/app start. With Debug.auto_run_program on, the mount comes
    up even if it was stopped by hand before (autostart means everything
    starts); otherwise a manual Stop is kept. Only when the built-in mount is
    in use: with an external mount nothing is started either way."""
    s = settings if settings is not None else _load_settings()
    if wanted(s) and (s.get('Debug') or {}).get('auto_run_program') and stopped_by_user():
        return 'autostart: ' + user_start(s)
    return sync(s)

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
