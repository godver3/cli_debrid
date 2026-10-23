"""Item state history: one row per media_items state change, from any code path.

A trigger on media_items writes the row, so queue moves, Plex/local scans,
route handlers and raw SQL updates are all captured. update_media_item_state()
then tags the row with the code that made the change (`source`); rows written
by other paths keep source NULL.

AUTOMATED_TRANSITIONS describes the pipeline as designed. An automated change
outside it is logged as a warning (once per from/to/source per process) and
is never blocked: the point is to make unexpected moves visible, not to stop
them. Changes made from the web UI or API (routes.*) are manual and never
flagged.

Like scraper_grabs, recording is best-effort: errors are logged and swallowed
so history can never break the pipeline.
"""

import logging
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database.core import get_db_connection

logger = logging.getLogger(__name__)

_PRUNE_DAYS = 60

STATES = (
    'Wanted', 'Scraping', 'Adding', 'Checking', 'Pending Uncached', 'Sleeping',
    'Unreleased', 'Pre_release', 'Final_Check', 'Blacklisted', 'Ghostlisted',
    'Collected', 'Upgrading',
)

# from-state -> states the automated pipeline may move it to.
# Two targets are reachable from everywhere and left out of the table:
# Wanted (rescrapes, repairs and missing-file resets send items back to the
# start) and Collected (library scans adopt a file found for an item in any
# state).
_ALWAYS_REACHABLE = ('Wanted', 'Collected')
AUTOMATED_TRANSITIONS = {
    'Wanted': {'Scraping', 'Unreleased', 'Pre_release', 'Blacklisted'},
    'Scraping': {'Adding', 'Sleeping', 'Unreleased', 'Final_Check', 'Blacklisted'},
    'Adding': {'Checking', 'Pending Uncached', 'Scraping', 'Sleeping', 'Final_Check',
               'Blacklisted'},
    'Checking': {'Upgrading', 'Blacklisted'},
    'Pending Uncached': {'Checking', 'Blacklisted'},
    'Sleeping': {'Final_Check', 'Blacklisted'},
    'Unreleased': {'Pre_release', 'Scraping'},
    'Pre_release': {'Adding', 'Unreleased'},
    'Final_Check': {'Adding', 'Blacklisted'},
    'Blacklisted': {'Ghostlisted'},
    'Ghostlisted': set(),
    'Collected': {'Upgrading', 'Blacklisted'},
    'Upgrading': {'Checking', 'Blacklisted'},
}

_warned = set()


def create_state_transitions_table() -> None:
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS item_state_transitions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     INTEGER NOT NULL,
                from_state  TEXT,
                to_state    TEXT,
                changed_at  TIMESTAMP NOT NULL,
                source      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_state_transitions_item ON item_state_transitions (item_id);
            CREATE INDEX IF NOT EXISTS idx_state_transitions_changed ON item_state_transitions (changed_at);
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_state_transitions_insert
            AFTER INSERT ON media_items
            BEGIN
                INSERT INTO item_state_transitions (item_id, from_state, to_state, changed_at)
                VALUES (NEW.id, NULL, NEW.state, strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'));
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_state_transitions_update
            AFTER UPDATE OF state ON media_items
            WHEN OLD.state IS NOT NEW.state
            BEGIN
                INSERT INTO item_state_transitions (item_id, from_state, to_state, changed_at)
                VALUES (NEW.id, OLD.state, NEW.state, strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'));
            END
        """)
        # Runs at startup: keep the log bounded.
        conn.execute(
            "DELETE FROM item_state_transitions WHERE changed_at < ?",
            ((datetime.now() - timedelta(days=_PRUNE_DAYS)).isoformat(sep=' '),),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[StateHistory] Could not create item_state_transitions table: {e}")
    finally:
        conn.close()


def caller_source() -> str:
    """'module.function' of the first caller outside the state-writing helpers."""
    skip_modules = ('database.database_writing', 'database.state_transitions', 'database.core',
                    'queues.queue_manager')
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get('__name__', '')
        if not module.startswith(skip_modules) and not module.startswith('tenacity'):
            return f"{module}.{frame.f_code.co_name}"
        frame = frame.f_back
    return 'unknown'


def is_manual(source: Optional[str]) -> bool:
    """Changes made from the web UI or API: the user asked for them."""
    return bool(source) and source.startswith('routes.')


def is_expected(from_state: Optional[str], to_state: str) -> bool:
    if from_state is None or to_state in _ALWAYS_REACHABLE:
        return True
    allowed = AUTOMATED_TRANSITIONS.get(from_state)
    if allowed is None:
        return True  # a state this table doesn't know about: nothing to say
    return to_state in allowed


def tag_transition(conn, item_id: int, from_state: Optional[str], to_state: str, source: str) -> None:
    """Attach `source` to the row the trigger just wrote, and flag unexpected moves.

    Call inside the transaction that changed the state, after the UPDATE.
    """
    if from_state == to_state:
        return
    try:
        conn.execute(
            "UPDATE item_state_transitions SET source = ? "
            "WHERE id = (SELECT MAX(id) FROM item_state_transitions WHERE item_id = ?)",
            (source, item_id),
        )
    except Exception as e:
        logger.debug(f"[StateHistory] Could not tag transition for item {item_id}: {e}")
        return
    if is_manual(source) or is_expected(from_state, to_state):
        return
    key = (from_state, to_state, source)
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            f"[StateHistory] Unexpected state change {from_state} -> {to_state} "
            f"(item {item_id}, by {source}). Logged once per kind; see the item's history."
        )


def get_item_history(item_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    """Newest first."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT from_state, to_state, changed_at, source FROM item_state_transitions "
            "WHERE item_id = ? ORDER BY id DESC LIMIT ?",
            (item_id, limit),
        ).fetchall()
        history = []
        for r in rows:
            entry = dict(r)
            entry['manual'] = is_manual(entry['source'])
            entry['expected'] = entry['manual'] or is_expected(entry['from_state'], entry['to_state'])
            history.append(entry)
        return history
    except Exception as e:
        logger.warning(f"[StateHistory] Could not read history for item {item_id}: {e}")
        return []
    finally:
        conn.close()
