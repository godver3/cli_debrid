import json
import logging

from database.core import get_db_connection

logger = logging.getLogger(__name__)

_PRUNE_DAYS = 90

# Max repair attempts before giving up (requires manual intervention)
MAX_REPAIR_ATTEMPTS = 3

# Backoff formula: BASE_BACKOFF_HOURS * 2^(attempts-1), capped at MAX_BACKOFF_HOURS
BASE_BACKOFF_HOURS = 1
MAX_BACKOFF_HOURS = 24


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
                repair_attempts      INTEGER DEFAULT 0,
                last_repair_at       TIMESTAMP,
                next_repair_at       TIMESTAMP,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_created
                ON nzb_repair_activity (created_at);
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_outcome
                ON nzb_repair_activity (outcome);
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_item
                ON nzb_repair_activity (item_id);
            CREATE INDEX IF NOT EXISTS idx_nzb_repair_broken_id
                ON nzb_repair_activity (broken_nzb_id);
        """)
        # Migrate existing tables that lack the new columns
        try:
            conn.execute("ALTER TABLE nzb_repair_activity ADD COLUMN repair_attempts INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE nzb_repair_activity ADD COLUMN last_repair_at TIMESTAMP")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE nzb_repair_activity ADD COLUMN next_repair_at TIMESTAMP")
        except Exception:
            pass
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
    repair_attempts: int = 0,
    next_repair_at=None,
) -> None:
    """outcome: 'replaced' | 'not_found' | 'no_replacement' | 'submission_failed' |
                'plex_deleted' | 'error' | 'skipped_backoff' | 'skipped_max_attempts'"""
    try:
        from datetime import datetime as _dt
        now = _dt.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection()
        conn.execute(
            """INSERT INTO nzb_repair_activity
               (item_id, title, media_type, season_number, episode_number,
                broken_nzb_id, broken_nzb_title, replacement_nzb_id, replacement_title,
                outcome, triggered_by, repair_attempts, last_repair_at, next_repair_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, title, media_type, season_number, episode_number,
             broken_nzb_id, broken_nzb_title, replacement_nzb_id, replacement_title,
             outcome, triggered_by, repair_attempts, now, next_repair_at),
        )
        conn.commit()
        conn.execute(
            f"DELETE FROM nzb_repair_activity WHERE created_at < datetime('now', '-{_PRUNE_DAYS} days')"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[NZBRepair] log_repair_activity error: {e}")


def get_repair_state(broken_nzb_id: str) -> dict:
    """Return repair state for a broken NZB ID: attempts count, last attempt time, next allowed time."""
    if not broken_nzb_id:
        return {'attempts': 0, 'last_repair_at': None, 'next_repair_at': None, 'give_up': False}
    try:
        conn = get_db_connection()
        row = conn.execute(
            """SELECT repair_attempts, last_repair_at, next_repair_at
               FROM nzb_repair_activity
               WHERE broken_nzb_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (broken_nzb_id,),
        ).fetchone()
        conn.close()
        if not row:
            return {'attempts': 0, 'last_repair_at': None, 'next_repair_at': None, 'give_up': False}
        attempts = row[0] or 0
        return {
            'attempts': attempts,
            'last_repair_at': row[1],
            'next_repair_at': row[2],
            'give_up': attempts >= MAX_REPAIR_ATTEMPTS,
        }
    except Exception as e:
        logger.debug(f"[NZBRepair] get_repair_state error: {e}")
        return {'attempts': 0, 'last_repair_at': None, 'next_repair_at': None, 'give_up': False}


def calculate_next_repair_at(attempts: int) -> str:
    """Calculate next_repair_at timestamp using exponential backoff."""
    from datetime import datetime as _dt, timedelta as _td
    backoff_hours = min(BASE_BACKOFF_HOURS * (2 ** max(0, attempts - 1)), MAX_BACKOFF_HOURS)
    next_at = _dt.utcnow() + _td(hours=backoff_hours)
    return next_at.strftime('%Y-%m-%d %H:%M:%S')


def is_in_backoff(broken_nzb_id: str) -> bool:
    """Return True if this broken NZB is still in backoff period (too soon to retry)."""
    state = get_repair_state(broken_nzb_id)
    if not state['next_repair_at']:
        return False
    try:
        from datetime import datetime as _dt
        next_at = _dt.strptime(state['next_repair_at'], '%Y-%m-%d %H:%M:%S')
        return _dt.utcnow() < next_at
    except Exception:
        return False


def get_repair_activity(limit: int = 100, offset: int = 0, outcome: str = None, source: str = None):
    """
    Get repair activity log entries.
    source='usenet'  → only NZB entries (broken_nzb_id NOT LIKE 'debrid:%')
    source='debrid'  → only debrid entries (broken_nzb_id LIKE 'debrid:%')
    source=None      → all entries
    """
    conn = get_db_connection()
    try:
        # Build WHERE clause
        conditions = []
        params = []
        if outcome:
            conditions.append("outcome = ?")
            params.append(outcome)
        if source == 'usenet':
            conditions.append("(broken_nzb_id NOT LIKE 'debrid:%' OR broken_nzb_id IS NULL OR broken_nzb_id = '')")
        elif source == 'debrid':
            conditions.append("broken_nzb_id LIKE 'debrid:%'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM nzb_repair_activity {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM nzb_repair_activity {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total
    except Exception as e:
        logger.debug(f"[NZBRepair] get_repair_activity error: {e}")
        return [], 0
    finally:
        conn.close()


def get_repair_stats(days: int = 30, source: str = None) -> dict:
    conn = get_db_connection()
    try:
        since = f"datetime('now', '-{days} days')"
        source_filter = ""
        if source == 'usenet':
            source_filter = " AND (broken_nzb_id NOT LIKE 'debrid:%' OR broken_nzb_id IS NULL OR broken_nzb_id = '')"
        elif source == 'debrid':
            source_filter = " AND broken_nzb_id LIKE 'debrid:%'"
        rows = conn.execute(
            f"SELECT outcome, COUNT(*) FROM nzb_repair_activity "
            f"WHERE created_at >= {since}{source_filter} GROUP BY outcome"
        ).fetchall()
        stats = {r[0]: r[1] for r in rows}
        return {
            'replaced': stats.get('replaced', 0),
            'not_found': stats.get('not_found', 0),
            'no_replacement': stats.get('no_replacement', 0),
            'submission_failed': stats.get('submission_failed', 0),
            'plex_deleted': stats.get('plex_deleted', 0),
            'skipped_backoff': stats.get('skipped_backoff', 0),
            'skipped_max_attempts': stats.get('skipped_max_attempts', 0),
            'error': stats.get('error', 0),
            'total': sum(stats.values()),
        }
    except Exception as e:
        logger.debug(f"[NZBRepair] get_repair_stats error: {e}")
        return {k: 0 for k in ('replaced', 'not_found', 'no_replacement', 'submission_failed',
                                'plex_deleted', 'skipped_backoff', 'skipped_max_attempts', 'error', 'total')}
    finally:
        conn.close()
