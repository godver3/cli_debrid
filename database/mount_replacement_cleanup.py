"""Durable playback-verification and exact mount-cleanup saga."""

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from database.core import get_db_connection


PLAYBACK_CLEANUP_REASONS = frozenset({
    'media_probe_failed',
    'media_no_playable_stream',
})
# Older cli_mount builds persisted this reason as broken. Ignore it when
# starting new repairs, while existing durable sagas remain able to finish.
RETIRED_PLAYBACK_REASONS = frozenset({'mount_read_error'})
NZB_INTERMEDIATE_REASONS = PLAYBACK_CLEANUP_REASONS | {'usenet_segment_missing'}
STALE_CLEANUP_REASONS = NZB_INTERMEDIATE_REASONS

_RETRY_SECONDS = (60, 300, 1800)
_LEGACY_RETRY_SECONDS = (60, 300, 1800, 7200, 86400)
_PROCESS_LOCK = threading.Lock()
_VIDEO_EXTENSIONS = frozenset({'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'})
_EPISODE_TOKEN = re.compile(
    r'(?i)(?:^|[^a-z0-9])s0*(\d{1,3})e0*(\d{1,3})(?![0-9])'
)
_SEASON_TOKEN = re.compile(
    r'(?i)(?:^|[^a-z0-9])s0*(\d{1,3})(?![a-z0-9])'
)
_REGISTRATION_STALE_ERRORS = frozenset({
    'stale_target: cli_debrid_id does not match the mounted file registration',
    'stale_target: cli_debrid_id is registered to a different current provider source',
})


def _add_column(conn, table: str, definition: str) -> None:
    try:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
    except Exception:
        pass


def _table_columns(conn, table: str) -> set:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except Exception:
        return set()


def _ensure_activity_identity_columns(conn) -> bool:
    columns = _table_columns(conn, 'nzb_repair_activity')
    if not columns:
        return False
    if 'cleanup_cli_debrid_id' not in columns:
        _add_column(conn, 'nzb_repair_activity', 'cleanup_cli_debrid_id INTEGER')
    if 'cleanup_file_name' not in columns:
        _add_column(conn, 'nzb_repair_activity', 'cleanup_file_name TEXT')
    return True


def _reconcile_stale_activity_rows(conn) -> int:
    """Collapse duplicate stale UI rows without changing repair or media state."""
    if not _activity_table_exists(conn) or not _ensure_activity_identity_columns(conn):
        return 0
    required = {
        'id', 'title', 'media_type', 'season_number', 'episode_number',
        'broken_nzb_id', 'outcome', 'created_at', 'updated_at',
    }
    if not required.issubset(_table_columns(conn, 'nzb_repair_activity')):
        return 0

    # Backfill exact target identity where a durable cleanup already owns the
    # activity.  Legacy unresolved rows have no cleanup; those are collapsed
    # below only when UUID and media identity are identical.
    conn.execute(
        """UPDATE nzb_repair_activity
              SET cleanup_cli_debrid_id=(
                    SELECT c.cli_debrid_id FROM mount_replacement_cleanups c
                    WHERE c.activity_id=nzb_repair_activity.id LIMIT 1),
                  cleanup_file_name=(
                    SELECT c.file_name FROM mount_replacement_cleanups c
                    WHERE c.activity_id=nzb_repair_activity.id LIMIT 1)
            WHERE outcome IN ('stale_cleanup_pending','stale_entry_unresolved',
                              'stale_entry_cleaned','replacement_cleanup_stale')
              AND cleanup_cli_debrid_id IS NULL
              AND id IN (SELECT activity_id FROM mount_replacement_cleanups
                         WHERE activity_id IS NOT NULL)"""
    )

    exact_groups = conn.execute(
        """SELECT cleanup_cli_debrid_id, lower(broken_nzb_id), cleanup_file_name,
                  COUNT(*)
             FROM nzb_repair_activity
            WHERE outcome IN ('stale_cleanup_pending','stale_entry_unresolved',
                              'stale_entry_cleaned','replacement_cleanup_stale')
              AND cleanup_cli_debrid_id IS NOT NULL
              AND cleanup_file_name IS NOT NULL
            GROUP BY 1,2,3 HAVING COUNT(*)>1"""
    ).fetchall()
    removed = 0
    for item_id, old_hash, file_name, _count in exact_groups:
        rows = conn.execute(
            """SELECT a.id,
                      EXISTS(SELECT 1 FROM mount_replacement_sagas s
                             WHERE s.activity_id=a.id) AS saga_linked,
                      EXISTS(SELECT 1 FROM mount_replacement_cleanups c
                             WHERE c.activity_id=a.id) AS cleanup_linked
                 FROM nzb_repair_activity a
                WHERE a.cleanup_cli_debrid_id=?
                  AND lower(COALESCE(a.broken_nzb_id,''))=?
                  AND a.cleanup_file_name=?
                ORDER BY saga_linked DESC, cleanup_linked DESC,
                         COALESCE(a.updated_at,a.created_at) DESC, a.id DESC""",
            (item_id, old_hash, file_name),
        ).fetchall()
        keeper = rows[0]['id']
        for row in rows[1:]:
            conn.execute(
                'UPDATE mount_replacement_sagas SET activity_id=? WHERE activity_id=?',
                (keeper, row['id']),
            )
            conn.execute(
                'UPDATE mount_replacement_cleanups SET activity_id=? WHERE activity_id=?',
                (keeper, row['id']),
            )
            removed += max(conn.execute(
                'DELETE FROM nzb_repair_activity WHERE id=?', (row['id'],),
            ).rowcount, 0)

    groups = conn.execute(
        """SELECT COALESCE(title,''), COALESCE(media_type,''),
                  COALESCE(season_number,-1), COALESCE(episode_number,-1),
                  lower(COALESCE(broken_nzb_id,'')), COUNT(*)
             FROM nzb_repair_activity
            WHERE outcome IN ('stale_cleanup_pending','stale_entry_unresolved')
              AND COALESCE(broken_nzb_id,'')!=''
            GROUP BY 1,2,3,4,5 HAVING COUNT(*)>1"""
    ).fetchall()
    for title, media_type, season, episode, old_hash, _count in groups:
        rows = conn.execute(
            """SELECT a.id,
                      EXISTS(SELECT 1 FROM mount_replacement_sagas s
                             WHERE s.activity_id=a.id) AS saga_linked,
                      EXISTS(SELECT 1 FROM mount_replacement_cleanups c
                             WHERE c.activity_id=a.id) AS cleanup_linked
                 FROM nzb_repair_activity a
                WHERE a.outcome IN ('stale_cleanup_pending','stale_entry_unresolved')
                  AND COALESCE(a.title,'')=? AND COALESCE(a.media_type,'')=?
                  AND COALESCE(a.season_number,-1)=?
                  AND COALESCE(a.episode_number,-1)=?
                  AND lower(COALESCE(a.broken_nzb_id,''))=?
                ORDER BY saga_linked DESC, cleanup_linked DESC,
                         COALESCE(a.updated_at,a.created_at) DESC, a.id DESC""",
            (title, media_type, season, episode, old_hash),
        ).fetchall()
        if len(rows) < 2:
            continue
        keeper = rows[0]['id']
        for row in rows[1:]:
            # Never delete a row referenced by durable state. Fold those links
            # into the chosen canonical row first.
            conn.execute(
                'UPDATE mount_replacement_sagas SET activity_id=? WHERE activity_id=?',
                (keeper, row['id']),
            )
            conn.execute(
                'UPDATE mount_replacement_cleanups SET activity_id=? WHERE activity_id=?',
                (keeper, row['id']),
            )
            deleted = conn.execute(
                """DELETE FROM nzb_repair_activity WHERE id=?
                     AND outcome IN ('stale_cleanup_pending','stale_entry_unresolved')""",
                (row['id'],),
            )
            removed += max(deleted.rowcount, 0)
    return removed


def _requeue_registration_stale_sagas(conn) -> int:
    """Collapse duplicate registration-conflict sagas without reopening them."""
    rows = conn.execute(
        """SELECT * FROM mount_replacement_sagas
           WHERE protocol='nzb' AND status='blocked'
             AND last_error IN (?, ?)
             AND candidate_info_hash IS NOT NULL AND candidate_info_hash!=''
           ORDER BY cli_debrid_id, id""",
        tuple(_REGISTRATION_STALE_ERRORS),
    ).fetchall()
    if not rows:
        return 0

    activity_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nzb_repair_activity'"
    ).fetchone()
    grouped = {}
    for row in rows:
        grouped.setdefault(row['cli_debrid_id'], []).append(row)

    recovered = 0
    for cli_debrid_id, item_rows in grouped.items():
        # A blocked saga could previously fall out of the partial unique index,
        # allowing the same repair to be queued again. Preserve the original
        # activity row and fold exact targets/attempts from those duplicates into it.
        active = conn.execute(
            """SELECT * FROM mount_replacement_sagas
               WHERE cli_debrid_id=? AND protocol='nzb'
                 AND status IN ('pending','probe_failed')
               ORDER BY id LIMIT 1""",
            (cli_debrid_id,),
        ).fetchone()
        if active and not any(
                str(row['candidate_info_hash'] or '').lower() ==
                str(active['candidate_info_hash'] or '').lower()
                for row in item_rows):
            continue
        keeper = active or item_rows[0]
        for duplicate in item_rows:
            if duplicate['id'] == keeper['id']:
                continue
            if (str(duplicate['candidate_info_hash'] or '').lower() !=
                    str(keeper['candidate_info_hash'] or '').lower()):
                continue
            conn.execute(
                'UPDATE mount_replacement_cleanups SET saga_id=? WHERE saga_id=?',
                (keeper['id'], duplicate['id']),
            )
            conn.execute(
                'UPDATE mount_replacement_attempts SET saga_id=? WHERE saga_id=?',
                (keeper['id'], duplicate['id']),
            )
            if activity_table and duplicate['activity_id'] and duplicate['activity_id'] != keeper['activity_id']:
                conn.execute(
                    """DELETE FROM nzb_repair_activity WHERE id=?
                         AND outcome IN ('replacement_cleanup_stale',
                                         'stale_entry_unresolved',
                                         'stale_cleanup_pending')""",
                    (duplicate['activity_id'],),
                )
            conn.execute('DELETE FROM mount_replacement_sagas WHERE id=?', (duplicate['id'],))

        recovered += 1
    return recovered


def create_mount_replacement_cleanup_table() -> None:
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mount_replacement_sagas (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                cli_debrid_id       INTEGER NOT NULL,
                protocol            TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                activity_id         INTEGER,
                candidate_info_hash TEXT,
                candidate_title     TEXT,
                attempts            INTEGER NOT NULL DEFAULT 0,
                next_attempt_at     TIMESTAMP,
                last_error          TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at        TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mount_saga_active_item
                ON mount_replacement_sagas(cli_debrid_id)
                WHERE status IN ('pending', 'probe_failed');
            CREATE INDEX IF NOT EXISTS idx_mount_saga_due
                ON mount_replacement_sagas(status, next_attempt_at);

            CREATE TABLE IF NOT EXISTS mount_replacement_attempts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                saga_id          INTEGER NOT NULL,
                cli_debrid_id    INTEGER NOT NULL,
                job_id           TEXT,
                segment_id       TEXT,
                nzb_guid         TEXT,
                release_title    TEXT,
                normalized_title TEXT NOT NULL,
                source_normalized_title TEXT,
                status           TEXT NOT NULL,
                reason           TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                cleaned_at       TIMESTAMP,
                UNIQUE(cli_debrid_id, normalized_title)
            );
            CREATE INDEX IF NOT EXISTS idx_mount_attempt_saga
                ON mount_replacement_attempts(saga_id, status);

            CREATE TABLE IF NOT EXISTS mount_replacement_cleanups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                saga_id         INTEGER,
                cli_debrid_id   INTEGER NOT NULL,
                protocol        TEXT NOT NULL,
                entry_name      TEXT NOT NULL,
                file_name       TEXT NOT NULL,
                old_info_hash   TEXT NOT NULL,
                reason          TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP,
                last_error      TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at    TIMESTAMP,
                UNIQUE(cli_debrid_id, old_info_hash, file_name)
            );
            CREATE INDEX IF NOT EXISTS idx_mount_cleanup_pending
                ON mount_replacement_cleanups(status, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_mount_cleanup_item
                ON mount_replacement_cleanups(cli_debrid_id);
        """)
        # Existing installations have mount_replacement_cleanups without this
        # column. Add it before creating any index that references it.
        _add_column(conn, 'mount_replacement_cleanups', 'saga_id INTEGER')
        _add_column(conn, 'mount_replacement_attempts', 'release_title TEXT')
        _add_column(conn, 'mount_replacement_attempts', 'source_normalized_title TEXT')
        _add_column(conn, 'mount_replacement_sagas', 'last_reconciled_at TIMESTAMP')
        _add_column(conn, "mount_replacement_sagas", "saga_kind TEXT NOT NULL DEFAULT 'replacement'")
        _add_column(conn, "mount_replacement_sagas", "plex_refresh_status TEXT NOT NULL DEFAULT 'not_required'")
        _add_column(conn, 'mount_replacement_sagas', 'verified_info_hash TEXT')
        _add_column(conn, 'mount_replacement_sagas', 'verified_entry_name TEXT')
        _add_column(conn, 'mount_replacement_sagas', 'verified_file_name TEXT')
        _add_column(conn, 'mount_replacement_sagas', 'verified_at TIMESTAMP')
        _add_column(conn, 'mount_replacement_cleanups', 'activity_id INTEGER')
        _add_column(conn, 'nzb_repair_activity', 'cleanup_cli_debrid_id INTEGER')
        _add_column(conn, 'nzb_repair_activity', 'cleanup_file_name TEXT')
        # Give sagas created by older builds one immediate reconciliation pass
        # instead of preserving a stale 24-hour retry deadline.
        conn.execute(
            """UPDATE mount_replacement_sagas SET next_attempt_at=NULL
               WHERE status IN ('pending','probe_failed') AND last_reconciled_at IS NULL"""
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_mount_cleanup_saga '
            'ON mount_replacement_cleanups(saga_id)'
        )
        # Verification is NZB-only. Keep torrent rows on the original direct
        # Collected→acknowledge path even if an experimental build attached a saga.
        conn.execute(
            "UPDATE mount_replacement_cleanups SET saga_id=NULL "
            "WHERE protocol!='nzb' AND status='pending'"
        )
        conn.execute(
            "DELETE FROM mount_replacement_sagas "
            "WHERE protocol!='nzb' AND status IN ('pending','probe_failed')"
        )
        # Adopt pending rows created by the pre-verification cleanup handoff.
        # Completed/blocked history is left untouched.
        orphan_items = conn.execute(
            """SELECT DISTINCT cli_debrid_id, protocol FROM mount_replacement_cleanups
               WHERE status='pending' AND saga_id IS NULL AND protocol='nzb'"""
        ).fetchall()
        for orphan in orphan_items:
            existing = conn.execute(
                "SELECT id FROM mount_replacement_sagas WHERE cli_debrid_id=? AND status IN ('pending','probe_failed') LIMIT 1",
                (orphan['cli_debrid_id'],),
            ).fetchone()
            if existing:
                saga_id = existing['id']
            else:
                saga_id = conn.execute(
                    "INSERT INTO mount_replacement_sagas (cli_debrid_id, protocol) VALUES (?, ?)",
                    (orphan['cli_debrid_id'], orphan['protocol'] or ''),
                ).lastrowid
            conn.execute(
                "UPDATE mount_replacement_cleanups SET saga_id=? WHERE cli_debrid_id=? AND status='pending' AND saga_id IS NULL",
                (saga_id, orphan['cli_debrid_id']),
            )
        activity_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nzb_repair_activity'"
        ).fetchone()
        if activity_table:
            reconciled = conn.execute(
                """UPDATE nzb_repair_activity
                   SET replacement_nzb_id=(
                         SELECT s.candidate_info_hash FROM mount_replacement_sagas s
                         WHERE s.activity_id=nzb_repair_activity.id AND s.status='complete'
                       ),
                       replacement_title=COALESCE((
                         SELECT NULLIF(s.candidate_title, '') FROM mount_replacement_sagas s
                         WHERE s.activity_id=nzb_repair_activity.id AND s.status='complete'
                       ), replacement_title),
                       outcome='replaced',
                       updated_at=COALESCE((
                         SELECT s.completed_at FROM mount_replacement_sagas s
                         WHERE s.activity_id=nzb_repair_activity.id AND s.status='complete'
                       ), CURRENT_TIMESTAMP)
                   WHERE outcome='replacement_pending'
                     AND id IN (
                       SELECT activity_id FROM mount_replacement_sagas
                       WHERE status='complete' AND activity_id IS NOT NULL
                         AND COALESCE(saga_kind, 'replacement')='replacement'
                         AND candidate_info_hash IS NOT NULL AND candidate_info_hash!=''
                     )"""
            )
            if reconciled.rowcount:
                logging.info(
                    '[MountCleanup] Reconciled %s completed replacement activity row(s)',
                    reconciled.rowcount,
                )
        recovered = _requeue_registration_stale_sagas(conn)
        if recovered:
            logging.info(
                '[MountCleanup] Collapsed duplicate exact-registration saga groups: %s',
                recovered,
            )
        if activity_table:
            duplicate_activities = _reconcile_stale_activity_rows(conn)
            if duplicate_activities:
                logging.info(
                    '[MountCleanup] Removed %s duplicate stale activity row(s)',
                    duplicate_activities,
                )
            # Exact target identity prevents concurrent/repeated health scans
            # from creating another UI row for the same old mounted file.
            activity_columns = _table_columns(conn, 'nzb_repair_activity')
            if {'cleanup_cli_debrid_id', 'broken_nzb_id', 'cleanup_file_name',
                    'outcome'}.issubset(activity_columns):
                conn.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS idx_nzb_activity_exact_cleanup
                         ON nzb_repair_activity(cleanup_cli_debrid_id, broken_nzb_id,
                                                cleanup_file_name)
                      WHERE cleanup_cli_debrid_id IS NOT NULL
                        AND cleanup_file_name IS NOT NULL
                        AND outcome IN ('stale_cleanup_pending','stale_entry_unresolved',
                                        'stale_entry_cleaned','replacement_cleanup_stale')"""
                )
            # Old replacement sagas were all displayed as if a candidate were
            # being probed. Distinguish those waiting for candidate selection.
            if {'outcome', 'repair_attempts', 'updated_at'}.issubset(activity_columns):
                conn.execute(
                """UPDATE nzb_repair_activity
                      SET outcome=CASE WHEN COALESCE(repair_attempts,0)>=3
                                       THEN 'replacement_max_attempts'
                                       ELSE 'replacement_awaiting_candidate' END,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE outcome='replacement_pending'
                      AND id IN (
                        SELECT activity_id FROM mount_replacement_sagas
                         WHERE status IN ('pending','probe_failed')
                           AND COALESCE(candidate_info_hash,'')=''
                           AND activity_id IS NOT NULL
                      )"""
                )
        conn.commit()
    finally:
        conn.close()


def split_playback_cleanup_targets(entries: list, protocol: str = '') -> list:
    """Expand only playback-probe failures to exact, per-file repair targets."""
    expanded = []
    for entry in entries or []:
        broken_files = entry.get('broken_files') or []
        eligible = []
        ineligible = []
        for broken_file in broken_files:
            reason = broken_file.get('reason') or ''
            if reason in RETIRED_PLAYBACK_REASONS:
                continue
            item_id = int(broken_file.get('cli_debrid_id') or 0)
            protected_segment = reason == 'usenet_segment_missing' and _has_active_nzb_saga(item_id)
            (eligible if reason in PLAYBACK_CLEANUP_REASONS or protected_segment else ineligible).append(broken_file)
        for broken_file in eligible:
            target = dict(entry)
            target.update({
                'entry_name': broken_file.get('entry_name') or entry.get('entry_name') or '',
                'file_name': broken_file.get('file_name') or '',
                'info_hash': broken_file.get('info_hash') or '',
                'cli_debrid_id': broken_file.get('cli_debrid_id') or 0,
                'failure_reason': broken_file.get('reason') or '',
                'broken_files': [dict(broken_file)],
                '_playback_cleanup': True,
            })
            if not protocol or (target.get('protocol') or '').lower() == protocol.lower():
                expanded.append(target)
        if ineligible or (not eligible and not broken_files):
            legacy = dict(entry)
            if broken_files:
                legacy['broken_files'] = ineligible
            if ineligible:
                legacy['failure_reason'] = ineligible[0].get('reason') or legacy.get('failure_reason', '')
            expanded.append(legacy)
    return expanded


def _has_active_nzb_saga(cli_debrid_id: int) -> bool:
    if not cli_debrid_id:
        return False
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM mount_replacement_sagas WHERE cli_debrid_id=? AND protocol='nzb' "
            "AND status IN ('pending','probe_failed') LIMIT 1",
            (cli_debrid_id,),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def get_media_item_for_cleanup(cli_debrid_id: int):
    if not cli_debrid_id:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM media_items WHERE id = ? LIMIT 1', (cli_debrid_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _activity_table_exists(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nzb_repair_activity'"
    ).fetchone())


def _historical_media_identity(conn, old_item_id: int, old_hash: str = '',
                               entry_name: str = ''):
    """Recover the semantic identity of a deleted item from durable activity."""
    if not _activity_table_exists(conn) or not (old_item_id or old_hash or entry_name):
        return None
    return conn.execute(
        """SELECT title, media_type, season_number, episode_number
             FROM nzb_repair_activity
            WHERE (item_id=? OR broken_nzb_id=? OR broken_nzb_title=?)
              AND title IS NOT NULL AND title!=''
              AND media_type IN ('movie','episode')
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 1""",
        (old_item_id, old_hash, entry_name),
    ).fetchone()


def _unique_collected_replacement(conn, broken_file: dict):
    """Return one exact current NZB replacement, plus a classification detail."""
    old_item_id = int(broken_file.get('cli_debrid_id') or 0)
    old_hash = str(broken_file.get('info_hash') or '')
    direct = conn.execute(
        'SELECT * FROM media_items WHERE id=? LIMIT 1', (old_item_id,),
    ).fetchone()
    if direct:
        direct_item = dict(direct)
        direct_hash = _current_source_hash(direct_item, 'nzb')
        if direct_item.get('state') == 'Collected' and direct_hash:
            if direct_hash.lower() == old_hash.lower():
                return None, 'current_source', 'the registered media row still owns the broken UUID'
            if str(direct_item.get('filled_by_torrent_id') or '').startswith('nzb:'):
                return direct_item, 'resolved', ''

    identity = _historical_media_identity(
        conn, old_item_id, old_hash,
        str(broken_file.get('entry_name') or ''),
    )
    if not identity:
        return None, 'not_found', 'no historical media identity exists for the registered item ID'
    params = [identity['title'], identity['media_type']]
    sql = """SELECT * FROM media_items
              WHERE title=? COLLATE NOCASE AND type=? AND state='Collected'"""
    if identity['media_type'] == 'episode':
        if identity['season_number'] is None or identity['episode_number'] is None:
            return None, 'unresolved', 'historical episode identity is incomplete'
        sql += ' AND season_number=? AND episode_number=?'
        params.extend([identity['season_number'], identity['episode_number']])
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        return None, 'unresolved', (
            f'historical identity matched {len(rows)} collected media rows; exact cleanup requires one'
        )
    current = dict(rows[0])
    current_hash = _current_source_hash(current, 'nzb')
    if not str(current.get('filled_by_torrent_id') or '').startswith('nzb:'):
        return None, 'unresolved', 'the current replacement is not an NZB'
    if not current_hash or current_hash.lower() == old_hash.lower():
        return None, 'current_source', 'the collected media row does not use a different NZB UUID'
    return current, 'resolved', ''


def _canonical_stale_activity(conn, broken_file: dict, current_item: dict,
                              outcome: str, detail: str = '',
                              preferred_activity_id: int = None) -> int:
    """Adopt one existing Not Found row and collapse only its exact duplicates."""
    if not _activity_table_exists(conn) or not _ensure_activity_identity_columns(conn):
        return None
    old_hash = str(broken_file.get('info_hash') or '')
    entry_name = str(broken_file.get('entry_name') or '')
    old_item_id = int(broken_file.get('cli_debrid_id') or 0)
    file_name = str(broken_file.get('file_name') or '')
    rows = conn.execute(
        """SELECT id, outcome FROM nzb_repair_activity
            WHERE cleanup_cli_debrid_id=? AND lower(COALESCE(broken_nzb_id,''))=lower(?)
              AND cleanup_file_name=?
            ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,
                     COALESCE(updated_at, created_at) DESC, id DESC""",
        (old_item_id, old_hash, file_name, preferred_activity_id or -1),
    ).fetchall()
    if not rows and preferred_activity_id:
        preferred = conn.execute(
            'SELECT id, outcome FROM nzb_repair_activity WHERE id=?',
            (preferred_activity_id,),
        ).fetchone()
        rows = [preferred] if preferred else []
    if not rows:
        # Adopt only a legacy row with the same UUID and exact media identity.
        # Entry names are shared by season packs and are not sufficient proof.
        rows = conn.execute(
            """SELECT id, outcome FROM nzb_repair_activity
                WHERE outcome IN ('not_found','stale_entry_unresolved','stale_cleanup_pending')
                  AND lower(COALESCE(broken_nzb_id,''))=lower(?)
                  AND COALESCE(title,'')=COALESCE(?, '')
                  AND COALESCE(media_type,'')=COALESCE(?, '')
                  AND COALESCE(season_number,-1)=COALESCE(?, -1)
                  AND COALESCE(episode_number,-1)=COALESCE(?, -1)
                ORDER BY COALESCE(updated_at, created_at) DESC, id DESC""",
            (old_hash, current_item.get('title'), current_item.get('type'),
             current_item.get('season_number'), current_item.get('episode_number')),
        ).fetchall()
    if rows:
        activity_id = rows[0]['id']
        conn.execute(
            """UPDATE nzb_repair_activity
                  SET item_id=?, title=?, media_type=?, season_number=?, episode_number=?,
                      broken_nzb_id=?, broken_nzb_title=?, replacement_nzb_id=NULL,
                      replacement_title=NULL, outcome=?, triggered_by='stale_mount_cleanup',
                      cleanup_cli_debrid_id=?, cleanup_file_name=?,
                      updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (current_item.get('id'), current_item.get('title'), current_item.get('type'),
             current_item.get('season_number'), current_item.get('episode_number'),
             old_hash, entry_name, outcome, old_item_id, file_name, activity_id),
        )
        duplicate_ids = [row['id'] for row in rows[1:]]
        # Older orphan handling sometimes logged the exact entry name in both
        # ID/title columns without media identity. It is the same health event,
        # not a separate file-level activity row.
        legacy_orphans = conn.execute(
            """SELECT id FROM nzb_repair_activity
                WHERE outcome='not_found' AND id!=?
                  AND broken_nzb_id=? AND broken_nzb_title=?
                  AND title IS NULL AND season_number IS NULL AND episode_number IS NULL""",
            (activity_id, entry_name, entry_name),
        ).fetchall()
        duplicate_ids.extend(
            row['id'] for row in legacy_orphans if row['id'] not in duplicate_ids
        )
        if duplicate_ids:
            conn.executemany(
                'UPDATE mount_replacement_sagas SET activity_id=? WHERE activity_id=?',
                [(activity_id, value) for value in duplicate_ids],
            )
            conn.executemany(
                'UPDATE mount_replacement_cleanups SET activity_id=? WHERE activity_id=?',
                [(activity_id, value) for value in duplicate_ids],
            )
            conn.executemany(
                """DELETE FROM nzb_repair_activity WHERE id=?
                     AND outcome IN ('not_found','stale_entry_unresolved','stale_cleanup_pending')""",
                [(value,) for value in duplicate_ids],
            )
        return activity_id
    conn.execute(
        """INSERT OR IGNORE INTO nzb_repair_activity
           (item_id, title, media_type, season_number, episode_number,
            broken_nzb_id, broken_nzb_title, outcome, triggered_by,
            last_repair_at, updated_at, cleanup_cli_debrid_id, cleanup_file_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stale_mount_cleanup',
                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)""",
        (current_item.get('id'), current_item.get('title'), current_item.get('type'),
         current_item.get('season_number'), current_item.get('episode_number'),
         old_hash, entry_name, outcome, old_item_id, file_name),
    )
    row = conn.execute(
        """SELECT id FROM nzb_repair_activity
            WHERE cleanup_cli_debrid_id=? AND lower(COALESCE(broken_nzb_id,''))=lower(?)
              AND cleanup_file_name=?
            ORDER BY id LIMIT 1""",
        (old_item_id, old_hash, file_name),
    ).fetchone()
    if row:
        return row['id']
    raise RuntimeError('exact stale activity could not be created')


def _record_unresolved_stale_target(conn, broken_file: dict, detail: str) -> None:
    """Surface one unsafe stale target without producing repeated Not Found rows."""
    placeholder = {
        'id': None, 'title': None, 'type': None,
        'season_number': None, 'episode_number': None,
    }
    old_item_id = int(broken_file.get('cli_debrid_id') or 0)
    identity = _historical_media_identity(
        conn, old_item_id, str(broken_file.get('info_hash') or ''),
        str(broken_file.get('entry_name') or ''),
    )
    if identity:
        placeholder.update({
            'title': identity['title'], 'type': identity['media_type'],
            'season_number': identity['season_number'],
            'episode_number': identity['episode_number'],
        })
    activity_id = _canonical_stale_activity(
        conn, broken_file, placeholder, 'stale_entry_unresolved', detail,
    )
    if activity_id:
        conn.execute(
            "UPDATE nzb_repair_activity SET next_repair_at=NULL WHERE id=?",
            (activity_id,),
        )


def _queue_stale_replaced_file(broken_file: dict) -> str:
    """Persist a verified-before-delete cleanup for one already-replaced old NZB."""
    required = ('entry_name', 'file_name', 'info_hash', 'cli_debrid_id', 'reason')
    if not all(broken_file.get(key) for key in required):
        return 'not_found'
    if broken_file.get('reason') not in STALE_CLEANUP_REASONS:
        return 'not_stale'
    conn = get_db_connection()
    try:
        # Exact durable ownership takes precedence over reclassification. A
        # repeated health scan must reuse this saga/activity even when it is
        # blocked, rather than creating a second unresolved UI row.
        owned = conn.execute(
            """SELECT s.*, c.activity_id AS cleanup_activity_id
                 FROM mount_replacement_cleanups c
                 JOIN mount_replacement_sagas s ON s.id=c.saga_id
                WHERE c.cli_debrid_id=? AND lower(c.old_info_hash)=lower(?)
                  AND c.file_name=? AND s.protocol='nzb'
                  AND s.status IN ('pending','probe_failed','blocked')
                ORDER BY s.id LIMIT 1""",
            (int(broken_file['cli_debrid_id']), broken_file['info_hash'],
             broken_file['file_name']),
        ).fetchone()
        if owned:
            activity_id = owned['activity_id'] or owned['cleanup_activity_id']
            if not activity_id:
                item = conn.execute(
                    'SELECT * FROM media_items WHERE id=? LIMIT 1',
                    (owned['cli_debrid_id'],),
                ).fetchone()
                item = dict(item) if item else {
                    'id': None, 'title': None, 'type': None,
                    'season_number': None, 'episode_number': None,
                }
                outcome = ('replacement_cleanup_stale' if owned['status'] == 'blocked'
                           else 'stale_cleanup_pending')
                activity_id = _canonical_stale_activity(
                    conn, broken_file, item, outcome,
                    preferred_activity_id=owned['activity_id'],
                )
            conn.execute(
                'UPDATE mount_replacement_sagas SET activity_id=COALESCE(activity_id, ?) WHERE id=?',
                (activity_id, owned['id']),
            )
            conn.execute(
                """UPDATE mount_replacement_cleanups
                      SET activity_id=COALESCE(activity_id, ?), updated_at=CURRENT_TIMESTAMP
                    WHERE cli_debrid_id=? AND lower(old_info_hash)=lower(?) AND file_name=?""",
                (activity_id, int(broken_file['cli_debrid_id']), broken_file['info_hash'],
                 broken_file['file_name']),
            )
            conn.commit()
            return 'unresolved' if owned['status'] == 'blocked' else 'queued'

        current_item, status, detail = _unique_collected_replacement(conn, broken_file)
        if status == 'not_found' or status == 'current_source':
            return status
        if status != 'resolved':
            _record_unresolved_stale_target(conn, broken_file, detail)
            conn.commit()
            logging.warning(
                '[MountCleanup] Stale old NZB requires attention: entry=%r file=%r reason=%s',
                broken_file.get('entry_name'), broken_file.get('file_name'), detail,
            )
            return 'unresolved'

        current_id = int(current_item['id'])
        current_hash = _current_source_hash(current_item, 'nzb')
        activity_id = None
        # An exact target already owned by a saga always wins, including a
        # blocked saga.  This prevents later scans from creating a second saga
        # or activity row for the same old UUID/file while avoiding reuse of an
        # unrelated blocked episode that happens to share the current item.
        saga = conn.execute(
            """SELECT s.* FROM mount_replacement_cleanups c
                JOIN mount_replacement_sagas s ON s.id=c.saga_id
                WHERE c.cli_debrid_id=? AND lower(c.old_info_hash)=lower(?)
                  AND c.file_name=? AND s.protocol='nzb'
                  AND s.status IN ('pending','probe_failed','blocked')
                ORDER BY s.id LIMIT 1""",
            (int(broken_file['cli_debrid_id']), broken_file['info_hash'],
             broken_file['file_name']),
        ).fetchone()
        if not saga:
            saga = conn.execute(
                """SELECT * FROM mount_replacement_sagas
                    WHERE cli_debrid_id=? AND protocol='nzb'
                      AND status IN ('pending','probe_failed')
                    ORDER BY id LIMIT 1""",
                (current_id,),
            ).fetchone()
        if saga and not _is_stale_cleanup_saga(saga):
            _record_unresolved_stale_target(
                conn, broken_file, 'an active replacement is still processing this media item',
            )
            conn.commit()
            return 'unresolved'
        if saga and str(saga['candidate_info_hash'] or '').lower() not in ('', current_hash.lower()):
            _record_unresolved_stale_target(
                conn, broken_file, 'another active replacement saga owns the current media item',
            )
            conn.commit()
            return 'unresolved'
        if saga and saga['status'] == 'blocked':
            _record_unresolved_stale_target(
                conn, broken_file,
                saga['last_error'] or 'the existing exact cleanup saga needs attention',
            )
            conn.commit()
            return 'unresolved'

        activity_id = _canonical_stale_activity(
            conn, broken_file, current_item, 'stale_cleanup_pending',
            preferred_activity_id=(saga['activity_id'] if saga else None),
        )

        if saga:
            saga_id = saga['id']
            if not saga['activity_id']:
                conn.execute(
                    'UPDATE mount_replacement_sagas SET activity_id=? WHERE id=?',
                    (activity_id, saga_id),
                )
        else:
            candidate_title = (current_item.get('filled_by_title') or
                               current_item.get('filled_by_file') or current_item.get('title'))
            saga_id = conn.execute(
                """INSERT INTO mount_replacement_sagas
                   (cli_debrid_id, protocol, status, activity_id, candidate_info_hash,
                    candidate_title, saga_kind, plex_refresh_status)
                   VALUES (?, 'nzb', 'pending', ?, ?, ?, 'stale_cleanup', 'not_required')""",
                (current_id, activity_id, current_hash, candidate_title),
            ).lastrowid
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (saga_id, cli_debrid_id, protocol, entry_name, file_name,
                old_info_hash, reason, activity_id)
               VALUES (?, ?, 'nzb', ?, ?, ?, ?, ?)
               ON CONFLICT(cli_debrid_id, old_info_hash, file_name) DO UPDATE SET
                 saga_id=excluded.saga_id, entry_name=excluded.entry_name,
                 reason=excluded.reason, activity_id=excluded.activity_id,
                 status=CASE WHEN mount_replacement_cleanups.status='complete'
                             THEN 'complete' ELSE 'pending' END,
                 next_attempt_at=NULL, last_error=NULL,
                 updated_at=CURRENT_TIMESTAMP""",
            (saga_id, int(broken_file['cli_debrid_id']), broken_file['entry_name'],
             broken_file['file_name'], broken_file['info_hash'], broken_file['reason'],
             activity_id),
        )
        conn.execute(
            """UPDATE mount_replacement_sagas
                  SET candidate_info_hash=?, status='pending', next_attempt_at=NULL,
                      last_error=NULL,
                      verified_info_hash=CASE
                          WHEN lower(COALESCE(verified_info_hash,''))=lower(?)
                          THEN verified_info_hash ELSE NULL END,
                      verified_entry_name=CASE
                          WHEN lower(COALESCE(verified_info_hash,''))=lower(?)
                          THEN verified_entry_name ELSE NULL END,
                      verified_file_name=CASE
                          WHEN lower(COALESCE(verified_info_hash,''))=lower(?)
                          THEN verified_file_name ELSE NULL END,
                      verified_at=CASE
                          WHEN lower(COALESCE(verified_info_hash,''))=lower(?)
                          THEN verified_at ELSE NULL END,
                      updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (current_hash, current_hash, current_hash, current_hash, current_hash, saga_id),
        )
        conn.commit()
        logging.info(
            '[MountCleanup] Adopted stale replaced NZB: old_item=%s old_hash=%s '
            'current_item=%s current_hash=%s file=%r',
            broken_file['cli_debrid_id'], broken_file['info_hash'],
            current_id, current_hash, broken_file['file_name'],
        )
        return 'queued'
    except Exception as exc:
        conn.rollback()
        logging.warning('[MountCleanup] Could not classify stale old NZB: %s', exc)
        return 'not_found'
    finally:
        conn.close()


def adopt_stale_replaced_nzbs(entries: list) -> list:
    """Remove safely-adopted stale files from the legacy Not Found workflow."""
    remaining_entries = []
    for entry in entries or []:
        files = entry.get('broken_files') or []
        if not files:
            remaining_entries.append(entry)
            continue
        remaining_files = []
        for raw_file in files:
            broken_file = dict(raw_file)
            broken_file.setdefault('entry_name', entry.get('entry_name') or '')
            broken_file.setdefault('reason', entry.get('failure_reason') or '')
            broken_file.setdefault('protocol', entry.get('protocol') or '')
            classification = _queue_stale_replaced_file(broken_file)
            if classification in ('queued', 'unresolved'):
                continue
            remaining_files.append(raw_file)
        if remaining_files:
            kept = dict(entry)
            kept['broken_files'] = remaining_files
            kept['failure_reason'] = remaining_files[0].get('reason') or kept.get('failure_reason', '')
            remaining_entries.append(kept)
    return remaining_entries


def _create_pending_activity(target: dict, item: dict) -> int:
    try:
        from database.nzb_repair_activity import log_repair_activity
        old_id = target.get('info_hash') or ''
        if (target.get('protocol') or '').lower() == 'torrent':
            old_id = f'debrid:{old_id}'
        return log_repair_activity(
            item_id=target.get('cli_debrid_id'), title=item.get('title'),
            media_type=item.get('type'), season_number=item.get('season_number'),
            episode_number=item.get('episode_number'), broken_nzb_id=old_id,
            broken_nzb_title=target.get('entry_name'),
            outcome='replacement_awaiting_candidate',
            triggered_by='mount_replacement_verification',
        )
    except Exception as exc:
        logging.debug('[MountCleanup] Could not create pending activity: %s', exc)
        return None


def _normalize_release_title(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower())


def record_mount_replacement_attempt(cli_debrid_id: int, *, job_id: str = '',
                                     title: str = '', segment_id: str = '',
                                     nzb_guid: str = '', source_title: str = '', status: str,
                                     reason: str = '') -> bool:
    """Persist a rejected/probe-failed NZB without exposing it as success."""
    normalized = _normalize_release_title(source_title or title)
    if (not cli_debrid_id or not normalized or status not in (
            'provisional', 'failed_submission', 'failed_collected',
            'rejected_identity')):
        return False
    conn = get_db_connection()
    try:
        saga = conn.execute(
            "SELECT id FROM mount_replacement_sagas WHERE cli_debrid_id=? AND protocol='nzb' "
            "AND status IN ('pending','probe_failed') LIMIT 1",
            (cli_debrid_id,),
        ).fetchone()
        if not saga:
            return False
        conn.execute(
            """INSERT INTO mount_replacement_attempts
               (saga_id, cli_debrid_id, job_id, segment_id, nzb_guid,
                release_title, normalized_title, source_normalized_title,
                status, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cli_debrid_id, normalized_title) DO UPDATE SET
                 saga_id=excluded.saga_id,
                 job_id=COALESCE(excluded.job_id, mount_replacement_attempts.job_id),
                 segment_id=COALESCE(excluded.segment_id, mount_replacement_attempts.segment_id),
                 nzb_guid=COALESCE(excluded.nzb_guid, mount_replacement_attempts.nzb_guid),
                 release_title=COALESCE(excluded.release_title, mount_replacement_attempts.release_title),
                 source_normalized_title=COALESCE(
                     excluded.source_normalized_title,
                     mount_replacement_attempts.source_normalized_title),
                 status=excluded.status, reason=excluded.reason,
                 updated_at=CURRENT_TIMESTAMP""",
            (saga['id'], cli_debrid_id, job_id or None, segment_id or None,
             nzb_guid or None, title or None, normalized, normalized,
             status, reason or None),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_attempted_candidate_keys(cli_debrid_id: int) -> dict:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT job_id, segment_id, nzb_guid, normalized_title,
                      source_normalized_title
               FROM mount_replacement_attempts WHERE cli_debrid_id=?""",
            (cli_debrid_id,),
        ).fetchall()
        return {
            'job_ids': {r['job_id'] for r in rows if r['job_id']},
            'segment_ids': {r['segment_id'] for r in rows if r['segment_id']},
            'guids': {r['nzb_guid'] for r in rows if r['nzb_guid']},
            'titles': {
                r['source_normalized_title'] or r['normalized_title']
                for r in rows
                if r['source_normalized_title'] or r['normalized_title']
            },
        }
    finally:
        conn.close()


def get_provisional_mount_attempt(cli_debrid_id: int):
    conn = get_db_connection()
    try:
        row = conn.execute(
            """SELECT * FROM mount_replacement_attempts
               WHERE cli_debrid_id=? AND status='provisional'
               ORDER BY updated_at DESC LIMIT 1""",
            (cli_debrid_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def queue_mount_replacement_cleanup(target: dict) -> bool:
    """Attach an exact old mounted file to the item's one active saga."""
    reason = target.get('failure_reason') or target.get('reason') or ''
    cli_debrid_id = int(target.get('cli_debrid_id') or 0)
    protocol = (target.get('protocol') or '').lower()
    allowed_reasons = NZB_INTERMEDIATE_REASONS if protocol == 'nzb' else PLAYBACK_CLEANUP_REASONS
    if reason not in allowed_reasons or not all((
            cli_debrid_id, target.get('entry_name'), target.get('file_name'), target.get('info_hash'))):
        return False
    item = get_media_item_for_cleanup(cli_debrid_id) or {}
    conn = get_db_connection()
    try:
        prior = conn.execute(
            """SELECT status FROM mount_replacement_cleanups
               WHERE cli_debrid_id=? AND old_info_hash=? AND file_name=? LIMIT 1""",
            (cli_debrid_id, target.get('info_hash'), target.get('file_name')),
        ).fetchone()
        if prior and prior['status'] in ('complete', 'blocked'):
            return True
        # Torrent exact cleanup retains the pre-verification rollback behavior.
        if protocol != 'nzb':
            conn.execute(
                """INSERT INTO mount_replacement_cleanups
                   (cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cli_debrid_id, old_info_hash, file_name) DO UPDATE SET
                     protocol=excluded.protocol, entry_name=excluded.entry_name,
                     reason=excluded.reason, updated_at=CURRENT_TIMESTAMP""",
                (cli_debrid_id, protocol, target['entry_name'], target['file_name'],
                 target['info_hash'], reason),
            )
            conn.commit()
            return True
        saga = conn.execute(
            "SELECT * FROM mount_replacement_sagas WHERE cli_debrid_id=? AND status IN ('pending','probe_failed') LIMIT 1",
            (cli_debrid_id,),
        ).fetchone()
        if saga:
            saga_id = saga['id']
            if not saga['activity_id']:
                activity_id = _create_pending_activity(target, item)
                conn.execute(
                    'UPDATE mount_replacement_sagas SET activity_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                    (activity_id, saga_id),
                )
        else:
            activity_id = _create_pending_activity(target, item)
            cursor = conn.execute(
                """INSERT INTO mount_replacement_sagas
                   (cli_debrid_id, protocol, activity_id) VALUES (?, ?, ?)""",
                (cli_debrid_id, 'nzb', activity_id),
            )
            saga_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (saga_id, cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cli_debrid_id, old_info_hash, file_name) DO UPDATE SET
                 saga_id=CASE WHEN mount_replacement_cleanups.status='complete'
                              THEN mount_replacement_cleanups.saga_id ELSE excluded.saga_id END,
                 protocol=excluded.protocol, entry_name=excluded.entry_name, reason=excluded.reason,
                 status=CASE WHEN mount_replacement_cleanups.status IN ('complete','blocked')
                             THEN mount_replacement_cleanups.status ELSE 'pending' END,
                 updated_at=CURRENT_TIMESTAMP""",
            (saga_id, cli_debrid_id, target.get('protocol') or '', target['entry_name'],
             target['file_name'], target['info_hash'], reason),
        )
        conn.execute(
            """UPDATE mount_replacement_sagas SET status='pending', next_attempt_at=NULL,
                      last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (saga_id,),
        )
        conn.commit()
        return True
    except Exception as exc:
        logging.error('[MountCleanup] Failed to persist replacement saga: %s', exc)
        return False
    finally:
        conn.close()


def set_mount_replacement_candidate(cli_debrid_id: int, info_hash: str, title: str = None) -> bool:
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """UPDATE mount_replacement_sagas
               SET candidate_info_hash=?, candidate_title=?, status='pending', attempts=0,
                   next_attempt_at=NULL, last_error=NULL,
                   verified_info_hash=NULL, verified_entry_name=NULL,
                   verified_file_name=NULL, verified_at=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE cli_debrid_id=? AND status IN ('pending','probe_failed')""",
            (info_hash or None, title or None, cli_debrid_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def update_mount_replacement_activity(cli_debrid_id: int, outcome: str = None, **changes) -> bool:
    """Transition the one activity row attached to an item's active saga."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT activity_id FROM mount_replacement_sagas WHERE cli_debrid_id=? AND status IN ('pending','probe_failed') LIMIT 1",
            (cli_debrid_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['activity_id']:
        return False
    if outcome is not None:
        changes['outcome'] = outcome
    _update_activity(row['activity_id'], **changes)
    return True


def _current_source_hash(item: dict, protocol: str) -> str:
    if protocol == 'nzb':
        value = str(item.get('filled_by_torrent_id') or '')
        return value[4:] if value.startswith('nzb:') else value
    magnet = str(item.get('filled_by_magnet') or '')
    match = re.search(r'btih:([A-Za-z0-9]+)', magnet, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    provider_id = str(item.get('filled_by_torrent_id') or '')
    return provider_id if not provider_id.startswith('nzb:') else ''


def _mounted_candidate_path(item: dict, entry_name: str, file_name: str) -> str:
    """Build one exact __all__ path while preserving the configured mount root."""
    source_path = str((item or {}).get('original_path_for_symlink') or '')
    marker = f'{os.sep}__all__{os.sep}'
    mount_root = source_path.split(marker, 1)[0] if marker in source_path else ''
    if not mount_root:
        try:
            from utilities.settings import get_setting
            configured = str(get_setting('Plex', 'mounted_file_location', '') or '')
            configured = configured.rstrip(os.sep)
            if configured.endswith(f'{os.sep}__all__'):
                mount_root = configured[:-len(f'{os.sep}__all__')]
            elif configured:
                mount_root = configured
        except Exception:
            mount_root = ''
    if not mount_root or not entry_name or not file_name:
        return ''
    all_root = os.path.abspath(os.path.join(mount_root, '__all__'))
    candidate = os.path.abspath(os.path.join(all_root, entry_name, file_name))
    try:
        if os.path.commonpath((all_root, candidate)) != all_root:
            return ''
    except ValueError:
        return ''
    return candidate


def _verified_candidate_details(saga, current_hash: str, item: dict) -> dict:
    """Return a reusable healthy result only for the unchanged exact source."""
    try:
        verified_hash = str(saga['verified_info_hash'] or '')
        entry_name = str(saga['verified_entry_name'] or '')
        file_name = str(saga['verified_file_name'] or '')
    except (KeyError, IndexError):
        return {}
    if not verified_hash or verified_hash.lower() != current_hash.lower():
        return {}
    if not candidate_matches_episode(
            item, entry={'name': entry_name, 'original_filename': file_name}):
        return {}
    source_path = _mounted_candidate_path(item, entry_name, file_name)
    if not source_path or not os.path.exists(source_path):
        return {}
    return {
        'status': 'healthy', 'reason': '', 'entry_name': entry_name,
        'file_name': file_name, 'info_hash': current_hash,
    }


def _persist_verified_candidate(saga_id: int, current_hash: str,
                                details: dict) -> bool:
    entry_name = str((details or {}).get('entry_name') or '')
    file_name = str((details or {}).get('file_name') or '')
    if not entry_name or not file_name:
        return False
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """UPDATE mount_replacement_sagas
                  SET verified_info_hash=?, verified_entry_name=?, verified_file_name=?,
                      verified_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND lower(candidate_info_hash)=lower(?)
                  AND status IN ('pending','probe_failed')""",
            (current_hash, entry_name, file_name, saga_id, current_hash),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _reconcile_verified_candidate(item: dict, current_hash: str,
                                  details: dict) -> tuple:
    """CAS source metadata to the exact mounted file that passed verification."""
    entry_name = str((details or {}).get('entry_name') or '')
    file_name = str((details or {}).get('file_name') or '')
    if not candidate_matches_episode(
            item, entry={'name': entry_name, 'original_filename': file_name}):
        return False, 'replacement_identity_conflict: verified file does not match the requested media identity'
    source_path = _mounted_candidate_path(item, entry_name, file_name)
    if not source_path or not os.path.exists(source_path):
        return False, 'replacement_symlink_not_ready: exact verified mounted source does not exist'
    item_id = int((item or {}).get('id') or 0)
    if not item_id or _current_source_hash(item, 'nzb').lower() != current_hash.lower():
        return False, 'replacement_source_changed: candidate changed before ownership reconciliation'

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """UPDATE media_items
                  SET filled_by_title=?, filled_by_file=?, original_path_for_symlink=?
                WHERE id=? AND state='Collected'
                  AND filled_by_torrent_id IN (?, ?)""",
            (entry_name, file_name, source_path, item_id,
             current_hash, f'nzb:{current_hash}'),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return False, 'replacement_source_changed: candidate changed during ownership reconciliation'
    finally:
        conn.close()

    refreshed = get_media_item_for_cleanup(item_id) or {}
    if (_current_source_hash(refreshed, 'nzb').lower() != current_hash.lower() or
            str(refreshed.get('filled_by_file') or '') != file_name or
            os.path.normcase(str(refreshed.get('original_path_for_symlink') or '')) !=
            os.path.normcase(source_path)):
        return False, 'replacement_source_changed: exact candidate ownership was not retained'
    return True, refreshed


def _ensure_replacement_symlink(item: dict, current_hash: str,
                                old_targets: list, verified_details: dict = None) -> tuple:
    """Fail closed unless Plex's path resolves to the current mounted file.

    Verification proves that the replacement mounted file works, but Plex may
    still have a missing or stale symlink to the old file.  Acknowledging that
    old file first would turn the Plex path into a dangling link and the normal
    symlink verifier could then remove the episode.  Repair the handoff before
    any acknowledgement and verify it afterwards.
    """
    item_id = int(item.get('id') or 0)
    source_path = str(item.get('original_path_for_symlink') or '')
    if verified_details:
        source_path = _mounted_candidate_path(
            item, str(verified_details.get('entry_name') or ''),
            str(verified_details.get('file_name') or ''),
        )
    plex_path = str(item.get('location_on_disk') or '')
    if not item_id or not source_path or not plex_path:
        return False, 'replacement_symlink_not_ready: current source or Plex path is missing'
    if not os.path.isabs(source_path) or not os.path.isabs(plex_path):
        return False, 'replacement_symlink_not_ready: replacement paths must be absolute'
    if not os.path.exists(source_path):
        return False, 'replacement_symlink_not_ready: current mounted source does not exist'

    current_file = str((verified_details or {}).get('file_name') or
                       item.get('filled_by_file') or '')
    if (current_file and
            os.path.basename(source_path) != os.path.basename(current_file)):
        return False, 'replacement_symlink_not_ready: mounted source does not match the current media file'

    normalized_source = os.path.normcase(os.path.realpath(source_path))
    for target in old_targets or ():
        old_entry = str(target['entry_name'] or '')
        old_file = str(target['file_name'] or '')
        if (old_entry and old_file and
                os.path.basename(source_path) == os.path.basename(old_file) and
                os.path.basename(os.path.dirname(source_path)) == old_entry):
            return False, 'replacement_symlink_not_ready: database source still points to the old cleanup target'

    if os.path.lexists(plex_path) and not os.path.islink(plex_path):
        return False, 'replacement_symlink_not_ready: Plex path exists but is not a symlink'

    current_link = (os.path.normcase(os.path.realpath(plex_path))
                    if os.path.islink(plex_path) else '')
    if current_link != normalized_source:
        try:
            from utilities.local_library_scan import create_symlink
            if not create_symlink(source_path, plex_path, media_item_id=item_id):
                return False, 'replacement_symlink_not_ready: failed to create the replacement symlink'
        except Exception as exc:
            logging.warning(
                '[MountCleanup] Replacement symlink handoff failed: item=%s '
                'source=%r plex=%r error=%s',
                item_id, source_path, plex_path, exc,
            )
            return False, f'replacement_symlink_not_ready: {exc}'

    if (not os.path.islink(plex_path) or not os.path.exists(plex_path) or
            os.path.normcase(os.path.realpath(plex_path)) != normalized_source):
        return False, 'replacement_symlink_not_ready: replacement symlink verification failed'

    logging.info(
        '[MountCleanup] Replacement symlink handoff ready: item=%s provider=%s '
        'plex=%r source=%r', item_id, current_hash, plex_path, source_path,
    )
    return True, ''


def media_item_source_hash(item: dict, protocol: str) -> str:
    return _current_source_hash(item, protocol)


def _schedule_saga_retry(conn, saga, message: str, *, delay: int = None,
                         increment_attempts: bool = True) -> None:
    attempts = int(saga['attempts'] or 0) + (1 if increment_attempts else 0)
    if delay is None:
        retry_index = max(attempts - 1, 0)
        delay = _RETRY_SECONDS[min(retry_index, len(_RETRY_SECONDS) - 1)]
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
    conn.execute(
        """UPDATE mount_replacement_sagas SET status='pending', attempts=?, next_attempt_at=?,
                  last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (attempts, next_attempt.strftime('%Y-%m-%d %H:%M:%S'), message[:1000], saga['id']),
    )


def _schedule_retry_for_result(conn, saga, message: str, details: dict = None) -> None:
    code = _response_code(details or {}) or str(message or '').split(':', 1)[0]
    if code == 'repair_busy':
        _schedule_saga_retry(conn, saga, message, delay=15, increment_attempts=False)
    elif code in ('replacement_not_ready', 'replacement_symlink_not_ready'):
        _schedule_saga_retry(conn, saga, message, delay=30, increment_attempts=False)
    elif code == 'stale_target':
        _schedule_saga_retry(conn, saga, message, delay=30, increment_attempts=False)
    else:
        _schedule_saga_retry(conn, saga, message)


def _mount_request(path: str, payload: dict, timeout: int) -> tuple:
    from routes.api_tracker import api
    from utilities.settings import get_setting
    base_url = (get_setting('Usenet Provider', 'url', default='') or '').rstrip('/')
    token = get_setting('Usenet Provider', 'api_token', default='') or ''
    if not base_url:
        return None, {}, 'cli_mount URL is not configured'
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        response = api.post(f'{base_url}{path}', json=payload, headers=headers, timeout=timeout)
        try:
            body = response.json() if response.content else {}
        except Exception:
            body = {}
        return response, body, None
    except Exception as exc:
        # APITracker raises for every non-2xx response. Preserve its response so
        # replacement cleanup can classify cli_mount's structured 4xx codes.
        response = getattr(exc, 'response', None)
        if response is not None:
            try:
                body = response.json() if response.content else {}
            except Exception:
                body = {}
            return response, body, None
        return None, {}, str(exc)


def _response_code(body: dict) -> str:
    return str(body.get('code') or body.get('reason') or '') if isinstance(body, dict) else ''


def _exact_registration_map(entry: dict, file_name: str, cli_debrid_id: int) -> dict:
    """Replace this item's stale filename while preserving healthy siblings."""
    registered = dict((entry or {}).get('cli_debrid_ids') or {})
    registered = {
        name: item_id for name, item_id in registered.items()
        if int(item_id or 0) != int(cli_debrid_id)
    }
    registered[file_name] = int(cli_debrid_id)
    return registered


def _registration_map_for_source(info_hash: str, file_name: str,
                                 cli_debrid_id: int, entry: dict = None) -> dict:
    """Build cli_mount's complete replacement map without a library sync.

    The exact job API exposes mounted files but older cli_mount builds omit the
    persisted ID map from that response. cli_debrid remains authoritative for
    healthy siblings still owned by this source, while cleanup rows retain exact
    filenames for siblings already moved to later candidates.
    """
    registered = dict((entry or {}).get('cli_debrid_ids') or {})
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT id, filled_by_file FROM media_items
               WHERE filled_by_torrent_id IN (?, ?)
                 AND state IN ('Checking','Collected','Upgrading')""",
            (info_hash, f'nzb:{info_hash}'),
        ).fetchall()
        for row in rows:
            if row['filled_by_file']:
                registered[row['filled_by_file']] = row['id']
        cleanup_rows = conn.execute(
            """SELECT c.file_name, c.cli_debrid_id
                 FROM mount_replacement_cleanups c
                 JOIN mount_replacement_sagas s ON s.id=c.saga_id
                WHERE lower(c.old_info_hash)=lower(?)
                  AND c.status IN ('pending','blocked')
                  AND s.protocol='nzb'
                  AND (s.status IN ('pending','probe_failed') OR
                       (s.status='blocked' AND s.last_error IN (?, ?)))""",
            (info_hash, *_REGISTRATION_STALE_ERRORS),
        ).fetchall()
        for row in cleanup_rows:
            if row['file_name']:
                registered[row['file_name']] = row['cli_debrid_id']
    except Exception:
        # Minimal/legacy schemas can still safely register the one exact file.
        pass
    finally:
        conn.close()
    return _exact_registration_map(
        {'cli_debrid_ids': registered}, file_name, cli_debrid_id,
    )


def _ensure_candidate_registration(cli_debrid_id: int, info_hash: str,
                                   item: dict = None) -> bool:
    """Register the exact mounted candidate before asking cli_mount to probe it."""
    try:
        from usenet.climount_client import get_climount_client
        client = get_climount_client()
        if not client or not client.is_enabled():
            return False
        lookup = client.get_exact_job(info_hash)
        lookup_status = str(lookup.get('status') or '')
        if lookup_status not in ('ready', 'missing'):
            return False
        entry = lookup.get('entry') or {}
        candidate_file = _select_candidate_file(item or {}, entry)
        if not candidate_file and lookup_status == 'missing':
            stored_file = str((item or {}).get('filled_by_file') or '')
            if any(stored_file.lower().endswith(ext) for ext in _VIDEO_EXTENSIONS):
                candidate_file = stored_file
        if not candidate_file:
            return False
        ids = _registration_map_for_source(
            info_hash, candidate_file, cli_debrid_id, entry,
        )
        if lookup_status == 'missing' and hasattr(client, 'register_cli_ids_with_status'):
            registered, _entry_not_found = client.register_cli_ids_with_status(info_hash, ids)
        else:
            registered = client.register_cli_ids(info_hash, ids)
        if lookup_status == 'missing':
            logging.info(
                '[MountCleanup] Candidate UUID absent from queue; attempted direct '
                'storage registration: item=%s info_hash=%s file=%r success=%s',
                cli_debrid_id, info_hash, candidate_file, bool(registered),
            )
        return bool(registered)
    except Exception as exc:
        logging.warning(
            '[MountCleanup] Candidate registration failed: item=%s info_hash=%s error=%s',
            cli_debrid_id, info_hash, exc,
        )
        return False


def _ensure_cleanup_registration(target) -> tuple:
    """Restore one old file's exact ID mapping immediately before acknowledgement."""
    try:
        from usenet.climount_client import get_climount_client
        client = get_climount_client()
        if not client or not client.is_enabled():
            return 'retry', 'cli_mount is unavailable'
        lookup = client.get_exact_job(target['old_info_hash'])
        status = str(lookup.get('status') or 'unavailable')
        entry = lookup.get('entry') or {}
        if status in ('unavailable', 'ambiguous'):
            return 'retry', f'replacement_not_ready: cleanup target {status}'
        if status != 'missing' and not entry:
            return 'retry', f'replacement_not_ready: cleanup target {status}'
        ids = _registration_map_for_source(
            target['old_info_hash'], target['file_name'],
            int(target['cli_debrid_id']), entry,
        )
        if hasattr(client, 'register_cli_ids_with_status'):
            registered, entry_not_found = client.register_cli_ids_with_status(
                target['old_info_hash'], ids,
            )
        else:
            registered = client.register_cli_ids(target['old_info_hash'], ids)
            entry_not_found = False
        if entry_not_found:
            logging.info(
                '[MountCleanup] Provider entry unavailable for exact cleanup; '
                'deferring ownership authorization to acknowledgement: '
                'item=%s info_hash=%s file=%r',
                target['cli_debrid_id'], target['old_info_hash'],
                target['file_name'],
            )
            return 'ready', ''
        if not registered and status != 'missing':
            return 'retry', 'replacement_not_ready: exact cleanup registration failed'
        if status == 'missing':
            # Retained storage entries are not always present in cli_mount's
            # queue listing. PATCH still resolves them through persistent
            # storage; if the entry truly disappeared, acknowledgement safely
            # returns already_removed. It also preserves structured retry/error
            # handling if PATCH failed for an infrastructure reason.
            logging.info(
                '[MountCleanup] Cleanup UUID absent from queue; attempted direct '
                'storage registration: item=%s info_hash=%s file=%r success=%s',
                target['cli_debrid_id'], target['old_info_hash'],
                target['file_name'], bool(registered),
            )
            if registered:
                return 'ready', ''
            return 'retry', 'replacement_not_ready: direct cleanup registration failed'
        logging.info(
            '[MountCleanup] Restored exact cleanup registration: item=%s info_hash=%s file=%r',
            target['cli_debrid_id'], target['old_info_hash'], target['file_name'],
        )
        return 'ready', ''
    except Exception as exc:
        logging.warning(
            '[MountCleanup] Exact cleanup registration failed: item=%s info_hash=%s error=%s',
            target['cli_debrid_id'], target['old_info_hash'], exc,
        )
        return 'retry', str(exc)


def _candidate_files(entry: dict) -> list:
    """Normalize active video files from cli_mount's exact job response."""
    raw_files = (entry or {}).get('files') or {}
    files = []
    if isinstance(raw_files, dict):
        iterable = []
        for key, value in raw_files.items():
            data = dict(value) if isinstance(value, dict) else {}
            data.setdefault('name', key)
            iterable.append(data)
    elif isinstance(raw_files, list):
        iterable = [value for value in raw_files if isinstance(value, dict)]
    else:
        iterable = []
    for data in iterable:
        name = str(data.get('name') or data.get('path') or '')
        try:
            size = int(data.get('size') or 0)
        except (TypeError, ValueError):
            size = 0
        if (name and not data.get('deleted') and
                re.search(r'\.[A-Za-z0-9]+$', name) and
                name[name.rfind('.'):].lower() in _VIDEO_EXTENSIONS):
            files.append({'name': name, 'size': size})
    return files


def _episode_tokens(value) -> tuple:
    """Return explicit episode pairs and season-only tokens from a name."""
    text = str(value or '')
    episodes = set()
    for match in _EPISODE_TOKEN.finditer(text):
        season = int(match.group(1))
        episodes.add((season, int(match.group(2))))
        # Preserve multi-episode releases such as S05E01E02 or S05E01-E02.
        remainder = text[match.end():]
        while True:
            extra = re.match(r'(?i)^[. _-]*e0*(\d{1,3})(?![0-9])', remainder)
            if not extra:
                break
            episodes.add((season, int(extra.group(1))))
            remainder = remainder[extra.end():]
    seasons = {int(match.group(1)) for match in _SEASON_TOKEN.finditer(text)}
    return episodes, seasons


def candidate_matches_episode(item: dict, result: dict = None,
                              entry: dict = None) -> bool:
    """Require a candidate to prove it contains the requested episode.

    Explicit conflicting episode information always wins over renamed/provider
    metadata. A season pack is allowed here, but its mounted file must still be
    selected by exact SxxExx identity in ``_select_candidate_file``.
    """
    if str((item or {}).get('type') or '').lower() != 'episode':
        return True
    try:
        target = (int(item.get('season_number')), int(item.get('episode_number')))
    except (TypeError, ValueError):
        return False

    proved_episode = False
    proved_season_pack = False
    parsed = ((result or {}).get('parsed_info') or {})
    season_episode = parsed.get('season_episode_info') or {}
    parsed_seasons = parsed.get('seasons') or season_episode.get('seasons') or []
    parsed_episodes = parsed.get('episodes') or season_episode.get('episodes') or []
    try:
        seasons = {int(value) for value in parsed_seasons}
        episodes = {int(value) for value in parsed_episodes}
    except (TypeError, ValueError):
        return False
    if episodes:
        if target[1] not in episodes or (seasons and target[0] not in seasons):
            return False
        proved_episode = True
    elif seasons:
        if target[0] not in seasons:
            return False
        proved_season_pack = True

    values = []
    for source in (result or {}, entry or {}):
        for key in ('title', 'original_title', 'name', 'folder_name',
                    'original_filename'):
            value = source.get(key)
            if value:
                values.append(value)
    for value in values:
        explicit_episodes, explicit_seasons = _episode_tokens(value)
        if explicit_episodes:
            if target not in explicit_episodes:
                return False
            proved_episode = True
        elif explicit_seasons:
            if target[0] not in explicit_seasons:
                return False
            proved_season_pack = True

    return proved_episode or proved_season_pack


def _entry_conflicts_with_episode(item: dict, entry: dict) -> bool:
    """Reject mounted-entry metadata that explicitly names a sibling episode."""
    try:
        target = (int(item.get('season_number')), int(item.get('episode_number')))
    except (TypeError, ValueError):
        return True
    for key in ('name', 'folder_name', 'original_filename', 'original_title'):
        value = (entry or {}).get(key)
        if not value:
            continue
        episodes, seasons = _episode_tokens(value)
        if episodes and target not in episodes:
            return True
        if not episodes and seasons and target[0] not in seasons:
            return True
    return False


def _select_candidate_file(item: dict, entry: dict) -> str:
    """Select one exact candidate file without release-name matching."""
    files = _candidate_files(entry)
    if not files:
        return ''
    if str(item.get('type') or '').lower() == 'episode':
        season = item.get('season_number')
        episode = item.get('episode_number')
        if season is None or episode is None:
            return ''
        if _entry_conflicts_with_episode(item, entry):
            return ''
        pattern = re.compile(
            rf'(?i)(?:^|[^a-z0-9])s0*{int(season)}e0*{int(episode)}(?![0-9])'
        )
        matches = [value for value in files if pattern.search(value['name'])]
        if len(matches) == 1:
            return matches[0]['name']
        if len(files) != 1 or not candidate_matches_episode(item, entry=entry):
            return ''
        # An opaque provider filename is safe only when the mounted entry itself
        # proves this exact episode. A season-pack label is insufficient because
        # it cannot identify which sibling the sole opaque file contains.
        entry_values = [
            (entry or {}).get(key)
            for key in ('name', 'folder_name', 'original_filename', 'original_title')
        ]
        if any(
                (int(season), int(episode)) in _episode_tokens(value)[0]
                for value in entry_values if value):
            return files[0]['name']
        return ''
    if len(files) == 1:
        return files[0]['name']
    files.sort(key=lambda value: value['size'], reverse=True)
    if files[0]['size'] > files[1]['size']:
        return files[0]['name']
    return ''


def _recover_recorded_candidate(saga, item: dict, old_hashes: set) -> tuple:
    """Target and register one recorded candidate; never synchronize the library."""
    candidate_hash = str(saga['candidate_info_hash'] or '')
    if not candidate_hash or candidate_hash.lower() in old_hashes:
        return False, 'replacement candidate is not recorded'
    try:
        from usenet.climount_client import get_climount_client
        client = get_climount_client()
        lookup = client.get_exact_job(candidate_hash) if client and client.is_enabled() else {
            'status': 'unavailable', 'entry': None,
        }
    except Exception as exc:
        lookup = {'status': 'unavailable', 'entry': None, 'error': str(exc)}

    lookup_status = str(lookup.get('status') or 'unavailable')
    if lookup_status != 'ready':
        logging.info(
            '[MountCleanup] Exact candidate recovery deferred: saga=%s item=%s '
            'candidate=%s status=%s',
            saga['id'], saga['cli_debrid_id'], candidate_hash, lookup_status,
        )
        return False, f'replacement_not_ready: exact candidate {lookup_status}'
    candidate_file = _select_candidate_file(item, lookup.get('entry') or {})
    if not candidate_file:
        return False, 'replacement_not_ready: exact candidate file is missing or ambiguous'
    if not client.register_cli_ids(
            candidate_hash,
            _registration_map_for_source(
                candidate_hash, candidate_file,
                int(saga['cli_debrid_id']),
                lookup.get('entry') or {},
            )):
        return False, 'replacement_not_ready: exact candidate registration failed'

    restore_conn = get_db_connection()
    try:
        old_source = str(item.get('filled_by_torrent_id') or '')
        restored = restore_conn.execute(
            """UPDATE media_items SET filled_by_torrent_id=?
               WHERE id=? AND filled_by_torrent_id=?""",
            (f'nzb:{candidate_hash}', saga['cli_debrid_id'], old_source),
        )
        restore_conn.execute(
            """UPDATE mount_replacement_sagas SET last_reconciled_at=CURRENT_TIMESTAMP,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (saga['id'],),
        )
        restore_conn.commit()
    finally:
        restore_conn.close()
    if restored.rowcount != 1:
        return False, 'replacement_not_ready: media source changed during targeted recovery'
    logging.info(
        '[MountCleanup] Restored exact candidate ownership: saga=%s item=%s '
        'candidate=%s file=%r',
        saga['id'], saga['cli_debrid_id'], candidate_hash, candidate_file,
    )
    return True, candidate_hash


def _source_is_torrent(item: dict) -> bool:
    provider_id = str(item.get('filled_by_torrent_id') or '')
    magnet = str(item.get('filled_by_magnet') or '')
    return bool(provider_id and not provider_id.startswith('nzb:') and
                re.search(r'urn:btih:[A-Za-z0-9]+', magnet, re.IGNORECASE))


def _close_terminal_saga(saga, *, status: str, outcome: str, message: str) -> bool:
    """Close an unsafe-to-continue saga without touching mount or Plex state."""
    conn = get_db_connection()
    try:
        conn.execute(
            """UPDATE mount_replacement_sagas SET status=?, last_error=?,
                      next_attempt_at=NULL, completed_at=CURRENT_TIMESTAMP,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, message[:1000], saga['id']),
        )
        if saga['activity_id'] and not _update_activity(
                saga['activity_id'], connection=conn, outcome=outcome,
                replacement_nzb_id=None, replacement_title=None):
            raise RuntimeError('linked replacement activity could not be finalized')
        conn.commit()
        logging.warning('[MountCleanup] Closed saga %s as %s: %s', saga['id'], status, message)
        return True
    except Exception as exc:
        conn.rollback()
        logging.warning('[MountCleanup] Could not close saga %s: %s', saga['id'], exc)
        return False
    finally:
        conn.close()


def _verify_replacement(cli_debrid_id: int, info_hash: str) -> tuple:
    logging.info(
        '[MountCleanup] Verifying NZB replacement: cli_debrid_id=%s info_hash=%s',
        cli_debrid_id, info_hash,
    )
    response, body, error = _mount_request(
        '/api/repair/replacements/verify',
        {'cli_debrid_id': cli_debrid_id, 'info_hash': info_hash},
        75,
    )
    if error:
        return 'retry', error, {}
    if response.status_code == 200:
        status = body.get('status') if isinstance(body, dict) else None
        if status in ('healthy', 'broken', 'unknown'):
            logging.info(
                '[MountCleanup] Verification result: cli_debrid_id=%s info_hash=%s '
                'status=%s reason=%s entry=%r file=%r',
                cli_debrid_id, info_hash, status, body.get('reason', ''),
                body.get('entry_name', ''), body.get('file_name', ''),
            )
            return status, body.get('reason', ''), body
        return 'retry', 'invalid verification response', body
    code = body.get('code', '') if isinstance(body, dict) else ''
    message = (body.get('message') if isinstance(body, dict) else None) or f'HTTP {response.status_code}'
    if response.status_code == 404:
        return 'retry', 'cli_mount does not support replacement verification yet', body
    if response.status_code >= 500 or code in ('repair_busy', 'replacement_not_ready'):
        return 'retry', message, body
    return 'blocked', f'{code or response.status_code}: {message}', body


def _acknowledge(row) -> tuple:
    logging.info(
        '[MountCleanup] Acknowledging exact old file: cli_debrid_id=%s '
        'info_hash=%s entry=%r file=%r reason=%s',
        row['cli_debrid_id'], row['old_info_hash'], row['entry_name'],
        row['file_name'], row['reason'],
    )
    response, body, error = _mount_request(
        '/api/repair/replacements/ack',
        {'entry_name': row['entry_name'], 'file_name': row['file_name'],
         'info_hash': row['old_info_hash'], 'cli_debrid_id': row['cli_debrid_id'],
         'reason': row['reason']},
        20,
    )
    if error:
        return 'retry', error
    if response.status_code == 200 and body.get('status') in ('removed', 'already_removed'):
        logging.info(
            '[MountCleanup] Exact old-file acknowledgement completed: '
            'info_hash=%s file=%r status=%s',
            row['old_info_hash'], row['file_name'], body.get('status'),
        )
        return 'complete', body.get('status')
    code = body.get('code', '') if isinstance(body, dict) else ''
    message = (body.get('message') if isinstance(body, dict) else None) or f'HTTP {response.status_code}'
    if response.status_code == 404:
        return 'retry', 'cli_mount does not support replacement acknowledgement yet'
    if response.status_code >= 500 or code == 'repair_busy':
        return 'retry', f'{code}: {message}' if code else message
    return 'blocked', f'{code or response.status_code}: {message}'


def _update_activity(activity_id: int, connection=None, **changes) -> bool:
    try:
        from database.nzb_repair_activity import update_repair_activity
        return update_repair_activity(activity_id, connection=connection, **changes)
    except Exception as exc:
        logging.warning('[MountCleanup] Could not update activity: %s', exc)
        return False


def _process_legacy_cleanups(item_id: int = None) -> dict:
    """Preserve the rollback's direct Collected→ack behavior for torrents."""
    result = {'completed': 0, 'retried': 0, 'blocked': 0, 'waiting': 0}
    conn = get_db_connection()
    sql = """SELECT c.*, m.state, m.filled_by_torrent_id, m.filled_by_magnet
             FROM mount_replacement_cleanups c
             LEFT JOIN media_items m ON m.id=c.cli_debrid_id
             WHERE c.status='pending' AND c.saga_id IS NULL
               AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= CURRENT_TIMESTAMP)"""
    params = []
    if item_id is not None:
        sql += ' AND c.cli_debrid_id=?'
        params.append(item_id)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    for row in rows:
        if row['state'] != 'Collected':
            result['waiting'] += 1
            continue
        current_hash = _current_source_hash(dict(row), (row['protocol'] or '').lower())
        if not current_hash or current_hash.lower() == (row['old_info_hash'] or '').lower():
            result['waiting'] += 1
            continue
        handoff_item = get_media_item_for_cleanup(row['cli_debrid_id']) or dict(row)
        handoff_item['id'] = row['cli_debrid_id']
        handoff_ready, handoff_message = _ensure_replacement_symlink(
            handoff_item, current_hash, [row],
        )
        if not handoff_ready:
            conn = get_db_connection()
            try:
                next_attempt = datetime.now(timezone.utc) + timedelta(seconds=30)
                conn.execute(
                    """UPDATE mount_replacement_cleanups
                          SET next_attempt_at=?, last_error=?, updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                    (next_attempt.strftime('%Y-%m-%d %H:%M:%S'),
                     handoff_message[:1000], row['id']),
                )
                conn.commit()
            finally:
                conn.close()
            logging.warning(
                '[MountCleanup] Exact cleanup deferred until replacement symlink is ready: '
                'item=%s reason=%s', row['cli_debrid_id'], handoff_message,
            )
            result['retried'] += 1
            continue
        status, message = _acknowledge(row)
        conn = get_db_connection()
        try:
            if status == 'complete':
                conn.execute(
                    """UPDATE mount_replacement_cleanups SET status='complete',
                              completed_at=CURRENT_TIMESTAMP, next_attempt_at=NULL,
                              last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (row['id'],),
                )
                result['completed'] += 1
            elif status == 'blocked':
                conn.execute(
                    "UPDATE mount_replacement_cleanups SET status='blocked', last_error=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (message[:1000], row['id']),
                )
                result['blocked'] += 1
            else:
                attempts = int(row['attempts'] or 0) + 1
                delay = _LEGACY_RETRY_SECONDS[
                    min(attempts - 1, len(_LEGACY_RETRY_SECONDS) - 1)
                ]
                next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
                conn.execute(
                    """UPDATE mount_replacement_cleanups SET attempts=?, next_attempt_at=?,
                              last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (attempts, next_attempt.strftime('%Y-%m-%d %H:%M:%S'),
                     message[:1000], row['id']),
                )
                result['retried'] += 1
            conn.commit()
        finally:
            conn.close()
    return result


def _cleanup_failed_submission_jobs(saga_id: int) -> bool:
    """Delete retained terminal jobs by exact UUID; never fall back to names."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT id, job_id FROM mount_replacement_attempts
               WHERE saga_id=? AND status='failed_submission' AND cleaned_at IS NULL""",
            (saga_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return True
    from usenet import get_usenet_client
    client = get_usenet_client()
    for row in rows:
        if not row['job_id']:
            # The submission failed before cli_mount returned an addressable job.
            # There is no exact UUID to delete, so it must not hold the saga forever.
            conn = get_db_connection()
            try:
                conn.execute(
                    "UPDATE mount_replacement_attempts SET cleaned_at=CURRENT_TIMESTAMP, "
                    "reason=COALESCE(reason, 'no_exact_job_id'), updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row['id'],),
                )
                conn.commit()
            finally:
                conn.close()
            continue
        if not hasattr(client, 'remove_nzb_exact') or not client.remove_nzb_exact(row['job_id']):
            return False
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE mount_replacement_attempts SET cleaned_at=CURRENT_TIMESTAMP, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row['id'],),
            )
            conn.commit()
        finally:
            conn.close()
    return True


def _mark_collected_attempt_cleaned(conn, saga_id: int, info_hash: str) -> None:
    """Record exact cleanup completion for a mounted failed candidate."""
    if not info_hash:
        return
    conn.execute(
        """UPDATE mount_replacement_attempts
           SET cleaned_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
           WHERE saga_id=? AND status='failed_collected' AND job_id=?""",
        (saga_id, info_hash),
    )


def _refresh_verified_plex_item(item: dict) -> bool:
    """Refresh the retained Plex path without deleting its metadata item."""
    path = item.get('location_on_disk') or ''
    if not path:
        logging.info('[MountCleanup] Plex refresh skipped for item %s: no location_on_disk', item.get('id'))
        return False
    if not os.path.islink(path) or not os.path.exists(path):
        logging.warning(
            '[MountCleanup] Plex refresh refused for item %s: replacement symlink '
            'is missing or unresolved at %r', item.get('id'), path,
        )
        return False
    try:
        from utilities.plex_functions import plex_update_item
        refreshed = plex_update_item({
            'full_path': path,
            'location_on_disk': path,
            'type': item.get('type') or 'movie',
        })
        logging.info(
            '[MountCleanup] Targeted Plex refresh after exact cleanup: item=%s path=%r success=%s',
            item.get('id'), path, bool(refreshed),
        )
        return bool(refreshed)
    except Exception as exc:
        logging.warning('[MountCleanup] Targeted Plex refresh failed for item %s: %s', item.get('id'), exc)
        return False


def _is_stale_cleanup_saga(saga) -> bool:
    try:
        return str(saga['saga_kind'] or '') == 'stale_cleanup'
    except (IndexError, KeyError):
        return False


def _update_stale_cleanup_activities(conn, saga_id: int, **changes) -> bool:
    rows = conn.execute(
        """SELECT DISTINCT activity_id FROM mount_replacement_cleanups
            WHERE saga_id=? AND activity_id IS NOT NULL""",
        (saga_id,),
    ).fetchall()
    for row in rows:
        if not _update_activity(row['activity_id'], connection=conn, **changes):
            return False
    return True


def _finish_stale_plex_refresh(saga, item: dict, current_hash: str,
                               result: dict) -> None:
    """Finish retroactive cleanup only after Plex accepts the targeted refresh."""
    conn = get_db_connection()
    try:
        targets = conn.execute(
            'SELECT * FROM mount_replacement_cleanups WHERE saga_id=? ORDER BY id',
            (saga['id'],),
        ).fetchall()
    finally:
        conn.close()
    handoff_ready, handoff_message = _ensure_replacement_symlink(
        item, current_hash, targets,
    )
    if not handoff_ready:
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE mount_replacement_sagas SET plex_refresh_status='pending' WHERE id=?",
                (saga['id'],),
            )
            _schedule_saga_retry(
                conn, saga, handoff_message, delay=30,
                increment_attempts=False,
            )
            conn.commit()
        finally:
            conn.close()
        result['retried'] += 1
        logging.warning(
            '[MountCleanup] Stale cleanup Plex refresh deferred until the '
            'replacement symlink is ready: saga=%s reason=%s',
            saga['id'], handoff_message,
        )
        return
    refreshed = _refresh_verified_plex_item(item)
    final_title = (item.get('filled_by_title') or item.get('filled_by_file') or
                   saga['candidate_title'] or item.get('title'))
    conn = get_db_connection()
    try:
        if refreshed:
            conn.execute(
                """UPDATE mount_replacement_sagas
                      SET status='complete', candidate_info_hash=?, candidate_title=?,
                          plex_refresh_status='complete', completed_at=CURRENT_TIMESTAMP,
                          last_error=NULL, next_attempt_at=NULL,
                          updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (current_hash, final_title, saga['id']),
            )
            if not _update_stale_cleanup_activities(
                    conn, saga['id'],
                    replacement_nzb_id=current_hash,
                    replacement_title=final_title, outcome='stale_entry_cleaned'):
                raise RuntimeError('linked stale-cleanup activity could not be finalized')
            conn.commit()
            logging.info(
                '[MountCleanup] Stale old NZB cleanup completed: saga=%s current_item=%s '
                'replacement=%s', saga['id'], saga['cli_debrid_id'], current_hash,
            )
            return
        conn.execute(
            "UPDATE mount_replacement_sagas SET plex_refresh_status='pending' WHERE id=?",
            (saga['id'],),
        )
        _schedule_saga_retry(conn, saga, 'targeted Plex refresh is pending')
        if not _update_stale_cleanup_activities(
                conn, saga['id'],
                replacement_nzb_id=None, replacement_title=None,
                outcome='stale_cleanup_pending'):
            raise RuntimeError('linked stale-cleanup activity could not be updated')
        conn.commit()
        result['retried'] += 1
    except Exception as exc:
        conn.rollback()
        result['retried'] += 1
        logging.warning(
            '[MountCleanup] Stale cleanup Plex finalization rolled back for saga %s: %s',
            saga['id'], exc,
        )
    finally:
        conn.close()


def _finish_torrent_supersession(saga, item: dict, all_targets: list,
                                 result: dict) -> None:
    """Remove exact failed NZB files after a torrent has reached Collected."""
    current_hash = _current_source_hash(item, 'torrent')
    handoff_ready, handoff_message = _ensure_replacement_symlink(
        item, current_hash, all_targets,
    )
    if not handoff_ready:
        conn = get_db_connection()
        try:
            _schedule_saga_retry(
                conn, saga, handoff_message, delay=30,
                increment_attempts=False,
            )
            conn.commit()
        finally:
            conn.close()
        logging.warning(
            '[MountCleanup] Torrent supersession cleanup deferred until the '
            'replacement symlink is ready: saga=%s item=%s reason=%s',
            saga['id'], saga['cli_debrid_id'], handoff_message,
        )
        result['retried'] += 1
        return
    all_complete = _cleanup_failed_submission_jobs(saga['id'])
    retry_message = None
    terminal_failure = None
    for target in (row for row in all_targets if row['status'] == 'pending'):
        registration_status, registration_message = _ensure_cleanup_registration(target)
        if registration_status != 'ready':
            retry_message = registration_message
            all_complete = False
            result['retried'] += 1
            continue
        ack_status, ack_message = _acknowledge(target)
        conn = get_db_connection()
        try:
            if ack_status == 'complete':
                conn.execute(
                    """UPDATE mount_replacement_cleanups SET status='complete',
                              completed_at=CURRENT_TIMESTAMP, next_attempt_at=NULL,
                              last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (target['id'],),
                )
                _mark_collected_attempt_cleaned(conn, saga['id'], target['old_info_hash'])
                result['completed'] += 1
            elif ack_status == 'blocked':
                conn.execute(
                    """UPDATE mount_replacement_cleanups SET status='blocked',
                              last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (ack_message[:1000], target['id']),
                )
                terminal_failure = ack_message
                all_complete = False
                result['blocked'] += 1
            else:
                retry_message = ack_message
                all_complete = False
                result['retried'] += 1
            conn.commit()
        finally:
            conn.close()

    blocked_target = next((row for row in all_targets if row['status'] == 'blocked'), None)
    if blocked_target:
        terminal_failure = blocked_target['last_error'] or 'exact NZB cleanup is blocked'
        all_complete = False
    if all_complete:
        handoff_ready, handoff_message = _ensure_replacement_symlink(
            item, current_hash, all_targets,
        )
        if not handoff_ready:
            all_complete = False
            retry_message = handoff_message
            result['retried'] += 1

    finish_conn = get_db_connection()
    refresh_item = None
    try:
        if terminal_failure:
            finish_conn.execute(
                """UPDATE mount_replacement_sagas SET status='blocked', last_error=?,
                          updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (terminal_failure[:1000], saga['id']),
            )
            if saga['activity_id'] and not _update_activity(
                    saga['activity_id'], connection=finish_conn,
                    outcome='replacement_cleanup_stale'):
                raise RuntimeError('linked replacement activity could not be finalized')
        elif all_complete:
            finish_conn.execute(
                """UPDATE mount_replacement_sagas SET status='superseded',
                          last_error='active NZB replacement was superseded by a collected torrent',
                          next_attempt_at=NULL, completed_at=CURRENT_TIMESTAMP,
                          updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (saga['id'],),
            )
            if saga['activity_id'] and not _update_activity(
                    saga['activity_id'], connection=finish_conn,
                    replacement_nzb_id=None, replacement_title=None,
                    outcome='replacement_superseded'):
                raise RuntimeError('linked replacement activity could not be finalized')
            refresh_item = item
        else:
            _schedule_retry_for_result(
                finish_conn, saga,
                retry_message or 'torrent collected; exact NZB cleanup is pending',
            )
        finish_conn.commit()
    except Exception as exc:
        finish_conn.rollback()
        refresh_item = None
        result['retried'] += 1
        logging.warning(
            '[MountCleanup] Torrent supersession finalization rolled back for saga %s: %s',
            saga['id'], exc,
        )
    finally:
        finish_conn.close()
    if refresh_item is not None:
        logging.info(
            '[MountCleanup] Collected torrent superseded NZB saga %s; '
            'exact old-file cleanup completed', saga['id'],
        )
        _refresh_verified_plex_item(refresh_item)


def _process_pending_mount_cleanups(item_id: int = None) -> dict:
    """Verify collected candidates, then acknowledge every exact old file."""
    result = {'completed': 0, 'retried': 0, 'blocked': 0, 'waiting': 0, 'probe_failed': 0}
    legacy = _process_legacy_cleanups(item_id=item_id)
    for key, value in legacy.items():
        result[key] += value
    conn = get_db_connection()
    sql = """SELECT s.*, m.state, m.filled_by_torrent_id, m.filled_by_magnet,
                    m.title, m.type, m.season_number, m.episode_number
             FROM mount_replacement_sagas s
             LEFT JOIN media_items m ON m.id=s.cli_debrid_id
             WHERE s.protocol='nzb' AND s.status IN ('pending','probe_failed')
               AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= CURRENT_TIMESTAMP)"""
    params = []
    if item_id is not None:
        sql += ' AND s.cli_debrid_id=?'
        params.append(item_id)
    try:
        sagas = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    for saga in sagas:
        saga_dict = dict(saga)
        if saga['state'] is None:
            stale_cleanup = _is_stale_cleanup_saga(saga)
            if _close_terminal_saga(
                    saga, status='abandoned',
                    outcome=('stale_entry_unresolved' if stale_cleanup else 'replacement_abandoned'),
                    message='media item no longer exists; no cleanup was attempted'):
                result['blocked'] += 1
            else:
                result['retried'] += 1
            continue
        if saga['state'] != 'Collected':
            result['waiting'] += 1
            continue
        conn = get_db_connection()
        try:
            all_targets = conn.execute(
                "SELECT * FROM mount_replacement_cleanups WHERE saga_id=? ORDER BY id",
                (saga['id'],),
            ).fetchall()
        finally:
            conn.close()
        if not all_targets:
            result['waiting'] += 1
            continue
        if _source_is_torrent(saga_dict):
            _finish_torrent_supersession(
                saga, get_media_item_for_cleanup(saga['cli_debrid_id']) or saga_dict,
                all_targets, result,
            )
            continue
        targets = [row for row in all_targets if row['status'] == 'pending']
        current_hash = _current_source_hash(saga_dict, (saga['protocol'] or '').lower())
        old_hashes = {(row['old_info_hash'] or '').lower() for row in all_targets}
        if not current_hash or current_hash.lower() in old_hashes:
            candidate_hash = str(saga['candidate_info_hash'] or '')
            if candidate_hash and candidate_hash.lower() not in old_hashes:
                recovered, recovery_detail = _recover_recorded_candidate(
                    saga, saga_dict, old_hashes,
                )
                if recovered:
                    current_hash = recovery_detail
                    saga_dict['filled_by_torrent_id'] = f'nzb:{current_hash}'
                else:
                    retry_conn = get_db_connection()
                    try:
                        _schedule_saga_retry(
                            retry_conn, saga, recovery_detail, delay=30,
                            increment_attempts=False,
                        )
                        retry_conn.commit()
                    finally:
                        retry_conn.close()
                    result['retried'] += 1
            if not current_hash or current_hash.lower() in old_hashes:
                if not candidate_hash or candidate_hash.lower() in old_hashes:
                    result['waiting'] += 1
                continue
        if (_is_stale_cleanup_saga(saga) and
                all(row['status'] == 'complete' for row in all_targets)):
            _finish_stale_plex_refresh(
                saga, get_media_item_for_cleanup(saga['cli_debrid_id']) or saga_dict,
                current_hash, result,
            )
            continue
        if saga['status'] == 'probe_failed' and current_hash.lower() == str(saga['candidate_info_hash'] or '').lower():
            result['waiting'] += 1
            continue

        candidate_item = get_media_item_for_cleanup(saga['cli_debrid_id']) or {}
        candidate_title = (candidate_item.get('filled_by_title') or
                           candidate_item.get('filled_by_file') or
                           saga['candidate_title'])
        if current_hash.lower() != str(saga['candidate_info_hash'] or '').lower():
            set_mount_replacement_candidate(saga['cli_debrid_id'], current_hash, candidate_title)
            # Candidate details stay private until verification and cleanup
            # both succeed; the activity row remains a neutral pending record.

        registration_item = get_media_item_for_cleanup(saga['cli_debrid_id']) or saga_dict
        if not _ensure_candidate_registration(
                saga['cli_debrid_id'], current_hash, registration_item):
            retry_conn = get_db_connection()
            try:
                _schedule_saga_retry(
                    retry_conn, saga, 'replacement_not_ready', delay=30,
                    increment_attempts=False,
                )
                retry_conn.commit()
            finally:
                retry_conn.close()
            result['retried'] += 1
            continue

        verify_details = _verified_candidate_details(saga, current_hash, registration_item)
        if verify_details:
            verify_status, message = 'healthy', ''
            logging.info(
                '[MountCleanup] Reusing healthy replacement verification: '
                'cli_debrid_id=%s info_hash=%s entry=%r file=%r',
                saga['cli_debrid_id'], current_hash,
                verify_details['entry_name'], verify_details['file_name'],
            )
        else:
            verify_status, message, verify_details = _verify_replacement(
                saga['cli_debrid_id'], current_hash,
            )
        # A collection/update may have won the race while cli_mount was probing.
        current_item = get_media_item_for_cleanup(saga['cli_debrid_id']) or {}
        if current_item.get('state') != 'Collected' or _current_source_hash(current_item, (saga['protocol'] or '').lower()).lower() != current_hash.lower():
            result['waiting'] += 1
            continue

        if verify_status == 'healthy':
            if not verify_details.get('entry_name') or not verify_details.get('file_name'):
                verify_status = 'retry'
                message = 'invalid verification response: exact mounted file identity is missing'
            elif not _persist_verified_candidate(saga['id'], current_hash, verify_details):
                verify_status = 'retry'
                message = 'replacement_source_changed: candidate changed before verification was persisted'
            else:
                reconciled, reconciliation = _reconcile_verified_candidate(
                    current_item, current_hash, verify_details,
                )
                if not reconciled:
                    verify_status = 'retry'
                    message = str(reconciliation)
                else:
                    current_item = reconciliation

        update_conn = get_db_connection()
        try:
            if verify_status == 'broken':
                if _is_stale_cleanup_saga(saga):
                    _schedule_saga_retry(
                        update_conn, saga,
                        f'replacement playback validation failed: {message}',
                    )
                    update_conn.execute(
                        """UPDATE mount_replacement_sagas
                              SET verified_info_hash=NULL, verified_entry_name=NULL,
                                  verified_file_name=NULL, verified_at=NULL
                            WHERE id=?""",
                        (saga['id'],),
                    )
                    update_conn.commit()
                    activity_conn = get_db_connection()
                    try:
                        _update_stale_cleanup_activities(
                            activity_conn, saga['id'], replacement_nzb_id=None,
                            replacement_title=None, outcome='stale_cleanup_pending',
                        )
                        activity_conn.commit()
                    finally:
                        activity_conn.close()
                    result['probe_failed'] += 1
                    continue
                failed_title = (current_item.get('filled_by_title') or current_item.get('filled_by_file')
                                or candidate_title or current_hash)
                record_mount_replacement_attempt(
                    saga['cli_debrid_id'], job_id=current_hash, title=failed_title,
                    segment_id=current_item.get('nzb_segment_id') or '',
                    nzb_guid=current_item.get('filled_by_magnet') or '',
                    status='failed_collected', reason=message,
                )
                queue_mount_replacement_cleanup({
                    'entry_name': verify_details.get('entry_name') or '',
                    'file_name': verify_details.get('file_name') or '',
                    'info_hash': current_hash,
                    'cli_debrid_id': saga['cli_debrid_id'],
                    'failure_reason': message,
                    'protocol': 'nzb',
                })
                try:
                    from usenet.repair_engine import _blacklist_broken_nzb
                    _blacklist_broken_nzb(
                        current_item.get('filled_by_magnet') or '',
                        segment_id=current_item.get('nzb_segment_id') or '',
                    )
                except Exception as exc:
                    logging.warning('[MountCleanup] Could not blacklist failed collected candidate: %s', exc)
                update_conn.execute(
                    """UPDATE mount_replacement_sagas SET status='probe_failed', candidate_info_hash=?,
                              last_error=?, next_attempt_at=NULL,
                              verified_info_hash=NULL, verified_entry_name=NULL,
                              verified_file_name=NULL, verified_at=NULL,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (current_hash, message[:1000], saga['id']),
                )
                update_conn.commit()
                _update_activity(saga['activity_id'], replacement_nzb_id=None,
                                 replacement_title=None, outcome='replacement_pending')
                result['probe_failed'] += 1
                continue
            if verify_status != 'healthy':
                update_conn.execute(
                    """UPDATE mount_replacement_sagas
                          SET verified_info_hash=NULL, verified_entry_name=NULL,
                              verified_file_name=NULL, verified_at=NULL,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                    (saga['id'],),
                )
                if verify_status == 'blocked':
                    update_conn.execute(
                        "UPDATE mount_replacement_sagas SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (message[:1000], saga['id']),
                    )
                    update_conn.commit()
                    if _is_stale_cleanup_saga(saga):
                        activity_conn = get_db_connection()
                        try:
                            _update_stale_cleanup_activities(
                                activity_conn, saga['id'], outcome='stale_entry_unresolved',
                            )
                            activity_conn.commit()
                        finally:
                            activity_conn.close()
                    else:
                        _update_activity(
                            saga['activity_id'], outcome='replacement_cleanup_stale',
                        )
                    result['blocked'] += 1
                else:
                    _schedule_retry_for_result(update_conn, saga, message, verify_details)
                    update_conn.commit()
                    result['retried'] += 1
                continue
        finally:
            update_conn.close()

        handoff_ready, handoff_message = _ensure_replacement_symlink(
            current_item, current_hash, all_targets, verify_details,
        )
        if not handoff_ready:
            handoff_conn = get_db_connection()
            try:
                _schedule_saga_retry(
                    handoff_conn, saga, handoff_message, delay=30,
                    increment_attempts=False,
                )
                handoff_conn.commit()
            finally:
                handoff_conn.close()
            logging.warning(
                '[MountCleanup] Healthy replacement cleanup deferred until the '
                'replacement symlink is ready: saga=%s item=%s reason=%s',
                saga['id'], saga['cli_debrid_id'], handoff_message,
            )
            result['retried'] += 1
            continue

        all_complete = _cleanup_failed_submission_jobs(saga['id'])
        if not all_complete:
            result['retried'] += 1
        blocked_target = next((row for row in all_targets if row['status'] == 'blocked'), None)
        terminal_failure = blocked_target['last_error'] if blocked_target else None
        retry_message = None
        if terminal_failure:
            all_complete = False
        for target in targets:
            registration_status, registration_message = _ensure_cleanup_registration(target)
            if registration_status != 'ready':
                all_complete = False
                retry_message = registration_message
                result['retried'] += 1
                continue
            ack_status, ack_message = _acknowledge(target)
            update_conn = get_db_connection()
            try:
                if ack_status == 'complete':
                    update_conn.execute(
                        """UPDATE mount_replacement_cleanups SET status='complete', completed_at=CURRENT_TIMESTAMP,
                                  next_attempt_at=NULL, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (target['id'],),
                    )
                    _mark_collected_attempt_cleaned(
                        update_conn, saga['id'], target['old_info_hash']
                    )
                    result['completed'] += 1
                elif ack_status == 'blocked':
                    update_conn.execute(
                        "UPDATE mount_replacement_cleanups SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (ack_message[:1000], target['id']),
                    )
                    terminal_failure = ack_message
                    result['blocked'] += 1
                    all_complete = False
                else:
                    all_complete = False
                    retry_message = ack_message
                    result['retried'] += 1
                update_conn.commit()
            finally:
                update_conn.close()

        if all_complete:
            handoff_ready, handoff_message = _ensure_replacement_symlink(
                current_item, current_hash, all_targets, verify_details,
            )
            if not handoff_ready:
                all_complete = False
                retry_message = handoff_message
                result['retried'] += 1

        if all_complete and _is_stale_cleanup_saga(saga):
            mark_conn = get_db_connection()
            try:
                mark_conn.execute(
                    """UPDATE mount_replacement_sagas
                          SET plex_refresh_status='pending', candidate_info_hash=?,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (current_hash, saga['id']),
                )
                mark_conn.commit()
            finally:
                mark_conn.close()
            _finish_stale_plex_refresh(saga, current_item, current_hash, result)
            continue

        finish_conn = get_db_connection()
        refresh_item = None
        try:
            if terminal_failure:
                finish_conn.execute(
                    "UPDATE mount_replacement_sagas SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (terminal_failure[:1000], saga['id']),
                )
                if _is_stale_cleanup_saga(saga):
                    activity_updated = _update_stale_cleanup_activities(
                        finish_conn, saga['id'], outcome='stale_entry_unresolved',
                    )
                else:
                    activity_updated = (not saga['activity_id'] or _update_activity(
                        saga['activity_id'], connection=finish_conn,
                        outcome='replacement_cleanup_stale'))
                if not activity_updated:
                    raise RuntimeError('linked replacement activity could not be finalized')
            elif all_complete:
                final_title = (current_item.get('filled_by_title') or current_item.get('filled_by_file')
                               or saga['candidate_title'] or current_item.get('title'))
                finish_conn.execute(
                    """UPDATE mount_replacement_sagas SET status='complete', candidate_info_hash=?,
                              candidate_title=?, completed_at=CURRENT_TIMESTAMP, last_error=NULL,
                              next_attempt_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (current_hash, final_title, saga['id']),
                )
                if saga['activity_id'] and not _update_activity(
                        saga['activity_id'], connection=finish_conn,
                        replacement_nzb_id=current_hash,
                        replacement_title=final_title, outcome='replaced'):
                    raise RuntimeError('linked replacement activity could not be finalized')
                refresh_item = current_item
            else:
                _schedule_retry_for_result(
                    finish_conn, saga,
                    retry_message or 'one or more exact acknowledgements are pending',
                )
            finish_conn.commit()
        except Exception as exc:
            finish_conn.rollback()
            refresh_item = None
            result['retried'] += 1
            logging.warning(
                '[MountCleanup] Replacement finalization rolled back for saga %s: %s',
                saga['id'], exc,
            )
        finally:
            finish_conn.close()
        if refresh_item is not None:
            _refresh_verified_plex_item(refresh_item)
    return result


def process_pending_mount_cleanups(item_id: int = None) -> dict:
    """Single-flight wrapper for all scheduled and post-processing callers."""
    if not _PROCESS_LOCK.acquire(blocking=False):
        logging.info('[MountCleanup] Cleanup processor already active; skipping duplicate invocation')
        return {'completed': 0, 'retried': 0, 'blocked': 0,
                'waiting': 1, 'probe_failed': 0, 'busy': 1}
    try:
        return _process_pending_mount_cleanups(item_id=item_id)
    finally:
        _PROCESS_LOCK.release()
