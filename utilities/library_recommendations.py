"""Discover recommendations built from the user's own library.

Used when neither Trakt nor Scrob is connected. Seeds come from one viewer's
watch history (utilities/viewer_history: any Plex account or Emby/Jellyfin
user; the main user also gets the owner-only watch_history.db), or everyone's.
The main user's and everyone's seeds are topped up with recently collected titles.
Each seed's TMDB recommendations are fetched once a week and cached on disk;
a title suggested by several seeds ranks higher. Anything already in the
database (any state), anything the viewer watched, and the manual blacklist
are dropped.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from utilities.settings import get_setting
from utilities.tmdb_cache import get_tmdb_json

SEED_TTL = 7 * 24 * 3600        # per-seed TMDB recommendations
IMDB_MAP_TTL = 30 * 24 * 3600   # imdb -> tmdb lookups (they don't change)
RESULT_TTL = 3600               # the finished, scored list

MAX_SEEDS = 40
MAX_WATCH_SEEDS = 30            # leave room for collected titles when there is history
COLLECTED_WEIGHT = 0.5          # a collected title says less than a watched one
RECENCY_HALF_LIFE_DAYS = 90
MIN_VOTE_COUNT = 10
MAX_PER_TYPE = 40

_db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
_CACHE_FILE = os.path.join(_db_dir, 'library_recommendations_cache.json')

_lock = threading.Lock()        # file cache
_build_lock = threading.Lock()  # one build at a time
_file_cache = None
_result_cache = {}             # viewer -> {'data', 'expires'}

_KEEP_FIELDS = (
    'id', 'title', 'name', 'overview', 'poster_path', 'backdrop_path',
    'release_date', 'first_air_date', 'vote_average', 'vote_count',
    'genre_ids', 'original_language', 'adult',
)


def _load_file_cache():
    global _file_cache
    if _file_cache is None:
        _file_cache = {'seeds': {}, 'imdb': {}}
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE) as f:
                    data = json.load(f)
                _file_cache['seeds'] = data.get('seeds') or {}
                _file_cache['imdb'] = data.get('imdb') or {}
        except Exception as e:
            logging.warning(f"Library recommendations: cache unreadable, starting fresh: {e}")
    return _file_cache


def _save_file_cache():
    now = time.time()
    with _lock:
        cache = _load_file_cache()
        cache['seeds'] = {k: v for k, v in cache['seeds'].items() if now - v.get('t', 0) < SEED_TTL}
        cache['imdb'] = {k: v for k, v in cache['imdb'].items() if now - v.get('t', 0) < IMDB_MAP_TTL}
        snapshot = json.dumps(cache)
    try:
        tmp = _CACHE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            f.write(snapshot)
        os.replace(tmp, _CACHE_FILE)
    except Exception as e:
        logging.debug(f"Library recommendations: could not save cache: {e}")


def _recency(ts, now):
    """1.0 for today, halving every RECENCY_HALF_LIFE_DAYS."""
    if not ts:
        return 0.25
    if not isinstance(ts, datetime):
        try:
            ts = datetime.fromisoformat(str(ts).replace('Z', '')[:19])
        except ValueError:
            return 0.25
    age_days = max((now - ts).total_seconds() / 86400, 0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def _watch_rows():
    path = os.path.join(_db_dir, 'watch_history.db')
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT type, tmdb_id, imdb_id, title, show_title, watched_at "
                "FROM watch_history WHERE watched_at IS NOT NULL "
                "ORDER BY watched_at DESC LIMIT 5000"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Library recommendations: watch_history.db read failed: {e}")
        return []


def _library_rows():
    """(type, tmdb_id, state, title, collected_at) for every movie/show in media_items."""
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT type, tmdb_id, "
            "MAX(CASE WHEN state = 'Collected' THEN 1 ELSE 0 END), "
            "MAX(title), MAX(collected_at) "
            "FROM media_items WHERE tmdb_id IS NOT NULL AND tmdb_id != '' "
            "AND type IN ('movie', 'episode') GROUP BY type, tmdb_id"
        ).fetchall()
    finally:
        conn.close()


def _media_type(db_type):
    return 'movie' if db_type == 'movie' else 'tv'


def _resolve_imdb(imdb_ids, api_key):
    """Map IMDb IDs to (media_type, tmdb_id) through TMDB /find, cached for a month."""
    now = time.time()
    with _lock:
        cache = _load_file_cache()['imdb']
        out = {i: cache[i] for i in imdb_ids if i in cache and now - cache[i].get('t', 0) < IMDB_MAP_TTL}
    missing = [i for i in imdb_ids if i not in out]

    def find(imdb_id):
        try:
            data = get_tmdb_json(
                f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={api_key}&external_source=imdb_id",
                timeout=8,
            )
        except Exception:
            return imdb_id, None
        for key, mt in (('movie_results', 'movie'), ('tv_results', 'tv')):
            if data.get(key):
                return imdb_id, {'t': now, 'type': mt, 'id': str(data[key][0]['id'])}
        return imdb_id, {'t': now, 'type': None, 'id': None}

    if missing:
        with ThreadPoolExecutor(max_workers=8) as ex:
            found = [r for r in ex.map(find, missing) if r[1] is not None]
        with _lock:
            cache = _load_file_cache()['imdb']
            for imdb_id, entry in found:
                cache[imdb_id] = entry
                out[imdb_id] = entry
    return {i: (e['type'], e['id']) for i, e in out.items() if e.get('id')}


def _resolve_viewer(viewer):
    """'main', 'all', or a viewer id such as 'plex:3'. The main user's own id maps to 'main'."""
    if not viewer or viewer == 'main':
        return 'main'
    if viewer == 'all':
        return 'all'
    from utilities.viewer_history import main_viewer_id
    return 'main' if viewer == main_viewer_id() else viewer


def _history(viewer):
    """(media_type, tmdb_id, imdb_id, title, watched_at, plays) for the viewer.

    The owner-only watch_history.db counts as the main user's history.
    """
    from utilities import viewer_history
    rows = []
    if viewer in ('main', 'all'):
        for db_type, tmdb_id, imdb_id, title, show_title, watched_at in _watch_rows():
            mt = _media_type(db_type)
            rows.append((mt, tmdb_id, imdb_id, title if mt == 'movie' else (show_title or title), watched_at, 1))
    source_viewer = viewer_history.main_viewer_id() if viewer == 'main' else viewer
    try:
        rows.extend(viewer_history.get_plays(source_viewer))
    except Exception as e:
        logging.debug(f"Library recommendations: viewer_history read failed: {e}")
    return rows


def _collect_seeds(api_key, viewer='main'):
    """Return (seeds, excluded_keys). A seed is {key, title, weight, verb}.

    The collection tops up the main user's and everyone's seeds; another user's
    recommendations come from their own history only.
    """
    now = datetime.now()
    excluded = set()

    watched = {}
    unresolved = {}

    def add(key, name, watched_at, plays):
        entry = watched.setdefault(key, {'title': name, 'last': None, 'plays': 0})
        entry['plays'] += plays or 1
        if watched_at and (entry['last'] is None or str(watched_at) > str(entry['last'])):
            entry['last'] = watched_at

    for mt, tmdb_id, imdb_id, name, watched_at, plays in _history(viewer):
        if tmdb_id:
            add((mt, str(tmdb_id)), name, watched_at, plays)
        elif imdb_id:
            unresolved.setdefault(imdb_id, []).append((mt, name, watched_at, plays))

    if unresolved:
        resolved = _resolve_imdb(list(unresolved), api_key)
        for imdb_id, entries in unresolved.items():
            for mt, name, watched_at, plays in entries:
                if imdb_id in resolved and resolved[imdb_id][0] == mt:
                    add((mt, resolved[imdb_id][1]), name, watched_at, plays)

    excluded.update(watched)
    watch_seeds = []
    for key, e in watched.items():
        # A show watched for ten episodes counts double a one-episode sample.
        depth = 1 + min(e['plays'] - 1, 9) / 9
        watch_seeds.append({'key': key, 'title': e['title'], 'verb': 'watched',
                            'weight': _recency(e['last'], now) * depth})
    watch_seeds.sort(key=lambda s: s['weight'], reverse=True)
    seeds = watch_seeds[:MAX_WATCH_SEEDS]

    # Anything in the library is dropped for every viewer: the list is for
    # deciding what to add. Only the main user's and everyone's seeds get the
    # collection top-up.
    collection_seeds = viewer in ('main', 'all')
    collected = []
    try:
        for db_type, tmdb_id, is_collected, title, collected_at in _library_rows():
            key = (_media_type(db_type), str(tmdb_id))
            excluded.add(key)
            if collection_seeds and is_collected and key not in watched:
                collected.append({'key': key, 'title': title, 'verb': 'collected',
                                  'weight': COLLECTED_WEIGHT * _recency(collected_at, now)})
    except Exception as e:
        logging.warning(f"Library recommendations: media_items read failed: {e}")
    collected.sort(key=lambda s: s['weight'], reverse=True)
    seeds += collected[:MAX_SEEDS - len(seeds)]

    try:
        from database.manual_blacklist import get_manual_blacklist
        whole = [i for i, v in get_manual_blacklist().items()
                 if v.get('media_type') != 'episode' or not v.get('seasons')]
        if whole:
            excluded.update(_resolve_imdb(whole, api_key).values())
    except Exception as e:
        logging.debug(f"Library recommendations: manual blacklist skipped: {e}")

    return seeds, excluded


def _seed_recommendations(key, api_key):
    """TMDB's recommendations for one seed; None when the fetch failed."""
    cache_key = f"{key[0]}:{key[1]}"
    now = time.time()
    with _lock:
        hit = _load_file_cache()['seeds'].get(cache_key)
    if hit and now - hit.get('t', 0) < SEED_TTL:
        return hit['r']
    try:
        data = get_tmdb_json(
            f"https://api.themoviedb.org/3/{key[0]}/{key[1]}/recommendations"
            f"?api_key={api_key}&language=en-US&page=1",
            timeout=8,
        )
    except Exception as e:
        logging.debug(f"Library recommendations: TMDB fetch failed for {cache_key}: {e}")
        return None
    results = [{f: r.get(f) for f in _KEEP_FIELDS} for r in data.get('results', [])]
    with _lock:
        _load_file_cache()['seeds'][cache_key] = {'t': now, 'r': results}
    return results


def _reason(contributions):
    top = sorted(contributions, key=lambda c: c[0], reverse=True)
    verb = top[0][1]['verb']
    names = [c[1]['title'] for c in top if c[1]['verb'] == verb][:2]
    return f"Because you {verb} {' and '.join(names)}"


def build_recommendations(api_key, viewer='main'):
    """Score every seed's TMDB recommendations; return discover-shaped results."""
    seeds, excluded = _collect_seeds(api_key, viewer)
    if not seeds:
        return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        fetched = list(ex.map(lambda s: _seed_recommendations(s['key'], api_key), seeds))
    _save_file_cache()

    candidates = {}
    for seed, recs in zip(seeds, fetched):
        mt = seed['key'][0]
        for rank, rec in enumerate(recs or []):
            key = (mt, str(rec.get('id')))
            if key in excluded or rec.get('adult') or not rec.get('poster_path'):
                continue
            if (rec.get('vote_count') or 0) < MIN_VOTE_COUNT:
                continue
            c = candidates.setdefault(key, {'rec': rec, 'score': 0.0, 'from': []})
            share = seed['weight'] * (1 - rank / 40)
            c['score'] += share
            c['from'].append((share, seed))

    per_type = {'movie': [], 'tv': []}
    for (mt, _), c in sorted(candidates.items(), key=lambda kv: kv[1]['score'], reverse=True):
        if len(per_type[mt]) >= MAX_PER_TYPE:
            continue
        item = {k: v for k, v in c['rec'].items() if k != 'adult'}
        item['media_type'] = mt
        item['rec_reason'] = _reason(c['from'])
        item['rec_score'] = round(c['score'], 3)
        per_type[mt].append(item)

    logging.info(
        f"Library recommendations ({viewer}): {len(seeds)} seeds -> {len(per_type['movie'])} movies, "
        f"{len(per_type['tv'])} shows"
    )
    return per_type['movie'] + per_type['tv']


def get_library_recommendations(viewer=None):
    """Cached wrapper around build_recommendations. Returns a fresh list each call."""
    api_key = str(get_setting('TMDB', 'api_key', '') or '').strip()
    if not api_key:
        return []
    viewer = _resolve_viewer(viewer)
    with _build_lock:
        entry = _result_cache.get(viewer)
        if entry is None or time.time() >= entry['expires']:
            entry = {'data': build_recommendations(api_key, viewer), 'expires': time.time() + RESULT_TTL}
            _result_cache[viewer] = entry
        return [dict(item) for item in entry['data']]


def clear_result_cache():
    """Drop the finished lists (seed caches stay), e.g. after a history sync."""
    with _build_lock:
        _result_cache.clear()
