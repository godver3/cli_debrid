"""
Debrid streaming through the built-in mount.

cli_debrid owns the debrid accounts; the engine only streams:
  - sync_once registers the account's finished torrents with the engine
    (POST /debrid/jobs) and removes ones gone from the account, so the mount
    mirrors the account like cli_mount/Zurg do: <mount>/__all__/<torrent>/<files>;
  - link() answers the engine's link callback (/mount/internal/link) with a
    fresh download URL for one file.

Providers opt in with supports_mount_links (all six: Real-Debrid, TorBox,
AllDebrid, Premiumize, Debrid-Link, HighWay).
File refs are "<provider key>:<torrent id>:<file id>" (AllDebrid's file id
is its locker link, which may itself contain colons).

Links are cached on disk (db_content/mount_links.json) until they expire, so
restarts don't re-generate a link for every file Plex touches: AllDebrid
suspends accounts that generate many links without downloads.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

SYNC_SECONDS = 30  # default of Built-in Mount.debrid_sync_seconds
MIN_SYNC_SECONDS = 10
ADD_PER_PASS = 50  # provider API calls per pass stay modest

_KEYS = {'RealDebridProvider': 'rd', 'TorboxProvider': 'tb', 'AllDebridProvider': 'ad',
         'PremiumizeProvider': 'pm', 'DebridLinkProvider': 'dl', 'HighwayProvider': 'hw'}
_started = False
_lock = threading.Lock()


def provider_key(p) -> str:
    name = type(p).__name__
    return _KEYS.get(name) or name.lower().replace('provider', '') or 'debrid'


def providers() -> List:
    """Configured debrid providers that can hand out per-file links."""
    try:
        from debrid import get_debrid_providers
        return [p for p in get_debrid_providers() if p is not None and getattr(p, 'supports_mount_links', False)]
    except Exception as exc:
        logging.debug(f'[mount debrid] no providers: {exc}')
        return []


def _job_id(key: str, torrent_id: str) -> str:
    return f'{key}-{torrent_id}'


def unique_names(files: List[Dict]) -> List[Dict]:
    """Flatten torrent paths to file names unique within the folder: plain
    base names, falling back to the path joined with ' - ' on clashes."""
    base = [os.path.basename((f.get('name') or '').replace('\\', '/')) for f in files]
    out, used = [], set()
    for f, b in zip(files, base):
        name = b
        if not name or base.count(b) > 1 or name in used:
            parts = [x for x in (f.get('name') or '').replace('\\', '/').split('/') if x]
            name = ' - '.join(parts[-3:]) or f"file {f.get('file_id')}"
        while name in used:
            name = f"{name} ({f.get('file_id')})"
        used.add(name)
        out.append({**f, 'name': name})
    return out


# ---- link cache (survives restarts) ----------------------------------------

#: Plain refreshes reuse recent links to avoid flooding the provider.
#: A connection failure can bypass this floor.
REFRESH_MIN_AGE = 60
_MAX_CACHED = 20000
_link_lock = threading.Lock()
_links: Optional[Dict[str, Dict]] = None


def _cache_path() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'mount_links.json')


def _load_links() -> Dict[str, Dict]:
    global _links
    if _links is None:
        try:
            with open(_cache_path()) as f:
                data = json.load(f)
            _links = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _links = {}
    return _links


def _save_links(links: Dict[str, Dict]) -> None:
    now = time.time()
    keep = {k: v for k, v in links.items() if isinstance(v, dict) and v.get('expires', 0) > now}
    if len(keep) > _MAX_CACHED:
        keep = dict(sorted(keep.items(), key=lambda kv: kv[1].get('minted', 0))[-_MAX_CACHED:])
    links.clear()
    links.update(keep)
    path = _cache_path()
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(keep, f)
        os.replace(tmp, path)
    except OSError as exc:
        logging.debug(f'[mount debrid] link cache not saved: {exc}')


def _mint(ref: str) -> Tuple[str, int]:
    try:
        key, torrent_id, file_id = ref.split(':', 2)
    except ValueError:
        raise ValueError(f'bad file reference {ref!r}')
    for p in providers():
        if provider_key(p) == key:
            return p.mount_link(torrent_id, file_id)
    raise ValueError(f'no configured debrid provider for {key!r}')


# Mints of the same ref are serialised (striped locks): concurrent requests
# for a file not yet cached reuse the first mint instead of each unlocking
# the file again at the provider.
_mint_locks = [threading.Lock() for _ in range(64)]


def _cached(ref: str, refresh: bool, unreachable_url: str = '') -> Optional[Tuple[str, int]]:
    now = time.time()
    with _link_lock:
        hit = _load_links().get(ref)
    if hit and hit.get('expires', 0) > now + 60:
        url = hit['url']
        if unreachable_url:
            # Reuse a replacement already minted by another reader.
            if url == unreachable_url:
                return None
            if ref.startswith('tb:'):
                from debrid.torbox.cdn_fallback import host
                if host(url) == host(unreachable_url):
                    return None
        elif refresh and now - hit.get('minted', 0) >= REFRESH_MIN_AGE:
            return None
        return url, int(hit['expires'] - now)
    return None


def link(ref: str, refresh: bool = False, unreachable_url: str = '') -> Tuple[str, int]:
    """(url, ttl seconds) for an engine file ref. Raises on failure."""
    if unreachable_url and ref.startswith('tb:'):
        from debrid.torbox.cdn_fallback import mark_down
        mark_down(unreachable_url)
    hit = _cached(ref, refresh, unreachable_url)
    if hit:
        return hit
    with _mint_locks[hash(ref) % len(_mint_locks)]:
        hit = _cached(ref, refresh, unreachable_url)
        if hit:
            return hit
        return _mint_and_store(ref)


def _link_lifetime() -> int:
    """Built-in Mount.debrid_link_lifetime_hours in seconds (0 = the
    provider's own lifetime)."""
    try:
        from utilities.settings import get_setting
        return max(0, int(get_setting('Built-in Mount', 'debrid_link_lifetime_hours', 0) or 0)) * 3600
    except Exception:
        return 0


def _mint_and_store(ref: str) -> Tuple[str, int]:
    now = time.time()
    url, ttl = _mint(ref)
    ttl = _link_lifetime() or ttl
    with _link_lock:
        links = _load_links()
        links[ref] = {'url': url, 'expires': now + int(ttl), 'minted': now}
        _save_links(links)
    return url, int(ttl)


def check(ref: str) -> Tuple[str, str]:
    """(state, reason) of one engine file ref by the provider's cli_mount
    rules (debrid/mount_proof.py). Never raises: trouble is unknown."""
    from debrid.mount_proof import BROKEN, UNKNOWN
    try:
        key, torrent_id, file_id = ref.split(':', 2)
    except ValueError:
        return UNKNOWN, f'bad file reference {ref!r}'
    for p in providers():
        if provider_key(p) == key:
            if not hasattr(p, 'mount_check'):
                return UNKNOWN, 'provider_check_unsupported'
            try:
                state, reason = p.mount_check(torrent_id, file_id)
            except Exception as exc:
                return UNKNOWN, f'provider_probe_error: {exc}'
            if state == BROKEN:
                logging.info(f'[mount debrid] check: {ref} is gone ({reason})')
                forget_links(ref + ':')  # never hand out its cached link again
                with _link_lock:
                    links = _load_links()
                    if links.pop(ref, None) is not None:
                        _save_links(links)
            return state, reason
    return UNKNOWN, 'provider_client_not_found'


def forget_links(prefix: str) -> None:
    """Drop cached links of a removed torrent ("<key>:<torrent id>:")."""
    with _link_lock:
        links = _load_links()
        gone = [k for k in links if k.startswith(prefix)]
        for k in gone:
            links.pop(k, None)
        if gone:
            _save_links(links)


def _finished(t: dict) -> bool:
    st = str(t.get('status') or '').lower()
    return st in ('downloaded', 'cached', 'completed', 'torrentstatus.cached') or st.endswith('cached')


# ---- removal safety ----------------------------------------------------------
#
# The mount mirrors the account, so a torrent missing from a listing is
# removed. A listing can come back short (a failed page, an API hiccup), and
# removing on one listing would pull good files out from under Plex and the
# symlinks. So a torrent is removed only when MISS_PASSES listings in a row
# don't have it, and a pass that would remove an unusual share of a
# provider's torrents is held (logged) until the same torrents have been
# missing for MASS_HOLD_SECONDS.

MISS_PASSES = 2
# ...and for at least this long, however often the account is listed (the
# old fixed 2 x 120 s passes), so a faster sync doesn't shorten the guard.
MIN_MISS_SECONDS = 240
MASS_MIN = 10           # more removals than this in one pass...
MASS_SHARE = 0.10       # ...and more than this share of the provider's torrents
MASS_HOLD_SECONDS = 6 * 3600
SKIP_RETRY_SECONDS = 3600  # torrents mount_files declined are retried this often

_missing: Dict[str, Dict] = {}   # job id -> {'count': passes missing, 'since': first missed}
_skipped: Dict[str, float] = {}  # job id -> when mount_files declined it
_mass_warned: Dict[str, float] = {}
_status: Dict = {'synced_at': 0.0, 'pending': 0, 'skipped': 0, 'held_removals': 0, 'complete': False,
                 'errors': 0, 'providers': 0}


def engine_holds_debrid(get_setting) -> bool:
    """The built-in mount (not cli_mount/Zurg) serves the debrid torrents:
    whenever it is in use, it streams the debrid account too. Debrid repair
    then goes to it."""
    from utilities import builtin_engine
    bm = get_setting('Built-in Mount') or {}
    return builtin_engine.selected({'Usenet Provider': get_setting('Usenet Provider') or {}, 'Built-in Mount': bm})


def _engine_job_id(torrent_id) -> List[str]:
    """Engine job ids a Clyde torrent id can be: debrid "<key>-<id>" per
    configured provider, or the id of a usenet job ("nzb:<id>")."""
    tid = str(torrent_id)
    if tid.startswith('nzb:'):
        return [tid[4:]]
    return [_job_id(provider_key(p), tid) for p in providers()]


def folder_for(torrent_id, get_setting) -> Optional[str]:
    """The folder the built-in mount REALLY made for a torrent or NZB, from
    the engine's job record, or None when it can't be told (another mount,
    engine down, job unknown). Two torrents with one name get "Name" and
    "Name (2)", and the second keeps its suffix after the first is gone, so
    a folder worked out from the release name can be wrong (17 Married...
    with Children episodes stuck in Checking on "... [i_c]" while their
    files were in "... [i_c] (2)"). Every place that builds a mount path
    for a known torrent asks this first; a name is only a last fallback."""
    if not torrent_id:
        return None
    try:
        if not engine_holds_debrid(get_setting):
            return None
        from usenet.builtin_client import get_builtin_client
        client = get_builtin_client()
        for jid in _engine_job_id(torrent_id):
            code, job = client._call('GET', f'/jobs/{jid}', timeout=10)
            if code == 200 and isinstance(job, dict) and job.get('folder'):
                return job['folder']
    except Exception as exc:
        logging.debug(f'[mount debrid] folder lookup for {torrent_id} failed: {exc}')
    return None


engine_folder = folder_for  # the name c2b981cb introduced


_SIBLING = re.compile(r' \(\d{1,3}\)$')


def folder_family(entries, name: str) -> List[str]:
    """Folders of one name on the mount: "Name" and every "Name (N)", from the
    names in a directory listing. More than one means a name guess can pick
    the wrong torrent."""
    base = _SIBLING.sub('', name)
    pat = re.compile(re.escape(base) + r'(?: \(\d{1,3}\))?$')
    return sorted(e for e in entries if pat.match(e))


def guarded_folder(mount: str, torrent_id, guess: str, get_setting) -> Optional[str]:
    """The folder to use for a torrent under a mount root: the engine's real
    one, else the stored/guessed name, unless sibling "Name (N)" folders make
    that guess ambiguous (None, logged at info)."""
    real = folder_for(torrent_id, get_setting)
    if real:
        return real
    if not guess:
        return None
    try:
        family = folder_family(os.listdir(mount), guess) if mount else []
    except OSError:
        family = []
    if len(family) > 1:
        logging.info(f"[mount debrid] folder of {torrent_id} not known and {family} share one name on the "
                     f"mount; not guessing")
        return None
    return guess


def mount_file_name(files: List[Dict], path: str) -> str:
    """The name the built-in mount gives one file of a torrent (members of
    different season folders are flattened to distinct names, see
    unique_names). files: the provider's file rows with 'path' or 'name',
    'bytes' or 'size' and 'id' or 'file_id'; path: the matched file's path."""
    rows = [{'name': f.get('path') or f.get('name') or '', 'file_id': f.get('id', f.get('file_id')),
             'size': int(f.get('bytes') or f.get('size') or 0)} for f in files or []]
    rows = [r for r in rows if r['size'] > 0]
    want = (path or '').replace('\\', '/')
    for r, u in zip(rows, unique_names(rows)):
        if r['name'].replace('\\', '/') == want:
            return u['name']
    return os.path.basename(want)

_VIDEO_EXTS = {'mkv', 'mp4', 'm4v', 'avi', 'mov', 'mpg', 'mpeg', 'ts', 'm2ts', 'wmv', 'webm', 'flv'}
#: Video types MediaMatcher.is_video_file accepts; extras/samples/other
#: extensions (like .webm) are ignored by the matcher and must not mask a
#: release whose real video files are hidden by the built-in mount.
_MATCHER_VIDEO_EXTS = {'mkv', 'mp4', 'avi', 'm4v', 'ts', 'mov'}
_MATCHER_EXTRA_RE = re.compile(
    r'(?<![a-zA-Z0-9])(?:ncop|nced|opening|ending|ova|bloopers?|omakes?)(?=[._-]|$)'
    r'|(?<=[._-])(?:specials?|extras?|bonus(?:es)?)(?=[._-]|$)'
    r'|\b(?:ncop|nced|op|ed)\d+\b|\b(?:opening|ending)\s*\d+\b',
    re.IGNORECASE,
)


def _matcher_video_ext(file_entry: Dict) -> Optional[str]:
    """Lowercase extension if MediaMatcher would treat this file as a main video."""
    name = str(file_entry.get('path') or file_entry.get('name') or '')
    base = os.path.basename(name.replace('\\', '/'))
    ext = os.path.splitext(base)[1].lstrip('.').lower()
    if ext not in _MATCHER_VIDEO_EXTS:
        return None
    base_lower = base.lower()
    if 'sample' in base_lower or 'specials' in base_lower or _MATCHER_EXTRA_RE.search(base_lower):
        return None
    from debrid.common.utils import is_promo_file, is_unwanted_file
    if is_unwanted_file(base) or is_promo_file(base, file_entry.get('bytes')):
        return None
    return ext


def videos_all_hidden(files: List[Dict], get_setting) -> List[str]:
    """The extensions that hide every main video file of a torrent on the
    built-in mount (a .ts-only release under the default list), or [] when
    the mount isn't serving debrid, lists everything, or shows at least one
    video the matcher would use. Picking such a torrent can never produce a
    playable file."""
    try:
        if not engine_holds_debrid(get_setting):
            return []
        from utilities import builtin_engine
        allowed = builtin_engine.allowed_extensions(get_setting('Built-in Mount') or {})
    except Exception as exc:
        logging.debug(f'[mount debrid] extension check skipped: {exc}')
        return []
    if not allowed:
        return []
    exts = []
    for f in files or []:
        ext = _matcher_video_ext(f)
        if ext is not None:
            if ext in allowed:
                return []
            exts.append(ext)
    return sorted(set(exts))


def hidden_by_extension(torrent_id: str) -> bool:
    """The built-in mount holds this finished debrid torrent but the allowed
    extensions hide all of its files (a .ts-only release): its folder will
    never appear, so waiting for it is pointless."""
    try:
        from usenet.builtin_client import get_builtin_client
        client = get_builtin_client()
        return any(client.hidden_by_extension(_job_id(provider_key(p), str(torrent_id))) for p in providers())
    except Exception as exc:
        logging.debug(f'[mount debrid] hidden check for {torrent_id} failed: {exc}')
        return False


def needed(get_setting) -> bool:
    """Does Switch have to wait for the debrid mirror?"""
    return bool(providers())


def status() -> Dict:
    """Last sync's state: pending = finished torrents not on the mount yet."""
    with _lock:
        return dict(_status)


def _list(p) -> List[Dict]:
    """The provider's full torrent list; raises if it may be incomplete."""
    try:
        return p.list_active_torrents(strict=True) or []
    except TypeError:
        return p.list_active_torrents() or []


def _due(m: Dict, now: float) -> bool:
    return m['count'] >= MISS_PASSES and now - m['since'] >= MIN_MISS_SECONDS


def _removals(key: str, have: set, on_account: set, now: float) -> List[str]:
    """Job ids of this provider safe to remove now."""
    mine = {h for h in have if h.startswith(key + '-')}
    gone = mine - on_account
    for jid in list(_missing):
        if jid.startswith(key + '-') and jid not in gone:
            _missing.pop(jid, None)  # back on the account, or already removed
    ready = []
    for jid in gone:
        m = _missing.setdefault(jid, {'count': 0, 'since': now})
        m['count'] += 1
        if _due(m, now):
            ready.append(jid)
    if len(ready) > MASS_MIN and len(ready) > MASS_SHARE * len(mine):
        oldest = min(_missing[j]['since'] for j in ready)
        if now - oldest < MASS_HOLD_SECONDS:
            if now - _mass_warned.get(key, 0) > 3600:
                _mass_warned[key] = now
                logging.warning(f'[mount debrid] {key}: {len(ready)} of {len(mine)} torrents are missing from the '
                                f'account listing; holding their removal from the mount (a short listing would '
                                f'otherwise pull them from Plex). They are removed if still missing after '
                                f'{MASS_HOLD_SECONDS // 3600}h.')
            return []
    return sorted(ready)


def _register(engine, key: str, t: dict, jid: str, folder: str, files: List[Dict]):
    """POST one torrent's files to the engine under mount names that are
    unique within the folder (see unique_names)."""
    files = unique_names([f for f in files if int(f.get('size') or 0) > 0])
    body = {'id': jid, 'name': folder, 'provider': key, 'hash': str(t.get('hash') or '').lower(), 'files': [
        {'name': f['name'], 'size': int(f['size']), 'ref': f"{key}:{t['id']}:{f['file_id']}",
         # a byte range of a provider file (member of a TorBox stored zip)
         **({'offset': int(f['offset']), 'source_size': int(f['source_size'])} if f.get('source_size') else {})}
        for f in files]}
    return engine._call('POST', '/debrid/jobs', timeout=30, json=body)


_zip_checked: Dict[str, float] = {}  # job id -> when its zip was last looked at


def _reregister_zip(engine, p, key: str, t: dict, jid: str) -> str:
    """Replace a job that holds a TorBox zip as one file by one with the
    zip's members, once the zip's index can be read. Only a job the mount
    hides (a .zip isn't a shown extension, so nothing is playable, linked or
    streamed from it) is touched, so no link or open stream can break; a
    failed re-add leaves no job and the next pass registers it normally.
    Returns '' (nothing to do, no provider call), 'asked' or 'added'."""
    code, job = engine._call('GET', f'/jobs/{jid}', timeout=10)
    if code != 200 or not isinstance(job, dict) or not job.get('hidden_by_extension'):
        return ''
    try:
        found = p.mount_files(str(t['id']))
    except Exception as exc:
        logging.debug(f'[mount debrid] {key} {t.get("id")}: {exc}')
        return 'asked'
    if not found or not any(f.get('source_size') for f in found[1]):
        return 'asked'  # the index still can't be read: look again later
    folder, files = found
    if engine._call('DELETE', f'/jobs/{jid}', timeout=20)[0] not in (204, 404):
        return 'asked'
    c, resp = _register(engine, key, t, jid, folder, files)
    if c != 201:
        logging.warning(f'[mount debrid] re-adding zip {folder} failed: {c} {resp}')
        return 'asked'
    logging.info(f'[mount debrid] {folder}: zip registered with its {len(files)} files')
    return 'added'


def sync_once(engine, provs: Optional[List] = None, budget: int = ADD_PER_PASS) -> Dict[str, int]:
    """Mirror the debrid accounts into the engine. Returns counts; pending
    is finished torrents still to add (more passes needed)."""
    provs = providers() if provs is None else provs
    out = {'added': 0, 'removed': 0, 'skipped': 0, 'errors': 0, 'pending': 0, 'held_removals': 0}
    if not provs:
        return out
    code, jobs = engine._call('GET', '/jobs', timeout=20)
    if code != 200 or not isinstance(jobs, list):
        out['errors'] += 1
        return out
    have = {j['id'] for j in jobs if j.get('protocol') == 'debrid'}
    now = time.time()
    complete = True
    for p in provs:
        key = provider_key(p)
        try:
            torrents = _list(p)
        except Exception as exc:
            logging.warning(f'[mount debrid] listing {key} failed (nothing removed this pass): {exc}')
            out['errors'] += 1
            complete = False
            continue
        if not torrents:
            continue  # never empty the mount because a listing came back empty
        on_account = {_job_id(key, str(t.get('id'))) for t in torrents if t.get('id')}
        for t in torrents:
            jid = _job_id(key, str(t.get('id')))
            if not _finished(t):
                continue
            if jid in have:
                # A TorBox zip registered before its member index existed (or
                # while reading it failed) is one hidden .zip: register the
                # members now that they can be listed.
                if (getattr(p, 'mounts_zip_members', False) and budget > 0
                        and now - _zip_checked.get(jid, 0) >= SKIP_RETRY_SECONDS):
                    _zip_checked[jid] = now
                    state = _reregister_zip(engine, p, key, t, jid)
                    if state:
                        budget -= 1  # asked the provider
                    if state == 'added':
                        out['added'] += 1
                continue
            if now - _skipped.get(jid, 0) < SKIP_RETRY_SECONDS:
                out['skipped'] += 1
                continue
            if budget <= 0:
                out['pending'] += 1
                continue
            budget -= 1
            try:
                found = p.mount_files(str(t['id']))
            except Exception as exc:
                logging.debug(f'[mount debrid] {key} {t.get("id")}: {exc}')
                out['errors'] += 1
                out['pending'] += 1
                continue
            if not found:
                _skipped[jid] = now
                out['skipped'] += 1
                continue
            folder, files = found
            c, resp = _register(engine, key, t, jid, folder, files)
            if c == 201:
                out['added'] += 1
                _skipped.pop(jid, None)
            elif c != 409:
                out['errors'] += 1
                out['pending'] += 1
                logging.warning(f'[mount debrid] engine refused {folder}: {c} {resp}')
        ready = _removals(key, have, on_account, now)
        out['held_removals'] += sum(1 for j, m in _missing.items()
                                    if j.startswith(key + '-') and _due(m, now) and j not in ready)
        for jid in ready:
            if engine._call('DELETE', f'/jobs/{jid}', timeout=20)[0] in (204, 404):
                out['removed'] += 1
                _missing.pop(jid, None)
                forget_links(f"{key}:{jid[len(key) + 1:]}:")
    if out['added'] or out['removed']:
        logging.info(f"[mount debrid] synced: {out['added']} added, {out['removed']} removed"
                     + (f", {out['pending']} still to add" if out['pending'] else ''))
    with _lock:
        _status.update(synced_at=now, pending=out['pending'], skipped=out['skipped'],
                       held_removals=out['held_removals'], errors=out['errors'], providers=len(provs),
                       complete=complete and out['pending'] == 0)
    return out


#: First pass soon after start; while torrents are still waiting to be
#: added, the next pass follows quickly (each provider client keeps its own
#: API rate limit), so a large account fills in minutes, not hours.
FIRST_SYNC_SECONDS = 15
BACKLOG_SECONDS = 5


def _enabled(get_setting) -> bool:
    from utilities import builtin_engine
    bm = get_setting('Built-in Mount') or {}
    settings = {'Usenet Provider': get_setting('Usenet Provider') or {}, 'Built-in Mount': bm}
    # Also while staged next to cli_mount: Switch needs the debrid library
    # in place before the engine takes over the mount path.
    return builtin_engine.selected(settings) or builtin_engine.alongside(settings)


def sync_seconds(get_setting) -> int:
    """How often the debrid accounts are listed (new torrents show up on
    the mount within this many seconds)."""
    try:
        v = int((get_setting('Built-in Mount') or {}).get('debrid_sync_seconds') or SYNC_SECONDS)
    except (TypeError, ValueError):
        v = SYNC_SECONDS
    return max(MIN_SYNC_SECONDS, v)


def _loop(get_setting):
    from usenet.builtin_client import BuiltinClient
    wait = FIRST_SYNC_SECONDS
    while True:
        time.sleep(wait)
        wait = sync_seconds(get_setting)
        try:
            if _enabled(get_setting):
                if sync_once(BuiltinClient()).get('pending'):
                    wait = BACKLOG_SECONDS
        except Exception as exc:
            logging.debug(f'[mount debrid] sync failed: {exc}')


def start(get_setting):
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(get_setting,), name='mount-debrid-sync', daemon=True).start()
