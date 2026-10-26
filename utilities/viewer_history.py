"""Per-user watch history from Plex and Emby/Jellyfin, for Discover recommendations.

Kept separate from watch_history.db (the owner-only Plex sync that feeds the
"don't add watched items" setting). Each sync replaces one source's rows, so a
failed sync leaves the previous data in place. Viewer ids look like
``plex:1`` (account 1 is the server owner) or ``jellyfin:<user id>``.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta

import requests

from utilities.settings import get_setting

HISTORY_DAYS = 730
EMBY_ITEM_LIMIT = 3000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers (
    source TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT,
    is_main INTEGER DEFAULT 0,
    PRIMARY KEY (source, user_id)
);
CREATE TABLE IF NOT EXISTS plays (
    source TEXT NOT NULL,
    user_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id TEXT,
    imdb_id TEXT,
    title TEXT,
    last_watched TIMESTAMP,
    plays INTEGER DEFAULT 1,
    items INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS plays_viewer ON plays (source, user_id);
"""


def _db_path():
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'viewer_history.db')


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.executescript(_SCHEMA)
    if 'items' not in {row[1] for row in conn.execute("PRAGMA table_info(plays)")}:
        conn.execute("ALTER TABLE plays ADD COLUMN items INTEGER DEFAULT 1")
    return conn


def _store(source, viewers, plays):
    """Replace everything stored for one source.

    viewers: [(user_id, name, is_main)]
    plays: {(user_id, media_type, tmdb_id, imdb_id): {'title', 'last', 'plays', 'keys'}}
    ``plays`` counts every view; ``keys`` holds the distinct movies/episodes viewed,
    so plays > len(keys) means rewatches.
    """
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM viewers WHERE source = ?", (source,))
            conn.execute("DELETE FROM plays WHERE source = ?", (source,))
            conn.executemany(
                "INSERT INTO viewers (source, user_id, name, is_main) VALUES (?, ?, ?, ?)",
                [(source, str(uid), name, int(bool(main))) for uid, name, main in viewers],
            )
            conn.executemany(
                "INSERT INTO plays (source, user_id, media_type, tmdb_id, imdb_id, title, last_watched, plays, items) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (source, str(uid), mt, tmdb, imdb, p['title'],
                     p['last'].strftime('%Y-%m-%d %H:%M:%S') if p['last'] else None,
                     p['plays'], max(len(p.get('keys') or ()), 1))
                    for (uid, mt, tmdb, imdb), p in plays.items()
                ],
            )
    finally:
        conn.close()


def _add_play(plays, uid, mt, ids, title, watched_at, item_key, count=1):
    """Record ``count`` views of one movie/episode (``item_key``) under its title."""
    tmdb, imdb = ids
    if not tmdb and not imdb:
        return
    entry = plays.setdefault((str(uid), mt, tmdb, imdb), {'title': title, 'last': None, 'plays': 0, 'keys': set()})
    entry['plays'] += max(count, 1)
    entry['keys'].add(str(item_key))
    if watched_at and (entry['last'] is None or watched_at > entry['last']):
        entry['last'] = watched_at


def _guid_ids(guids):
    tmdb = imdb = None
    for guid in guids or []:
        value = str(getattr(guid, 'id', guid))
        if value.startswith('tmdb://'):
            tmdb = value[7:].split('?')[0]
        elif value.startswith('imdb://'):
            imdb = value[7:].split('?')[0]
    return tmdb, imdb


def _library_ids_by_title():
    """{(media_type, lower title, year): (tmdb_id, imdb_id)} from cli_debrid's own database.

    Fallback for Plex items whose metadata has no external ids.
    """
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT type, title, year, MAX(tmdb_id), MAX(imdb_id) FROM media_items "
                "WHERE type IN ('movie', 'episode') AND title IS NOT NULL "
                "AND (tmdb_id IS NOT NULL OR imdb_id IS NOT NULL) GROUP BY type, title, year"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Viewer history: media_items title lookup unavailable: {e}")
        return {}
    return {('movie' if t == 'movie' else 'tv', title.lower(), str(year or '')): (tmdb or None, imdb or None)
            for t, title, year, tmdb, imdb in rows}


def sync_plex():
    """Every account's plays on this Plex server. Returns (viewers, titles) counts."""
    url = get_setting('Plex', 'url', '')
    token = get_setting('Plex', 'token', '')
    if not url or not token:
        return None
    from plexapi.server import PlexServer
    plex = PlexServer(url, token, timeout=60)

    # Library items carry the guids; history entries only carry rating keys.
    by_key = {}
    by_title = None
    for section in plex.library.sections():
        if section.type not in ('movie', 'show'):
            continue
        mt = 'movie' if section.type == 'movie' else 'tv'
        for item in section.all():
            ids = _guid_ids(getattr(item, 'guids', None))
            if not any(ids):
                if by_title is None:
                    by_title = _library_ids_by_title()
                ids = by_title.get((mt, (item.title or '').lower(), str(getattr(item, 'year', '') or '')), ids)
            by_key[str(item.ratingKey)] = (mt, ids, item.title)

    viewers = [(a.id, a.name, a.id == 1) for a in plex.systemAccounts() if a.id and a.name]
    plays = {}
    mindate = datetime.now() - timedelta(days=HISTORY_DAYS)
    for entry in plex.history(mindate=mindate):
        if entry.type == 'movie':
            key = entry.ratingKey
        elif entry.type == 'episode':
            # History entries leave grandparentRatingKey empty; the show's key is
            # the tail of grandparentKey (/library/metadata/<key>).
            key = getattr(entry, 'grandparentRatingKey', None) or \
                str(getattr(entry, 'grandparentKey', None) or '').rsplit('/', 1)[-1] or None
        else:
            continue
        hit = by_key.get(str(key))
        if hit and entry.accountID:
            _add_play(plays, entry.accountID, hit[0], hit[1], hit[2], entry.viewedAt, entry.ratingKey)

    _store('plex', viewers, plays)
    return len(viewers), len(plays)


def _emby_ids(provider_ids):
    lowered = {k.lower(): v for k, v in (provider_ids or {}).items() if v}
    return lowered.get('tmdb'), lowered.get('imdb')


def _emby_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '')[:19])
    except ValueError:
        return None


def sync_emby():
    """Every user's played items on the Emby/Jellyfin server. Returns (viewers, titles) counts."""
    url = (get_setting('Debug', 'emby_jellyfin_url', '') or '').strip().rstrip('/')
    token = (get_setting('Debug', 'emby_jellyfin_token', '') or '').strip()
    if not url or not token:
        return None
    session = requests.Session()
    session.headers.update({'X-Emby-Token': token, 'Accept': 'application/json'})

    def get(path, **params):
        r = session.get(f"{url}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    try:
        product = get('/System/Info/Public').get('ProductName', '')
    except Exception:
        product = ''
    source = 'jellyfin' if 'jellyfin' in product.lower() else 'emby'

    users = get('/Users')
    main_id = next((u['Id'] for u in users if (u.get('Policy') or {}).get('IsAdministrator')), None)
    viewers = [(u['Id'], u.get('Name'), u['Id'] == main_id) for u in users]

    plays = {}
    for user in users:
        uid = user['Id']
        items = get(
            f"/Users/{uid}/Items", Recursive='true', IncludeItemTypes='Movie,Episode',
            Filters='IsPlayed', SortBy='DatePlayed', SortOrder='Descending',
            Limit=EMBY_ITEM_LIMIT, Fields='ProviderIds', EnableUserData='true',
        ).get('Items', [])

        series_ids = sorted({i['SeriesId'] for i in items if i.get('Type') == 'Episode' and i.get('SeriesId')})
        series = {}
        for start in range(0, len(series_ids), 100):
            for s in get(f"/Users/{uid}/Items", Ids=','.join(series_ids[start:start + 100]),
                         Fields='ProviderIds').get('Items', []):
                series[s['Id']] = (_emby_ids(s.get('ProviderIds')), s.get('Name'))

        for item in items:
            user_data = item.get('UserData') or {}
            watched_at = _emby_date(user_data.get('LastPlayedDate'))
            count = user_data.get('PlayCount') or 1
            if item.get('Type') == 'Movie':
                _add_play(plays, uid, 'movie', _emby_ids(item.get('ProviderIds')), item.get('Name'),
                          watched_at, item.get('Id'), count)
            elif item.get('SeriesId') in series:
                ids, name = series[item['SeriesId']]
                _add_play(plays, uid, 'tv', ids, name or item.get('SeriesName'), watched_at, item.get('Id'), count)

    _store(source, viewers, plays)
    return len(viewers), len(plays)


def sync_all():
    """Sync every configured server; one failing doesn't stop the other."""
    for name, fn in (('Plex', sync_plex), ('Emby/Jellyfin', sync_emby)):
        try:
            counts = fn()
            if counts:
                logging.info(f"Viewer history: {name} synced {counts[0]} users, {counts[1]} titles")
        except Exception as e:
            logging.error(f"Viewer history: {name} sync failed: {e}", exc_info=True)


def get_viewers():
    """[{'id': 'plex:1', 'name', 'source', 'is_main'}], main users first."""
    if not os.path.exists(_db_path()):
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT v.source, v.user_id, v.name, v.is_main FROM viewers v "
            "WHERE EXISTS (SELECT 1 FROM plays p WHERE p.source = v.source AND p.user_id = v.user_id) "
            "ORDER BY v.is_main DESC, v.source = 'plex' DESC, lower(v.name)"
        ).fetchall()
    finally:
        conn.close()
    return [{'id': f"{s}:{u}", 'name': n or u, 'source': s, 'is_main': bool(m)} for s, u, n, m in rows]


def main_viewer_id():
    viewers = get_viewers()
    mains = [v for v in viewers if v['is_main']]
    return (mains or viewers or [{'id': None}])[0]['id']


def get_plays(viewer_id):
    """(media_type, tmdb_id, imdb_id, title, last_watched, plays, items) for one viewer, or 'all'."""
    if not viewer_id or not os.path.exists(_db_path()):
        return []
    query = "SELECT media_type, tmdb_id, imdb_id, title, last_watched, plays, items FROM plays"
    args = ()
    if viewer_id != 'all':
        source, _, user_id = viewer_id.partition(':')
        query += " WHERE source = ? AND user_id = ?"
        args = (source, user_id)
    conn = _connect()
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()
