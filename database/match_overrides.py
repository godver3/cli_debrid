"""Durable title -> IMDb ID corrections recorded by the library Fix Match button.

Every import of a file cli_debrid did not place itself re-derives the match
from the release name: fuzzy-search the metadata provider for the parsed title
and take the best-scoring result, falling back to the first result when nothing
clears the threshold. That search is stateless, so a title it resolves wrongly
(``Sugar`` 2024 landing on ``Sugar Sugar Honey``) resolves wrongly again for
every new release, and a Fix Match correction is undone as soon as the next
file arrives.

An override records "this title, this year, is that IMDb ID" so the importer
can short-circuit the search. Keyed on title *and* year deliberately: a title
alone would hijack an unrelated show that happens to share the name.

The title stored is the *corrected* one, which is what release names actually
carry -- the wrong show's title is what the DB held, not what the files are
called.
"""

import logging
from datetime import datetime
from typing import Optional

from .core import get_db_connection
from .database_reading import normalize_string_for_comparison


OVERRIDE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS match_overrides (
        norm_title TEXT NOT NULL,
        year INTEGER,
        media_type TEXT NOT NULL,
        imdb_id TEXT NOT NULL,
        title TEXT,
        updated_at TIMESTAMP NOT NULL,
        PRIMARY KEY (norm_title, year, media_type)
    )
"""


def ensure_match_override_table(conn) -> None:
    conn.execute(OVERRIDE_TABLE_SQL)


def _normalize(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    normalized = normalize_string_for_comparison(title)
    return normalized.strip().lower() if normalized else None


def _coerce_year(year) -> Optional[int]:
    try:
        return int(year) if year not in (None, '') else None
    except (TypeError, ValueError):
        return None


def set_match_override(title: str, year, media_type: str, imdb_id: str) -> bool:
    """Record (or replace) the correction for one title/year/type."""
    norm_title = _normalize(title)
    if not norm_title or not imdb_id or media_type not in ('show', 'movie'):
        return False

    conn = get_db_connection()
    try:
        ensure_match_override_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO match_overrides "
            "(norm_title, year, media_type, imdb_id, title, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (norm_title, _coerce_year(year), media_type, imdb_id, title, datetime.now()),
        )
        conn.commit()
        logging.info(f"[MatchOverride] '{title}' ({year}) [{media_type}] -> {imdb_id}")
        return True
    except Exception as e:
        logging.error(f"[MatchOverride] Could not record override for '{title}': {e}",
                      exc_info=True)
        return False
    finally:
        conn.close()


def get_match_override(title: str, year, media_type: str) -> Optional[str]:
    """
    The IMDb ID recorded for this title/year/type, or None.

    A row stored with no year acts as a wildcard, so a correction made from an
    entry whose year was unknown still applies to releases that carry one.
    """
    norm_title = _normalize(title)
    if not norm_title or media_type not in ('show', 'movie'):
        return None

    conn = get_db_connection()
    try:
        ensure_match_override_table(conn)
        # Exact year first, then the year-less wildcard.
        row = conn.execute(
            "SELECT imdb_id FROM match_overrides "
            "WHERE norm_title = ? AND media_type = ? AND year IS ? ",
            (norm_title, media_type, _coerce_year(year)),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT imdb_id FROM match_overrides "
                "WHERE norm_title = ? AND media_type = ? AND year IS NULL",
                (norm_title, media_type),
            ).fetchone()
        return row['imdb_id'] if row else None
    except Exception as e:
        logging.error(f"[MatchOverride] Lookup failed for '{title}': {e}", exc_info=True)
        return None
    finally:
        conn.close()


def find_match_override(titles, year, media_type: str) -> Optional[str]:
    """First override hit across several candidate titles (folder, filename…)."""
    seen = set()
    for title in titles:
        norm = _normalize(title)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        imdb_id = get_match_override(title, year, media_type)
        if imdb_id:
            return imdb_id
    return None
