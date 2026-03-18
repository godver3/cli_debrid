"""
Upgrade Hub Activity Logger

Provides fire-and-forget activity logging for all Upgrade Hub events:
  - Scans (manual and scheduled)
  - Queue actions (moving items to Upgrading state)
  - Upgrade outcomes (success/failed, old filename → new filename)

Mirrors the overlay_activity pattern from overlays/activity_logger.py.
"""

import json
import logging

from database.core import get_db_connection

logger = logging.getLogger(__name__)


def create_upgrade_hub_activity_table() -> None:
    """Create the upgrade_hub_activity and upgrade_hub_ignored tables if they don't exist."""
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS upgrade_hub_activity (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type  TEXT NOT NULL,
                triggered_by TEXT NOT NULL DEFAULT 'manual',
                result       TEXT NOT NULL DEFAULT 'success',
                title        TEXT,
                stats_json   TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_uh_activity_created
                ON upgrade_hub_activity (created_at);
            CREATE INDEX IF NOT EXISTS idx_uh_activity_type
                ON upgrade_hub_activity (action_type);
            CREATE TABLE IF NOT EXISTS upgrade_hub_ignored (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ignore_key  TEXT NOT NULL UNIQUE,
                imdb_id     TEXT NOT NULL,
                season      INTEGER,
                episode     INTEGER,
                item_type   TEXT NOT NULL DEFAULT 'season_pack',
                title       TEXT,
                ignored_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_uh_ignored_imdb
                ON upgrade_hub_ignored (imdb_id);
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Could not create activity table: {e}")
    finally:
        conn.close()


def _make_ignore_key(imdb_id: str, item_type: str, season=None, episode=None) -> str:
    return f"{imdb_id}|{item_type}|{season if season is not None else ''}|{episode if episode is not None else ''}"


def add_ignored_items(items: list) -> int:
    """
    Persist item-level ignores to upgrade_hub_ignored.
    Each item is a dict with: imdb_id, item_type, season (opt), episode (opt), title (opt).
    Returns count of newly inserted rows.
    """
    if not items:
        return 0
    conn = get_db_connection()
    added = 0
    try:
        for it in items:
            imdb_id   = it.get('imdb_id', '')
            item_type = it.get('item_type', 'season_pack')
            season    = it.get('season')
            episode   = it.get('episode')
            title     = it.get('title', '')
            if not imdb_id:
                continue
            key = _make_ignore_key(imdb_id, item_type, season, episode)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO upgrade_hub_ignored "
                    "(ignore_key, imdb_id, season, episode, item_type, title) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, imdb_id, season, episode, item_type, title),
                )
                added += conn.execute("SELECT changes()").fetchone()[0]
            except Exception as e:
                logger.debug(f"[UPGRADE_HUB] Could not insert ignored item: {e}")
        conn.commit()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] add_ignored_items error: {e}")
    finally:
        conn.close()
    return added


def get_ignored_items_set() -> frozenset:
    """
    Return a frozenset of (imdb_id, season_or_None) tuples for fast scan-time filtering.
    Also includes (imdb_id, None) for movies.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT imdb_id, season FROM upgrade_hub_ignored"
        ).fetchall()
        return frozenset((r[0], r[1]) for r in rows)
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] get_ignored_items_set error: {e}")
        return frozenset()
    finally:
        conn.close()


def get_ignored_items_list() -> list:
    """Return all ignored items as a list of dicts (for management UI)."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, imdb_id, season, episode, item_type, title, ignored_at "
            "FROM upgrade_hub_ignored ORDER BY ignored_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] get_ignored_items_list error: {e}")
        return []
    finally:
        conn.close()


def remove_ignored_item(row_id: int) -> bool:
    """Remove a single row from upgrade_hub_ignored by its primary key."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM upgrade_hub_ignored WHERE id = ?", (row_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] remove_ignored_item error: {e}")
        return False
    finally:
        conn.close()


def log_hub_activity(
    action_type: str,
    *,
    triggered_by: str = 'manual',
    result: str = 'success',
    title: str = None,
    stats: dict = None,
) -> None:
    """
    Fire-and-forget activity logger. Never raises.

    Args:
        action_type:  'scan' | 'queue' | 'upgrade_processed'
        triggered_by: 'manual' | 'scheduled'
        result:       'success' | 'failed' | 'partial'
        title:        Human-readable one-liner shown in the activity row
        stats:        Arbitrary dict of counts/details (serialised as JSON)
    """
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO upgrade_hub_activity "
            "(action_type, triggered_by, result, title, stats_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                action_type,
                triggered_by,
                result,
                title,
                json.dumps(stats) if stats else None,
            ),
        )
        conn.commit()
        # Prune rows older than 30 days
        conn.execute(
            "DELETE FROM upgrade_hub_activity WHERE created_at < datetime('now', '-30 days')"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Activity log error: {e}")


def get_hub_activity(
    limit: int = 50,
    offset: int = 0,
    action_type: str = None,
):
    """
    Fetch activity rows, newest first.

    Returns:
        (rows, total) where rows is a list of dicts and total is the unfiltered count.
    """
    conn = get_db_connection()
    try:
        if action_type:
            total = conn.execute(
                "SELECT COUNT(*) FROM upgrade_hub_activity WHERE action_type = ?",
                (action_type,),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id, action_type, triggered_by, result, title, stats_json, created_at "
                "FROM upgrade_hub_activity WHERE action_type = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (action_type, limit, offset),
            ).fetchall()
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM upgrade_hub_activity"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id, action_type, triggered_by, result, title, stats_json, created_at "
                "FROM upgrade_hub_activity "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

        result_list = []
        for row in rows:
            entry = dict(row)
            raw_stats = entry.pop('stats_json', None)
            try:
                entry['stats'] = json.loads(raw_stats) if raw_stats else None
            except Exception:
                entry['stats'] = None
            # Normalise timestamp to ISO string
            if entry.get('created_at') and not isinstance(entry['created_at'], str):
                entry['created_at'] = str(entry['created_at'])
            result_list.append(entry)

        return result_list, total
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not fetch activity: {e}")
        return [], 0
    finally:
        conn.close()
