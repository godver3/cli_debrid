"""Persistent, file-scoped cleanup handoff from cli_debrid to cli_mount."""

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


def create_mount_replacement_cleanup_table() -> None:
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mount_replacement_cleanups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
            if protocol and (target.get('protocol') or '').lower() != protocol.lower():
                continue
            expanded.append(target)

        # Preserve every legacy reason on its existing entry-oriented path.
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


def queue_mount_replacement_cleanup(target: dict) -> bool:
    reason = target.get('failure_reason') or target.get('reason') or ''
    cli_debrid_id = int(target.get('cli_debrid_id') or 0)
    required = (
        cli_debrid_id,
        target.get('entry_name'),
        target.get('file_name'),
        target.get('info_hash'),
    )
    if reason not in PLAYBACK_CLEANUP_REASONS or not all(required):
        return False
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO mount_replacement_cleanups
               (cli_debrid_id, protocol, entry_name, file_name, old_info_hash, reason)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(cli_debrid_id, old_info_hash, file_name) DO UPDATE SET
                 protocol=excluded.protocol,
                 entry_name=excluded.entry_name,
                 reason=excluded.reason,
                 status=CASE
                   WHEN mount_replacement_cleanups.status IN ('complete', 'blocked')
                     THEN mount_replacement_cleanups.status
                   ELSE 'pending'
                 END,
                 updated_at=CURRENT_TIMESTAMP""",
            (cli_debrid_id, target.get('protocol') or '', target['entry_name'],
             target['file_name'], target['info_hash'], reason),
        )
        conn.commit()
        return True
    except Exception as exc:
        logging.error('[MountCleanup] Failed to persist cleanup target: %s', exc)
        return False
    finally:
        conn.close()


def _current_source_hash(item: dict, protocol: str) -> str:
    if protocol == 'nzb':
        value = str(item.get('filled_by_torrent_id') or '')
        return value[4:] if value.startswith('nzb:') else value
    provider_id = str(item.get('filled_by_torrent_id') or '')
    if provider_id.startswith('nzb:'):
        return provider_id
    magnet = str(item.get('filled_by_magnet') or '')
    match = re.search(r'btih:([A-Za-z0-9]+)', magnet, re.IGNORECASE)
    return match.group(1).lower() if match else ''


def media_item_source_hash(item: dict, protocol: str) -> str:
    return _current_source_hash(item, protocol)


def _schedule_retry(conn, row, message: str) -> None:
    attempts = int(row['attempts'] or 0) + 1
    delay = _RETRY_SECONDS[min(attempts - 1, len(_RETRY_SECONDS) - 1)]
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
    conn.execute(
        """UPDATE mount_replacement_cleanups
           SET attempts=?, next_attempt_at=?, last_error=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (attempts, next_attempt.strftime('%Y-%m-%d %H:%M:%S'), message[:1000], row['id']),
    )


def _acknowledge(row) -> tuple:
    from routes.api_tracker import api
    from utilities.settings import get_setting

    base_url = (get_setting('Usenet Provider', 'url', default='') or '').rstrip('/')
    token = get_setting('Usenet Provider', 'api_token', default='') or ''
    if not base_url:
        return 'retry', 'cli_mount URL is not configured'
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    payload = {
        'entry_name': row['entry_name'],
        'file_name': row['file_name'],
        'info_hash': row['old_info_hash'],
        'cli_debrid_id': row['cli_debrid_id'],
        'reason': row['reason'],
    }
    try:
        response = api.post(
            f'{base_url}/api/repair/replacements/ack', json=payload,
            headers=headers, timeout=20,
        )
    except Exception as exc:
        return 'retry', str(exc)

    try:
        body = response.json() if response.content else {}
    except Exception:
        body = {}
    if response.status_code == 200:
        return 'complete', body.get('status', 'removed')
    code = body.get('code', '') if isinstance(body, dict) else ''
    message = body.get('message') if isinstance(body, dict) else None
    message = message or f'HTTP {response.status_code}'
    if response.status_code == 404:
        return 'retry', 'cli_mount does not support replacement acknowledgement yet'
    if response.status_code == 409 and code == 'repair_busy':
        return 'retry', message
    if response.status_code >= 500:
        return 'retry', message
    return 'blocked', f'{code or response.status_code}: {message}'


def process_pending_mount_cleanups(item_id: int = None) -> dict:
    """Acknowledge due cleanups whose replacement has reached Collected."""
    result = {'completed': 0, 'retried': 0, 'blocked': 0, 'waiting': 0}
    conn = get_db_connection()
    sql = """SELECT c.*, m.state, m.filled_by_torrent_id, m.filled_by_magnet,
                    m.title, m.type, m.season_number, m.episode_number
             FROM mount_replacement_cleanups c
             LEFT JOIN media_items m ON m.id = c.cli_debrid_id
             WHERE c.status='pending'
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
        update_conn = get_db_connection()
        try:
            if status == 'complete':
                update_conn.execute(
                    """UPDATE mount_replacement_cleanups
                       SET status='complete', completed_at=CURRENT_TIMESTAMP,
                           next_attempt_at=NULL, last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (row['id'],),
                )
                result['completed'] += 1
                logging.info('[MountCleanup] Removed replaced old file %r/%r', row['entry_name'], row['file_name'])
                update_conn.commit()
                _log_cleanup_activity(row, 'replacement_cleanup_complete')
            elif status == 'blocked':
                update_conn.execute(
                    """UPDATE mount_replacement_cleanups
                       SET status='blocked', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (message[:1000], row['id']),
                )
                result['blocked'] += 1
                logging.error('[MountCleanup] Cleanup blocked for item %s: %s', row['cli_debrid_id'], message)
                update_conn.commit()
                _log_cleanup_activity(row, 'replacement_cleanup_stale')
            else:
                _schedule_retry(update_conn, row, message)
                result['retried'] += 1
                logging.warning('[MountCleanup] Cleanup deferred for item %s: %s', row['cli_debrid_id'], message)
            update_conn.commit()
        finally:
            update_conn.close()
    return result


def _log_cleanup_activity(row, outcome: str) -> None:
    try:
        from database.nzb_repair_activity import log_repair_activity
        broken_source_id = row['old_info_hash']
        if (row['protocol'] or '').lower() == 'torrent':
            broken_source_id = f'debrid:{broken_source_id}'
        log_repair_activity(
            item_id=row['cli_debrid_id'], title=row['title'], media_type=row['type'],
            season_number=row['season_number'], episode_number=row['episode_number'],
            broken_nzb_id=broken_source_id, broken_nzb_title=row['entry_name'],
            outcome=outcome, triggered_by='mount_replacement_cleanup',
        )
    except Exception as exc:
        logging.debug('[MountCleanup] Could not log cleanup activity: %s', exc)
