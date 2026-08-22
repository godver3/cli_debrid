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
# A stale_target ack response is usually just the mount not having caught up
# yet with a supersession decypharr already applied, or transient contention
# with a long-running decypharr health sweep touching the same entry — both
# self-resolve, just not always within a couple of minutes. If it's still
# stale after this many quick attempts, don't block the repair's own
# finalization any further (the new file is already confirmed healthy) —
# defer the old-file cleanup to the slower background retry
# (retry_deferred_playback_cleanups) instead of abandoning it outright.
STALE_TARGET_MAX_ATTEMPTS = 5
# Once deferred, retry the same safety-checked ack on a much slower cadence
# (minutes, not seconds) for a long time before concluding it's genuinely
# unresolvable rather than just unlucky timing.
CLEANUP_RETRY_BACKOFF_MINUTES = (15, 30, 60, 120)
CLEANUP_GIVE_UP_HOURS = 48
# A failed-job delete can be legitimately transient (mount briefly
# unreachable), but a job that's simply gone from decypharr will never
# succeed no matter how many times it's retried. Cap retries so a stale
# job ID doesn't block the repair's own old-file cleanup forever.
FAILED_JOB_MAX_ATTEMPTS = 5
# Verification-stage caps, before a candidate has been confirmed healthy —
# unlike the cleanup-stage backstops above, there's no already-healthy new
# file to protect here, so a candidate that can't be verified is rejected
# (excluded, back to searching) rather than deferred.
VERIFY_UNKNOWN_MAX_ATTEMPTS = 10   # ~5 min at the 30s unknown-status cadence
VERIFY_STALE_TARGET_MAX_ATTEMPTS = 5    # ~75s at the 15s 409 cadence, matches STALE_TARGET_MAX_ATTEMPTS
# repair_busy just means decypharr's own (possibly hours-long) sweep is
# running — not a sign this candidate is bad, so give it a much longer
# leash than stale_target before treating it as stuck.
VERIFY_BUSY_MAX_ATTEMPTS = 240     # ~60 min at the 15s 409 cadence
# The candidate-search retry loop (status='awaiting_candidate') never fully
# gives up on its own — usenet content can reappear days after it's pulled —
# but it should stop being silent about a title that isn't resolving.
# First it becomes visible (skipped_max_attempts, matching the tooltip the
# older debrid-side repair UI already uses) and slows down; only after a
# much longer stretch with nothing found does it actually stop scheduling.
CANDIDATE_SEARCH_VISIBILITY_ATTEMPTS = 24        # ~2h at the 5-minute search cadence
CANDIDATE_SEARCH_SLOW_CADENCE_SECONDS = 1800     # 30 min, once past the visibility threshold
CANDIDATE_SEARCH_GIVE_UP_DAYS = 7
# A submitted candidate's info_hash can stop matching the item's current
# filled_by_torrent_id if something outside this repair changes it —
# normally prevented by has_active_exact_repair() guarding the general
# repair engine, but kept as a backstop in case that race happens for some
# other reason. ~5 min at the 30s mismatch cadence, matching
# VERIFY_UNKNOWN_MAX_ATTEMPTS.
CANDIDATE_SOURCE_MISMATCH_MAX_ATTEMPTS = 10


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
        if 'cleanup_status' not in columns:
            conn.execute('ALTER TABLE nzb_playback_repairs ADD COLUMN cleanup_status TEXT')
        if 'cleanup_first_pending_at' not in columns:
            conn.execute('ALTER TABLE nzb_playback_repairs ADD COLUMN cleanup_first_pending_at TIMESTAMP')
        for column in ('old_guid', 'old_segment', 'old_normalized_title',
                       'candidate_guid', 'candidate_segment', 'candidate_normalized_title'):
            if column not in columns:
                conn.execute(f'ALTER TABLE nzb_playback_repairs ADD COLUMN {column} TEXT')
        for column in ('verify_unknown_attempts', 'verify_stale_target_attempts', 'verify_busy_attempts',
                       'candidate_search_attempts', 'source_mismatch_attempts'):
            if column not in columns:
                conn.execute(f'ALTER TABLE nzb_playback_repairs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
        if 'candidate_search_stuck_since' not in columns:
            conn.execute('ALTER TABLE nzb_playback_repairs ADD COLUMN candidate_search_stuck_since TIMESTAMP')
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


def has_pending_playback_repair(item_id):
    """Return whether this item currently has an active (non-complete)
    playback repair in progress — i.e. it's a replacement candidate whose
    playability decypharr's own VerifyReplacement already ffprobes before
    accepting it. Used to skip the redundant ffprobe_all_nzbs check on the
    same file during Adding/Checking, instead of running two independent,
    uncoordinated ffprobe checks on the same replacement candidate."""
    if not item_id:
        return False
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM nzb_playback_repairs WHERE cli_debrid_id=? AND status!='complete' LIMIT 1",
            (int(item_id),),
        ).fetchone()
        return bool(row)
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
                   verify_unknown_attempts=0,verify_stale_target_attempts=0,verify_busy_attempts=0,
                   candidate_search_attempts=0,candidate_search_stuck_since=NULL,source_mismatch_attempts=0,
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
    """Confirm location_on_disk actually points at the given replacement file.

    Symlinked/Local mode: location_on_disk is a cli_debrid-created symlink (or,
    on Windows, a hardlink — os.path.islink() is False for those too) whose
    target reveals the real mounted path.

    Plex mode: location_on_disk is already the real mounted file path as
    reported by Plex's own API — there is no cli_debrid-owned symlink to
    resolve, so it's checked directly.
    """
    link = item.get('location_on_disk') or ''
    if not link:
        return False
    if os.path.islink(link):
        target = os.path.normpath(os.path.realpath(link))
    elif os.path.exists(link):
        target = os.path.normpath(link)
    else:
        return False
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


def _reject_verification_candidate(repair, reason):
    """Give up on the active candidate after it fails to verify within the
    attempt cap for its failure mode, and fall back to candidate search —
    same reset shape as the existing 'verification found broken' path, but
    with nothing to add to cleanup_targets since no replacement was ever
    confirmed healthy.
    """
    excluded = set(_json(repair['excluded_keys_json'], [])) | set(_json(repair['candidate_keys_json'], []))
    conn = get_db_connection()
    try:
        conn.execute(
            """UPDATE nzb_playback_repairs SET status='awaiting_candidate',
               excluded_keys_json=?,candidate_info_hash=NULL,candidate_title=NULL,
               candidate_guid=NULL,candidate_segment=NULL,candidate_normalized_title=NULL,
               verify_unknown_attempts=0,verify_stale_target_attempts=0,verify_busy_attempts=0,
               source_mismatch_attempts=0,
               next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,
               last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (json.dumps(sorted(excluded)), reason, repair['id']),
        )
        conn.commit()
    finally:
        conn.close()
    log.warning(
        '[NZBPlayback] Rejecting candidate after repeated verification failures repair=%s item=%s reason=%s',
        repair['id'], repair['cli_debrid_id'], reason,
    )


def _bump_attempt(repair_id, column):
    """Increment one of the verify-stage attempt counters and return the new value."""
    conn = get_db_connection()
    try:
        conn.execute(
            f"UPDATE nzb_playback_repairs SET {column}={column}+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (repair_id,),
        )
        conn.commit()
        return conn.execute(f"SELECT {column} FROM nzb_playback_repairs WHERE id=?", (repair_id,)).fetchone()[0]
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
                   WHERE status IN ('awaiting_candidate','awaiting_collection','awaiting_verification','verified','cleanup_complete')
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
                       WHERE id=? AND status IN ('awaiting_candidate','awaiting_collection','awaiting_verification','verified','cleanup_complete')
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
            if repair['status'] == 'awaiting_candidate':
                # The last accepted candidate was itself verified broken. Reuse
                # the exact same scrape/submit/exclude pipeline that produced
                # the first candidate to keep searching until a healthy
                # replacement is found, instead of stalling here forever —
                # nothing else in this module reads this status.
                if not item:
                    _schedule(repair['id'], 'item_missing', 300)
                    continue
                from usenet.repair_engine import find_and_submit_playback_candidate
                outcome = find_and_submit_playback_candidate(repair, item)
                log.info(
                    '[NZBPlayback] Candidate search retry repair=%s item=%s outcome=%s',
                    repair['id'], repair['cli_debrid_id'], outcome,
                )
                if outcome == 'submitted':
                    # set_playback_candidate() already reset the search-attempt
                    # counters and updates status, but doesn't clear the lease
                    # this pass just took — always reschedule so the row isn't
                    # stuck holding that 10-minute claim lease before
                    # collection-wait can run.
                    _schedule(repair['id'], outcome, 15)
                    continue

                attempts = _bump_attempt(repair['id'], 'candidate_search_attempts')
                stuck_since = repair.get('candidate_search_stuck_since')
                if attempts >= CANDIDATE_SEARCH_VISIBILITY_ATTEMPTS and not stuck_since:
                    stuck_since = _now()
                    conn = get_db_connection()
                    try:
                        conn.execute(
                            "UPDATE nzb_playback_repairs SET candidate_search_stuck_since=? WHERE id=?",
                            (stuck_since, repair['id']),
                        )
                        if repair.get('activity_id'):
                            conn.execute(
                                """UPDATE nzb_repair_activity SET outcome='skipped_max_attempts',
                                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                (repair['activity_id'],),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                    log.warning(
                        '[NZBPlayback] No healthy candidate found after %s attempts, flagging for visibility '
                        'repair=%s item=%s',
                        attempts, repair['id'], repair['cli_debrid_id'],
                    )

                if stuck_since:
                    stuck_dt = datetime.strptime(stuck_since, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - stuck_dt >= timedelta(days=CANDIDATE_SEARCH_GIVE_UP_DAYS):
                        conn = get_db_connection()
                        try:
                            if repair.get('activity_id'):
                                conn.execute(
                                    """UPDATE nzb_repair_activity SET outcome='abandoned_after_retries',
                                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                    (repair['activity_id'],),
                                )
                            conn.execute(
                                """UPDATE nzb_playback_repairs SET status='complete',completed_at=CURRENT_TIMESTAMP,
                                   next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,
                                   last_error='abandoned_after_retries',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                (repair['id'],),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                        log.warning(
                            '[NZBPlayback] Giving up after %s days with no healthy candidate found repair=%s item=%s',
                            CANDIDATE_SEARCH_GIVE_UP_DAYS, repair['id'], repair['cli_debrid_id'],
                        )
                        continue
                    _schedule(repair['id'], outcome, CANDIDATE_SEARCH_SLOW_CADENCE_SECONDS)
                    continue
                _schedule(repair['id'], outcome, 300)
                continue
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
                attempts = _bump_attempt(repair['id'], 'source_mismatch_attempts')
                if attempts >= CANDIDATE_SOURCE_MISMATCH_MAX_ATTEMPTS:
                    current_source = _source_uuid(item).lower()
                    if current_source and current_source != (repair['old_info_hash'] or '').lower():
                        # Something outside this repair already replaced the
                        # item (normally prevented by has_active_exact_repair
                        # guarding the general repair engine — this is the
                        # backstop for if that race happens some other way).
                        # Don't guess at cleanup for the NEW candidate we no
                        # longer own, but the ORIGINAL broken file this
                        # repair started against is still known exactly
                        # (cleanup_targets_json) and independent of whatever
                        # replaced it — hand it to the same background
                        # cleanup retry a normal successful repair uses,
                        # rather than leaving it referenced nowhere.
                        conn = get_db_connection()
                        try:
                            replacement_title = (
                                item.get('filled_by_title') or item.get('debrid_folder_name') or
                                item.get('filled_by_file') or current_source
                            )
                            if repair.get('activity_id'):
                                conn.execute(
                                    """UPDATE nzb_repair_activity SET replacement_nzb_id=?,replacement_title=?,
                                       outcome='replaced',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                    (current_source, replacement_title, repair['activity_id']),
                                )
                            conn.execute(
                                """UPDATE nzb_playback_repairs SET status='complete',completed_at=CURRENT_TIMESTAMP,
                                   cleanup_status='pending',
                                   cleanup_first_pending_at=COALESCE(cleanup_first_pending_at,CURRENT_TIMESTAMP),
                                   next_attempt_at=NULL,lease_owner=NULL,lease_until=NULL,
                                   last_error='superseded_externally',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                (repair['id'],),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                        log.warning(
                            '[NZBPlayback] Item %s replaced by something outside this repair (now %s); '
                            'finalized activity as replaced and deferring original old-file cleanup '
                            'to background retry repair=%s',
                            repair['cli_debrid_id'], current_source, repair['id'],
                        )
                        continue
                    _reject_verification_candidate(repair, 'candidate_source_changed_max_attempts')
                    continue
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
                    busy_code = body.get('code') or 'repair_busy'
                    if busy_code == 'stale_target':
                        attempts = _bump_attempt(repair['id'], 'verify_stale_target_attempts')
                        if attempts >= VERIFY_STALE_TARGET_MAX_ATTEMPTS:
                            _reject_verification_candidate(repair, 'verify_stale_target_max_attempts')
                            continue
                    else:
                        attempts = _bump_attempt(repair['id'], 'verify_busy_attempts')
                        if attempts >= VERIFY_BUSY_MAX_ATTEMPTS:
                            _reject_verification_candidate(repair, 'verify_busy_max_attempts')
                            continue
                    _schedule(repair['id'], busy_code, 15)
                    continue
                status = body.get('status')
                if status == 'unknown':
                    attempts = _bump_attempt(repair['id'], 'verify_unknown_attempts')
                    if attempts >= VERIFY_UNKNOWN_MAX_ATTEMPTS:
                        _reject_verification_candidate(repair, body.get('reason') or 'verify_unknown_max_attempts')
                        continue
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
            all_actionable = True  # nothing left this fast pass needs to keep retrying
            for target in targets:
                if target.get('status') == 'complete' or target.get('deferred'):
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
                    all_actionable = False
                    continue
                if code == 200 and body.get('status') in ('removed', 'already_removed'):
                    target['status'] = 'complete'
                    continue
                if code == 409 and body.get('code') == 'stale_target':
                    attempts = target.get('stale_attempts', 0) + 1
                    target['stale_attempts'] = attempts
                    if attempts >= STALE_TARGET_MAX_ATTEMPTS:
                        # New file is already confirmed healthy — don't hold the
                        # repair's own finalization hostage to the old file's
                        # cleanup any longer. Hand it off to the slower
                        # background retry instead of abandoning it outright.
                        log.warning(
                            '[NZBPlayback] Exact cleanup deferring stale target repair=%s old_uuid=%s file=%s '
                            'to background retry after %s attempts; finalizing repair now',
                            repair['id'], target.get('info_hash') or '', target.get('file_name') or '', attempts,
                        )
                        target['deferred'] = True
                        target['last_error'] = 'stale_target'
                        continue
                    all_actionable = False
                    target['last_error'] = 'stale_target'
                    continue
                attempts = target.get('generic_attempts', 0) + 1
                target['generic_attempts'] = attempts
                reason = body.get('code') or f'HTTP {code}'
                if attempts >= STALE_TARGET_MAX_ATTEMPTS:
                    # Same reasoning as the stale_target case above: the new
                    # file is already confirmed healthy, so a persistent
                    # non-stale, non-5xx cleanup failure shouldn't hold the
                    # repair's own finalization hostage either — defer to
                    # the slower background retry instead of retrying
                    # inline forever.
                    log.warning(
                        '[NZBPlayback] Exact cleanup deferring persistent failure repair=%s old_uuid=%s file=%s '
                        'reason=%s to background retry after %s attempts; finalizing repair now',
                        repair['id'], target.get('info_hash') or '', target.get('file_name') or '', reason, attempts,
                    )
                    target['deferred'] = True
                    target['last_error'] = reason
                    continue
                all_actionable = False
                target['last_error'] = reason
            has_deferred = any(t.get('status') != 'complete' and t.get('deferred') for t in targets)
            conn = get_db_connection()
            try:
                if has_deferred:
                    conn.execute(
                        """UPDATE nzb_playback_repairs SET cleanup_targets_json=?,status=?,
                           cleanup_status='pending',
                           cleanup_first_pending_at=COALESCE(cleanup_first_pending_at,CURRENT_TIMESTAMP),
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (json.dumps(targets), 'cleanup_complete' if all_actionable else 'verified', repair['id']),
                    )
                else:
                    conn.execute(
                        "UPDATE nzb_playback_repairs SET cleanup_targets_json=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (json.dumps(targets), 'cleanup_complete' if all_actionable else 'verified', repair['id']),
                    )
                conn.commit()
            finally:
                conn.close()
            if not all_actionable:
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


def retry_deferred_playback_cleanups():
    """Background retry for old-file cleanup deferred by the fast completion
    worker after it exhausted its quick attempts on a repair that has already
    finalized as replaced. Runs on a much slower cadence (minutes, not
    seconds) so it can keep patiently retrying the same safety-checked ack
    call for up to CLEANUP_GIVE_UP_HOURS without blocking — or being blocked
    by — repair finalization, which already happened.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM nzb_playback_repairs
               WHERE status='complete' AND cleanup_status='pending'
                 AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP)
               ORDER BY updated_at LIMIT 12"""
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return

    for raw in rows:
        repair = dict(raw)
        targets = _json(repair['cleanup_targets_json'], [])
        pending = [t for t in targets if t.get('status') != 'complete']
        if not pending:
            conn = get_db_connection()
            try:
                conn.execute(
                    "UPDATE nzb_playback_repairs SET cleanup_status='complete',next_attempt_at=NULL,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (repair['id'],),
                )
                conn.commit()
            finally:
                conn.close()
            continue

        expired = False
        first_pending_at = repair.get('cleanup_first_pending_at')
        if first_pending_at:
            try:
                first_dt = datetime.strptime(first_pending_at, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                expired = (datetime.now(timezone.utc) - first_dt) > timedelta(hours=CLEANUP_GIVE_UP_HOURS)
            except Exception:
                expired = False

        for target in pending:
            log.info(
                '[NZBPlayback] Background retry: requesting exact cleanup repair=%s old_uuid=%s file=%s',
                repair['id'], target.get('info_hash') or '', target.get('file_name') or '',
            )
            code, body, error = _mount_request('/api/repair/replacements/ack', target, 20)
            log.info(
                '[NZBPlayback] Background retry result repair=%s old_uuid=%s status=%s code=%s http=%s error=%s',
                repair['id'], target.get('info_hash') or '', body.get('status') or '',
                body.get('code') or '', code, error or '',
            )
            if code == 200 and body.get('status') in ('removed', 'already_removed'):
                target['status'] = 'complete'
                target.pop('deferred', None)
                target.pop('last_error', None)
                continue
            target['last_error'] = body.get('code') or (error or f'HTTP {code}')
            target['background_attempts'] = target.get('background_attempts', 0) + 1

        still_pending = [t for t in targets if t.get('status') != 'complete']
        conn = get_db_connection()
        try:
            if not still_pending:
                log.info('[NZBPlayback] Background cleanup completed repair=%s after handoff', repair['id'])
                conn.execute(
                    "UPDATE nzb_playback_repairs SET cleanup_targets_json=?,cleanup_status='complete',"
                    "next_attempt_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(targets), repair['id']),
                )
            elif expired:
                log.warning(
                    '[NZBPlayback] Giving up on background cleanup for repair=%s after %sh; '
                    'old file(s) left behind: %s',
                    repair['id'], CLEANUP_GIVE_UP_HOURS,
                    [t.get('file_name') for t in still_pending],
                )
                conn.execute(
                    "UPDATE nzb_playback_repairs SET cleanup_targets_json=?,cleanup_status='abandoned',"
                    "next_attempt_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(targets), repair['id']),
                )
            else:
                backoff_idx = min(
                    max(t.get('background_attempts', 1) for t in still_pending) - 1,
                    len(CLEANUP_RETRY_BACKOFF_MINUTES) - 1,
                )
                delay_minutes = CLEANUP_RETRY_BACKOFF_MINUTES[backoff_idx]
                conn.execute(
                    "UPDATE nzb_playback_repairs SET cleanup_targets_json=?,"
                    "next_attempt_at=datetime('now',?),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(targets), f'+{delay_minutes} minutes', repair['id']),
                )
            conn.commit()
        finally:
            conn.close()
