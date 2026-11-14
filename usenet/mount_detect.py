"""Find the usenet mount's URL and paths for Settings → Usenet Mount → Detect.

Only suggests values: Settings shows what was found and the user picks it (a field is
filled on its own only when it is empty and exactly one match was found).
"""
import logging
import os
import socket
import struct
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import requests

PROBE_TIMEOUT = 1.5

# Host names a compose stack commonly gives these containers.
SERVICE_NAMES = {
    'climount': ['climount', 'cli_mount', 'cli-mount', 'decypharr'],
    'nzbdav': ['nzbdav'],
    'zurg': ['zurg'],
}
PORTS = {
    'climount': [8888, 8282, 18282],
    'nzbdav': [3000],
    'zurg': [9999],
}
# Where the docs tell users to bind cli_mount's data folder.
DATA_PATH_GUESSES = ['/climount_data', '/cli_mount_data']


def _default_gateway() -> Optional[str]:
    """The container's default gateway (the Docker host), from /proc/net/route."""
    try:
        with open('/proc/net/route') as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == '00000000' and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack('<L', int(fields[2], 16)))
    except (OSError, ValueError):
        pass
    return None


def candidate_urls(provider: str, current_url: str = '') -> List[str]:
    hosts = list(SERVICE_NAMES.get(provider, []))
    hosts += ['host.docker.internal']
    gw = _default_gateway()
    if gw:
        hosts.append(gw)
    hosts += ['localhost', '127.0.0.1']
    ports = PORTS.get(provider, [])
    urls = []
    if current_url:
        urls.append(current_url.rstrip('/'))
    for host in hosts:
        for port in ports:
            url = f'http://{host}:{port}'
            if url not in urls:
                urls.append(url)
    return urls


def probe(provider: str, base_url: str, token: str = '') -> Tuple[bool, str]:
    """(found, version) using the backend's own handshake."""
    try:
        if provider == 'climount':
            headers = {'Accept': 'application/json'}
            if token:
                headers['Authorization'] = f'Bearer {token}'
            r = requests.get(f'{base_url}/version', headers=headers, timeout=PROBE_TIMEOUT)
            if r.status_code != 200:
                return False, ''
            try:
                data = r.json()
            except ValueError:
                return False, ''
            version = data.get('version', '') if isinstance(data, dict) else ''
            return True, str(version or '')
        params = {'mode': 'version', 'output': 'json'}
        if token:
            params['apikey'] = token
        r = requests.get(f'{base_url}/api', params=params, timeout=PROBE_TIMEOUT)
        if r.status_code != 200:
            return False, ''
        data = r.json()
        if isinstance(data, dict) and data.get('version'):
            return True, str(data['version'])
    except (requests.RequestException, ValueError):
        pass
    return False, ''


def detect_urls(provider: str, current_url: str = '', token: str = '') -> List[Dict[str, str]]:
    urls = candidate_urls(provider, current_url)
    found = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for url, (ok, version) in zip(urls, pool.map(lambda u: probe(provider, u, token), urls)):
            if ok:
                found.append({'url': url, 'version': version})
    # The same server answers on several names (localhost and 127.0.0.1...): keep one per version+port.
    seen, unique = set(), []
    for item in found:
        key = (item['version'], item['url'].rsplit(':', 1)[-1])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def parse_mountinfo(text: str) -> List[Tuple[str, str]]:
    """(mount point, filesystem type) for each line of /proc/self/mountinfo."""
    mounts = []
    for line in text.splitlines():
        parts = line.split(' - ', 1)
        if len(parts) != 2:
            continue
        left, right = parts[0].split(), parts[1].split()
        if len(left) < 5 or not right:
            continue
        mount_point = left[4].replace('\\040', ' ')
        mounts.append((mount_point, right[0]))
    return mounts


def _read_mountinfo() -> List[Tuple[str, str]]:
    try:
        with open('/proc/self/mountinfo') as f:
            return parse_mountinfo(f.read())
    except OSError:
        return []


_SYSTEM_PREFIXES = ('/proc', '/sys', '/dev', '/etc', '/run', '/usr', '/var/lib', '/boot')


def _looks_like_climount_data(path: str) -> bool:
    return (os.path.isfile(os.path.join(path, 'config.json'))
            and (os.path.isdir(os.path.join(path, 'usenet')) or os.path.isdir(os.path.join(path, 'db'))))


def detect_data_paths(mounts: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """cli_mount data folders bound into this container."""
    if mounts is None:
        mounts = _read_mountinfo()
    candidates = list(DATA_PATH_GUESSES)
    for mount_point, fstype in mounts:
        if fstype.startswith('fuse') or mount_point == '/' or mount_point.startswith(_SYSTEM_PREFIXES):
            continue
        candidates.append(mount_point)
    found = []
    for path in candidates:
        if path not in found and _looks_like_climount_data(path):
            found.append(path)
    return found


def detect_mount_paths(mounts: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    """FUSE (rclone/WebDAV) mounts visible in this container."""
    if mounts is None:
        mounts = _read_mountinfo()
    found = []
    for mount_point, fstype in mounts:
        if fstype.startswith('fuse') and not mount_point.startswith(_SYSTEM_PREFIXES) and mount_point not in found:
            found.append(mount_point)
    return found


def detect(provider: str, current_url: str = '', token: str = '') -> Dict[str, list]:
    provider = provider if provider in PORTS else 'climount'
    result = {'urls': [], 'data_paths': [], 'mount_paths': []}
    try:
        result['urls'] = detect_urls(provider, current_url, token)
    except Exception as e:
        logging.warning(f"[MountDetect] URL probe failed: {e}")
    mounts = _read_mountinfo()
    if provider == 'climount':
        result['data_paths'] = detect_data_paths(mounts)
    else:
        # NzbDAV's library is the content/ folder inside its mount; Zurg's is __all__.
        sub = 'content' if provider == 'nzbdav' else '__all__'
        result['mount_paths'] = [os.path.join(m, sub) if os.path.isdir(os.path.join(m, sub)) else m
                                 for m in detect_mount_paths(mounts)]
    return result
