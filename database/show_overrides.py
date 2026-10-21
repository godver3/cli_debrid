"""Per-show settings chosen on the library show page.

- series_type: 'anime' or 'standard' forces how the show is treated, whatever
  its genres say; NULL means automatic (see utilities/anime_detection.py).
- episode_order: which season/episode numbering the show uses, e.g.
  'tvdb:dvd' or 'tmdb_group:<id>'; NULL means the metadata source's default
  (TVDB aired order, or Trakt/TMDB order without a TVDB key).

Keyed by the show's IMDb ID, like the rest of the show's rows.
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

from .core import get_db_connection, retry_on_db_lock

SERIES_TYPES = ('anime', 'standard')

SHOW_OVERRIDES_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS show_overrides (
        imdb_id TEXT PRIMARY KEY,
        tmdb_id TEXT,
        series_type TEXT,
        episode_order TEXT,
        updated_at TIMESTAMP NOT NULL
    )
"""

_UNSET = object()


def ensure_show_overrides_table(conn) -> None:
    conn.execute(SHOW_OVERRIDES_TABLE_SQL)


@retry_on_db_lock()
def get_show_override(imdb_id: Optional[str]) -> Dict[str, Any]:
    """The show's overrides as a dict; empty when there are none."""
    if not imdb_id:
        return {}
    conn = get_db_connection()
    try:
        try:
            row = conn.execute("SELECT * FROM show_overrides WHERE imdb_id = ?", (imdb_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            # Only "no such table" is handled; a lock error goes to @retry_on_db_lock.
            if 'no such table' not in str(exc).lower():
                raise
            ensure_show_overrides_table(conn)
            row = None
        return dict(row) if row else {}
    except sqlite3.OperationalError:
        raise
    except Exception as e:
        logging.error(f"[ShowOverride] Lookup failed for {imdb_id}: {e}", exc_info=True)
        return {}
    finally:
        conn.close()


@retry_on_db_lock()
def set_show_override(imdb_id: str, tmdb_id: Optional[str] = None,
                      series_type: Any = _UNSET, episode_order: Any = _UNSET) -> bool:
    """Set the given fields (None clears one); fields not passed keep their value."""
    if not imdb_id:
        return False
    if series_type is not _UNSET and series_type not in (None,) + SERIES_TYPES:
        raise ValueError(f"series_type must be one of {SERIES_TYPES} or None, not {series_type!r}")
    current = get_show_override(imdb_id)
    row = {
        'imdb_id': imdb_id,
        'tmdb_id': str(tmdb_id) if tmdb_id else current.get('tmdb_id'),
        'series_type': current.get('series_type') if series_type is _UNSET else series_type,
        'episode_order': current.get('episode_order') if episode_order is _UNSET else episode_order,
        'updated_at': datetime.now(),
    }
    conn = get_db_connection()
    try:
        ensure_show_overrides_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO show_overrides (imdb_id, tmdb_id, series_type, episode_order, updated_at) "
            "VALUES (:imdb_id, :tmdb_id, :series_type, :episode_order, :updated_at)", row)
        conn.commit()
        if episode_order is not _UNSET:
            from scraper.functions.episode_order_mapping import clear_order_mapping_cache
            clear_order_mapping_cache(imdb_id)
        logging.info(f"[ShowOverride] {imdb_id}: series_type={row['series_type']!r}, "
                     f"episode_order={row['episode_order']!r}")
        return True
    except sqlite3.OperationalError:
        raise
    except Exception as e:
        logging.error(f"[ShowOverride] Could not save overrides for {imdb_id}: {e}", exc_info=True)
        return False
    finally:
        conn.close()


@retry_on_db_lock()
def apply_series_type_to_episodes(imdb_id: str, is_anime: bool) -> int:
    """Add or remove the 'anime' genre on the show's existing episode rows.

    Everything downstream (scraping, filtering, matching, folders) reads
    is-anime from each row's genres, so a changed series type takes effect
    without waiting for a metadata refresh. Returns the rows changed.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, genres FROM media_items WHERE imdb_id = ? AND type = 'episode'", (imdb_id,)
        ).fetchall()
        changed = 0
        for row in rows:
            genres = _parse_genres(row['genres'])
            has_anime = any(str(g).lower() == 'anime' for g in genres)
            if is_anime and not has_anime:
                new_genres = ['anime'] + genres
            elif not is_anime and has_anime:
                new_genres = [g for g in genres if str(g).lower() != 'anime']
            else:
                continue
            conn.execute("UPDATE media_items SET genres = ? WHERE id = ?", (json.dumps(new_genres), row['id']))
            changed += 1
        conn.commit()
        logging.info(f"[ShowOverride] {imdb_id}: {'added' if is_anime else 'removed'} 'anime' genre on {changed} episode rows")
        return changed
    finally:
        conn.close()


def _parse_genres(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (TypeError, ValueError):
        return [g.strip() for g in str(value).split(',') if g.strip()]
