import json
import logging

from database.core import get_db_connection

logger = logging.getLogger(__name__)

_PRUNE_DAYS = 90


def create_nzb_repair_activity_table() -> None:
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS nzb_repair_activity (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id              INTEGER,
                title                TEXT,
                media_type           TEXT,
                season_number        INTEGER,
                episode_number       INTEGER,
                broken_nzb_id        TEXT,
                broken_nzb_title     TEXT,
                replacement_nzb_id   TEXT,
                replacement_title    TEXT,
                outcome              TEXT NOT NULL DEFAULT 'unknown',
                triggered_by         TEXT NOT NULL DEFAULT 'scheduled',
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_created
                ON nzb_repair_activity (created_at);
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_outcome
                ON nzb_repair_activity (outcome);
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_item
                ON nzb_repair_activity (item_id);
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"[NZBRepair] Could not create activity table: {e}")
    finally:
        conn.close()


def log_repair_activity(
    *,
    item_id: int = None,
    title: str = None,
    media_type: str = None,
    season_number: int = None,
    episode_number: int = None,
    broken_nzb_id: str = None,
    broken_nzb_title: str = None,
    replacement_nzb_id: str = None,
    replacement_title: str = None,
    outcome: str,
    triggered_by: str = 'scheduled',
) -> None:
    """outcome: 'replaced' | 'not_found' | 'plex_deleted' | 'error'"""
    try:
        conn = get_db_connection()
        conn.execute(
            """INSERT INTO nzb_repair_activity
               (item_id, title, media_type, season_number, episode_number,
                broken_nzb_id, broken_nzb_title, replacement_nzb_id, replacement_title,
                outcome, triggered_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, title, media_type, season_number, episode_number,
             broken_nzb_id, broken_nzb_title, replacement_nzb_id, replacement_title,
             outcome, triggered_by),
        )
        conn.commit()
        conn.execute(
            f"DELETE FROM nzb_repair_activity WHERE created_at < datetime('now', '-{_PRUNE_DAYS} days')"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[NZBRepair] log_repair_activity error: {e}")


def get_repair_activity(limit: int = 100, offset: int = 0, outcome: str = None):
    conn = get_db_connection()
    try:
        if outcome:
            total = conn.execute(
                "SELECT COUNT(*) FROM nzb_repair_activity WHERE outcome = ?", (outcome,)
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM nzb_repair_activity WHERE outcome = ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (outcome, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM nzb_repair_activity").fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM nzb_repair_activity
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], total
    except Exception as e:
        logger.debug(f"[NZBRepair] get_repair_activity error: {e}")
        return [], 0
    finally:
        conn.close()


def get_repair_stats(days: int = 30) -> dict:
    conn = get_db_connection()
    try:
        since = f"datetime('now', '-{days} days')"
        rows = conn.execute(
            f"SELECT outcome, COUNT(*) FROM nzb_repair_activity "
            f"WHERE created_at >= {since} GROUP BY outcome"
        ).fetchall()
        stats = {r[0]: r[1] for r in rows}
        return {
            'replaced': stats.get('replaced', 0),
            'not_found': stats.get('not_found', 0),
            'plex_deleted': stats.get('plex_deleted', 0),
            'error': stats.get('error', 0),
            'total': sum(stats.values()),
        }
    except Exception as e:
        logger.debug(f"[NZBRepair] get_repair_stats error: {e}")
        return {'replaced': 0, 'not_found': 0, 'plex_deleted': 0, 'error': 0, 'total': 0}
    finally:
        conn.close()
