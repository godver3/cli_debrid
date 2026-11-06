"""Plex Reported Issues: one row per issue Plex users reported to this server.

Plex keeps the reports themselves (community.plex.tv) and offers no way to
close or dismiss one, so this table is how cli_debrid remembers which reports
it has already handled. Statuses:

  baseline     - already there the first time the feature ran; never acted on
  replacing    - matched items were sent back to Wanted for a different release
  replaced     - every matched item is Collected again; the "done" reply was posted
  needs_review - reported a whole show/season, or the items couldn't be replaced
  no_match     - the Plex item isn't one cli_debrid manages
  ignored      - for another server, or the reporter's item no longer exists
  failed       - an error while handling it; retried every few minutes
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from database.core import get_db_connection

logger = logging.getLogger(__name__)


def create_plex_reported_issues_table() -> None:
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS plex_reported_issues (
                report_id    TEXT PRIMARY KEY,
                rating_key   TEXT,
                server_id    TEXT,
                message      TEXT,
                reporter     TEXT,
                reported_at  TEXT,
                title        TEXT,
                status       TEXT NOT NULL,
                detail       TEXT,
                item_ids     TEXT,
                triggered    INTEGER NOT NULL DEFAULT 0,
                handled_at   TIMESTAMP,
                done_at      TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_plex_reported_issues_status
                ON plex_reported_issues (status);
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"[PLEX_REPORTS] Could not create plex_reported_issues table: {e}")
    finally:
        conn.close()


def _row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d['item_ids'] = json.loads(d.get('item_ids') or '[]')
    except (TypeError, ValueError):
        d['item_ids'] = []
    return d


def get_known_report_ids() -> set:
    conn = get_db_connection()
    try:
        return {r[0] for r in conn.execute("SELECT report_id FROM plex_reported_issues").fetchall()}
    finally:
        conn.close()


# Marker row written on the first run, so an inbox that was empty then doesn't make the
# first real report look like a pre-existing one.
INITIALIZED_MARKER = '__initialized__'


def is_initialized() -> bool:
    conn = get_db_connection()
    try:
        return conn.execute("SELECT 1 FROM plex_reported_issues WHERE report_id = ?",
                            (INITIALIZED_MARKER,)).fetchone() is not None
    finally:
        conn.close()


def mark_initialized() -> None:
    save_report({'id': INITIALIZED_MARKER}, 'baseline', 'First run')


def save_report(report: Dict[str, Any], status: str, detail: str = '', title: str = '',
                item_ids: Optional[List[int]] = None, triggered: bool = False) -> None:
    """Insert or update one report's row. report is the parsed Plex report (see
    utilities.plex_reported_issues.parse_report); triggered marks a report that sent its
    items back to Wanted (rather than finding them already on the way)."""
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO plex_reported_issues
                   (report_id, rating_key, server_id, message, reporter, reported_at,
                    title, status, detail, item_ids, triggered, handled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(report_id) DO UPDATE SET
                   title = excluded.title, status = excluded.status, detail = excluded.detail,
                   item_ids = excluded.item_ids, triggered = excluded.triggered,
                   handled_at = excluded.handled_at""",
            (report['id'], report.get('rating_key'), report.get('server_id'), report.get('message'),
             report.get('reporter'), report.get('date'), title, status, detail,
             json.dumps(item_ids or []), int(triggered), datetime.now()),
        )
        conn.commit()
    finally:
        conn.close()


def set_report_status(report_id: str, status: str, detail: Optional[str] = None,
                      done: bool = False) -> None:
    conn = get_db_connection()
    try:
        sets, params = ["status = ?"], [status]
        if detail is not None:
            sets.append("detail = ?")
            params.append(detail)
        if done:
            sets.append("done_at = ?")
            params.append(datetime.now())
        conn.execute(f"UPDATE plex_reported_issues SET {', '.join(sets)} WHERE report_id = ?",
                     (*params, report_id))
        conn.commit()
    finally:
        conn.close()


def count_recent_replacements(item_ids: List[int], since: datetime) -> int:
    """How many reports handled since `since` sent any of these items back to Wanted."""
    wanted = set(item_ids)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT item_ids FROM plex_reported_issues WHERE triggered = 1 AND handled_at >= ?",
            (since,)).fetchall()
    finally:
        conn.close()
    count = 0
    for (raw,) in rows:
        try:
            if wanted & set(json.loads(raw or '[]')):
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def get_failed_reports(handled_before: datetime) -> List[Dict[str, Any]]:
    """Reports that errored and haven't been retried since handled_before, oldest first, in
    the shape parse_report returns so they can be handled again."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM plex_reported_issues WHERE status = 'failed' AND handled_at < ? "
            "ORDER BY reported_at", (handled_before,)).fetchall()
    finally:
        conn.close()
    return [{'id': r['report_id'], 'rating_key': r['rating_key'], 'server_id': r['server_id'],
             'message': r['message'] or '', 'reporter': r['reporter'], 'date': r['reported_at']}
            for r in rows]


def get_reports(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM plex_reported_issues WHERE status = ? ORDER BY reported_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM plex_reported_issues WHERE status != 'baseline' "
                "ORDER BY reported_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
