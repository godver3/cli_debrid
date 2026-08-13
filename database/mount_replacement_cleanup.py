"""Durable playback-verification and exact mount-cleanup saga."""

import logging
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

_RETRY_SECONDS = (60, 300, 1800)
_LEGACY_RETRY_SECONDS = (60, 300, 1800, 7200, 86400)
_PROCESS_LOCK = threading.Lock()
_VIDEO_EXTENSIONS = frozenset({'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'})
_REGISTRATION_STALE_ERRORS = frozenset({
    'stale_target: cli_debrid_id does not match the mounted file registration',
    'stale_target: cli_debrid_id is registered to a different current provider source',
})


def _add_column(conn, table: str, definition: str) -> None:
    try:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
    except Exception:
        pass


def _requeue_registration_stale_sagas(conn) -> int:
    """Recover only the registration conflicts created by saga-aware sync.

    These conflicts do not mean the replacement or cleanup target is stale. They
    mean cli_mount received a filename from the media row belonging to the other
    side of the replacement. Re-probing is safe after exact registration is
    restored because no acknowledgement occurs until the probe is healthy.
    """
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
                    "DELETE FROM nzb_repair_activity WHERE id=? AND outcome='replacement_cleanup_stale'",
                    (duplicate['activity_id'],),
                )
            conn.execute('DELETE FROM mount_replacement_sagas WHERE id=?', (duplicate['id'],))

        conn.execute(
            """UPDATE mount_replacement_cleanups
               SET status='pending', attempts=0, next_attempt_at=NULL,
                   last_error=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE saga_id=? AND status='blocked' AND last_error IN (?, ?)""",
            (keeper['id'], *_REGISTRATION_STALE_ERRORS),
        )
        if keeper['status'] == 'blocked':
            conn.execute(
                """UPDATE mount_replacement_sagas
                   SET status='pending', attempts=0, next_attempt_at=NULL,
                       last_error=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (keeper['id'],),
            )
        if activity_table and keeper['activity_id']:
            conn.execute(
                """UPDATE nzb_repair_activity
                   SET replacement_nzb_id=NULL, replacement_title=NULL,
                       outcome='replacement_pending', updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND outcome='replacement_cleanup_stale'""",
                (keeper['activity_id'],),
            )
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
        _add_column(conn, 'mount_replacement_sagas', 'last_reconciled_at TIMESTAMP')
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
                '[MountCleanup] Requeued %s replacement saga(s) after exact-registration repair',
                recovered,
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
            broken_nzb_title=target.get('entry_name'), outcome='replacement_pending',
            triggered_by='mount_replacement_verification',
        )
    except Exception as exc:
        logging.debug('[MountCleanup] Could not create pending activity: %s', exc)
        return None


def _normalize_release_title(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower())


def record_mount_replacement_attempt(cli_debrid_id: int, *, job_id: str = '',
                                     title: str = '', segment_id: str = '',
                                     nzb_guid: str = '', status: str,
                                     reason: str = '') -> bool:
    """Persist a rejected/probe-failed NZB without exposing it as success."""
    normalized = _normalize_release_title(title)
    if not cli_debrid_id or not normalized or status not in ('provisional', 'failed_submission', 'failed_collected'):
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
                release_title, normalized_title, status, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cli_debrid_id, normalized_title) DO UPDATE SET
                 saga_id=excluded.saga_id,
                 job_id=COALESCE(excluded.job_id, mount_replacement_attempts.job_id),
                 segment_id=COALESCE(excluded.segment_id, mount_replacement_attempts.segment_id),
                 nzb_guid=COALESCE(excluded.nzb_guid, mount_replacement_attempts.nzb_guid),
                 release_title=COALESCE(excluded.release_title, mount_replacement_attempts.release_title),
                 status=excluded.status, reason=excluded.reason,
                 updated_at=CURRENT_TIMESTAMP""",
            (saga['id'], cli_debrid_id, job_id or None, segment_id or None,
             nzb_guid or None, title or None, normalized, status, reason or None),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_attempted_candidate_keys(cli_debrid_id: int) -> dict:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT job_id, segment_id, nzb_guid, normalized_title
               FROM mount_replacement_attempts WHERE cli_debrid_id=?""",
            (cli_debrid_id,),
        ).fetchall()
        return {
            'job_ids': {r['job_id'] for r in rows if r['job_id']},
            'segment_ids': {r['segment_id'] for r in rows if r['segment_id']},
            'guids': {r['nzb_guid'] for r in rows if r['nzb_guid']},
            'titles': {r['normalized_title'] for r in rows if r['normalized_title']},
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
                   next_attempt_at=NULL, last_error=NULL, updated_at=CURRENT_TIMESTAMP
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
    elif code == 'replacement_not_ready':
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
        if str(lookup.get('status') or '') != 'ready':
            return False
        entry = lookup.get('entry') or {}
        candidate_file = _select_candidate_file(item or {}, entry)
        if not candidate_file:
            return False
        return bool(client.register_cli_ids(
            info_hash,
            _registration_map_for_source(
                info_hash, candidate_file, cli_debrid_id, entry,
            ),
        ))
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
        if status == 'missing':
            # The acknowledgement endpoint will return already_removed if the
            # mounted item disappeared with the provider entry.
            return 'ready', ''
        entry = lookup.get('entry') or {}
        if status in ('unavailable', 'ambiguous') or not entry:
            return 'retry', f'replacement_not_ready: cleanup target {status}'
        ids = _registration_map_for_source(
            target['old_info_hash'], target['file_name'],
            int(target['cli_debrid_id']), entry,
        )
        if not client.register_cli_ids(target['old_info_hash'], ids):
            return 'retry', 'replacement_not_ready: exact cleanup registration failed'
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
        pattern = re.compile(
            rf'(?i)(?:^|[^a-z0-9])s0*{int(season)}e0*{int(episode)}(?![0-9])'
        )
        matches = [value for value in files if pattern.search(value['name'])]
        if len(matches) == 1:
            return matches[0]['name']
        return files[0]['name'] if len(files) == 1 else ''
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


def _refresh_verified_plex_item(item: dict) -> None:
    """Refresh the retained Plex path without deleting its metadata item."""
    path = item.get('location_on_disk') or ''
    if not path:
        logging.info('[MountCleanup] Plex refresh skipped for item %s: no location_on_disk', item.get('id'))
        return
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
    except Exception as exc:
        logging.warning('[MountCleanup] Targeted Plex refresh failed for item %s: %s', item.get('id'), exc)


def _finish_torrent_supersession(saga, item: dict, all_targets: list,
                                 result: dict) -> None:
    """Remove exact failed NZB files after a torrent has reached Collected."""
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
            if _close_terminal_saga(
                    saga, status='abandoned', outcome='replacement_abandoned',
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
        if saga['status'] == 'probe_failed' and current_hash.lower() == str(saga['candidate_info_hash'] or '').lower():
            result['waiting'] += 1
            continue

        if current_hash.lower() != str(saga['candidate_info_hash'] or '').lower():
            candidate_item = get_media_item_for_cleanup(saga['cli_debrid_id']) or {}
            candidate_title = (candidate_item.get('filled_by_title') or candidate_item.get('filled_by_file')
                               or saga['candidate_title'])
            set_mount_replacement_candidate(saga['cli_debrid_id'], current_hash, candidate_title)
            # Candidate details stay private until verification and cleanup
            # both succeed; the activity row remains a neutral pending record.

        if not _ensure_candidate_registration(
                saga['cli_debrid_id'], current_hash, saga_dict):
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

        verify_status, message, verify_details = _verify_replacement(saga['cli_debrid_id'], current_hash)
        # A collection/update may have won the race while cli_mount was probing.
        current_item = get_media_item_for_cleanup(saga['cli_debrid_id']) or {}
        if current_item.get('state') != 'Collected' or _current_source_hash(current_item, (saga['protocol'] or '').lower()).lower() != current_hash.lower():
            result['waiting'] += 1
            continue

        update_conn = get_db_connection()
        try:
            if verify_status == 'broken':
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
                              last_error=?, next_attempt_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (current_hash, message[:1000], saga['id']),
                )
                update_conn.commit()
                _update_activity(saga['activity_id'], replacement_nzb_id=None,
                                 replacement_title=None, outcome='replacement_pending')
                result['probe_failed'] += 1
                continue
            if verify_status != 'healthy':
                if verify_status == 'blocked':
                    update_conn.execute(
                        "UPDATE mount_replacement_sagas SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (message[:1000], saga['id']),
                    )
                    update_conn.commit()
                    _update_activity(saga['activity_id'], outcome='replacement_cleanup_stale')
                    result['blocked'] += 1
                else:
                    _schedule_retry_for_result(update_conn, saga, message, verify_details)
                    update_conn.commit()
                    result['retried'] += 1
                continue
        finally:
            update_conn.close()

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

        finish_conn = get_db_connection()
        refresh_item = None
        try:
            if terminal_failure:
                finish_conn.execute(
                    "UPDATE mount_replacement_sagas SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (terminal_failure[:1000], saga['id']),
                )
                if saga['activity_id'] and not _update_activity(
                        saga['activity_id'], connection=finish_conn,
                        outcome='replacement_cleanup_stale'):
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
