"""
ai_habits.py — Phase 5: Habit tracking for the AI Butler.

Records significant user-initiated actions so the AI can detect patterns
and suggest automation or scheduling.

Table schema (ai_habits in media_items.db):
  id          INTEGER PK AUTOINCREMENT
  action      TEXT    — e.g. 'debug_run_wanted', 'manual_blacklist_add', 'upgrade_scan'
  detail      TEXT    — optional context (source name, item title, etc.)
  user_id     TEXT    — username or 'system'
  created_at  TEXT    — ISO datetime

Patterns the AI can detect from this log:
- User runs "get wanted from source X" every morning → suggest scheduling
- User manually triggers upgrade scan every few days → suggest enabling auto-scan
- User frequently removes items from blacklist → scraper config issue
- User repeatedly adds items to library manually → suggest adding Trakt list
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TABLE_INITIALIZED = False


def _ensure_table(conn):
    global _TABLE_INITIALIZED
    if _TABLE_INITIALIZED:
        return
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_habits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                action     TEXT    NOT NULL,
                detail     TEXT,
                user_id    TEXT    NOT NULL DEFAULT 'system',
                created_at TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_habits_action ON ai_habits(action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_habits_created ON ai_habits(created_at)"
        )
        conn.commit()
        _TABLE_INITIALIZED = True
    except Exception as e:
        logger.debug(f"ai_habits: table init failed: {e}")


def track_action(action: str, detail: str = '', user_id: str = 'system'):
    """
    Record a user action in the ai_habits table.
    Non-blocking — swallows all errors to never disrupt the caller.

    action examples:
      'wanted_source_run'       — user triggered "get wanted from source"
      'upgrade_scan_manual'     — user clicked Upgrade Hub → Scan Now
      'blacklist_remove'        — user removed item from blacklist
      'library_add_manual'      — user added item via content requestor
      'debug_function_run'      — user ran a debug function
      'queue_pause'             — user paused the queue
      'queue_resume'            — user resumed the queue
    """
    # Respect the Phase 5 toggle — bail early if disabled
    try:
        from utilities.settings import get_setting
        if not get_setting('AI Assistant', 'enable_habit_tracking', True):
            return
    except Exception:
        pass
    try:
        from database import get_db_connection
        conn = get_db_connection()
        _ensure_table(conn)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "INSERT INTO ai_habits (action, detail, user_id, created_at) VALUES (?, ?, ?, ?)",
            (action, detail[:500] if detail else '', user_id, ts)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"ai_habits: track_action failed: {e}")


def get_habit_summary() -> str:
    """
    Analyse the ai_habits table and return a plain-text summary for the system prompt.
    Focuses on frequency patterns that the AI can act on.
    """
    try:
        from database import get_db_connection
        conn = get_db_connection()
        _ensure_table(conn)
        c = conn.cursor()

        # Action frequency over the last 30 days
        c.execute("""
            SELECT action, COUNT(*) as cnt,
                   MAX(created_at) as last_seen,
                   MIN(created_at) as first_seen
            FROM ai_habits
            WHERE created_at >= datetime('now', '-30 days')
            GROUP BY action
            ORDER BY cnt DESC
        """)
        rows = c.fetchall()

        # Recent actions (last 20)
        c.execute("""
            SELECT action, detail, user_id, created_at
            FROM ai_habits
            ORDER BY id DESC LIMIT 20
        """)
        recent = c.fetchall()

        # Hour-of-day distribution for the most frequent actions
        c.execute("""
            SELECT action,
                   CAST(strftime('%H', created_at) AS INTEGER) as hour,
                   COUNT(*) as cnt
            FROM ai_habits
            WHERE created_at >= datetime('now', '-30 days')
            GROUP BY action, hour
            ORDER BY action, cnt DESC
        """)
        hourly = c.fetchall()
        conn.close()

        if not rows:
            return '  No habit data recorded yet (less than one day of activity).'

        lines = ['  Action frequency (last 30 days):']
        for action, cnt, last_seen, first_seen in rows:
            lines.append(f"    {action}: {cnt}x (last: {last_seen[:10]})")

        # Peak hours per action
        peak_hours: dict = {}
        for action, hour, cnt in hourly:
            if action not in peak_hours:
                peak_hours[action] = (hour, cnt)

        if peak_hours:
            lines.append('\n  Typical activity hours (UTC):')
            for action, (hour, cnt) in peak_hours.items():
                lines.append(f"    {action}: most often at {hour:02d}:xx UTC ({cnt}x)")

        lines.append('\n  Recent actions (newest first):')
        for action, detail, user_id, created_at in recent:
            det = f' — {detail}' if detail else ''
            lines.append(f"    [{created_at[:16]}] {action}{det}")

        return '\n'.join(lines)

    except Exception as e:
        logger.debug(f"ai_habits: get_habit_summary failed: {e}")
        return '  (unavailable)'
