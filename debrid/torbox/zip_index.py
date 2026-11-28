"""Files inside a TorBox zip, for torrents TorBox stores as one .zip.

TorBox's allow_zip (default true) stores a torrent with 100+ files as a
single .zip. cli_debrid adds with allow_zip=false, but a hash someone else
cached zipped first is handed back zipped anyway (Pokemon S01-S19 SRS:
one 686 GB zip of 2,847 files). A zip TorBox builds is *stored*
(uncompressed), so every member is a plain byte range of the zip: the
mount streams it from the zip's own link and the matcher sees the real
episode files instead of one archive.

The index (member name, size, data offset) is read once from the zip's
central directory with a few HTTP range requests and kept on disk; it
never changes for a given torrent file.
"""
import json
import logging
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

import requests

#: Members are listed only up to this many (a zip TorBox makes is a
#: torrent's files; anything bigger is not a media pack).
MAX_MEMBERS = 100_000
#: Local headers read one by one when a member's data offset can't be
#: derived from the directory (data descriptors, odd layouts).
_PARALLEL = 8
_TIMEOUT = 60

_lock = threading.Lock()
_mem: Dict[str, Optional[List[Dict]]] = {}


def zip_only(files: List[Dict]) -> Optional[Dict]:
    """The torrent's single .zip file, when that is all it has."""
    if len(files) != 1:
        return None
    f = files[0]
    name = str(f.get('path') or f.get('name') or '')
    return f if name.lower().endswith('.zip') and int(f.get('bytes') or 0) > 0 else None


def _cache_dir() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'torbox_zip_index')


def _key(hash_value: str, file_id, size: int) -> str:
    return f"{(hash_value or 'nohash').lower()}_{file_id}_{int(size)}"


def cached(hash_value: str, file_id, size: int) -> Optional[List[Dict]]:
    """The stored index, or None when it was never built."""
    key = _key(hash_value, file_id, size)
    with _lock:
        if key in _mem:
            return _mem[key]
    try:
        with open(os.path.join(_cache_dir(), key + '.json')) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    members = data.get('members') if isinstance(data, dict) else None
    if not isinstance(members, list):
        return None
    with _lock:
        _mem[key] = members
    return members


def _store(key: str, members: List[Dict]) -> None:
    with _lock:
        _mem[key] = members
    path = os.path.join(_cache_dir(), key + '.json')
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(path + '.tmp', 'w') as fh:
            json.dump({'members': members}, fh)
        os.replace(path + '.tmp', path)
    except OSError as exc:
        logging.warning(f'[torbox zip] index not saved ({key}): {exc}')


def index(hash_value: str, file_id, size: int, get_url: Callable[[], str]) -> Optional[List[Dict]]:
    """The zip's streamable members [{path, size, offset}], building and
    storing the index on first use. None when the zip can't be read or has
    no stored members (compressed/encrypted members are left out)."""
    got = cached(hash_value, file_id, size)
    if got is not None:
        return got
    key = _key(hash_value, file_id, size)
    try:
        members = read_index(get_url(), int(size))
    except Exception as exc:
        logging.warning(f'[torbox zip] reading the index of {key} failed: {exc}')
        return None
    if not members:
        return None
    _store(key, members)
    logging.info(f'[torbox zip] indexed {key}: {len(members)} files')
    return members


# ---- zip parsing ------------------------------------------------------------

class _Range:
    def __init__(self, url: str, size: int):
        self.url, self.size = url, size
        self.session = requests.Session()

    def read(self, start: int, length: int) -> bytes:
        if start < 0 or length <= 0 or start + length > self.size:
            raise ValueError(f'range {start}+{length} outside the zip ({self.size})')
        last = None
        for attempt in range(3):
            try:
                with self.session.get(
                    self.url,
                    headers={'Range': f'bytes={start}-{start + length - 1}'},
                    timeout=_TIMEOUT,
                    stream=True,
                ) as r:
                    if r.status_code == 206:
                        buf = bytearray()
                        for chunk in r.iter_content(chunk_size=min(length, 65536)):
                            if not chunk:
                                continue
                            need = length - len(buf)
                            buf.extend(chunk[:need])
                            if len(buf) >= length:
                                break
                        data = bytes(buf)
                        if len(data) != length:
                            raise ValueError(f'range request returned {len(data)} of {length} bytes')
                        return data
                    last = ValueError(f'range request answered {r.status_code}')
            except requests.RequestException as exc:
                last = exc
            time.sleep(1 + 2 * attempt)
        raise last


def _zip64(extra: bytes, usize: int, csize: int, local: int):
    i = 0
    while i + 4 <= len(extra):
        tag, n = struct.unpack_from('<HH', extra, i)
        body = extra[i + 4:i + 4 + n]
        if tag == 0x0001:
            j = 0
            vals = []
            for want in (usize == 0xFFFFFFFF, csize == 0xFFFFFFFF, local == 0xFFFFFFFF):
                if want and j + 8 <= len(body):
                    vals.append(struct.unpack_from('<Q', body, j)[0])
                    j += 8
                else:
                    vals.append(None)
            usize = vals[0] if vals[0] is not None else usize
            csize = vals[1] if vals[1] is not None else csize
            local = vals[2] if vals[2] is not None else local
            break
        i += 4 + n
    return usize, csize, local


def read_index(url: str, size: int) -> List[Dict]:
    r = _Range(url, size)
    tail_len = min(size, 22 + 65535)
    tail = r.read(size - tail_len, tail_len)
    i = tail.rfind(b'PK\x05\x06')
    if i < 0 or len(tail) - i < 22:
        raise ValueError('end of central directory not found')
    eocd_at = size - tail_len + i
    entries, cd_size, cd_off = struct.unpack_from('<H', tail, i + 10)[0], *struct.unpack_from('<II', tail, i + 12)
    if entries == 0xFFFF or cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        j = eocd_at - 20 - (size - tail_len)
        if j < 0 or tail[j:j + 4] != b'PK\x06\x07':
            raise ValueError('zip64 locator missing')
        rec_off = struct.unpack_from('<Q', tail, j + 8)[0]
        rec = r.read(rec_off, 56)
        if rec[:4] != b'PK\x06\x06':
            raise ValueError('zip64 end record missing')
        entries, cd_size, cd_off = struct.unpack_from('<QQQ', rec, 32)
    if entries > MAX_MEMBERS or cd_off + cd_size > size:
        raise ValueError(f'central directory out of range ({entries} entries)')
    cd = r.read(cd_off, cd_size)

    rows = []  # every entry, for the next-local-header boundaries
    k = 0
    for _ in range(entries):
        if cd[k:k + 4] != b'PK\x01\x02':
            raise ValueError('bad central directory entry')
        flags, method = struct.unpack_from('<HH', cd, k + 8)
        csize, usize = struct.unpack_from('<II', cd, k + 20)
        nlen, elen, clen = struct.unpack_from('<HHH', cd, k + 28)
        local = struct.unpack_from('<I', cd, k + 42)[0]
        raw = cd[k + 46:k + 46 + nlen]
        name = raw.decode('utf-8' if flags & 0x800 else 'cp437', 'replace').replace('\\', '/')
        usize, csize, local = _zip64(cd[k + 46 + nlen:k + 46 + nlen + elen], usize, csize, local)
        k += 46 + nlen + elen + clen
        rows.append({'name': name, 'nlen': nlen, 'flags': flags, 'method': method,
                     'csize': csize, 'usize': usize, 'local': local})

    order = sorted(rows, key=lambda x: x['local'])
    for a, b in zip(order, order[1:] + [None]):
        a['next'] = b['local'] if b else cd_off

    want = [x for x in rows if not x['name'].endswith('/') and x['usize'] > 0]
    skipped = [x['name'] for x in want if x['method'] != 0 or x['flags'] & 1 or x['csize'] != x['usize']]
    if skipped:
        logging.info(f'[torbox zip] {len(skipped)} compressed/encrypted files left out, e.g. {skipped[0]}')
    want = [x for x in want if x['method'] == 0 and not x['flags'] & 1 and x['csize'] == x['usize']]

    # Stored and without a data descriptor, a member's data ends where the
    # next local header (or the directory) starts.
    unknown = []
    for x in want:
        x['data'] = None
        if not x['flags'] & 0x08:
            start = x['next'] - x['usize']
            gap = start - x['local'] - 30 - x['nlen']
            if 0 <= gap <= 0xFFFF:
                x['data'] = start
        if x['data'] is None:
            unknown.append(x)

    def local_header(x):
        lh = r.read(x['local'], 30)
        if lh[:4] != b'PK\x03\x04':
            raise ValueError(f'local header missing for {x["name"]}')
        n, e = struct.unpack_from('<HH', lh, 26)
        return x['local'] + 30 + n + e

    # Spot-check derived offsets against real local headers; any mismatch
    # means the layout isn't what the shortcut assumes: read them all.
    derived = [x for x in want if x['data'] is not None]
    sample = [derived[i] for i in sorted({0, len(derived) // 2, len(derived) - 1})] if derived else []
    if any(local_header(x) != x['data'] for x in sample):
        logging.info('[torbox zip] derived offsets did not match the local headers; reading every header')
        unknown = want
    if unknown:
        with ThreadPoolExecutor(_PARALLEL) as pool:
            for x, start in zip(unknown, pool.map(local_header, unknown)):
                x['data'] = start

    out = []
    for x in want:
        if x['data'] + x['usize'] > size:
            continue
        out.append({'path': x['name'].lstrip('/'), 'size': x['usize'], 'offset': x['data']})
    return out


def expand(zip_file: Dict, members: List[Dict]) -> List[Dict]:
    """The zip's members as torrent files (the _normalize_files shape).
    They keep the zip's file id: links are minted for the zip, and
    zip_offset/zip_size say where in it each member's bytes lie. Paths stay
    under the zip's folder, so the torrent's folder name doesn't change."""
    zpath = str(zip_file.get('path') or zip_file.get('name') or '').replace('\\', '/')
    root = zpath.rsplit('/', 1)[0] if '/' in zpath else ''
    out = []
    for m in members:
        path = f"{root}/{m['path']}" if root else m['path']
        out.append({
            'id': zip_file.get('id'),
            'path': path,
            'name': os.path.basename(path),
            'bytes': int(m['size']),
            'selected': True,
            'zip_offset': int(m['offset']),
            'zip_size': int(zip_file.get('bytes') or 0),
        })
    return out
