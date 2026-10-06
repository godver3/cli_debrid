"""Drop blacklisted titles from a Plex watchlist fetch before metadata processing.

Trakt and Scrob sources already skip blacklisted and ghostlisted titles in their fetchers.
The Plex sources did not, so every startup run (which bypasses the per-source cache) and
every scheduled run whose cache entry had expired sent those titles through metadata
processing, only for add_wanted_items to throw them away. This gate applies the same rules
add_wanted_items would, but per title and before the expensive work:

- manual blacklist: the whole title is listed (a show with only some seasons listed passes)
- ghostlisted: any row of a movie, or every row of a show
- Blacklisted state: any row of a movie, or every row of a show is Blacklisted/ghostlisted.
  Passes when the source unblacklists on run, or when granular version additions are on
  (add_wanted_items does not stop a granular add on a Blacklisted row either).

Blocked titles are not written to the source cache, so un-blacklisting takes effect on the
next run.
"""
import logging
from typing import Any, Dict, List, Tuple

from content_checkers.source_run_report import PLEX_SOURCE_TYPES

_CHUNK = 500


def _load_rows(ids: List[str]) -> Dict[str, List[Tuple[str, str, int]]]:
    """Map each imdb/tmdb id to its (type, state, ghostlisted) rows."""
    from database.core import get_db_connection

    rows: Dict[str, List[Tuple[str, str, int]]] = {}
    if not ids:
        return rows
    conn = get_db_connection()
    try:
        for column in ('imdb_id', 'tmdb_id'):
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start:start + _CHUNK]
                placeholders = ','.join('?' * len(chunk))
                cursor = conn.execute(
                    f"SELECT {column}, type, state, ghostlisted FROM media_items WHERE {column} IN ({placeholders})",
                    chunk,
                )
                for key, item_type, state, ghostlisted in cursor.fetchall():
                    rows.setdefault(str(key), []).append((item_type, state, ghostlisted))
    finally:
        conn.close()
    return rows


def _block_reason(item: Dict[str, Any], rows_by_id, unblacklist: bool, granular: bool):
    from database.manual_blacklist import is_blacklisted

    is_movie = item.get('media_type') == 'movie'
    ids = [str(i) for i in (item.get('imdb_id'), item.get('tmdb_id')) if i]
    if any(is_blacklisted(i) for i in ids):
        return 'manual blacklist'

    rows = []
    for i in ids:
        rows.extend(r for r in rows_by_id.get(i, []) if (r[0] == 'movie') == is_movie)
    if not rows:
        return None

    ghost = [r[2] == 1 for r in rows]
    blocked = [r[2] == 1 or r[1] == 'Blacklisted' for r in rows]
    if (any(ghost) if is_movie else all(ghost)):
        return 'ghostlisted (deleted by the user)'
    if unblacklist or granular:
        return None
    if (any(blocked) if is_movie else all(blocked)):
        return 'Blacklisted'
    return None


def drop_blocked_items(wanted_content, source: str, source_type: str, unblacklist: bool, granular: bool, report):
    """Return wanted_content without the blocked titles; each one is recorded on the run report.

    A no-op for non-Plex sources.
    """
    if source_type not in PLEX_SOURCE_TYPES or not wanted_content:
        return wanted_content

    is_batched = isinstance(wanted_content[0], tuple)
    batches = wanted_content if is_batched else [(wanted_content, None)]
    ids = sorted({str(i) for items, _ in batches for item in items
                  for i in (item.get('imdb_id'), item.get('tmdb_id')) if i})
    try:
        rows_by_id = _load_rows(ids)
    except Exception as e:
        # Never lose a whole run to this check: add_wanted_items still applies the same rules.
        logging.error(f"Blacklist pre-check failed for {source}, processing every item: {e}")
        return wanted_content

    filtered = []
    for items, versions in batches:
        kept = []
        for item in items:
            reason = _block_reason(item, rows_by_id, unblacklist, granular)
            if reason:
                report.blocked(reason, item)
            else:
                kept.append(item)
        filtered.append((kept, versions))
    return filtered if is_batched else filtered[0][0]
