"""What the subtitle downloader saved: provider, score, why it matched, where.

Feeds the Subtitles tool page. In Plex mode it is also the only record of a
subtitle cli_debrid uploaded, since the file itself lives in Plex.
"""
import json
import logging

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
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_subtitle_history_item ON subtitle_history (item_id);
        CREATE INDEX IF NOT EXISTS idx_subtitle_history_created ON subtitle_history (created_at);
    """)
    try:
        conn.execute("ALTER TABLE subtitle_history ADD COLUMN sync TEXT")
    except Exception:
        pass
    _ready = True


def record(item_id, title, language, provider, score, max_score, matches,
           release, fmt, saved_via, path, source, sync=None):
    conn = get_db_connection()
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO subtitle_history (item_id, title, language, provider, score, max_score, "
            "matches, release, format, saved_via, path, source, sync) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, title, language, provider, score, max_score, json.dumps(sorted(matches or [])),
             release, fmt, saved_via, path, source, sync))
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


def languages_by_item(item_ids):
    """{item_id: set(language)} recorded for these items."""
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
                    f"SELECT item_id, language FROM subtitle_history WHERE item_id IN ({q})", chunk):
                out.setdefault(r[0], set()).add(r[1])
        return out
    finally:
        conn.close()
