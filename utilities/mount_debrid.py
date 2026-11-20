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
import threading
import time
from typing import Dict, List, Optional, Tuple

SYNC_SECONDS = 120
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

#: A refresh within this many seconds of minting gets the same link back:
#: a link that fails right away won't be fixed by minting another, and a
#: refresh loop must not flood the provider.
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


def _cached(ref: str, refresh: bool) -> Optional[Tuple[str, int]]:
    now = time.time()
    with _link_lock:
        hit = _load_links().get(ref)
    if hit and hit.get('expires', 0) > now + 60:
        if not refresh or now - hit.get('minted', 0) < REFRESH_MIN_AGE:
            return hit['url'], int(hit['expires'] - now)
    return None


def link(ref: str, refresh: bool = False) -> Tuple[str, int]:
    """(url, ttl seconds) for an engine file ref. Raises on failure."""
    hit = _cached(ref, refresh)
    if hit:
        return hit
    with _mint_locks[hash(ref) % len(_mint_locks)]:
        hit = _cached(ref, refresh)
        if hit:
            return hit
        return _mint_and_store(ref)


def _mint_and_store(ref: str) -> Tuple[str, int]:
    now = time.time()
    url, ttl = _mint(ref)
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
        if m['count'] >= MISS_PASSES:
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
            if jid in have or not _finished(t):
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
            files = unique_names([f for f in files if int(f.get('size') or 0) > 0])
            body = {'id': jid, 'name': folder, 'provider': key, 'hash': str(t.get('hash') or '').lower(), 'files': [
                {'name': f['name'], 'size': int(f['size']), 'ref': f"{key}:{t['id']}:{f['file_id']}"} for f in files]}
            c, resp = engine._call('POST', '/debrid/jobs', timeout=30, json=body)
            if c == 201:
                out['added'] += 1
                _skipped.pop(jid, None)
            elif c != 409:
                out['errors'] += 1
                out['pending'] += 1
                logging.warning(f'[mount debrid] engine refused {folder}: {c} {resp}')
        ready = _removals(key, have, on_account, now)
        out['held_removals'] += sum(1 for j, m in _missing.items()
                                    if j.startswith(key + '-') and m['count'] >= MISS_PASSES and j not in ready)
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


def _loop(get_setting):
    from usenet.builtin_client import BuiltinClient
    wait = FIRST_SYNC_SECONDS
    while True:
        time.sleep(wait)
        wait = SYNC_SECONDS
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
