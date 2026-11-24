"""
Migrate from cli_mount to the built-in mount, side by side.

  1. The built-in engine runs alongside cli_mount (Built-in Mount.run_alongside)
     on its own mount path.
  2. Import: every job cli_mount lists (GET /api/browse/nzbs: folder name +
     job id) is added to the engine with the SAME id, folder and file names,
     from cli_mount's own metadata (<data_path>/usenet/meta/<id>.meta: a
     protobuf NZBProto, or the zstd "v2" format upstream decypharr writes
     since it rewrote its metadata). The engine reads files through cli_mount's segment
     maps, so they are byte-identical and nothing is downloaded or
     re-parsed. nzb:<id> references in cli_debrid stay valid.
  3. Compare: a sample of files is read through both mounts (size + SHA-256
     of their start, middle and end).
  4. Switch: with cli_mount stopped, the engine takes over cli_mount's mount
     path (so existing symlinks and Plex paths keep resolving) and becomes
     the usenet backend. Debrid torrents aren't imported: the engine mirrors
     the debrid accounts itself, and Switch waits until that has caught up.
  5. Switch back: with the program stopped (its queue checks would take
     jobs missing from cli_mount for dead ones), cli_mount becomes the
     backend again and the engine keeps running on its own path. Copy back
     then re-submits every usenet job cli_mount doesn't have (added since
     the switch) with the SAME id and folder name, from the NZB the engine
     stored. cli_mount re-imports it; nothing is downloaded but article
     checks. Then the program can be started again.
Nothing is moved or deleted on either side: every step copies.
"""

import base64
import hashlib
import logging
import os
import re
import random
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

# ---- cli_mount metadata (protobuf NZBProto, decoded without a schema dep) ----


def _varint(b: bytes, i: int) -> Tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _fields(b: bytes) -> List[Tuple[int, object]]:
    i, out = 0, []
    while i < len(b):
        key, i = _varint(b, i)
        f, t = key >> 3, key & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 2:
            n, i = _varint(b, i)
            v = b[i:i + n]
            i += n
        elif t == 1:
            v, i = b[i:i + 8], i + 8
        elif t == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f'unsupported protobuf wire type {t}')
        out.append((f, v))
    return out


def parse_meta(data: bytes) -> dict:
    """cli_mount's metadata -> {id, name, files: [{name, size, segments}],
    skipped: [(name, why)]}. Reads both the protobuf NZBProto and upstream
    decypharr's v2 format. Segment fields follow cli_mount: start/end are
    inclusive file offsets, data_start is where the file's bytes begin in
    the decoded article."""
    if data[:1] == bytes([META_V2_MAGIC]):
        return _parse_meta_v2(data)
    out = {'id': '', 'name': '', 'files': [], 'skipped': []}
    for f, v in _fields(data):
        if f == 1:
            out['id'] = v.decode('utf-8', 'replace')
        elif f == 2:
            out['name'] = v.decode('utf-8', 'replace')
        elif f == 9:
            meta, segs = {}, []
            for ff, vv in _fields(v):
                if ff == 6:
                    s = dict(_fields(vv))
                    segs.append({'number': s.get(1, 0), 'id': (s.get(2) or b'').decode('utf-8', 'replace'),
                                 'start': s.get(4, 0), 'end': s.get(5, 0), 'data_start': s.get(7, 0)})
                else:
                    meta[ff] = vv
            _add_file(out, (meta.get(2) or b'').decode('utf-8', 'replace'), meta.get(4, 0), segs,
                      bool(meta.get(10)), bool(meta.get(15)), meta.get(13) or b'', meta.get(14) or b'')
    return _dedupe(out)


def _add_file(out: dict, name: str, size: int, segs: list, deleted: bool, encrypted: bool,
              key: bytes, iv: bytes) -> None:
    if deleted:
        return  # deleted from the job (repair cleanup)
    if not segs:
        out['skipped'].append((name, 'no articles'))
        return
    segs.sort(key=lambda s: s['start'])
    entry = {'name': name, 'size': size, 'segments': segs}
    if encrypted:
        # Encrypted RAR member: cli_mount kept the derived AES key and IV;
        # the segments map the ciphertext, size is the plaintext.
        if len(key) not in (16, 24, 32) or len(iv) != 16:
            out['skipped'].append((name, 'encrypted archive without a usable key in cli_mount\'s metadata'))
            return
        entry['key'], entry['iv'] = key, iv
    out['files'].append(entry)


def _dedupe(out: dict) -> dict:
    # A name listed twice (seen live: a stray ~100 MB entry next to the real
    # 6-8 GB movie) would make the engine refuse the whole job: keep the largest.
    best = {}
    for f in out['files']:
        if f['name'] not in best or f['size'] > best[f['name']]['size']:
            best[f['name']] = f
    if len(best) < len(out['files']):
        keep = {id(f) for f in best.values()}
        for f in out['files']:
            if id(f) not in keep:
                out['skipped'].append((f['name'], f'duplicate name, kept the larger copy ({f["size"]} bytes left out)'))
        out['files'] = [f for f in out['files'] if id(f) in keep]
    return out


# ---- upstream decypharr's v2 metadata (pkg/usenet/nzbcodec.go) ---------------
# [0xB1] [uvarint len + zstd(header)] [uvarint len + zstd(segment columns)]
# [zstd(message ids)]. The header holds the NZB's scalars and each file's
# metadata plus its segment count; segment fields are stored column by column
# for all files in order (number, bytes, start, end, data start, group index),
# then the message ids, each length-prefixed. Upstream converts every older
# protobuf .meta to this on startup, so a current decypharr has only these.

META_V2_MAGIC = 0xB1


class _Reader:
    def __init__(self, buf: bytes):
        self.buf, self.pos = buf, 0

    def uvarint(self) -> int:
        try:
            v, self.pos = _varint(self.buf, self.pos)
        except IndexError:
            raise ValueError('v2 metadata: truncated') from None
        return v

    def varint(self) -> int:
        v = self.uvarint()
        return (v >> 1) ^ -(v & 1)  # zigzag (Go's binary.Varint)

    def span(self) -> bytes:
        n = self.uvarint()
        if self.pos + n > len(self.buf):
            raise ValueError('v2 metadata: field out of range')
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def str(self) -> str:
        return self.span().decode('utf-8', 'replace')

    def skip(self, n: int) -> None:
        if self.pos + n > len(self.buf):
            raise ValueError('v2 metadata: field out of range')
        self.pos += n

    def boolean(self) -> bool:
        self.skip(1)
        return self.buf[self.pos - 1] != 0

    def strings(self) -> List[str]:
        return [self.str() for _ in range(self.uvarint())]


def _zstd(b: bytes) -> bytes:
    import zstandard
    # Upstream's frames carry their content size, but don't rely on it.
    return zstandard.ZstdDecompressor().decompressobj().decompress(b)


def _parse_meta_v2(data: bytes) -> dict:
    r = _Reader(data)
    r.skip(1)
    hc = r.span()
    sc = r.span()
    mc = data[r.pos:]

    h = _Reader(_zstd(hc))
    out = {'id': '', 'name': '', 'files': [], 'skipped': []}
    out['id'], out['name'] = h.str(), h.str()
    h.str(), h.str()                      # title, path
    h.varint(), h.varint()                # total size, date posted
    h.str()                               # category
    h.strings()                           # groups
    h.boolean()                           # downloaded
    h.varint(), h.varint()                # added on, last activity
    h.str()                               # status
    h.skip(16)                            # progress, percentage (float64)
    h.varint(), h.varint(), h.varint()    # size downloaded, eta, speed
    h.varint()                            # completed on
    h.boolean()                           # is bad
    h.str(), h.str(), h.str()             # storage, fail message, password
    files = []
    for _ in range(h.uvarint()):
        f = {'name': h.str()}
        h.str()                           # internal path
        f['size'] = h.varint()
        h.varint()                        # start offset
        h.strings()                       # groups
        h.str(), h.str()                  # file type, password
        f['deleted'] = h.boolean()
        h.boolean()                       # is stored
        h.varint()                        # segment size
        f['key'], f['iv'] = h.span(), h.span()
        f['encrypted'] = h.boolean()
        f['count'] = h.uvarint()
        files.append(f)

    total = sum(f['count'] for f in files)
    s = _Reader(_zstd(sc))
    s.strings()                           # group table
    cols = [[s.varint() for _ in range(total)] for _ in range(5)]  # number, bytes, start, end, data start
    m = _Reader(_zstd(mc))
    ids = [m.str() for _ in range(total)]

    off = 0
    for f in files:
        segs = [{'number': cols[0][i], 'id': ids[i], 'start': cols[2][i], 'end': cols[3][i],
                 'data_start': cols[4][i]} for i in range(off, off + f['count'])]
        off += f['count']
        _add_file(out, f['name'], f['size'], segs, f['deleted'], f['encrypted'], f['key'], f['iv'])
    return _dedupe(out)


def meta_dir(data_path: str) -> str:
    for sub in ('usenet/meta', 'meta'):
        p = os.path.join(data_path or '', sub)
        if os.path.isdir(p):
            return p
    return os.path.join(data_path or '', 'usenet', 'meta')


# ---- disk space -------------------------------------------------------------
# An imported job stores the NZB rebuilt from cli_mount's map plus a byte map
# (result.json). Measured on 300 real cli_mount jobs: 1.12x the .meta size
# overall, 1.76x at p95, 2.4x at most. 1.5x over the whole import plus a
# margin keeps a large library from filling the disk the engine shares with
# cli_debrid's own database.
SPACE_FACTOR = 1.5
MIN_MARGIN = 2 << 30


def _existing(path: str) -> str:
    path = os.path.abspath(path or '/')
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


# Upstream decypharr's v2 .meta is zstd-compressed, about 4x smaller than the
# protobuf the factor above was measured on; count it at its protobuf size.
V2_SIZE_FACTOR = 4


def meta_sizes(mdir: str) -> Dict[str, int]:
    """job id -> .meta size in bytes (v2 files at their protobuf size)."""
    out = {}
    try:
        with os.scandir(mdir) as it:
            for e in it:
                if e.name.endswith('.meta'):
                    try:
                        size = e.stat().st_size
                        with open(e.path, 'rb') as f:
                            if f.read(1) == bytes([META_V2_MAGIC]):
                                size *= V2_SIZE_FACTOR
                        out[e.name[:-5]] = size
                    except OSError:
                        pass
    except OSError:
        pass
    return out


def space_plan(job_ids: List[str], sizes: Dict[str, int], data_dir: str,
               cache_dir: str = '', cache_left: int = 0) -> dict:
    """Whether importing job_ids (in order) fits next to the engine's data.

    needed: estimated bytes; free: free bytes on the data dir's disk, minus
    the disk cache's unused allowance when the cache shares that disk;
    margin: kept free (5% of the disk, at least 2 GiB); fits_jobs: how many
    of job_ids fit, in order."""
    import shutil
    d = _existing(data_dir)
    usage = shutil.disk_usage(d)
    free, reserved = usage.free, 0
    if cache_dir and cache_left > 0:
        try:
            if os.stat(_existing(cache_dir)).st_dev == os.stat(d).st_dev:
                reserved = cache_left
        except OSError:
            pass
    margin = max(MIN_MARGIN, usage.total // 20)
    room = free - reserved - margin
    needed, fits = 0, 0
    for jid in job_ids:
        needed += int(sizes.get(jid, 0) * SPACE_FACTOR)
        if needed <= room:
            fits += 1
    return {'needed': needed, 'free': free, 'cache_reserved': reserved, 'margin': margin,
            'room': max(0, room), 'fits_jobs': fits, 'jobs': len(job_ids), 'fits': needed <= room,
            'meta_bytes': sum(sizes.get(j, 0) for j in job_ids)}


def _gb(n: int) -> str:
    return f'{n / (1 << 30):.1f} GB'


def climount_repair_settings(data_path: str) -> Dict[str, object]:
    """cli_mount's repair standards as Built-in Mount settings, from its
    config.json when it is visible next to its data (empty when not): the
    ffprobe check (repair.media_probe_usenet) and the availability samples."""
    import json
    for cand in ('config.json', os.path.join('config', 'config.json'), os.path.join('..', 'config', 'config.json')):
        p = os.path.join(data_path or '', cand)
        try:
            with open(p) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        out: Dict[str, object] = {}
        rep, use = cfg.get('repair') or {}, cfg.get('usenet') or {}
        if isinstance(rep.get('media_probe_usenet'), bool):
            out['media_probe'] = rep['media_probe_usenet']
        for src, dst in (('availability_sample_percent', 'health_sample_percent'),
                         ('import_availability_sample_percent', 'import_sample_percent')):
            v = use.get(src)
            if isinstance(v, int) and 0 < v <= 100:
                out[dst] = v
        out.update(_climount_streaming_settings(cfg))
        backbones = climount_backbones(cfg)
        if backbones:
            out[BACKBONES_KEY] = backbones
        return out
    return {}


#: Key of the {host: backbone} map in climount_repair_settings' result; not
#: a setting itself: apply_backbones puts it on the matching servers.
BACKBONES_KEY = '_server_backbones'


def climount_backbones(cfg: dict) -> Dict[str, str]:
    """cli_mount's per-server backbone names ({host (lowercase): backbone})."""
    out = {}
    for p in (cfg.get('usenet') or {}).get('providers') or []:
        if isinstance(p, dict) and str(p.get('host') or '').strip() and str(p.get('backbone') or '').strip():
            out[str(p['host']).strip().lower()] = str(p['backbone']).strip()
    return out


def apply_backbones(servers: list, backbones: Dict[str, str]) -> Tuple[list, int]:
    """Servers with cli_mount's backbone set where the host matches and the
    server has none yet (a backbone typed on the Mount page wins). Returns
    (servers, how many changed)."""
    out, changed = [], 0
    for s in servers or []:
        s = dict(s) if isinstance(s, dict) else s
        if isinstance(s, dict) and not str(s.get('backbone') or '').strip():
            bb = backbones.get(str(s.get('host') or '').strip().lower())
            if bb:
                s['backbone'] = bb
                changed += 1
        out.append(s)
    return out, changed


def _size_mb(v) -> Optional[int]:
    """cli_mount size strings ("8MB", "1GB", "512KB", bare bytes) -> whole MB."""
    if isinstance(v, int):
        return max(0, v >> 20)
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*', str(v or ''), re.I)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).upper()
    mb = n * {'': 1 / (1 << 20), 'K': 1 / 1024, 'M': 1, 'G': 1024, 'T': 1 << 20}[unit]
    return int(round(mb))


def _seconds(v) -> Optional[int]:
    """Go-style durations ("10s", "5m", "3d", "1h30m") -> seconds."""
    total, rest = 0, str(v or '').strip().lower()
    if not rest:
        return None
    for n, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)', rest):
        total += float(n) * {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
    return int(total) or None


def _climount_streaming_settings(cfg: dict) -> Dict[str, object]:
    """cli_mount's Plex protection / prewarm settings (and Plex URL/token,
    which the caller only uses when cli_debrid has none)."""
    out: Dict[str, object] = {}
    use = cfg.get('usenet') or {}
    dfs = ((cfg.get('mount') or {}).get('dfs')) or {}
    if isinstance(use.get('interactive_pool_reserve_enabled'), bool):
        on = use['interactive_pool_reserve_enabled']
        out['plex_playback_protection'] = on
        pct = use.get('interactive_pool_reserve_percent')
        if not on:
            out['playback_reserve_percent'] = 0
        elif isinstance(pct, int) and 0 < pct <= 100:
            out['playback_reserve_percent'] = pct
        for src, dst in (('interactive_pool_reserve_min', 'playback_reserve_min'),
                         ('interactive_pool_reserve_max', 'playback_reserve_max')):
            if on and isinstance(use.get(src), int) and use[src] >= 0:
                out[dst] = use[src]
    if isinstance(dfs.get('prewarm_next_episode'), bool):
        out['plex_prewarm'] = dfs['prewarm_next_episode']
    if dfs.get('prewarm_max_size'):
        mb = _size_mb(dfs['prewarm_max_size'])
        if mb:
            out['prewarm_max_mb'] = mb
    for src, dst in (('chunk_size', 'debrid_chunk_mb'), ('read_ahead_size', 'debrid_read_ahead_mb')):
        mb = _size_mb(dfs.get(src)) if dfs.get(src) else None
        if mb:
            out[dst] = mb
    rd = next((d for d in cfg.get('debrids') or [] if isinstance(d, dict)
               and str(d.get('provider') or d.get('name') or '').lower().replace('-', '') == 'realdebrid'), None)
    if rd:
        secs = _seconds(rd.get('torrents_refresh_interval'))
        if secs:
            out['debrid_sync_seconds'] = max(10, secs)
        hours = (_seconds(rd.get('auto_expire_links_after')) or 0) // 3600
        if hours:
            out['debrid_link_lifetime_hours'] = hours
        keys = [k for k in rd.get('download_api_keys') or [] if isinstance(k, str) and k and k != rd.get('api_key')]
        if keys:
            out['debrid_download_api_keys'] = ', '.join(keys)
        if (rd.get('proxy') or '').strip():
            out['debrid_proxy'] = rd['proxy'].strip()
    if (dfs.get('plex_url') or '').strip() and (dfs.get('plex_token') or '').strip():
        out['plex_url'] = dfs['plex_url'].strip().rstrip('/')
        out['plex_token'] = dfs['plex_token'].strip()
    return out


# ---- job runner (progress polled by the Mount page) -------------------------

_jobs: Dict[str, dict] = {}
_lock = threading.Lock()


def _set(job_id: str, **kw):
    with _lock:
        _jobs[job_id].update(kw)


def _add(job_id: str, key: str, n: int = 1):
    with _lock:
        _jobs[job_id][key] += n


def _log(job_id: str, msg: str):
    with _lock:
        log = _jobs[job_id]['log']
        log.append(msg)
        del log[:-300]


def status(job_id: str) -> dict:
    with _lock:
        return dict(_jobs.get(job_id) or {})


def cancel(job_id: str) -> bool:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]['cancel'] = True
            return True
    return False


def start_import(source, engine, data_path: str, limit: int = 0, space: Optional[dict] = None,
                 prepare: Optional[Callable[[], object]] = None,
                 why_down: Optional[Callable[[], str]] = None) -> str:
    """Import cli_mount's library into the engine in the background.
    source: a CliMountClient; engine: a BuiltinClient. space: {'data_dir',
    'cache_dir', 'cache_left'} turns on the disk-space check. prepare: run
    first, in the import thread (the engine settings sync, which may restart
    the engine: the import then waits for it to answer again). why_down:
    returns why the engine can't come up (no servers, crashed...), or ''
    while it may still start: a reason ends the wait at once.)"""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {'kind': 'import', 'status': 'starting', 'total': 0, 'done': 0, 'imported': 0,
                         'already': 0, 'failed': 0, 'skipped_files': 0, 'complete': False, 'log': [], 'error': None}
    threading.Thread(target=_run_import, args=(job_id, source, engine, data_path, limit, space, prepare, why_down),
                     name='builtin-migrate', daemon=True).start()
    return job_id


SPACE_RECHECK_EVERY = 25
# The engine may be (re)starting when an import begins (the import's own
# settings sync restarts it): wait this long for it to answer before giving up.
ENGINE_WAIT = 90
ENGINE_POLL = 2


def _engine_reason(engine) -> str:
    return getattr(engine, 'last_error', '') or 'no answer'


def wait_for_engine(engine, timeout: Optional[float] = None, cancelled: Callable[[], bool] = lambda: False,
                    why_down: Optional[Callable[[], str]] = None) -> bool:
    """True once the engine answers HTTP at all (any status code), within
    timeout seconds (default ENGINE_WAIT). Gives up early when why_down
    names a reason it won't come up."""
    deadline = time.monotonic() + (ENGINE_WAIT if timeout is None else timeout)
    while True:
        code, _ = engine._call('GET', '/status', timeout=5)
        if code is not None:
            return True
        if cancelled() or time.monotonic() >= deadline or (why_down and _safe(why_down)):
            return False
        time.sleep(ENGINE_POLL)


def _safe(fn: Callable[[], str]) -> str:
    try:
        return fn() or ''
    except Exception as exc:
        logging.debug(f'[builtin migrate] engine diagnosis failed: {exc}')
        return ''


def _engine_down_error(engine, why_down: Optional[Callable[[], str]] = None) -> str:
    why = _safe(why_down) if why_down else ''
    if why:
        return f"The built-in mount engine isn't running: {why}. Fix that on the Mount page, then run Import again."
    return (f"The built-in mount engine isn't answering ({_engine_reason(engine)}). Start it with step 1 "
            f"(Start alongside), check the Mount page shows it running, then run Import again.")


def _file_body(f: dict) -> dict:
    """One file of a POST /jobs/import body (key/iv base64, as Go's []byte)."""
    body = {'name': f['name'], 'segments': [
        {'id': s['id'], 'start': s['start'], 'end': s['end'], 'data_start': s['data_start']}
        for s in f['segments']]}
    if f.get('key'):
        body['key'] = base64.b64encode(f['key']).decode('ascii')
        body['iv'] = base64.b64encode(f['iv']).decode('ascii')
        body['size'] = f['size']
    return body


def import_one(engine, entry: dict, mdir: str) -> Tuple[str, str, int]:
    """(outcome, message, skipped files). outcome: imported/already/failed,
    or unreachable when the engine gave no answer (the job wasn't tried)."""
    jid, folder = entry.get('info_hash') or '', entry.get('name') or ''
    if not jid or not folder:
        return 'failed', f'{folder or jid}: missing name or id', 0
    path = os.path.join(mdir, f'{jid}.meta')
    try:
        with open(path, 'rb') as f:
            meta = parse_meta(f.read())
    except FileNotFoundError:
        return 'failed', f'{folder}: no cli_mount metadata ({path})', 0
    except Exception as exc:
        return 'failed', f'{folder}: unreadable metadata: {exc}', 0
    if not meta['files']:
        why = '; '.join(f'{n}: {w}' for n, w in meta['skipped']) or 'no files'
        return 'failed', f'{folder}: nothing importable ({why})', len(meta['skipped'])
    body = {'id': jid, 'name': folder, 'category': entry.get('category') or '',
            'files': [_file_body(f) for f in meta['files']]}
    code, resp = engine._call('POST', '/jobs/import', timeout=60, json=body)
    if code is None:
        return 'unreachable', f'{folder}: engine unreachable: {_engine_reason(engine)}', 0
    if code == 201:
        return 'imported', '', len(meta['skipped'])
    if code == 409:
        return 'already', '', 0
    err = (resp or {}).get('error') if isinstance(resp, dict) else None
    return 'failed', f'{folder}: engine said {code}: {err or "no details"}', len(meta['skipped'])


def _run_import(job_id, source, engine, data_path, limit, space=None, prepare=None, why_down=None):
    cancelled = lambda: bool(status(job_id).get('cancel'))  # noqa: E731
    try:
        if prepare:
            _set(job_id, status='applying settings')
            prepare()
        mdir = meta_dir(data_path)
        if not os.path.isdir(mdir):
            _set(job_id, status='error', complete=True,
                 error=f"cli_mount's metadata folder isn't visible here ({mdir}). Bind cli_mount's appdata into "
                       f"cli_debrid and set Usenet Provider → cli_mount Data Path.")
            return
        entries = list(source.iter_library_nzbs())
        if limit:
            entries = entries[:limit]
        if not entries:
            _set(job_id, status='error', complete=True, error='cli_mount listed no jobs (is it running and reachable?)')
            return
        sizes = meta_sizes(mdir) if space else {}
        if space:
            plan = space_plan([e.get('info_hash') or '' for e in entries], sizes, space['data_dir'],
                              space.get('cache_dir', ''), space.get('cache_left', 0))
            _log(job_id, f"Disk: about {_gb(plan['needed'])} needed, {_gb(plan['room'])} usable on the engine's disk "
                         f"({_gb(plan['free'])} free, {_gb(plan['margin'])} kept free"
                         + (f", {_gb(plan['cache_reserved'])} for the disk cache to grow" if plan['cache_reserved'] else '') + ')')
            if not plan['fits']:
                _set(job_id, status='error', complete=True,
                     error=f"Not enough disk space: importing {plan['jobs']} job(s) needs about {_gb(plan['needed'])}, "
                           f"but only {_gb(plan['room'])} can be used on the disk holding {space['data_dir']}. "
                           f"Free up space, or import at most {plan['fits_jobs']} job(s) for now (Jobs to import).")
                return
        _set(job_id, status='waiting for the engine', total=len(entries))
        if not wait_for_engine(engine, cancelled=cancelled, why_down=why_down):
            if cancelled():
                _set(job_id, status='cancelled', complete=True)
            else:
                _set(job_id, status='error', complete=True, error=_engine_down_error(engine, why_down))
            return
        _set(job_id, status='running')
        _log(job_id, f'{len(entries)} job(s) listed by cli_mount; metadata from {mdir}')
        for n, e in enumerate(entries):
            if status(job_id).get('cancel'):
                _set(job_id, status='cancelled', complete=True)
                return
            if space and n and n % SPACE_RECHECK_EVERY == 0:
                # Something else may be filling the disk too: stop before it's full.
                rest = space_plan([x.get('info_hash') or '' for x in entries[n:n + SPACE_RECHECK_EVERY]], sizes,
                                  space['data_dir'], space.get('cache_dir', ''), space.get('cache_left', 0))
                if not rest['fits']:
                    _set(job_id, status='error', complete=True,
                         error=f"Stopped after {n} job(s): the disk holding {space['data_dir']} is nearly full "
                               f"({_gb(rest['free'])} free). Jobs imported so far stay; free up space and run Import again.")
                    return
            outcome, msg, skipped = import_one(engine, e, mdir)
            if outcome == 'unreachable':
                # Restarted or crashed mid-run: wait for it once, then retry
                # this job. Still down: stop instead of failing every job left.
                if wait_for_engine(engine, cancelled=cancelled, why_down=why_down):
                    outcome, msg, skipped = import_one(engine, e, mdir)
                if outcome == 'unreachable':
                    if cancelled():
                        _set(job_id, status='cancelled', complete=True)
                        return
                    st = status(job_id)
                    _set(job_id, status='error', complete=True,
                         error=f"Stopped after {n} job(s) ({st['imported']} imported): " + _engine_down_error(engine, why_down)
                               + ' Jobs imported so far stay.')
                    return
            _add(job_id, outcome)
            _add(job_id, 'done')
            if skipped:
                _add(job_id, 'skipped_files', skipped)
            if msg:
                _log(job_id, msg)
        st = status(job_id)
        _log(job_id, f"Done: {st['imported']} imported, {st['already']} already there, {st['failed']} failed"
                     + (f", {st['skipped_files']} encrypted/empty file(s) left out" if st['skipped_files'] else ''))
        _set(job_id, status='complete', complete=True)
    except Exception as exc:
        logging.exception('[builtin migrate] import failed')
        _set(job_id, status='error', error=str(exc), complete=True)


# ---- copy back: built-in mount -> cli_mount --------------------------------

def start_export(engine, target, limit: int = 0) -> str:
    """Re-submit the engine's usenet jobs cli_mount lacks, in the background.
    engine: a BuiltinClient; target: a CliMountClient."""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {'kind': 'export', 'status': 'starting', 'total': 0, 'done': 0, 'imported': 0,
                         'already': 0, 'failed': 0, 'skipped_files': 0, 'complete': False, 'log': [], 'error': None}
    threading.Thread(target=_run_export, args=(job_id, engine, target, limit),
                     name='builtin-copy-back', daemon=True).start()
    return job_id


def _engine_nzb(engine, jid: str) -> Optional[bytes]:
    import requests
    try:
        r = requests.get(f'{engine.base_url}/jobs/{jid}/nzb', headers=engine._headers(), timeout=60)
    except Exception as exc:
        logging.debug(f'[builtin migrate] NZB of {jid}: {exc}')
        return None
    return r.content if r.status_code == 200 and r.content else None


class IdNotKept(Exception):
    """cli_mount gave the job a new id: it predates the id field."""


def export_one(engine, target, job: dict) -> Tuple[str, str]:
    """(outcome, message) for one engine job. outcome: imported/failed."""
    import requests
    jid = job.get('id') or ''
    folder = job.get('folder') or job.get('name') or jid
    data = _engine_nzb(engine, jid)
    if not data:
        return 'failed', f'{folder}: the built-in mount has no NZB for it'
    fields = {'arr': (None, 'cli_debrid'), 'id': (None, jid),
              'nzbFiles': (f'{folder}.nzb', data, 'application/x-nzb')}
    if job.get('category'):
        fields['downloadFolder'] = (None, job['category'])
    try:
        r = requests.post(f'{target.base_url}/api/add', headers=target._headers(), files=fields, timeout=120)
    except Exception as exc:
        return 'failed', f'{folder}: cli_mount unreachable: {exc}'
    if r.status_code != 200:
        return 'failed', f'{folder}: cli_mount said HTTP {r.status_code}: {r.text[:200]}'
    try:
        res = r.json()
    except ValueError:
        res = None
    first = res[0] if isinstance(res, list) and res and isinstance(res[0], dict) else {}
    if first.get('status') == 'error':
        return 'failed', f"{folder}: {first.get('error') or 'rejected'}"
    got = str(first.get('id') or first.get('nzo_id') or '')
    if got and got != jid:
        raise IdNotKept(f'{folder}: cli_mount gave it id {got} instead of {jid}')
    return 'imported', ''


def _run_export(job_id, engine, target, limit):
    try:
        try:
            have = {e.get('info_hash') for e in target.iter_library_nzbs()}
        except Exception as exc:
            _set(job_id, status='error', complete=True, error=f'cli_mount unreachable: {exc}')
            return
        code, jobs = engine._call('GET', '/jobs', params={'status': 'ready'}, timeout=30)
        if code != 200 or not isinstance(jobs, list):
            _set(job_id, status='error', complete=True, error='the built-in mount is not answering (is it running?)')
            return
        usenet = [j for j in jobs if j.get('protocol') != 'debrid']
        todo = [j for j in usenet if j.get('id') not in have]
        todo.sort(key=lambda j: j.get('created_at') or '')
        if limit:
            todo = todo[:limit]
        _set(job_id, status='running', total=len(todo), already=len(usenet) - len(todo))
        _log(job_id, f'{len(usenet)} usenet job(s) on the built-in mount, {len(usenet) - len(todo)} already in '
                     f'cli_mount, {len(todo)} to copy back')
        for j in todo:
            if status(job_id).get('cancel'):
                _set(job_id, status='cancelled', complete=True)
                return
            try:
                outcome, msg = export_one(engine, target, j)
            except IdNotKept as exc:
                _log(job_id, str(exc))
                _set(job_id, status='error', complete=True,
                     error="This cli_mount doesn't keep job ids yet, so cli_debrid's records wouldn't match. "
                           "Update cli_mount (needs the 'id' field of /api/add) and run Copy back again; "
                           "remove the one job it just added from cli_mount first.")
                return
            _add(job_id, outcome)
            _add(job_id, 'done')
            if msg:
                _log(job_id, msg)
            time.sleep(0.2)  # cli_mount imports each one (article checks)
        st = status(job_id)
        _log(job_id, f"Done: {st['imported']} copied back, {st['already']} already in cli_mount, {st['failed']} failed")
        _set(job_id, status='complete', complete=True)
    except Exception as exc:
        logging.exception('[builtin migrate] copy back failed')
        _set(job_id, status='error', error=str(exc), complete=True)


# ---- compare a sample through both mounts -----------------------------------

def _digest(path: str, size: int) -> str:
    h = hashlib.sha256()
    chunk = 256 << 10
    with open(path, 'rb') as f:
        for off in sorted({0, max(0, size // 2 - chunk // 2), max(0, size - chunk)}):
            f.seek(off)
            h.update(f.read(chunk))
    return h.hexdigest()[:16]


def compare(engine, climount_root: str, builtin_root: str, sample: int = 10,
            rng: Optional[random.Random] = None) -> dict:
    """Read sample files through cli_mount's mount and the engine's mount
    (<root>/<folder>/<file>) and compare size + hashes of 3 ranges. Debrid
    folders are compared by name and size only: reading them would ask the
    debrid service for links twice (AllDebrid suspends accounts for that)."""
    code, jobs = engine._call('GET', '/jobs', params={'status': 'ready'}, timeout=20)
    jobs = jobs if code == 200 and isinstance(jobs, list) else []
    (rng or random.Random()).shuffle(jobs)
    usenet = [j for j in jobs if j.get('protocol') != 'debrid']
    debrid = [j for j in jobs if j.get('protocol') == 'debrid']
    pick = usenet[:sample] + debrid[:sample]
    rows, same = [], 0
    for j in pick:
        folder = j.get('folder') or j.get('name', '')
        is_debrid = j.get('protocol') == 'debrid'
        try:
            names = sorted(os.listdir(os.path.join(builtin_root, folder)))
        except OSError as exc:
            rows.append({'folder': folder, 'file': '', 'ok': False, 'detail': f'built-in mount: {exc}'})
            continue
        if not names:
            continue
        name = names[0]
        a, b = os.path.join(climount_root, folder, name), os.path.join(builtin_root, folder, name)
        t0 = time.time()
        try:
            sa, sb = os.path.getsize(a), os.path.getsize(b)
            if is_debrid:
                ok = sa == sb
                detail = 'same name and size (debrid: not read)' if ok else f'size {sa} vs {sb}'
            else:
                ok = sa == sb and _digest(a, sa) == _digest(b, sb)
                detail = 'identical' if ok else (f'size {sa} vs {sb}' if sa != sb else 'bytes differ')
        except OSError as exc:
            ok, detail = False, ('not in cli_mount under this name: ' if is_debrid else '') + str(exc)
        same += ok
        rows.append({'folder': folder, 'file': name, 'ok': ok, 'detail': detail, 'seconds': round(time.time() - t0, 1)})
    return {'checked': len(rows), 'identical': same, 'rows': rows}


# ---- switch over / back ------------------------------------------------------

def climount_mount_root(get_setting: Callable) -> str:
    """cli_mount's mount as cli_debrid sees it, minus __all__. Direct Mount
    keeps it in Plex → mounted_file_location (onboarding hides the original
    files path there, so it's often blank); Symlinked/Local in File
    Management → original_files_path. Either falls back to the other."""
    original = get_setting('File Management', 'original_files_path', '') or ''
    mounted = get_setting('Plex', 'mounted_file_location', '') or ''
    direct = (get_setting('File Management', 'file_collection_management', '') or '') == 'Plex'
    p = ((mounted or original) if direct else (original or mounted)).rstrip('/\\')
    return p[:-len('__all__')].rstrip('/\\') if p.endswith('__all__') else p
