"""Accent-insensitive, alias-aware title search helpers.

Used by the Library page search so that:
  * "pokemon" finds "Pokémon" (accents and case are folded on both sides), and
  * "money heist" finds a show stored under its original title "La casa de papel"
    (matched through the alternative titles the metadata battery already stores).
"""
import json
import logging
import os
import sqlite3
import threading
import time
import unicodedata

# Letters NFKD does not split into base + accent.
_EXTRA_FOLDS = str.maketrans({
    'æ': 'ae', 'ø': 'o', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'ł': 'l', 'œ': 'oe', 'ı': 'i',
})

_ALIAS_INDEX_TTL = 600  # seconds
_ALIAS_MIN_TERM_LEN = 3  # shorter terms would match unrelated foreign aliases
_alias_lock = threading.Lock()
_alias_index = {'ts': 0.0, 'mtime': None, 'entries': []}


def fold_title(text):
    """Lower-case, strip accents. Returns '' for None."""
    if not text:
        return ''
    text = unicodedata.normalize('NFKD', str(text).casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return text.translate(_EXTRA_FOLDS)


def register_fold_function(conn):
    """Expose fold_title() to SQL on this connection as FOLD_TITLE(x)."""
    try:
        conn.create_function('FOLD_TITLE', 1, fold_title, deterministic=True)
    except TypeError:  # sqlite3 < 3.8.3 has no deterministic flag
        conn.create_function('FOLD_TITLE', 1, fold_title)


def like_pattern(term):
    """Folded, LIKE-escaped '%term%' pattern (use with ESCAPE '\\')."""
    folded = fold_title(term).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{folded}%'


def title_search_clause(term, title_col='title', imdb_col='imdb_id'):
    """SQL fragment + params matching `term` against titles and battery aliases.

    The connection must have register_fold_function() applied. FOLD_TITLE only
    runs on titles containing non-ASCII characters; ASCII titles use plain LIKE
    (already ASCII case-insensitive) against the folded term, keeping the scan fast.
    """
    pattern = like_pattern(term)
    clause = (f"({title_col} LIKE ? ESCAPE '\\' OR ({title_col} GLOB '*[^ -~]*' "
              f"AND FOLD_TITLE({title_col}) LIKE ? ESCAPE '\\')")
    params = [pattern, pattern]
    ids = alias_imdb_ids(term)
    if ids:
        clause += f" OR {imdb_col} IN ({','.join('?' * len(ids))})"
        params.extend(ids)
    return clause + ')', params


def _battery_db_path():
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'cli_battery.db')


def _alias_values(raw):
    """Yield every title string from a stored aliases value.

    Battery stores aliases as JSON (sometimes double-encoded) shaped like
    {"us": ["Money Heist"], "es": ["La casa de papel"]}.
    """
    data = raw
    for _ in range(2):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return
    if isinstance(data, dict):
        for vals in data.values():
            if isinstance(vals, str):
                yield vals
            elif isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str):
                        yield v
    elif isinstance(data, list):
        for v in data:
            if isinstance(v, str):
                yield v


def _load_alias_index(path):
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT i.imdb_id, m.key, m.value FROM metadata m "
            "JOIN items i ON i.id = m.item_id "
            "WHERE m.key IN ('aliases', 'title') AND i.imdb_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    names = {}
    for imdb_id, key, value in rows:
        bucket = names.setdefault(imdb_id, set())
        if key == 'title':
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
            if isinstance(value, str):
                bucket.add(fold_title(value))
        else:
            for alias in _alias_values(value):
                bucket.add(fold_title(alias))
    return [(imdb_id, '\n'.join(sorted(n for n in bucket if n))) for imdb_id, bucket in names.items()]


def _get_alias_index():
    path = _battery_db_path()
    now = time.time()
    with _alias_lock:
        if now - _alias_index['ts'] < _ALIAS_INDEX_TTL:
            return _alias_index['entries']
        try:
            mtime = os.path.getmtime(path) if os.path.exists(path) else None
            wal = path + '-wal'
            if os.path.exists(wal):
                mtime = max(mtime or 0, os.path.getmtime(wal))
        except OSError:
            mtime = None
        if mtime is None:
            entries = []
        elif mtime == _alias_index['mtime'] and _alias_index['entries']:
            entries = _alias_index['entries']
        else:
            try:
                entries = _load_alias_index(path)
            except Exception as e:
                logging.debug(f"Title search: could not read battery aliases: {e}")
                entries = _alias_index['entries']
        _alias_index.update(ts=now, mtime=mtime, entries=entries)
        return entries


def alias_imdb_ids(term):
    """IMDb ids whose battery title or any alias contains the (folded) term."""
    folded = fold_title(term).strip()
    if len(folded) < _ALIAS_MIN_TERM_LEN:
        return []
    return [imdb_id for imdb_id, blob in _get_alias_index() if folded in blob]


def reset_alias_cache():
    with _alias_lock:
        _alias_index.update(ts=0.0, mtime=None, entries=[])
