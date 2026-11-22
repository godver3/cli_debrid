"""What the media server (Plex, or Jellyfin/Emby) knows about an item's
subtitles, without reading the file: the server already scanned every file
(as Bazarr does with ffprobe, but Bazarr re-reads each file itself).

- The right Plex ratingKey for an item. For episodes cli_debrid stores the
  SHOW's ratingKey in media_items.ms_item_id (movies store their own), so
  an episode's key is looked up from its show's allLeaves (cached per show).
- Embedded subtitle tracks per item, from Plex's own scan of the file,
  fetched in batches and cached in the database (7 days), so the Subtitle
  Manager can count good embedded tracks as present.
"""
import json
import logging
import threading
import time

import requests

TEXT_CODECS = {'srt', 'subrip', 'mov_text', 'webvtt', 'vtt', 'text', 'smi', 'sami'}
PAGE = 500
CACHE_DAYS = 7
UNKNOWN_RETRY_MIN = 10
BATCH = 120

_LEAVES = {}            # show ratingKey -> (monotonic, {(season, episode): ratingKey})
_LEAVES_TTL = 1800
_LOCK = threading.Lock()
_SCAN = {'running': False, 'done': 0, 'total': 0}


def _conn():
    from utilities.settings import get_setting
    return (get_setting('Plex', 'url', '') or '').rstrip('/'), get_setting('Plex', 'token', '') or ''


def _jf_conn():
    from utilities.settings import get_setting
    return ((get_setting('Debug', 'emby_jellyfin_url', '') or '').rstrip('/'),
            get_setting('Debug', 'emby_jellyfin_token', '') or '')


def server():
    """'plex', 'jellyfin' (also Emby) or None: Plex in Plex mode; in
    Symlinked/Local mode the selected media server type."""
    from utilities.settings import get_setting
    mode = get_setting('File Management', 'file_collection_management', 'Plex')
    want = 'plex'
    if mode != 'Plex':
        want = (get_setting('File Management', 'media_server_type', 'plex') or 'plex').lower()
    if want == 'silo':
        return None  # no track lookup for Silo yet: the add-time ffprobe tracks are used
    if want == 'jellyfin':
        url, token = _jf_conn()
        return 'jellyfin' if url and token else None
    url, token = _conn()
    return 'plex' if url and token else None


def available():
    return server() is not None


def _get(path, timeout=30):
    url, token = _conn()
    r = requests.get(url + path, params={'X-Plex-Token': token}, headers={'Accept': 'application/json'},
                     timeout=timeout)
    if r.status_code != 200:
        return []
    return (r.json().get('MediaContainer') or {}).get('Metadata') or []


def _episode_map(show_key):
    hit = _LEAVES.get(str(show_key))
    if hit and time.monotonic() - hit[0] < _LEAVES_TTL:
        return hit[1]
    out = {}
    try:
        for m in _get(f'/library/metadata/{show_key}/allLeaves', timeout=60):
            if m.get('parentIndex') is not None and m.get('index') is not None:
                out[(int(m['parentIndex']), int(m['index']))] = str(m['ratingKey'])
    except Exception as e:
        logging.debug(f"[subs] Plex allLeaves for {show_key} failed: {e}")
        return {}
    if len(_LEAVES) > 2000:
        _LEAVES.clear()
    _LEAVES[str(show_key)] = (time.monotonic(), out)
    return out


def rating_key(item):
    """The Plex ratingKey of this movie or episode, or None."""
    item = item or {}
    rk = item.get('ms_item_id')
    if not rk:
        return None
    if item.get('type') == 'episode' and item.get('season_number') is not None:
        want = (int(item['season_number']), int(item.get('episode_number') or 0))
        ep = _episode_map(rk).get(want)
        if ep is None:
            # a just-added episode may not be in the cached list yet
            hit = _LEAVES.get(str(rk))
            if hit and time.monotonic() - hit[0] > 30:
                _LEAVES.pop(str(rk), None)
                ep = _episode_map(rk).get(want)
        return ep
    return str(rk)


def cache_key(item):
    """Cache key for an item that needs no Plex call: the movie's ratingKey,
    or show ratingKey:season:episode for an episode."""
    item = item or {}
    rk = item.get('ms_item_id')
    if not rk:
        return f"item:{item['id']}" if item.get('id') is not None else None
    if item.get('type') == 'episode' and item.get('season_number') is not None:
        return f"{rk}:{int(item['season_number'])}:{int(item.get('episode_number') or 0)}"
    return str(rk)


def _tracks_of(meta):
    out = []
    for md in meta.get('Media') or []:
        for p in md.get('Part') or []:
            for st in p.get('Stream') or []:
                if st.get('streamType') == 3 and not st.get('key') \
                        and 'commentary' not in (st.get('title') or st.get('displayTitle') or '').lower():
                    out.append({'lang': (st.get('languageTag') or st.get('languageCode') or '').lower(),
                                'code': (st.get('languageCode') or '').lower(),
                                'codec': (st.get('codec') or '').lower(), 'forced': bool(st.get('forced')),
                                'title': st.get('title') or ''})
    return out


def _cache_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS subtitle_embedded (
        rating_key TEXT PRIMARY KEY, tracks TEXT, checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")


def forget(item):
    """Drop an item's cached tracks: its file was just collected or replaced,
    so the next check reads the new file's tracks from the server."""
    from database.core import get_db_connection
    key = cache_key(item)
    if not key:
        return
    conn = get_db_connection()
    try:
        _cache_table(conn)
        conn.execute("DELETE FROM subtitle_embedded WHERE rating_key = ?", (key,))
        conn.commit()
    except Exception as e:
        logging.debug(f"[subs] embedded cache forget failed: {e}")
    finally:
        conn.close()


def cached_tracks(keys):
    """{cache_key: tracks} from the cache (fresh entries only)."""
    from database.core import get_db_connection
    keys = [k for k in keys if k]
    if not keys:
        return {}
    conn = get_db_connection()
    try:
        _cache_table(conn)
        out = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = ','.join('?' * len(chunk))
            for k, t in conn.execute(
                    f"SELECT rating_key, tracks FROM subtitle_embedded WHERE rating_key IN ({q}) "
                    f"AND checked_at >= datetime('now', '-{CACHE_DAYS} days')", chunk):
                try:
                    out[k] = json.loads(t or '[]')
                except ValueError:
                    out[k] = []
        return out
    finally:
        conn.close()


def _store(found, retry_minutes=None):
    """Cache tracks. retry_minutes: for items the server couldn't tell us
    about (not in its library yet), expire after that many minutes instead
    of CACHE_DAYS, so a just-added item is checked again soon."""
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        _cache_table(conn)
        when = ("CURRENT_TIMESTAMP" if retry_minutes is None
                else f"datetime('now', '-{CACHE_DAYS} days', '+{int(retry_minutes)} minutes')")
        conn.executemany("INSERT OR REPLACE INTO subtitle_embedded (rating_key, tracks, checked_at) "
                         f"VALUES (?, ?, {when})", [(k, json.dumps(v)) for k, v in found.items()])
        conn.commit()
    finally:
        conn.close()


def fetch_tracks(pairs):
    """pairs: [(cache_key, plex ratingKey)]. Ask Plex for the embedded tracks
    in batches and cache them under the cache keys."""
    pairs = [(c, k) for c, k in dict.fromkeys(pairs) if c and k]
    for i in range(0, len(pairs), BATCH):
        chunk = pairs[i:i + BATCH]
        by_rk = {k: c for c, k in chunk}
        found = {}
        try:
            for m in _get('/library/metadata/' + ','.join(by_rk), timeout=60):
                c = by_rk.get(str(m.get('ratingKey')))
                if c:
                    found[c] = _tracks_of(m)
        except Exception as e:
            logging.debug(f"[subs] Plex batch metadata failed: {e}")
            continue
        _store(found)
        missing = {c: [] for c, _ in chunk if c not in found}
        if missing:
            _store(missing, retry_minutes=UNKNOWN_RETRY_MIN)
        with _LOCK:
            _SCAN['done'] = _SCAN.get('done', 0) + len(chunk)


def scan_status():
    with _LOCK:
        return dict(_SCAN)


def _jf_tracks(item):
    out = []
    for st in item.get('MediaStreams') or []:
        if st.get('Type') != 'Subtitle' or st.get('IsExternal'):
            continue
        if 'commentary' in (st.get('Title') or st.get('DisplayTitle') or '').lower():
            continue
        lang = (st.get('Language') or '').lower()
        out.append({'lang': lang, 'code': lang, 'codec': (st.get('Codec') or '').lower(),
                    'forced': bool(st.get('IsForced')), 'title': st.get('Title') or ''})
    return out


def _jellyfin_scan(items):
    """Page through the server's movies and episodes once (with their
    MediaStreams) and match them to our items by file name."""
    import os
    from utilities.jellyfin_auth import jellyfin_auth_headers
    url, token = _jf_conn()
    headers = {**jellyfin_auth_headers(token), 'Accept': 'application/json'}
    by_name = {}
    for it in items:
        name = os.path.basename(it.get('location_on_disk') or '') or it.get('filled_by_file') or ''
        if name:
            by_name.setdefault(name.lower(), []).append(cache_key(it))
    with requests.Session() as s:
        users = s.get(f"{url}/Users", headers=headers, params={'isDisabled': False}, timeout=30).json()
        if not users:
            return
        uid = users[0]['Id']
        start = 0
        while True:
            r = s.get(f"{url}/Users/{uid}/Items", headers=headers, timeout=120, params={
                'Recursive': 'true', 'IncludeItemTypes': 'Movie,Episode', 'Fields': 'Path,MediaStreams',
                'StartIndex': start, 'Limit': PAGE})
            r.raise_for_status()
            page = r.json().get('Items') or []
            found = {}
            for m in page:
                keys = by_name.get(os.path.basename((m.get('Path') or '').replace('\\', '/')).lower())
                for k in keys or []:
                    found[k] = _jf_tracks(m)
            if found:
                _store(found)
            with _LOCK:
                _SCAN['done'] = min(_SCAN.get('total', 0), _SCAN.get('done', 0) + len(found))
            if len(page) < PAGE:
                break
            start += PAGE


def _remember_unknown(items):
    """Items the server couldn't tell us about (no episode key, not matched)
    are cached as 'no embedded tracks' for an hour, so every page load
    doesn't scan them again."""
    keys = [cache_key(it) for it in items if cache_key(it)]
    have = cached_tracks(keys)
    unknown = {k: [] for k in keys if k not in have}
    if unknown:
        _store(unknown, retry_minutes=UNKNOWN_RETRY_MIN)
        logging.info(f"[subs] embedded-subtitle scan: {len(unknown)} item(s) not found on the media server yet; "
                     f"checking again in {UNKNOWN_RETRY_MIN} minutes")


def start_scan(items):
    """Background: resolve keys and cache embedded tracks for items not cached
    yet. No-op if Plex isn't configured or a scan is running."""
    if not available():
        return False
    with _LOCK:
        if _SCAN.get('running'):
            return False
        _SCAN.update({'running': True, 'done': 0, 'total': len(items), 'started_at': time.time()})

    which = server()

    def run():
        try:
            have = cached_tracks([cache_key(it) for it in items])
            todo_items = [it for it in items if cache_key(it) and cache_key(it) not in have]
            with _LOCK:
                _SCAN['total'] = len(todo_items)
            if which == 'jellyfin':
                if todo_items:
                    _jellyfin_scan(todo_items)
                _remember_unknown(todo_items)
                logging.info(f"[subs] Jellyfin/Emby embedded-subtitle scan: {len(todo_items)} item(s) checked")
                return
            pairs = []
            for it in todo_items:
                try:
                    k = rating_key(it)
                except Exception:
                    k = None
                if k:
                    pairs.append((cache_key(it), k))
            fetch_tracks(pairs)
            _remember_unknown(todo_items)
            todo = todo_items
            logging.info(f"[subs] Plex embedded-subtitle scan: {len(todo)} item(s) checked")
        except Exception as e:
            logging.warning(f"[subs] Plex embedded-subtitle scan failed: {e}")
        finally:
            with _LOCK:
                _SCAN['running'] = False
                _SCAN['finished_at'] = time.time()
    threading.Thread(target=run, daemon=True, name='subs-plex-scan').start()
    return True


def good_embedded_langs(tracks):
    """Languages covered by embedded tracks that play without transcoding:
    text formats only (not image PGS/VobSub or styled ASS/SSA), not forced
    (a forced-flagged track that is clearly full dialogue still counts)."""
    from utilities.subtitle_lang import counts_as_full, key
    out = set()
    for t in tracks or []:
        if not counts_as_full(t) or t.get('codec') not in TEXT_CODECS:
            continue
        k = key(t.get('lang')) or key(t.get('code'))
        if k:
            out.add(k)
    return out
