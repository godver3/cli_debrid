"""
Overlay Activity Logger

Writes one row to overlay_activity for every meaningful overlay action.
Designed to be called fire-and-forget — never raises, always swallows errors.
"""

import json
import logging

logger = logging.getLogger(__name__)


def log_activity(
    action_type: str,
    *,
    triggered_by: str = 'manual',
    result: str = 'success',
    title: str = None,
    stats: dict = None,
    duration_seconds: int = None,
) -> None:
    """
    Insert one activity row into overlay_activity.

    Args:
        action_type:      Short identifier, e.g. 'overlay_sync', 'cleanup',
                          'generate', 'regenerate', 'remove', 'layout_create',
                          'layout_update', 'layout_delete', 'sync_library',
                          'season_generate', 'season_regenerate', 'season_remove',
                          'full_sync', 'batch_generate', 'generate_all',
                          'regenerate_all', 'remove_all'
        triggered_by:     'manual' or 'scheduled'
        result:           'success', 'failed', or 'partial'
        title:            Human-readable one-liner shown in the activity row
        stats:            Dict of counts / details serialised as JSON
        duration_seconds: How long the task took in seconds (integer)
    """
    try:
        from database.core import get_db_connection
        import sqlite3
        conn = get_db_connection()
        conn.execute(
            '''INSERT INTO overlay_activity
               (action_type, triggered_by, result, title, stats_json, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (
                action_type,
                triggered_by,
                result,
                title,
                json.dumps(stats) if stats else None,
                duration_seconds,
            )
        )
        conn.commit()
        # Prune rows older than 30 days
        conn.execute(
            "DELETE FROM overlay_activity WHERE created_at < datetime('now', '-30 days')"
        )
        conn.commit()
        conn.close()
    except Exception as _e:
        logger.warning(f"Failed to log overlay activity ({action_type}): {_e}")
