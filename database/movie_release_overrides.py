"""Durable, per-movie release-date overrides.

Overrides are keyed by a stable external identifier so every version of a
movie, including versions added later, receives the same effective date.
"""

import logging
import sqlite3
from datetime import date, datetime
from typing import Dict, Optional

from .core import get_db_connection


OVERRIDE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS movie_release_date_overrides (
        media_key TEXT PRIMARY KEY,
        imdb_id TEXT,
        tmdb_id TEXT,
        release_date DATE NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        updated_by TEXT
    )
"""

_AVAILABILITY_STATES = {'Unreleased', 'Wanted'}


def ensure_movie_release_override_table(conn) -> None:
    conn.execute(OVERRIDE_TABLE_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_movie_release_overrides_imdb "
        "ON movie_release_date_overrides(imdb_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_movie_release_overrides_tmdb "
        "ON movie_release_date_overrides(tmdb_id)"
    )


def _media_key(imdb_id: Optional[str], tmdb_id: Optional[str]) -> Optional[str]:
    if imdb_id:
        return f"imdb:{str(imdb_id).strip().lower()}"
    if tmdb_id:
        return f"tmdb:{str(tmdb_id).strip()}"
    return None


def _resolve_movie(conn, media_id) -> Optional[Dict]:
    value = str(media_id).strip()
    if value.lower().startswith('tt'):
        row = conn.execute(
            "SELECT id, imdb_id, tmdb_id, title FROM media_items "
            "WHERE imdb_id = ? AND type = 'movie' LIMIT 1",
            (value,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, imdb_id, tmdb_id, title FROM media_items "
            "WHERE tmdb_id = ? AND type = 'movie' LIMIT 1",
            (value,),
        ).fetchone()
        if not row and value.isdigit():
            row = conn.execute(
                "SELECT id, imdb_id, tmdb_id, title FROM media_items "
                "WHERE id = ? AND type = 'movie' LIMIT 1",
                (int(value),),
            ).fetchone()
    return dict(row) if row else None


def _identity_clause(movie: Dict):
    if movie.get('imdb_id'):
        return 'imdb_id = ?', str(movie['imdb_id'])
    return 'tmdb_id = ?', str(movie['tmdb_id'])


def get_movie_release_override(
    imdb_id: Optional[str] = None,
    tmdb_id: Optional[str] = None,
    conn=None,
) -> Optional[Dict]:
    key = _media_key(imdb_id, tmdb_id)
    if not key:
        return None

    owns_connection = conn is None
    if owns_connection:
        conn = get_db_connection()
    try:
        try:
            row = conn.execute(
                "SELECT media_key, imdb_id, tmdb_id, release_date, updated_at, updated_by "
                "FROM movie_release_date_overrides WHERE media_key = ?",
                (key,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc).lower():
                raise
            ensure_movie_release_override_table(conn)
            row = conn.execute(
                "SELECT media_key, imdb_id, tmdb_id, release_date, updated_at, updated_by "
                "FROM movie_release_date_overrides WHERE media_key = ?",
                (key,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_connection:
            conn.close()


def _requires_physical_release(version: Optional[str]) -> bool:
    try:
        from utilities.settings import get_setting

        versions = get_setting('Scraping', 'versions', {}) or {}
        clean_version = str(version or '').replace('*', '')
        settings = versions.get(version, {}) or versions.get(clean_version, {}) or {}
        return bool(settings.get('require_physical_release', False))
    except Exception:
        logging.exception("Unable to evaluate physical-release setting for version %r", version)
        return False


def _availability_state(
    current_state: str,
    effective_date: str,
    version: Optional[str],
    physical_release_date: Optional[str],
    as_of: date,
) -> str:
    if current_state not in _AVAILABILITY_STATES:
        return current_state

    date_to_check = effective_date
    if _requires_physical_release(version):
        if not physical_release_date or str(physical_release_date).lower() in {'unknown', 'none'}:
            return 'Unreleased'
        date_to_check = str(physical_release_date)

    try:
        eligible = datetime.strptime(str(date_to_check), '%Y-%m-%d').date() <= as_of
    except (TypeError, ValueError):
        return 'Unreleased'
    return 'Wanted' if eligible else 'Unreleased'


def apply_movie_release_override_to_item(item: Dict, conn=None, as_of: Optional[date] = None) -> Dict:
    """Apply a stored override to a movie dict before it is inserted."""
    if (item.get('type') or '').lower() != 'movie':
        return item
    override = get_movie_release_override(item.get('imdb_id'), item.get('tmdb_id'), conn=conn)
    if not override:
        return item

    item['release_date'] = override['release_date']
    current_state = item.get('state') or 'Unreleased'
    item['state'] = _availability_state(
        current_state,
        override['release_date'],
        item.get('version'),
        item.get('physical_release_date'),
        as_of or date.today(),
    )
    return item


def _apply_effective_date(conn, movie: Dict, effective_date: str, as_of: date) -> int:
    clause, value = _identity_clause(movie)
    rows = conn.execute(
        f"SELECT id, state, version, physical_release_date FROM media_items "
        f"WHERE {clause} AND type = 'movie'",
        (value,),
    ).fetchall()

    now = datetime.now()
    for row in rows:
        new_state = _availability_state(
            row['state'], effective_date, row['version'], row['physical_release_date'], as_of
        )
        conn.execute(
            "UPDATE media_items SET release_date = ?, state = ?, last_updated = ? WHERE id = ?",
            (effective_date, new_state, now, row['id']),
        )
    return len(rows)


def set_movie_release_override(
    media_id,
    release_date: str,
    updated_by: Optional[str] = None,
    as_of: Optional[date] = None,
) -> Dict:
    parsed_date = datetime.strptime(release_date, '%Y-%m-%d').date()
    normalized_date = parsed_date.isoformat()
    conn = get_db_connection()
    try:
        ensure_movie_release_override_table(conn)
        movie = _resolve_movie(conn, media_id)
        if not movie:
            raise LookupError('Movie not found')
        key = _media_key(movie.get('imdb_id'), movie.get('tmdb_id'))
        if not key:
            raise ValueError('Movie has neither an IMDb nor TMDB identifier')

        now = datetime.now()
        conn.execute(
            """
            INSERT INTO movie_release_date_overrides
                (media_key, imdb_id, tmdb_id, release_date, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_key) DO UPDATE SET
                imdb_id = excluded.imdb_id,
                tmdb_id = excluded.tmdb_id,
                release_date = excluded.release_date,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (key, movie.get('imdb_id'), movie.get('tmdb_id'), normalized_date, now, updated_by),
        )
        affected = _apply_effective_date(conn, movie, normalized_date, as_of or date.today())
        conn.commit()
        return {
            'media_key': key,
            'release_date': normalized_date,
            'affected_count': affected,
            'title': movie.get('title'),
            'imdb_id': movie.get('imdb_id'),
            'tmdb_id': movie.get('tmdb_id'),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_movie_release_override(
    media_id,
    provider_release_date: Optional[str],
    as_of: Optional[date] = None,
) -> Dict:
    effective_date = provider_release_date or 'Unknown'
    conn = get_db_connection()
    try:
        ensure_movie_release_override_table(conn)
        movie = _resolve_movie(conn, media_id)
        if not movie:
            raise LookupError('Movie not found')
        key = _media_key(movie.get('imdb_id'), movie.get('tmdb_id'))
        conn.execute("DELETE FROM movie_release_date_overrides WHERE media_key = ?", (key,))
        affected = _apply_effective_date(conn, movie, effective_date, as_of or date.today())
        conn.commit()
        return {
            'media_key': key,
            'release_date': effective_date,
            'affected_count': affected,
            'title': movie.get('title'),
            'imdb_id': movie.get('imdb_id'),
            'tmdb_id': movie.get('tmdb_id'),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refresh_movie_release_date(
    media_id,
    provider_release_date: Optional[str],
    as_of: Optional[date] = None,
) -> Dict:
    """Apply a freshly-fetched provider date without disturbing a manual override."""
    effective_date = provider_release_date or 'Unknown'
    conn = get_db_connection()
    try:
        ensure_movie_release_override_table(conn)
        movie = _resolve_movie(conn, media_id)
        if not movie:
            raise LookupError('Movie not found')
        key = _media_key(movie.get('imdb_id'), movie.get('tmdb_id'))
        if key and get_movie_release_override(movie.get('imdb_id'), movie.get('tmdb_id'), conn=conn):
            raise ValueError('A manual release date override is active; clear it to use the provider date')
        affected = _apply_effective_date(conn, movie, effective_date, as_of or date.today())
        conn.commit()
        return {
            'media_key': key,
            'release_date': effective_date,
            'affected_count': affected,
            'title': movie.get('title'),
            'imdb_id': movie.get('imdb_id'),
            'tmdb_id': movie.get('tmdb_id'),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
