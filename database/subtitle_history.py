"""What the subtitle downloader saved: provider, score, why it matched, where.

Feeds the Subtitles tool page. In Plex mode it is also the only record of a
subtitle cli_debrid uploaded, since the file itself lives in Plex.
"""
import json
import logging
import os

from database.core import get_db_connection

_ready = False
_KEEP_ROWS = 20000


def _ensure_table(conn):
    global _ready
    if _ready:
        return
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subtitle_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER,
            title       TEXT,
            language    TEXT,
            provider    TEXT,
            score       INTEGER,
            max_score   INTEGER,
            matches     TEXT,
            release     TEXT,
            format      TEXT,
            saved_via   TEXT,
            path        TEXT,
            source      TEXT,
            sync        TEXT,
            media_key   TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_subtitle_history_item ON subtitle_history (item_id);
        CREATE INDEX IF NOT EXISTS idx_subtitle_history_created ON subtitle_history (created_at);
        CREATE INDEX IF NOT EXISTS idx_subtitle_history_path ON subtitle_history (path);
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_sync_checked (
            digest     TEXT PRIMARY KEY,
            result     TEXT,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_attempts (
            item_id   INTEGER PRIMARY KEY,
            tried_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            found     INTEGER NOT NULL DEFAULT 0
        )""")
    for col in ('sync', 'media_key'):
        try:
            conn.execute(f"ALTER TABLE subtitle_history ADD COLUMN {col} TEXT")
        except Exception:
            pass
    _ready = True


def record(item_id, title, language, provider, score, max_score, matches,
           release, fmt, saved_via, path, source, sync=None, media_key=None):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        norm_path = os.path.normpath(str(path)) if (path and saved_via == 'sidecar') else path
        conn.execute(
            "INSERT INTO subtitle_history (item_id, title, language, provider, score, max_score, "
            "matches, release, format, saved_via, path, source, sync, media_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, title, language, provider, score, max_score, json.dumps(sorted(matches or [])),
             release, fmt, saved_via, norm_path, source, sync, media_key))
        conn.execute(
            "DELETE FROM subtitle_history WHERE id <= "
            "(SELECT MAX(id) FROM subtitle_history) - ?", (_KEEP_ROWS,))
        conn.commit()
    except Exception as e:
        logging.debug(f"[subs] history write failed: {e}")
    finally:
        conn.close()


def _rows(cur):
    out = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d['matches'] = json.loads(d.get('matches') or '[]')
        except ValueError:
            d['matches'] = []
        out.append(d)
    return out


def recent(limit=100, offset=0):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        return _rows(conn.execute(
            "SELECT * FROM subtitle_history ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)))
    finally:
        conn.close()


def for_item(item_id):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        return _rows(conn.execute(
            "SELECT * FROM subtitle_history WHERE item_id = ? ORDER BY id DESC", (item_id,)))
    finally:
        conn.close()


def media_key_of(item):
    """Which file a subtitle belongs to: the Plex ratingKey (a repair
    replacement in Plex mode is a new Plex item) or the file name."""
    item = item or {}
    # episodes carry their SHOW's ratingKey in ms_item_id, so the file name
    # identifies the file better; movies' ms_item_id is their own
    if item.get('type') == 'episode':
        return str(item.get('filled_by_file') or item.get('ms_item_id') or '') or None
    return str(item.get('ms_item_id') or item.get('filled_by_file') or '') or None


def languages_by_item(item_ids, keys=None):
    """{item_id: set(language)} recorded for these items. With keys
    ({item_id: media_key}), rows saved for an earlier file of the item (before
    a repair replacement or upgrade) don't count."""
    if not item_ids:
        return {}
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        out = {}
        ids = list(item_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ','.join('?' * len(chunk))
            for r in conn.execute(
                    f"SELECT item_id, language, media_key FROM subtitle_history WHERE item_id IN ({q})", chunk):
                want = (keys or {}).get(r[0])
                if want and r[2] and r[2] not in (want if isinstance(want, (tuple, set, list)) else (want,)):
                    continue
                out.setdefault(r[0], set()).add(r[1])
        return out
    finally:
        conn.close()


def record_attempt(item_id, found):
    """Last search of an item (manual, bulk or task) and whether it saved anything."""
    if item_id is None:
        return
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        # found: 0 nothing saved, 1 saved, 2 every language already embedded
        value = 2 if found == 'embedded' else (1 if found else 0)
        conn.execute("INSERT INTO subtitle_attempts (item_id, tried_at, found) VALUES (?, CURRENT_TIMESTAMP, ?) "
                     "ON CONFLICT(item_id) DO UPDATE SET tried_at = CURRENT_TIMESTAMP, found = excluded.found",
                     (item_id, value))
        conn.commit()
    except Exception as e:
        logging.debug(f"[subs] attempt write failed: {e}")
    finally:
        conn.close()


def recently_tried_without_result(hours):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        return {r[0] for r in conn.execute(
            "SELECT item_id FROM subtitle_attempts WHERE found = 0 AND tried_at >= datetime('now', ?)",
            (f'-{int(hours)} hours',))}
    finally:
        conn.close()


def embedded_items(item_ids):
    """Items whose last search found every language already embedded."""
    if not item_ids:
        return set()
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        out, ids = set(), list(item_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ','.join('?' * len(chunk))
            out |= {r[0] for r in conn.execute(
                f"SELECT item_id FROM subtitle_attempts WHERE found = 2 AND item_id IN ({q})", chunk)}
        return out
    finally:
        conn.close()


def sync_checked(digest):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        return conn.execute("SELECT 1 FROM subtitle_sync_checked WHERE digest = ?", (digest,)).fetchone() is not None
    finally:
        conn.close()


def mark_sync_checked(digest, result):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        conn.execute("INSERT OR REPLACE INTO subtitle_sync_checked (digest, result, checked_at) "
                     "VALUES (?, ?, CURRENT_TIMESTAMP)", (digest, (result or '')[:200]))
        conn.commit()
    except Exception as e:
        logging.debug(f"[subs] sync-checked write failed: {e}")
    finally:
        conn.close()


def sync_result(digest):
    """The stored result for a checked subtitle, or None if never checked."""
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        row = conn.execute("SELECT result FROM subtitle_sync_checked WHERE digest = ?", (digest,)).fetchone()
        return (row[0] or '') if row else None
    finally:
        conn.close()


def clyde_sidecar_paths(saved_via='sidecar'):
    """Set of normalized paths Clyde recorded writing via `saved_via`
    (default 'sidecar'). Returns an empty set on DB error so callers
    prefer keeping a file over deleting or overwriting it."""
    try:
        conn = get_db_connection()
    except Exception:
        return set()
    try:
        _ensure_table(conn)
        if saved_via is None:
            rows = conn.execute(
                "SELECT DISTINCT path FROM subtitle_history "
                "WHERE path IS NOT NULL AND path != ''").fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT path FROM subtitle_history "
                "WHERE saved_via = ? AND path IS NOT NULL AND path != ''",
                (saved_via,)).fetchall()
        return {os.path.normpath(r[0]) for r in rows if r[0]}
    except Exception as e:
        logging.debug(f"[subs] history path lookup failed: {e}")
        return set()
    finally:
        conn.close()


def is_clyde_path(path, saved_via='sidecar'):
    """True only when `path` is recorded in subtitle_history as written by
    Clyde (and, for local sidecars, is not a symlink). On missing/ambiguous
    history or DB error, returns False so callers keep the file."""
    if not path:
        return False
    p_str = str(path)
    if saved_via == 'sidecar' and os.path.islink(p_str):
        return False
    norm = os.path.normpath(p_str)
    try:
        conn = get_db_connection()
    except Exception:
        return False
    try:
        _ensure_table(conn)
        candidates = (p_str,) if norm == p_str else (p_str, norm)
        q = ','.join('?' * len(candidates))
        if saved_via is None:
            row = conn.execute(
                f"SELECT 1 FROM subtitle_history WHERE path IN ({q}) LIMIT 1",
                candidates).fetchone()
        else:
            row = conn.execute(
                f"SELECT 1 FROM subtitle_history WHERE saved_via = ? AND path IN ({q}) LIMIT 1",
                (saved_via, *candidates)).fetchone()
        return row is not None
    except Exception as e:
        logging.debug(f"[subs] history path check failed: {e}")
        return False
    finally:
        conn.close()


def plex_uploaded_languages(item_id, keys=None):
    """Set of languages Clyde recorded uploading directly to Plex
    (`saved_via = 'plex'`) for `item_id` (filtered by media_key when both
    are present). Returns an empty set on error."""
    if item_id is None:
        return set()
    try:
        conn = get_db_connection()
    except Exception:
        return set()
    try:
        _ensure_table(conn)
        want = None
        if keys:
            want = set(keys) if isinstance(keys, (tuple, set, list)) else {keys}
        out = set()
        for r in conn.execute(
                "SELECT language, media_key FROM subtitle_history "
                "WHERE item_id = ? AND saved_via = 'plex'", (item_id,)):
            if want and r[1] and r[1] not in want:
                continue
            if r[0]:
                out.add(r[0])
        return out
    except Exception as e:
        logging.debug(f"[subs] plex upload history lookup failed: {e}")
        return set()
    finally:
        conn.close()
