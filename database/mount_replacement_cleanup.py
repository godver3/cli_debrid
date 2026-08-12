"""Durable playback-verification and exact mount-cleanup saga."""

import logging
import re
from datetime import datetime, timedelta, timezone

from database.core import get_db_connection


PLAYBACK_CLEANUP_REASONS = frozenset({
    'mount_read_error',
    'media_probe_failed',
    'media_no_playable_stream',
})

_RETRY_SECONDS = (60, 300, 1800, 7200, 86400)
def _add_column(conn, table: str, definition: str) -> None:
    try:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
    except Exception:
        pass


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
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_mount_cleanup_saga '
            'ON mount_replacement_cleanups(saga_id)'
        )
        # Adopt pending rows created by the pre-verification cleanup handoff.
        # Completed/blocked history is left untouched.
        orphan_items = conn.execute(
            """SELECT DISTINCT cli_debrid_id, protocol FROM mount_replacement_cleanups
               WHERE status='pending' AND saga_id IS NULL"""
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
        conn.commit()
    finally:
        conn.close()


def split_playback_cleanup_targets(entries: list, protocol: str = '') -> list:
    """Expand only playback-probe failures to exact, per-file repair targets."""
    expanded = []
    for entry in entries or []:
        broken_files = entry.get('broken_files') or []
        eligible = [f for f in broken_files if (f.get('reason') or '') in PLAYBACK_CLEANUP_REASONS]
        ineligible = [f for f in broken_files if (f.get('reason') or '') not in PLAYBACK_CLEANUP_REASONS]
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
        if ineligible or not eligible:
            legacy = dict(entry)
            if eligible:
                legacy['broken_files'] = ineligible
                if ineligible:
                    legacy['failure_reason'] = ineligible[0].get('reason') or legacy.get('failure_reason', '')
            expanded.append(legacy)
    return expanded


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


def queue_mount_replacement_cleanup(target: dict) -> bool:
    """Attach an exact old mounted file to the item's one active saga."""
    reason = target.get('failure_reason') or target.get('reason') or ''
    cli_debrid_id = int(target.get('cli_debrid_id') or 0)
    if reason not in PLAYBACK_CLEANUP_REASONS or not all((
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
                (cli_debrid_id, target.get('protocol') or '', activity_id),
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


def _schedule_saga_retry(conn, saga, message: str) -> None:
    attempts = int(saga['attempts'] or 0) + 1
    delay = _RETRY_SECONDS[min(attempts - 1, len(_RETRY_SECONDS) - 1)]
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
    conn.execute(
        """UPDATE mount_replacement_sagas SET status='pending', attempts=?, next_attempt_at=?,
                  last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (attempts, next_attempt.strftime('%Y-%m-%d %H:%M:%S'), message[:1000], saga['id']),
    )


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
        return None, {}, str(exc)


def _verify_replacement(cli_debrid_id: int, info_hash: str) -> tuple:
    response, body, error = _mount_request(
        '/api/repair/replacements/verify',
        {'cli_debrid_id': cli_debrid_id, 'info_hash': info_hash},
        75,
    )
    if error:
        return 'retry', error
    if response.status_code == 200:
        status = body.get('status') if isinstance(body, dict) else None
        if status in ('healthy', 'broken', 'unknown'):
            return status, body.get('reason', '')
        return 'retry', 'invalid verification response'
    code = body.get('code', '') if isinstance(body, dict) else ''
    message = (body.get('message') if isinstance(body, dict) else None) or f'HTTP {response.status_code}'
    if response.status_code == 404:
        return 'retry', 'cli_mount does not support replacement verification yet'
    if response.status_code >= 500 or code in ('repair_busy', 'replacement_not_ready'):
        return 'retry', message
    return 'blocked', f'{code or response.status_code}: {message}'


def _acknowledge(row) -> tuple:
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
        return 'complete', body.get('status')
    code = body.get('code', '') if isinstance(body, dict) else ''
    message = (body.get('message') if isinstance(body, dict) else None) or f'HTTP {response.status_code}'
    if response.status_code == 404:
        return 'retry', 'cli_mount does not support replacement acknowledgement yet'
    if response.status_code >= 500 or code == 'repair_busy':
        return 'retry', message
    return 'blocked', f'{code or response.status_code}: {message}'


def _update_activity(activity_id: int, **changes) -> None:
    try:
        from database.nzb_repair_activity import update_repair_activity
        update_repair_activity(activity_id, **changes)
    except Exception as exc:
        logging.debug('[MountCleanup] Could not update activity: %s', exc)


def process_pending_mount_cleanups(item_id: int = None) -> dict:
    """Verify collected candidates, then acknowledge every exact old file."""
    result = {'completed': 0, 'retried': 0, 'blocked': 0, 'waiting': 0, 'probe_failed': 0}
    conn = get_db_connection()
    sql = """SELECT s.*, m.state, m.filled_by_torrent_id, m.filled_by_magnet,
                    m.title, m.type, m.season_number, m.episode_number
             FROM mount_replacement_sagas s
             LEFT JOIN media_items m ON m.id=s.cli_debrid_id
             WHERE s.status IN ('pending','probe_failed')
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
        if saga['state'] != 'Collected':
            result['waiting'] += 1
            continue
        conn = get_db_connection()
        try:
            targets = conn.execute(
                "SELECT * FROM mount_replacement_cleanups WHERE saga_id=? AND status='pending' ORDER BY id",
                (saga['id'],),
            ).fetchall()
        finally:
            conn.close()
        if not targets:
            result['waiting'] += 1
            continue
        current_hash = _current_source_hash(saga_dict, (saga['protocol'] or '').lower())
        old_hashes = {(row['old_info_hash'] or '').lower() for row in targets}
        if not current_hash or current_hash.lower() in old_hashes:
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
            _update_activity(saga['activity_id'], replacement_nzb_id=current_hash,
                             replacement_title=candidate_title, outcome='replacement_pending')

        verify_status, message = _verify_replacement(saga['cli_debrid_id'], current_hash)
        # A collection/update may have won the race while cli_mount was probing.
        current_item = get_media_item_for_cleanup(saga['cli_debrid_id']) or {}
        if current_item.get('state') != 'Collected' or _current_source_hash(current_item, (saga['protocol'] or '').lower()).lower() != current_hash.lower():
            result['waiting'] += 1
            continue

        update_conn = get_db_connection()
        try:
            if verify_status == 'broken':
                update_conn.execute(
                    """UPDATE mount_replacement_sagas SET status='probe_failed', candidate_info_hash=?,
                              last_error=?, next_attempt_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (current_hash, message[:1000], saga['id']),
                )
                update_conn.commit()
                _update_activity(saga['activity_id'], replacement_nzb_id=current_hash,
                                 replacement_title=saga['candidate_title'], outcome='replacement_probe_failed')
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
                    _schedule_saga_retry(update_conn, saga, message)
                    update_conn.commit()
                    result['retried'] += 1
                continue
        finally:
            update_conn.close()

        all_complete = True
        terminal_failure = None
        for target in targets:
            ack_status, ack_message = _acknowledge(target)
            update_conn = get_db_connection()
            try:
                if ack_status == 'complete':
                    update_conn.execute(
                        """UPDATE mount_replacement_cleanups SET status='complete', completed_at=CURRENT_TIMESTAMP,
                                  next_attempt_at=NULL, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (target['id'],),
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
                    result['retried'] += 1
                update_conn.commit()
            finally:
                update_conn.close()

        finish_conn = get_db_connection()
        try:
            if terminal_failure:
                finish_conn.execute(
                    "UPDATE mount_replacement_sagas SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (terminal_failure[:1000], saga['id']),
                )
                _update_activity(saga['activity_id'], outcome='replacement_cleanup_stale')
            elif all_complete:
                final_title = (current_item.get('filled_by_title') or current_item.get('filled_by_file')
                               or saga['candidate_title'] or current_item.get('title'))
                finish_conn.execute(
                    """UPDATE mount_replacement_sagas SET status='complete', candidate_info_hash=?,
                              candidate_title=?, completed_at=CURRENT_TIMESTAMP, last_error=NULL,
                              next_attempt_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (current_hash, final_title, saga['id']),
                )
                _update_activity(saga['activity_id'], replacement_nzb_id=current_hash,
                                 replacement_title=final_title, outcome='replaced')
            else:
                _schedule_saga_retry(finish_conn, saga, 'one or more exact acknowledgements are pending')
            finish_conn.commit()
        finally:
            finish_conn.close()
    return result
