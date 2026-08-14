"""Durable, minimal state for NZB playback-failure replacement.

This module deliberately owns one table.  It does not depend on the older
experimental replacement-saga tables and never starts a new library repair.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from database.core import get_db_connection, retry_on_db_lock

log = logging.getLogger(__name__)
_worker_lock = threading.Lock()
ELIGIBLE_REASONS = {
    'usenet_segment_missing', 'media_probe_failed', 'media_no_playable_stream',
}
# A stale_target ack response is only transient if the mount hasn't caught up
# yet with a supersession decypharr already applied. If it's still stale
# after this many exact-cleanup attempts, further retries won't help (the
# target genuinely changed), so stop retrying and finish the repair without
# the old-file cleanup rather than looping forever.
STALE_TARGET_MAX_ATTEMPTS = 5
# A failed-job delete can be legitimately transient (mount briefly
# unreachable), but a job that's simply gone from decypharr will never
# succeed no matter how many times it's retried. Cap retries so a stale
# job ID doesn't block the repair's own old-file cleanup forever.
FAILED_JOB_MAX_ATTEMPTS = 5


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _json(value, default):
    try:
        parsed = json.loads(value or '')
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def normalize_release(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower())


def candidate_keys(result):
    title = result.get('original_title') or result.get('title') or ''
    url = result.get('nzb_url') or result.get('magnet') or ''
    guid = str(result.get('guid') or result.get('nzb_guid') or '').strip().lower()
    match = re.search(r'(?:id=|/)([0-9a-f]{20,})(?:[.&/?]|$)', url, re.I)
    if match:
        guid = match.group(1).lower()
    segment = str(result.get('segment_id') or result.get('nzb_segment_id') or '').strip()
    content = result.get('_prefetched_nzb') or ''
    if content:
        try:
            from database.not_wanted_magnets import extract_nzb_segment_id
            segment = segment or extract_nzb_segment_id(content) or ''
        except Exception:
            pass
    return {key for key in (f't:{normalize_release(title)}' if title else '',
                            f'g:{guid}' if guid else '',
                            f's:{segment}' if segment else '') if key}


def create_nzb_playback_repair_table():
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS nzb_playback_repairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cli_debrid_id INTEGER NOT NULL,
                old_info_hash TEXT NOT NULL,
                old_entry_name TEXT NOT NULL,
                old_file_name TEXT NOT NULL,
                old_reason TEXT NOT NULL,
                old_guid TEXT,
                old_segment TEXT,
                old_normalized_title TEXT,
                excluded_keys_json TEXT NOT NULL DEFAULT '[]',
                cleanup_targets_json TEXT NOT NULL DEFAULT '[]',
                failed_job_ids_json TEXT NOT NULL DEFAULT '[]',
                failed_job_attempts_json TEXT NOT NULL DEFAULT '{}',
                candidate_info_hash TEXT,
                candidate_title TEXT,
                candidate_guid TEXT,
                candidate_segment TEXT,
                candidate_normalized_title TEXT,
                candidate_keys_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'awaiting_candidate',
                activity_id INTEGER,
                next_attempt_at TIMESTAMP,
                lease_owner TEXT,
                lease_until TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nzb_playback_active_item
              ON nzb_playback_repairs(cli_debrid_id)
              WHERE status != 'complete';
            CREATE INDEX IF NOT EXISTS idx_nzb_playback_due
              ON nzb_playback_repairs(status, next_attempt_at);
        """)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(nzb_playback_repairs)')}
        if 'lease_owner' not in columns:
            conn.execute('ALTER TABLE nzb_playback_repairs ADD COLUMN lease_owner TEXT')
        if 'lease_until' not in columns:
            conn.execute('ALTER TABLE nzb_playback_repairs ADD COLUMN lease_until TIMESTAMP')
        if 'failed_job_attempts_json' not in columns:
            conn.execute(
                "ALTER TABLE nzb_playback_repairs ADD COLUMN failed_job_attempts_json TEXT NOT NULL DEFAULT '{}'"
            )
        for column in ('old_guid', 'old_segment', 'old_normalized_title',
                       'candidate_guid', 'candidate_segment', 'candidate_normalized_title'):
            if column not in columns:
                conn.execute(f'ALTER TABLE nzb_playback_repairs ADD COLUMN {column} TEXT')
        conn.commit()
    finally:
        conn.close()


def _target(entry):
    return {
        'entry_name': entry['entry_name'], 'file_name': entry['file_name'],
        'info_hash': entry['info_hash'], 'cli_debrid_id': int(entry['cli_debrid_id']),
        'reason': entry['reason'], 'status': 'pending',
    }


@retry_on_db_lock(max_attempts=4, initial_wait=0.1, backoff_factor=2)
def begin_playback_repair(item, entry, triggered_by='manual'):
    """Create repair + activity atomically before any candidate is submitted."""
    if entry.get('reason') not in ELIGIBLE_REASONS:
        return None
    item_id = int(item['id'])
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            "SELECT * FROM nzb_playback_repairs WHERE cli_debrid_id=? AND status!='complete'",
            (item_id,),
        ).fetchone()
        if existing:
            row = dict(existing)
            targets = _json(row['cleanup_targets_json'], [])
            exact = _target(entry)
            if not any(t.get('info_hash') == exact['info_hash'] and
                       t.get('file_name') == exact['file_name'] for t in targets):
                targets.append(exact)
            excluded = set(_json(row['excluded_keys_json'], []))
            excluded.add(f"t:{normalize_release(entry.get('entry_name'))}")
            conn.execute(
                "UPDATE nzb_playback_repairs SET cleanup_targets_json=?, excluded_keys_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(targets), json.dumps(sorted(excluded)), row['id']),
            )
            conn.commit()
            return row['id']

        cursor = conn.execute(
            """INSERT INTO nzb_repair_activity
               (item_id,title,media_type,season_number,episode_number,broken_nzb_id,
                broken_nzb_title,outcome,triggered_by,repair_attempts,last_repair_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, item.get('title'), item.get('type'), item.get('season_number'),
             item.get('episode_number'), entry['info_hash'], entry['entry_name'],
             'replacement_pending', triggered_by, 0, _now()),
        )
        activity_id = cursor.lastrowid
        excluded = {f"t:{normalize_release(entry['entry_name'])}"}
        original_release = (item.get('original_scraped_torrent_title') or
                            item.get('filled_by_title') or item.get('debrid_folder_name') or '')
        if original_release:
            excluded.add(f"t:{normalize_release(original_release)}")
        if entry.get('segment_id'):
            excluded.add(f"s:{entry['segment_id']}")
        cursor = conn.execute(
            """INSERT INTO nzb_playback_repairs
               (cli_debrid_id,old_info_hash,old_entry_name,old_file_name,old_reason,
                old_guid,old_segment,old_normalized_title,excluded_keys_json,
                cleanup_targets_json,activity_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, entry['info_hash'], entry['entry_name'], entry['file_name'],
             entry['reason'], item.get('filled_by_magnet') or '', entry.get('segment_id') or '',
             normalize_release(original_release or entry['entry_name']), json.dumps(sorted(excluded)),
             json.dumps([_target(entry)]), activity_id),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if 'database is locked' in str(exc).lower():
            raise
        log.exception('[NZBPlayback] Could not persist repair before submission')
        return None
    except Exception:
        conn.rollback()
        log.exception('[NZBPlayback] Could not persist repair before submission')
        return None
    finally:
        conn.close()


def candidate_is_excluded(item_id, result):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT excluded_keys_json FROM nzb_playback_repairs WHERE cli_debrid_id=? AND status!='complete'",
            (item_id,),
        ).fetchone()
        return bool(row and candidate_keys(result) & set(_json(row[0], [])))
    finally:
        conn.close()


def has_active_exact_repair(item_id, old_info_hash, old_file_name):
    """Return whether the exact old mounted file already has an active repair."""
    if not item_id or not old_info_hash or not old_file_name:
        return False
    conn = get_db_connection()
    try:
        row = conn.execute(
            """SELECT 1 FROM nzb_playback_repairs
               WHERE cli_debrid_id=? AND old_info_hash=? AND old_file_name=?
                 AND status!='complete'
               LIMIT 1""",
            (int(item_id), old_info_hash, old_file_name),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


@retry_on_db_lock(max_attempts=4, initial_wait=0.1, backoff_factor=2)
def record_failed_candidate(item_id, result, job_id=''):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id,excluded_keys_json,failed_job_ids_json FROM nzb_playback_repairs WHERE cli_debrid_id=? AND status!='complete'",
            (item_id,),
        ).fetchone()
        if not row:
            return
        excluded = set(_json(row[1], [])) | candidate_keys(result)
        jobs = set(_json(row[2], []))
        if job_id:
            jobs.add(job_id)
        conn.execute(
            "UPDATE nzb_playback_repairs SET excluded_keys_json=?,failed_job_ids_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(sorted(excluded)), json.dumps(sorted(jobs)), row[0]),
        )
        conn.commit()
    finally:
        conn.close()


@retry_on_db_lock(max_attempts=4, initial_wait=0.1, backoff_factor=2)
def set_playback_candidate(item_id, result, job_id, title):
    keys = candidate_keys(result)
    guid = next((key[2:] for key in keys if key.startswith('g:')), '')
    segment = next((key[2:] for key in keys if key.startswith('s:')), '')
    normalized = normalize_release(result.get('original_title') or result.get('title') or title)
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """UPDATE nzb_playback_repairs
               SET candidate_info_hash=?,candidate_title=?,candidate_keys_json=?,
                   candidate_guid=?,candidate_segment=?,candidate_normalized_title=?,
                   status='awaiting_collection',next_attempt_at=NULL,last_error=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE cli_debrid_id=? AND status!='complete'""",
            (job_id, title, json.dumps(sorted(keys)), guid, segment, normalized, item_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


@retry_on_db_lock(max_attempts=4, initial_wait=0.1, backoff_factor=2)
def reject_active_candidate(item_id, job_id, reason='candidate_failed'):
    """Return a failed provisional replacement to its exact original source."""
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            "SELECT * FROM nzb_playback_repairs WHERE cli_debrid_id=? AND status!='complete'",
            (item_id,),
        ).fetchone()
        if not row or (row['candidate_info_hash'] or '').lower() != (job_id or '').lower():
            conn.rollback()
            return False
        excluded = set(_json(row['excluded_keys_json'], [])) | set(_json(row['candidate_keys_json'], []))
        failed_jobs = set(_json(row['failed_job_ids_json'], []))
        if job_id:
            failed_jobs.add(job_id)
        conn.execute(
            """UPDATE nzb_playback_repairs SET status='awaiting_candidate',candidate_info_hash=NULL,
                      candidate_title=NULL,candidate_guid=NULL,candidate_segment=NULL,
                      candidate_normalized_title=NULL,excluded_keys_json=?,failed_job_ids_json=?,last_error=?,
                      next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (json.dumps(sorted(excluded)), json.dumps(sorted(failed_jobs)), reason, row['id']),
        )
        conn.execute(
            """UPDATE media_items SET state='Collected',filled_by_torrent_id=?,filled_by_file=?,
                      filled_by_title=?,debrid_folder_name=? WHERE id=? AND filled_by_torrent_id=?""",
            (f"nzb:{row['old_info_hash']}", row['old_file_name'], row['old_entry_name'],
             row['old_entry_name'], item_id, f'nzb:{job_id}'),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mount_request(path, payload, timeout):
    import requests
    from utilities.settings import get_setting
    base = get_setting('Usenet Provider', 'url', '').rstrip('/')
    token = get_setting('Usenet Provider', 'api_token', '')
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    try:
        response = requests.post(base + path, json=payload, headers=headers, timeout=timeout)
        try:
            body = response.json()
        except Exception:
            body = {}
        return response.status_code, body, ''
    except Exception as exc:
        return 0, {}, str(exc)


@retry_on_db_lock(max_attempts=4, initial_wait=0.1, backoff_factor=2)
def _schedule(repair_id, message, seconds):
    conn = get_db_connection()
    try:
        when = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "UPDATE nzb_playback_repairs SET next_attempt_at=?,last_error=?,lease_owner=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (when, str(message)[:1000], repair_id),
        )
        conn.commit()
    finally:
        conn.close()


def _media_item(item_id):
    from database.database_reading import get_media_item_by_id
    row = get_media_item_by_id(item_id)
    return dict(row) if row else None


def _source_uuid(item):
    value = str(item.get('filled_by_torrent_id') or '')
    return value[4:] if value.startswith('nzb:') else value


def _symlink_matches(item, entry_name, file_name):
    link = item.get('location_on_disk') or ''
    if not link or not os.path.islink(link):
        return False
    target = os.path.normpath(os.path.realpath(link))
    return os.path.basename(target) == file_name and os.path.basename(os.path.dirname(target)) == entry_name


def _finish_activity(repair, candidate_uuid, title):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            """UPDATE nzb_repair_activity SET replacement_nzb_id=?,replacement_title=?,
                      outcome='replaced',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (candidate_uuid, title, repair['activity_id']),
        )
        conn.execute(
            """UPDATE nzb_playback_repairs SET status='complete',completed_at=CURRENT_TIMESTAMP,
                      next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,last_error=NULL,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (repair['id'],),
        )
        # A health scan may have encountered the retained old file after its
        # media row switched to the provisional candidate. Remove only those
        # duplicate reports that identify this exact original provider entry.
        conn.execute(
            """DELETE FROM nzb_repair_activity
               WHERE id!=? AND outcome='not_found'
                 AND broken_nzb_id=? AND broken_nzb_title=?""",
            (repair['activity_id'], repair['old_info_hash'], repair['old_entry_name']),
        )
        conn.commit()
        log.info(
            '[NZBPlayback] Repair finalized item=%s candidate=%s activity=%s',
            repair['cli_debrid_id'], candidate_uuid, repair['activity_id'],
        )
        return True
    except Exception:
        conn.rollback()
        log.exception('[NZBPlayback] Final activity transaction failed for repair %s', repair['id'])
        return False
    finally:
        conn.close()


def process_pending_playback_repairs():
    """Complete only already-started repairs; never scans or starts the backlog."""
    if not _worker_lock.acquire(blocking=False):
        log.debug('[NZBPlayback] Completion worker already active; skipping overlapping pass')
        return
    try:
        lease_owner = uuid.uuid4().hex
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """SELECT * FROM nzb_playback_repairs
                   WHERE status IN ('awaiting_collection','awaiting_verification','verified','cleanup_complete')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP)
                     AND (lease_until IS NULL OR lease_until<=CURRENT_TIMESTAMP)
                   ORDER BY updated_at LIMIT 12"""
            ).fetchall()
        finally:
            conn.close()
        for raw in rows:
            repair = dict(raw)
            conn = get_db_connection()
            try:
                claimed = conn.execute(
                    """UPDATE nzb_playback_repairs SET lease_owner=?,lease_until=datetime('now','+10 minutes')
                       WHERE id=? AND status IN ('awaiting_collection','awaiting_verification','verified','cleanup_complete')
                         AND (lease_until IS NULL OR lease_until<=CURRENT_TIMESTAMP)""",
                    (lease_owner, repair['id']),
                ).rowcount
                conn.commit()
            finally:
                conn.close()
            if not claimed:
                continue
            log.info(
                '[NZBPlayback] Claimed repair=%s item=%s stage=%s candidate=%s',
                repair['id'], repair['cli_debrid_id'], repair['status'],
                repair.get('candidate_info_hash') or '',
            )
            item = _media_item(repair['cli_debrid_id'])
            if not item or item.get('state') != 'Collected':
                log.debug(
                    '[NZBPlayback] Waiting for collection repair=%s item=%s state=%s',
                    repair['id'], repair['cli_debrid_id'],
                    item.get('state') if item else 'missing',
                )
                _schedule(repair['id'], 'awaiting_collection', 15)
                continue
            candidate = repair.get('candidate_info_hash') or ''
            if not candidate or _source_uuid(item).lower() != candidate.lower():
                _schedule(repair['id'], 'candidate_source_changed', 30)
                continue
            client = __import__('usenet', fromlist=['get_usenet_client']).get_usenet_client()
            if not client.register_cli_ids_for_item(candidate, repair['cli_debrid_id']):
                _schedule(repair['id'], 'replacement_not_ready', 30)
                continue

            if repair['status'] not in ('verified', 'cleanup_complete'):
                log.info(
                    '[NZBPlayback] Requesting replacement verification repair=%s item=%s candidate=%s',
                    repair['id'], repair['cli_debrid_id'], candidate,
                )
                code, body, error = _mount_request(
                    '/api/repair/replacements/verify',
                    {'cli_debrid_id': repair['cli_debrid_id'], 'info_hash': candidate}, 75)
                log.info(
                    '[NZBPlayback] Replacement verification result repair=%s status=%s reason=%s http=%s error=%s',
                    repair['id'], body.get('status') or '', body.get('reason') or body.get('code') or '',
                    code, error or '',
                )
                if error or code >= 500:
                    _schedule(repair['id'], error or f'HTTP {code}', 60)
                    continue
                if code == 409:
                    _schedule(repair['id'], body.get('code') or 'repair_busy', 15)
                    continue
                status = body.get('status')
                if status == 'unknown':
                    _schedule(repair['id'], body.get('reason') or 'verification_unknown', 30)
                    continue
                if status == 'broken':
                    failed = {
                        'entry_name': body.get('entry_name'), 'file_name': body.get('file_name'),
                        'info_hash': candidate, 'cli_debrid_id': repair['cli_debrid_id'],
                        'reason': body.get('reason'), 'status': 'pending',
                    }
                    targets = _json(repair['cleanup_targets_json'], [])
                    targets.append(failed)
                    excluded = set(_json(repair['excluded_keys_json'], [])) | set(_json(repair['candidate_keys_json'], []))
                    conn = get_db_connection()
                    try:
                        conn.execute(
                            """UPDATE nzb_playback_repairs SET status='awaiting_candidate',
                               cleanup_targets_json=?,excluded_keys_json=?,candidate_info_hash=NULL,
                               candidate_title=NULL,candidate_guid=NULL,candidate_segment=NULL,
                               candidate_normalized_title=NULL,next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,
                               last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (json.dumps(targets), json.dumps(sorted(excluded)), body.get('reason'), repair['id']),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    continue
                if status != 'healthy' or not body.get('entry_name') or not body.get('file_name'):
                    _schedule(repair['id'], 'invalid_verification_response', 60)
                    continue
                if not _symlink_matches(item, body['entry_name'], body['file_name']):
                    _schedule(repair['id'], 'replacement_symlink_not_ready', 15)
                    continue
                conn = get_db_connection()
                try:
                    conn.execute(
                        "UPDATE nzb_playback_repairs SET status='verified',last_error=NULL,next_attempt_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (repair['id'],),
                    )
                    conn.commit()
                finally:
                    conn.close()
                repair['status'] = 'verified'

            failed_jobs = _json(repair['failed_job_ids_json'], [])
            failed_job_attempts = _json(repair['failed_job_attempts_json'], {})
            remaining_jobs = []
            for failed_job_id in failed_jobs:
                if client.remove_nzb_exact(failed_job_id):
                    failed_job_attempts.pop(failed_job_id, None)
                    continue
                attempts = failed_job_attempts.get(failed_job_id, 0) + 1
                if attempts >= FAILED_JOB_MAX_ATTEMPTS:
                    log.warning(
                        '[NZBPlayback] Abandoning undeletable failed job repair=%s job=%s after %s attempts',
                        repair['id'], failed_job_id, attempts,
                    )
                    failed_job_attempts.pop(failed_job_id, None)
                    continue
                failed_job_attempts[failed_job_id] = attempts
                remaining_jobs.append(failed_job_id)
            if remaining_jobs:
                conn = get_db_connection()
                try:
                    conn.execute(
                        "UPDATE nzb_playback_repairs SET failed_job_ids_json=?,failed_job_attempts_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (json.dumps(remaining_jobs), json.dumps(failed_job_attempts), repair['id']),
                    )
                    conn.commit()
                finally:
                    conn.close()
                _schedule(repair['id'], 'failed_job_cleanup_pending', 30)
                continue
            if failed_jobs:
                conn = get_db_connection()
                try:
                    conn.execute(
                        "UPDATE nzb_playback_repairs SET failed_job_ids_json='[]',failed_job_attempts_json='{}',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (repair['id'],),
                    )
                    conn.commit()
                finally:
                    conn.close()

            targets = _json(repair['cleanup_targets_json'], [])
            all_complete = True
            for target in targets:
                if target.get('status') == 'complete':
                    continue
                log.info(
                    '[NZBPlayback] Requesting exact cleanup repair=%s old_uuid=%s file=%s',
                    repair['id'], target.get('info_hash') or '', target.get('file_name') or '',
                )
                code, body, error = _mount_request('/api/repair/replacements/ack', target, 20)
                log.info(
                    '[NZBPlayback] Exact cleanup result repair=%s old_uuid=%s status=%s code=%s http=%s error=%s',
                    repair['id'], target.get('info_hash') or '', body.get('status') or '',
                    body.get('code') or '', code, error or '',
                )
                if error or code >= 500 or (code == 409 and body.get('code') == 'repair_busy'):
                    all_complete = False
                    continue
                if code == 200 and body.get('status') in ('removed', 'already_removed'):
                    target['status'] = 'complete'
                    continue
                if code == 409 and body.get('code') == 'stale_target':
                    attempts = target.get('stale_attempts', 0) + 1
                    target['stale_attempts'] = attempts
                    if attempts >= STALE_TARGET_MAX_ATTEMPTS:
                        log.warning(
                            '[NZBPlayback] Exact cleanup abandoning stale target repair=%s old_uuid=%s file=%s '
                            'after %s attempts; finishing repair without old-file cleanup',
                            repair['id'], target.get('info_hash') or '', target.get('file_name') or '', attempts,
                        )
                        target['status'] = 'complete'
                        target['skipped_cleanup'] = True
                        continue
                    all_complete = False
                    target['last_error'] = 'stale_target'
                    continue
                all_complete = False
                target['last_error'] = body.get('code') or f'HTTP {code}'
            conn = get_db_connection()
            try:
                conn.execute(
                    "UPDATE nzb_playback_repairs SET cleanup_targets_json=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(targets), 'cleanup_complete' if all_complete else 'verified', repair['id']),
                )
                conn.commit()
            finally:
                conn.close()
            if not all_complete:
                _schedule(repair['id'], 'exact_cleanup_pending', 15)
                continue
            if not _finish_activity(repair, candidate, repair.get('candidate_title') or item.get('filled_by_title')):
                _schedule(repair['id'], 'activity_finalization_pending', 15)
                continue
            try:
                from utilities.plex_functions import plex_update_item
                plex_update_item({'full_path': os.path.dirname(item.get('location_on_disk') or ''),
                                  'location_on_disk': os.path.dirname(item.get('location_on_disk') or '')})
            except Exception as exc:
                log.warning('[NZBPlayback] Post-cleanup Plex refresh failed: %s', exc)
    finally:
        _worker_lock.release()
