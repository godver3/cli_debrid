"""
NZB Repair Engine

Correct repair workflow (modelled after altmount/nzbdav):

  1. Fetch broken NZB details from provider /api/repair/health
  2. Match to CLI DB item via filled_by_torrent_id = 'nzb:{info_hash}'
  3. Check repair backoff/attempt limit — skip if too soon or too many failures
  4. Blacklist broken NZB URL/segment so it won't be re-grabbed
  5. Targeted re-scrape for replacement (NZB-only, broken title filtered out)
  6. IF no replacement found:
       - Move item to Wanted (keep Plex/cli_mount intact — file still playable)
       - Log 'no_replacement', schedule backoff retry
  7. IF replacement found:
       a. Submit replacement to provider → get new job_id
       b. Confirm replacement is in provider queue (poll up to 30s)
       c. IF confirmed:
            - Delete from Plex FIRST (ms_item_id → location_on_disk fallback)
            - Delete broken from provider
            - Update DB: state=Adding, new torrent_id, clear location_on_disk
            - Log 'replaced'
       d. IF not confirmed:
            - Move to Wanted, NO deletions
            - Log 'submission_failed', increment backoff
  8. After MAX_REPAIR_ATTEMPTS failures → stop auto-repairing, log 'skipped_max_attempts'

Key differences from previous implementation:
  - Replacement is CONFIRMED before any deletion (never delete-before-confirm)
  - Plex deleted BEFORE cli_mount (Plex first so no orphaned Plex entries)
  - Exponential backoff: 1h, 2h, 4h... capped at 24h
  - Failure breaker: give up after MAX_REPAIR_ATTEMPTS, require manual intervention
"""

import json
import logging
import re
import time
from typing import Optional, Tuple

import requests

from database.core import get_db_connection
from database.nzb_repair_activity import (
    log_repair_activity,
    get_repair_state,
    calculate_next_repair_at,
    is_in_backoff,
    MAX_REPAIR_ATTEMPTS,
)
from database.not_wanted_magnets import (
    add_to_not_wanted_nzb_segment,
    extract_nzb_segment_id,
    is_nzb_segment_not_wanted,
    is_magnet_not_wanted,
    is_url_not_wanted,
    is_nzb_guid_not_wanted,
)
from utilities.settings import get_setting
from database.nzb_playback_repair import (
    ELIGIBLE_REASONS as PLAYBACK_REPAIR_REASONS,
    begin_playback_repair,
    candidate_is_excluded,
    candidate_keys,
    has_active_exact_repair,
    record_failed_candidate,
    set_playback_candidate,
)

logger = logging.getLogger(__name__)

# Module-level lock — prevents concurrent repair runs (manual UI + automated check)
import threading as _threading
_repair_lock = _threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plex_cfg():
    url = get_setting('Plex', 'url', '').rstrip('/')
    token = get_setting('Plex', 'token', '')
    disabled = get_setting('Plex', 'disable_plex_library_checks', False)
    return url, token, disabled


def _client():
    """Active usenet client (climount or nzbdav) via the provider factory."""
    from usenet import get_usenet_client
    return get_usenet_client()


def _find_db_item_by_id(item_id):
    if not item_id:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM media_items WHERE id=?', (int(item_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _has_pending_exact_playback_repair(playback_target, info_hash):
    """Whether a changed media row is already tracked by the exact repair lifecycle."""
    if not playback_target:
        return False
    return has_active_exact_repair(
        playback_target.get('cli_debrid_id'), info_hash,
        playback_target.get('file_name'),
    )


def _exact_playback_target(entry):
    """Return the exact cli_mount file identity, or None for legacy repairs."""
    reason = entry.get('failure_reason') or entry.get('reason') or ''
    target = {
        'entry_name': _entry_name(entry),
        'file_name': entry.get('file_name') or '',
        'info_hash': entry.get('info_hash') or entry.get('hash') or '',
        'cli_debrid_id': entry.get('cli_debrid_id'),
        'reason': reason,
        'segment_id': entry.get('nzb_segment_id') or '',
    }
    if reason not in PLAYBACK_REPAIR_REASONS:
        return None
    if not all((target['entry_name'], target['file_name'], target['info_hash'],
                target['cli_debrid_id'])):
        logger.warning('[NZBPlayback] Ignoring incomplete exact target: %r', target)
        return None
    return target


# ---------------------------------------------------------------------------
# Provider health API
# ---------------------------------------------------------------------------

def _entry_name(entry: dict) -> str:
    return entry.get('entry_name') or entry.get('name') or entry.get('title') or ''


def fetch_broken_items(annotate_mount: bool = False) -> list:
    """Return list of broken entry dicts from the active usenet provider.
    If annotate_mount=True, adds 'mount_status' to each entry:
      'missing'  — file not found on mount
      'readable' — file exists and passed read test (likely false positive)
      'unreadable' — file exists but read test timed out or failed
      'unknown'  — no location_on_disk in DB, can't verify

    When debrid file naming is enabled, debrid torrent entries (protocol='torrent')
    are excluded — their rename causes false 'broken' status in cli_mount's health
    checks because the download link resolution uses the renamed name which RD
    doesn't recognise.
    """
    try:
        items = _client().fetch_broken_items()
    except Exception as e:
        logger.error(f'[NZBRepair] fetch_broken_items error: {e}')
        return []

    # cli_mount's /api/repair/health is protocol-agnostic (nzb + torrent
    # entries together); this function is the usenet-only view, so torrent
    # entries always need excluding here regardless of any other setting —
    # debrid_repair_engine.fetch_broken_items() is the symmetric torrent-only
    # counterpart. This used to only filter when enable_debrid_naming was on,
    # which left torrent entries (and their broken files, since climount_client
    # flattens one row per broken file) leaking into the Usenet tab whenever
    # that unrelated setting was off.
    before = len(items)
    items = [e for e in items if (e.get('protocol') or '').lower() != 'torrent']
    if len(items) < before:
        logger.info(f'[NZBRepair] Skipped {before - len(items)} debrid torrent entries')

    if not annotate_mount or not items:
        return items

    for entry in items:
        entry_name = _entry_name(entry)
        try:
            db_items = _find_db_items_by_entry_name(entry_name)
            if db_items is AMBIGUOUS:
                db_items = []
            loc = db_items[0].get('location_on_disk', '') if db_items else ''
            if not loc:
                entry['mount_status'] = 'unknown'
            else:
                import os as _os
                from utilities.settings import get_setting as _gs
                mount = _gs('Usenet Provider', 'mount_path', '/debrid').rstrip('/')
                file_path = mount + loc[len('/debrid'):] if loc.startswith('/debrid/') else loc
                if not _os.path.exists(file_path):
                    entry['mount_status'] = 'missing'
                elif _verify_file_readable(loc, timeout=8):
                    entry['mount_status'] = 'readable'
                else:
                    entry['mount_status'] = 'unreadable'
        except Exception:
            entry['mount_status'] = 'unknown'

    return items


def trigger_health_scan(full: bool = False, wait: bool = False, timeout: int = 300) -> bool:
    """Trigger a health scan on the active provider.
    full=True scans all files; full=False scans only unchecked files.
    wait=True blocks until the scan completes."""
    try:
        return _client().trigger_health_scan(full=full, wait=wait, timeout=timeout)
    except Exception as e:
        logger.warning(f'[NZBRepair] trigger_health_scan error: {e}')
        return False


def get_health_summary() -> dict:
    """Return health-state counts from the active provider."""
    try:
        return _client().get_health_summary()
    except Exception as e:
        logger.debug(f'[NZBRepair] get_health_summary error: {e}')
        return {}


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------

def _resolve_info_hash_from_provider(entry_name: str) -> str:
    """Resolve job UUID from provider by entry name. Returns '' if not found."""
    if not entry_name:
        return ''
    try:
        return _client().resolve_job_id(entry_name)
    except Exception as e:
        logger.debug(f'[NZBRepair] resolve_job_id error: {e}')
        return ''


def _backfill_hash_for_item(item: dict) -> str:
    """
    Targeted hash backfill for a single DB item when _resolve_info_hash_from_provider fails.
    Mirrors the logic in task_backfill_nzb_torrent_ids: tries folder name, original names,
    filled_by_file, and original_scraped_torrent_title against the provider's entry list.
    Returns the resolved hash string, or '' if not found.
    Also updates filled_by_torrent_id in DB if a match is found.
    """
    if not item:
        return ''
    try:
        import requests as _req
        from utilities.settings import get_setting
        from database.database_writing import update_media_item

        dcy_url = get_setting('Usenet Provider', 'url', default='').rstrip('/')
        dcy_token = get_setting('Usenet Provider', 'api_token', default='')
        if not dcy_url:
            return ''
        headers = {'Authorization': f'Bearer {dcy_token}'} if dcy_token else {}

        # Build candidate names from the DB item
        candidates = []
        loc = item.get('location_on_disk', '') or ''
        parts = loc.split('/')
        if len(parts) >= 4:
            folder_name = parts[3]
            if folder_name:
                candidates.append(folder_name)
                # Extract original from {imdb-...} formatted folder
                if '{imdb-' in folder_name:
                    m = re.search(r'\(([^)]+)\)\s*$', folder_name)
                    if m:
                        candidates.append(m.group(1))

        fbf = item.get('filled_by_file', '') or ''
        if fbf:
            candidates.append(fbf)
            fbf_noext = fbf.rsplit('.', 1)[0] if '.' in fbf else fbf
            if fbf_noext != fbf:
                candidates.append(fbf_noext)
            if '{imdb-' in fbf_noext:
                m = re.search(r'\(([^)]+)\)\s*$', fbf_noext)
                if m:
                    candidates.append(m.group(1))

        orig = item.get('original_scraped_torrent_title', '') or ''
        if orig:
            candidates.append(orig)

        # Deduplicate while preserving order
        seen = set()
        unique_candidates = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                unique_candidates.append(c)

        if not unique_candidates:
            return ''

        # Search provider for each candidate
        for candidate in unique_candidates:
            try:
                r = _req.get(
                    f'{dcy_url}/api/torrents',
                    params={'search': candidate[:60]},
                    headers=headers,
                    timeout=10,
                )
                if r.status_code == 200:
                    for t in r.json().get('torrents', []):
                        if t.get('name', '').strip() == candidate.strip():
                            found_hash = t.get('info_hash', '')
                            if found_hash:
                                logger.info(
                                    f'[NZBRepair] Targeted backfill resolved hash {found_hash!r} '
                                    f'via candidate {candidate!r} for item id={item.get("id")}'
                                )
                                # Update DB so future lookups work
                                try:
                                    update_media_item(item['id'], filled_by_torrent_id=f'nzb:{found_hash}')
                                except Exception:
                                    pass
                                return found_hash
            except Exception as _e:
                logger.debug(f'[NZBRepair] backfill candidate {candidate!r} error: {_e}')

    except Exception as e:
        logger.debug(f'[NZBRepair] _backfill_hash_for_item error: {e}')
    return ''


def _find_db_item_by_info_hash(info_hash: str) -> Optional[dict]:
    """
    Look up a media_items row whose filled_by_torrent_id == 'nzb:{info_hash}'.

    Returns None (rather than an arbitrary row) if MORE THAN ONE live row shares
    this exact torrent id — e.g. two different versions (1080p/4k) that ended up
    sharing a job id due to a since-fixed dedup bug. Acting on an arbitrary one of
    them (repairing/resetting/deleting) can destroy the other version's file.
    Callers fall back to _find_db_items_by_entry_name, which returns the full set
    for the caller to handle explicitly.
    """
    if not info_hash:
        return None
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM media_items WHERE filled_by_torrent_id = ? "
            "AND state IN ('Collected','Checking','Upgrading','Adding')",
            (f'nzb:{info_hash}',),
        ).fetchall()
        if len(rows) > 1:
            logger.warning(
                f'[NZBRepair] {len(rows)} live items share torrent id nzb:{info_hash} '
                f'(likely different versions) — refusing to pick one arbitrarily'
            )
            return None
        return dict(rows[0]) if rows else None
    except Exception as e:
        logger.debug(f'[NZBRepair] DB lookup error for {info_hash}: {e}')
        return None
    finally:
        conn.close()


# Sentinel returned by _find_db_items_by_entry_name when a fuzzy strategy matched
# multiple rows that could not be disambiguated. Callers MUST check for this
# explicitly and treat it as "skip this entry" — NOT as "no DB item / orphan".
# Falling through to orphan-delete logic on this result would delete a cli_mount
# entry that a live (but unidentified) row still depends on.
AMBIGUOUS = object()


def _disambiguate_by_version_in_name(rows: list, entry_name: str):
    """
    When a fuzzy match strategy (imdb_id-only, or title+S/E-only) returns multiple
    rows, try to narrow to the one whose version appears in entry_name as a
    " - <version> - " delimited segment (the structured NZB naming convention
    used by _build_nzb_title in routes/scraper_routes.py, when 'Enable NZB File
    Naming' and 'Include Version' are both on — the version is sanitized the
    same way filenames are, stripping \\/*?:"<>| and trailing '*').

    This is inherently best-effort: if naming is off, or the version isn't
    embedded for any other reason, no segment will match and this correctly
    falls through to AMBIGUOUS rather than a false match. If more than one row's
    sanitized version matches (or none do), return the AMBIGUOUS sentinel rather
    than an empty list or a guess — an empty list would be mistaken for "no
    match" and trigger orphan-delete logic, destroying a file another live
    version still needs.
    """
    if len(rows) <= 1:
        return rows
    _name_lower = entry_name.lower()
    _matched = []
    for r in rows:
        _ver = (r.get('version') or '').rstrip('*')
        if not _ver:
            continue
        _ver_san = re.sub(r'[\\/*?:"<>|]', '', _ver).strip().lower()
        if _ver_san and f' - {_ver_san} - ' in _name_lower:
            _matched.append(r)
    if len(_matched) == 1:
        return _matched
    logger.warning(
        f'[NZBRepair] {len(rows)} live items ambiguously match entry {entry_name!r} '
        f'(likely different versions) — refusing to pick one arbitrarily'
    )
    return AMBIGUOUS


def _find_db_items_by_entry_name(entry_name: str):
    """
    Find ALL DB items whose files belong to this provider entry (folder).
    Falls through multiple strategies from most to least precise.
    Only used when hash-based lookup fails.

    Handles three scenarios:
    - Original entry: debrid_folder_name matches entry_name exactly
    - Replacement entry: filled_by_file matches entry_name (after repair, debrid_folder_name
      still has old name but filled_by_file was updated to the new replacement name)
    - Partial match: IMDB ID + episode extracted from entry_name pattern

    Returns a list (possibly empty = genuinely not found), OR the AMBIGUOUS
    sentinel if a fuzzy strategy matched multiple rows it couldn't tell apart
    (e.g. two versions of the same movie/episode). Callers MUST check for
    AMBIGUOUS explicitly and skip the entry — treating it as an empty list
    would trigger orphan-delete logic and destroy a file a live row still needs.
    """
    if not entry_name:
        return []
    conn = get_db_connection()
    _LIVE = "('Collected','Checking','Upgrading')"
    try:
        # Strategy 1: Exact debrid_folder_name match
        rows = conn.execute(
            f"SELECT * FROM media_items WHERE debrid_folder_name = ? AND state IN {_LIVE}",
            (entry_name,),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # Strategy 2: filled_by_file without extension equals entry_name
        # Handles replacement NZBs where filled_by_file was updated but debrid_folder_name wasn't
        rows = conn.execute(
            f"""SELECT * FROM media_items
               WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                   filled_by_file,'.mkv',''),'.mp4',''),'.avi',''),'.m4v',''),'.ts','') = ?
               AND state IN {_LIVE}""",
            (entry_name,),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # Strategy 3: location_on_disk path contains entry_name as directory component
        rows = conn.execute(
            f"SELECT * FROM media_items WHERE location_on_disk LIKE ? AND state IN {_LIVE}",
            (f'%/{entry_name}/%',),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # Strategy 4: IMDB ID + episode number extracted from entry_name
        # Handles {imdb-ttXXX} pattern with S/E — works when names diverge between releases.
        # These match on imdb_id (+ S/E for episodes) only — if two versions (e.g.
        # 1080p/4k) are both live, this can return both rows. If more than one row
        # comes back and we can't disambiguate, refuse to guess: acting on the wrong
        # row can delete another live version's file.
        imdb_match = re.search(r'\{imdb-(tt\d+)\}', entry_name)
        ep_match = re.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', entry_name)
        if imdb_match and ep_match:
            imdb_id = imdb_match.group(1)
            season_num = int(ep_match.group(1))
            ep_num = int(ep_match.group(2))
            rows = conn.execute(
                f"""SELECT * FROM media_items
                   WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                   AND state IN {_LIVE}""",
                (imdb_id, season_num, ep_num),
            ).fetchall()
            if rows:
                return _disambiguate_by_version_in_name([dict(r) for r in rows], entry_name)
        elif imdb_match and not ep_match:
            # Movie: IMDB ID only
            imdb_id = imdb_match.group(1)
            rows = conn.execute(
                f"SELECT * FROM media_items WHERE imdb_id = ? AND type = 'movie' AND state IN {_LIVE}",
                (imdb_id,),
            ).fetchall()
            if rows:
                return _disambiguate_by_version_in_name([dict(r) for r in rows], entry_name)

        # Strategy 5: original_scraped_torrent_title match
        rows = conn.execute(
            f"SELECT * FROM media_items WHERE original_scraped_torrent_title = ? AND state IN {_LIVE}",
            (entry_name,),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # Strategy 6: Episode SxxExx + title prefix (last resort for shows without IMDB tag)
        if ep_match and not imdb_match:
            season_num = int(ep_match.group(1))
            ep_num = int(ep_match.group(2))
            raw_title = entry_name[:ep_match.start()].replace('.', ' ').replace('_', ' ').strip()
            title_prefix = ' '.join(raw_title.split()[:3])
            if title_prefix:
                rows = conn.execute(
                    f"""SELECT * FROM media_items
                       WHERE title LIKE ? AND season_number = ? AND episode_number = ?
                       AND state IN {_LIVE}""",
                    (title_prefix + '%', season_num, ep_num),
                ).fetchall()
                if rows:
                    return _disambiguate_by_version_in_name([dict(r) for r in rows], entry_name)

        return []
    except Exception as e:
        logger.debug(f'[NZBRepair] DB entry lookup error for {entry_name!r}: {e}')
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# File readability verification (subprocess with hard timeout — safe on FUSE)
# ---------------------------------------------------------------------------

def _media_duration_seconds(file_path: str, timeout: int = 10):
    """Best-effort media duration in seconds via ffprobe (reads header only).

    Returns a float, or None if ffprobe is unavailable / the duration can't be
    determined. Used to pick a read offset deeper into the file.
    """
    import subprocess as _sp
    import shutil as _sh
    if not _sh.which('ffprobe'):
        return None
    try:
        r = _sp.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'csv=p=0', file_path],
            timeout=timeout, capture_output=True, text=True,
        )
        if r.returncode == 0:
            return float((r.stdout or '').strip())
    except Exception:
        pass
    return None


def _probe_readable_once(file_path: str, offset_seconds=None, timeout: int = 10):
    """One readability probe of a file on the (possibly lazy/FUSE/debrid) mount.

    Returns:
      True  — confirmed readable (a real packet was decoded at the probe point,
              or the dd fallback read its block);
      False — confidently UNreadable (the tool ran and could not get the data);
      None  — INCONCLUSIVE (timeout / spawn error) — the mount may just be busy,
              so the caller must NOT treat this as 'dead'.

    Runs in a subprocess with a hard timeout so a hung FUSE read parks in the
    child (D-state), never the main process. Prefers ffprobe: it seeks to
    `offset_seconds` (deeper into the file when known) and pulls a real packet via
    -read_intervals. Reading DEEP matters on lazy mounts — the header/first block
    is often cached while the body is gone, so a start-only read can pass on a
    file whose content is actually dead. Falls back to a dd first-block read when
    ffprobe isn't installed.
    """
    import subprocess as _sp
    import shutil as _sh
    try:
        if _sh.which('ffprobe'):
            # Seek to the offset (if known) and read one packet from there;
            # otherwise read one packet from the start.
            interval = f'{offset_seconds:.0f}%+#1' if offset_seconds else '%+#1'
            r = _sp.run(
                ['ffprobe', '-v', 'error',
                 '-read_intervals', interval,
                 '-show_entries', 'packet=pos',
                 '-of', 'csv=p=0', file_path],
                timeout=timeout, capture_output=True, text=True,
            )
            if r.returncode == 0 and (r.stdout or '').strip():
                return True
            # ran but produced no packet — could be genuinely dead OR a transient
            # backend blip; report a clean failure and let the caller's retry decide.
            return False
        # Fallback for images without ffmpeg: first-block read.
        r = _sp.run(['dd', f'if={file_path}', 'bs=1M', 'count=1', 'of=/dev/null'],
                    timeout=timeout, capture_output=True)
        return r.returncode == 0
    except _sp.TimeoutExpired:
        return None
    except Exception as e:
        logger.debug(f'[NZBRepair] read probe error for {file_path!r}: {e}')
        return None


def _verify_file_readable(location_on_disk: str, timeout: int = 10, attempts: int = 3) -> bool:
    """Return True if a file is confirmed-readable, False only if confidently dead.

    This guards the repair pipeline: a False here lets the engine delete + re-scrape
    the item, so the bias is deliberately CONSERVATIVE — on any inconclusive or
    transient signal we return True (keep the item) rather than risk deleting a
    good file. Lazy debrid/FUSE mounts routinely throw transient read errors when
    momentarily busy/unready (e.g. "transport endpoint not connected"), and a
    single failed read must never be read as 'dead' — that is exactly how a health
    check turns a server hiccup into a wave of false-positive deletions.

    Logic: succeed on any attempt → readable (True). Otherwise retry to ride out
    transient blips; conclude UNreadable (False) only when every attempt failed
    *cleanly* (the probe ran and got no data). If any attempt was inconclusive
    (timeout/error), assume readable (True).

    The read samples DEEP into the file (a random 20–80% point of its duration,
    constant across this call's retries but varied across runs) rather than the
    start, so a cached header can't mask a dead body and partial rot elsewhere is
    eventually sampled. Falls back to a start/first-block read when the duration
    is unknown or ffprobe is absent.
    """
    if not location_on_disk:
        return False
    import os as _os
    import time as _t
    # Translate /debrid/ path to the actual mount path inside the container.
    file_path = location_on_disk
    if location_on_disk.startswith('/debrid/'):
        mount = get_setting('Usenet Provider', 'mount_path', '/debrid').rstrip('/')
        file_path = mount + location_on_disk[len('/debrid'):]

    if not _os.path.exists(file_path):
        logger.debug(f'[NZBRepair] File not on mount: {file_path!r}')
        return False

    # Pick a read offset deeper into the file (once per call — the 3 retries below
    # then probe the SAME spot so they only ride out transient blips, not move the
    # goalposts). A RANDOM fraction in [0.2, 0.8] of the duration: well past the
    # cached header, away from the very end (padding/short-reads), and varied
    # across repair runs so partial rot elsewhere in the file is eventually
    # sampled instead of always testing one fixed point. Falls back to a
    # start/first-block read when the duration is unknown (or ffprobe is absent).
    import random as _rnd
    duration = _media_duration_seconds(file_path, timeout=timeout)
    offset = duration * _rnd.uniform(0.2, 0.8) if (duration and duration > 1) else None

    results = []
    for attempt in range(max(1, attempts)):
        res = _probe_readable_once(file_path, offset_seconds=offset, timeout=timeout)
        if res is True:
            return True
        results.append(res)
        if attempt < attempts - 1:
            _t.sleep(1.0)

    if any(r is None for r in results):
        # Never got a clean answer — treat as readable so a busy/unready mount
        # can't trigger a false-positive repair/deletion.
        logger.info(
            f'[NZBRepair] readability inconclusive (mount busy/unready?) for '
            f'{file_path!r} — treating as readable to avoid a false-positive repair'
        )
        return True

    logger.debug(f'[NZBRepair] read test failed across {len(results)} attempts for {file_path!r}')
    return False


# ---------------------------------------------------------------------------
# Provider deletion
# ---------------------------------------------------------------------------

def _delete_from_provider(info_hash: str, entry_name: str) -> bool:
    """Delete a broken entry from the active provider."""
    try:
        result = _client().remove_nzb(info_hash, entry_name)
        if not result:
            logger.warning(f'[NZBRepair] Provider deletion returned False for {info_hash!r} ({entry_name!r})')
        return result
    except Exception as e:
        logger.warning(f'[NZBRepair] Provider deletion error for {info_hash!r}: {e}')
        return False


# ---------------------------------------------------------------------------
# Plex deletion
# ---------------------------------------------------------------------------

def _delete_from_plex(item: dict) -> bool:
    """
    Delete item from Plex library.
    Priority: ms_item_id (ratingKey) → location_on_disk path → title+S/E search.
    Returns True if deletion succeeded.
    """
    plex_url, plex_token, disabled = _plex_cfg()
    if disabled or not plex_url or not plex_token:
        return False

    title = item.get('title', '')
    media_type = item.get('type', 'movie')
    season = item.get('season_number')
    episode = item.get('episode_number')
    ms_item_id = item.get('ms_item_id')
    location_on_disk = item.get('location_on_disk')
    params = {'X-Plex-Token': plex_token}
    hdrs = {'Accept': 'application/json'}

    try:
        # Priority 1: ms_item_id (direct ratingKey — fastest and most precise)
        if ms_item_id:
            r = requests.delete(
                f'{plex_url}/library/metadata/{ms_item_id}',
                params=params, timeout=10,
            )
            if r.status_code in (200, 204):
                logger.info(f'[NZBRepair] Deleted from Plex via ms_item_id={ms_item_id}: {title!r}')
                return True
            logger.debug(f'[NZBRepair] ms_item_id delete returned {r.status_code} for {title!r}, trying fallback')

        # Priority 2: location_on_disk path match
        if location_on_disk:
            try:
                from utilities.plex_functions import remove_file_from_plex
                ep_title = item.get('episode_title') if media_type != 'movie' else None
                if remove_file_from_plex(title, location_on_disk, ep_title):
                    logger.info(f'[NZBRepair] Deleted from Plex via location_on_disk: {title!r}')
                    return True
            except Exception as _e:
                logger.debug(f'[NZBRepair] location_on_disk Plex delete error for {title!r}: {_e}')

        # Priority 3: title + season/episode search (last resort)
        if media_type == 'movie':
            r = requests.get(
                f'{plex_url}/library/all',
                params={**params, 'title': title, 'type': 1},
                headers=hdrs, timeout=10,
            )
            if r.status_code == 200:
                for m in r.json().get('MediaContainer', {}).get('Metadata', []):
                    key = m.get('ratingKey', '')
                    if key:
                        del_r = requests.delete(f'{plex_url}/library/metadata/{key}',
                                                params=params, timeout=10)
                        if del_r.status_code in (200, 204):
                            logger.info(f'[NZBRepair] Deleted from Plex via title search (movie): {title!r}')
                            return True
        else:
            r = requests.get(
                f'{plex_url}/library/all',
                params={**params, 'title': title, 'type': 2},
                headers=hdrs, timeout=10,
            )
            if r.status_code == 200:
                for show in r.json().get('MediaContainer', {}).get('Metadata', []):
                    show_key = show.get('ratingKey', '')
                    if not show_key:
                        continue
                    rs = requests.get(f'{plex_url}/library/metadata/{show_key}/children',
                                      params=params, headers=hdrs, timeout=10)
                    if rs.status_code != 200:
                        continue
                    for season_meta in rs.json().get('MediaContainer', {}).get('Metadata', []):
                        if season_meta.get('index') != season:
                            continue
                        season_key = season_meta.get('ratingKey', '')
                        if not season_key:
                            continue
                        re_ = requests.get(f'{plex_url}/library/metadata/{season_key}/children',
                                           params=params, headers=hdrs, timeout=10)
                        if re_.status_code != 200:
                            continue
                        for ep in re_.json().get('MediaContainer', {}).get('Metadata', []):
                            if ep.get('index') == episode:
                                ep_key = ep.get('ratingKey', '')
                                if ep_key:
                                    del_r = requests.delete(f'{plex_url}/library/metadata/{ep_key}',
                                                            params=params, timeout=10)
                                    if del_r.status_code in (200, 204):
                                        logger.info(f'[NZBRepair] Deleted from Plex via title search (episode): '
                                                    f'{title!r} S{season:02d}E{episode:02d}')
                                        return True
    except Exception as e:
        logger.warning(f'[NZBRepair] Plex delete error for {title!r}: {e}')
    return False


def _bulk_delete_from_plex(items: list) -> dict:
    """
    Delete multiple items from Plex in a single API call using comma-separated ratingKeys.
    Items with ms_item_id are batched into one DELETE request.
    Items without ms_item_id fall back to individual _delete_from_plex calls.
    Returns dict mapping item id → True/False deletion result.
    """
    results = {}
    if not items:
        return results

    plex_url, plex_token, disabled = _plex_cfg()
    if disabled or not plex_url or not plex_token:
        for item in items:
            results[item['id']] = False
        return results

    params = {'X-Plex-Token': plex_token}

    # Split into items with and without ms_item_id
    with_key = [(item['id'], str(item['ms_item_id'])) for item in items if item.get('ms_item_id')]
    without_key = [item for item in items if not item.get('ms_item_id')]

    # Bulk delete items that have ratingKeys — retry once before falling back
    if with_key:
        ids = [k for _, k in with_key]
        keys_str = ','.join(ids)
        bulk_ok = False
        for _attempt in range(2):
            try:
                r = requests.delete(
                    f'{plex_url}/library/metadata/{keys_str}',
                    params=params, timeout=45,
                )
                if r.status_code in (200, 204, 404):
                    logger.info(f'[NZBRepair] Bulk Plex delete: {len(ids)} item(s) via ratingKeys ({r.status_code})')
                    for item_id, _ in with_key:
                        results[item_id] = True
                    bulk_ok = True
                    break
                else:
                    logger.warning(f'[NZBRepair] Bulk Plex delete returned {r.status_code} (attempt {_attempt+1})')
            except Exception as e:
                logger.warning(f'[NZBRepair] Bulk Plex delete error (attempt {_attempt+1}): {e}')
                if _attempt == 0:
                    time.sleep(5)
        if not bulk_ok:
            logger.warning(f'[NZBRepair] Bulk Plex delete failed after retries — falling back to individual deletes')
            for item in items:
                if item.get('ms_item_id'):
                    results[item['id']] = _delete_from_plex(item)

    # Individual fallback for items without ratingKey
    for item in without_key:
        results[item['id']] = _delete_from_plex(item)

    return results


# ---------------------------------------------------------------------------
# Segment blacklisting
# ---------------------------------------------------------------------------

def _blacklist_broken_nzb(nzb_url: str, segment_id: str = '') -> None:
    """Add broken NZB URL/guid and segment ID to not-wanted lists.
    segment_id should be passed from the DB item's nzb_segment_id column —
    no extra HTTP fetch needed."""
    try:
        from database.not_wanted_magnets import (
            add_to_not_wanted_nzb_guid,
            add_to_not_wanted_nzb_segment,
            add_to_not_wanted_urls,
        )
        if nzb_url:
            add_to_not_wanted_nzb_guid(nzb_url)
            # Also blacklist the raw URL as a backstop for indexers whose NZB
            # links extract_nzb_guid() can't cleanly parse (e.g. malformed
            # query strings missing '?') — is_url_not_wanted() does an exact
            # base-filename match, which works as long as the indexer serves
            # a stable URL for the same content (true for static-API-key
            # indexers), independent of guid extraction succeeding.
            add_to_not_wanted_urls(nzb_url)
            logger.info(f'[NZBRepair] Blacklisted NZB URL: {nzb_url[:80]}')
        if segment_id:
            add_to_not_wanted_nzb_segment(segment_id)
            logger.info(f'[NZBRepair] Blacklisted NZB segment ID: {segment_id!r}')
    except Exception as e:
        logger.debug(f'[NZBRepair] Blacklist error: {e}')


# ---------------------------------------------------------------------------
# Re-scrape for replacement
# ---------------------------------------------------------------------------

def _scrape_for_replacement(item: dict, broken_nzb_title: str, version_override: str = None) -> list:
    """
    Run targeted scrape for replacement NZB.
    Returns NZB-only results with broken title and blacklisted segments filtered out.
    """
    try:
        from scraper.scraper import scrape as do_scrape

        media_type = item.get('type', 'movie')
        is_episode = media_type == 'episode'
        version = version_override or item.get('version') or 'Default'

        logger.info(f'[NZBRepair] Scraping replacement for {item.get("title")!r} version={version!r}')

        results, _ = do_scrape(
            imdb_id=item.get('imdb_id', '') or '',
            tmdb_id=str(item.get('tmdb_id', '') or ''),
            title=item.get('title', ''),
            year=item.get('year'),
            content_type='episode' if is_episode else 'movie',
            version=version,
            season=item.get('season_number') if is_episode else None,
            episode=item.get('episode_number') if is_episode else None,
            multi=False,
            genres=json.loads(item['genres']) if item.get('genres') else None,
            skip_cache_check=True,
        )

        nzb_results = [r for r in (results or []) if r.get('protocol') == 'nzb']

        # Filter out not-wanted magnets/URLs/guids. Unlike scraping_queue.py's
        # normal scrape path, this repair-specific search never went through
        # that filtering at all — meaning a replacement candidate blacklisted
        # for being dead (including via _blacklist_broken_nzb below, on the
        # very entry this function is trying to replace) could still be
        # picked again here. Same filter expression scraping_queue.py uses
        # at its own call sites.
        nzb_results = [
            r for r in nzb_results
            if not (
                is_magnet_not_wanted(r.get('magnet') or r.get('nzb_url')) or
                is_url_not_wanted(r.get('magnet') or r.get('nzb_url')) or
                (r.get('nzb_url') and is_nzb_guid_not_wanted(r.get('parsed_info', {}).get('guid') or r.get('nzb_url')))
            )
        ]

        # Filter out broken release by both structured title and raw release name
        if broken_nzb_title:
            _raw_broken = broken_nzb_title
            _orig_match = re.search(r'\(([^)]+)\)\s*$', broken_nzb_title)
            if _orig_match:
                _raw_broken = _orig_match.group(1).replace('-[NZB Pack]', '').strip()
            nzb_results = [
                r for r in nzb_results
                if r.get('title') != broken_nzb_title
                and r.get('original_title') != broken_nzb_title
                and r.get('title') != _raw_broken
                and r.get('original_title') != _raw_broken
            ]

        # Pre-fetch NZBs and filter segment-blacklisted ones
        filtered = []
        for r in nzb_results:
            nzb_url = r.get('nzb_url') or r.get('magnet', '')
            if nzb_url:
                try:
                    resp = requests.get(nzb_url, timeout=15, allow_redirects=True,
                                        headers={'User-Agent': 'Sabnzbd/3.0.0'})
                    if resp.status_code == 200 and '<nzb' in resp.text.lower():
                        if is_nzb_segment_not_wanted(resp.text):
                            logger.debug(f'[NZBRepair] Skipping blacklisted segment: {r.get("title")}')
                            continue
                        r['_prefetched_nzb'] = resp.text
                except Exception:
                    pass
            filtered.append(r)

        logger.info(f'[NZBRepair] Found {len(filtered)} usable replacement candidates for {item.get("title")!r}')
        return filtered

    except Exception as e:
        logger.error(f'[NZBRepair] _scrape_for_replacement error: {e}', exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Submit replacement and confirm in queue
# ---------------------------------------------------------------------------

def _submit_and_confirm_replacement(result: dict, title: str, item: dict = None) -> Tuple[Optional[str], str, str]:
    """
    Submit replacement NZB to provider.
    Polls up to 30s to confirm it appears in the provider queue.
    Returns (job_id, release_title, disposition).  ``failed`` is reserved
    for an explicit terminal provider response; callers may immediately try
    the next distinct candidate.  ``provisional`` means the submission was
    accepted but its status is inconclusive, so callers must not duplicate it.
    """
    try:
        from usenet import get_usenet_client, reset_usenet_client
        reset_usenet_client()
        client = get_usenet_client()

        nzb_content = result.get('_prefetched_nzb', '')
        nzb_url = result.get('nzb_url') or result.get('magnet', '')
        release_title = result.get('title') or title

        # Apply NZB file naming if enabled
        if item:
            try:
                from routes.scraper_routes import _build_nzb_title
                _item_type = item.get('type', '')
                _media_type = 'tv' if _item_type == 'episode' else _item_type
                _parsed = result.get('parsed_info', {}) or {}
                _is_season_pack = bool(_parsed.get('seasons')) and not _parsed.get('episodes')
                named = _build_nzb_title(
                    title=item.get('title', '') or title,
                    year=item.get('year', ''),
                    imdb_id=item.get('imdb_id'),
                    version=item.get('version', ''),
                    original_scraped_torrent_title=release_title,
                    media_type=_media_type,
                    season=item.get('season_number'),
                    episode=None if _is_season_pack else item.get('episode_number'),
                    episode_title=item.get('episode_title'),
                )
                if named:
                    release_title = named
            except Exception:
                pass

        _is_anime = bool(item.get('trigger_is_anime', False)) if item else False
        _media_type = item.get('type', '') if item else ''
        _tags = item.get('tags') if item else None

        if nzb_content:
            job_id = client.add_nzb_content(nzb_content=nzb_content, title=release_title,
                                            is_anime=_is_anime, media_type=_media_type,
                                            tags=_tags, tags_exclusive=False)
        elif nzb_url:
            job_id = client.add_nzb(nzb_url=nzb_url, title=release_title,
                                    is_anime=_is_anime, media_type=_media_type,
                                    tags=_tags, tags_exclusive=False)
        else:
            return None, '', 'rejected'

        if not job_id:
            failed_job_id = getattr(client, 'last_failed_job_id', '') or ''
            if failed_job_id:
                logger.warning('[NZBRepair] Provider immediately failed job %s for %r', failed_job_id, release_title)
                return failed_job_id, release_title, 'failed'
            logger.warning(f'[NZBRepair] Provider rejected submission for {release_title!r}')
            return None, '', 'rejected'

        # Poll up to 30s to confirm job appears in provider queue
        confirmed = False
        terminal_failure = False
        for attempt in range(6):
            time.sleep(5)
            try:
                status = client.get_job_status(job_id)
                if status and status.get('state') not in ('failed', 'error', 'unknown'):
                    confirmed = True
                    break
                if status and status.get('state') in ('failed', 'error'):
                    logger.warning(f'[NZBRepair] Replacement job {job_id} immediately failed: {status}')
                    terminal_failure = True
                    break
            except Exception:
                pass

        if confirmed:
            logger.info(f'[NZBRepair] Replacement confirmed in queue: job_id={job_id} title={release_title!r}')
            return job_id, release_title, 'accepted'
        elif terminal_failure:
            return job_id, release_title, 'failed'
        else:
            logger.info(f'[NZBRepair] Replacement submitted provisionally: job_id={job_id} title={release_title!r}')
            return job_id, release_title, 'provisional'

    except Exception as e:
        logger.error(f'[NZBRepair] _submit_and_confirm_replacement error: {e}')
        return None, '', 'rejected'


# ---------------------------------------------------------------------------
# DB update for repaired item
# ---------------------------------------------------------------------------

def _update_db_for_repair(item: dict, new_job_id: str, replacement_result: dict,
                          all_results: list, preserve_location: bool = False) -> bool:
    """Update DB item in-place for the accepted candidate.
    Also stores the replacement NZB segment ID so the next repair cycle can
    blacklist it without an extra HTTP fetch."""
    try:
        from database.database_writing import update_media_item_state, update_media_item
        item_id = item['id']
        new_torrent_id = f'nzb:{new_job_id}'
        release_title = replacement_result.get('title') or item.get('title', '')

        # Strip _prefetched_nzb (full NZB XML) before storing — never persist NZB content to DB
        def _strip_prefetched(r):
            if '_prefetched_nzb' in r:
                r = dict(r)
                del r['_prefetched_nzb']
            return r
        chosen_keys = candidate_keys(replacement_result)
        remaining = [
            _strip_prefetched(r) for r in all_results
            if not (chosen_keys and candidate_keys(r) & chosen_keys)
            and r.get('title') != replacement_result.get('title')
        ]

        # Extract segment ID from the pre-fetched NZB XML (zero extra HTTP call)
        new_segment_id = ''
        _prefetched = replacement_result.get('_prefetched_nzb', '')
        if _prefetched:
            try:
                from database.not_wanted_magnets import extract_nzb_segment_id
                new_segment_id = extract_nzb_segment_id(_prefetched)
            except Exception:
                pass

        _seg_kwargs = {'nzb_segment_id': new_segment_id} if new_segment_id else {}
        update_media_item_state(
            item_id, 'Adding',
            filled_by_torrent_id=new_torrent_id,
            filled_by_file=release_title,
            filled_by_title=release_title,
            filled_by_magnet=replacement_result.get('nzb_url') or replacement_result.get('magnet', ''),
            debrid_folder_name=None,
            scrape_results=remaining,
            **_seg_kwargs,
        )
        update_kwargs = {'fall_back_to_single_scraper': False}
        if not preserve_location:
            update_kwargs['location_on_disk'] = None
        update_media_item(item_id, **update_kwargs)
        logger.info(f'[NZBRepair] DB updated item {item_id}: state=Adding torrent_id={new_torrent_id} segment={new_segment_id!r}')
        return True
    except Exception as e:
        logger.error(f'[NZBRepair] _update_db_for_repair error: {e}')
        return False


def find_and_submit_playback_candidate(repair_row: dict, item: dict) -> str:
    """Re-run the same scrape/submit/exclude pipeline used for the initial
    broken-entry repair, for a playback repair whose accepted candidate was
    itself later found broken (status='awaiting_candidate'). Reuses
    candidate_is_excluded/set_playback_candidate so a previously-failed
    candidate (already recorded in excluded_keys_json by the caller) is
    never re-tried. Returns an outcome string used only for retry
    scheduling/logging by the completion worker — never raises.
    """
    item_id = item['id']
    broken_nzb_title = (item.get('debrid_folder_name') or item.get('filled_by_file')
                         or repair_row.get('old_entry_name') or item.get('title') or '')
    candidates = _scrape_for_replacement(item, broken_nzb_title)
    seen_keys = set()
    unique_candidates = []
    for candidate in candidates:
        keys = candidate_keys(candidate)
        if not keys or keys & seen_keys:
            continue
        seen_keys |= keys
        if candidate_is_excluded(item_id, candidate):
            continue
        unique_candidates.append(candidate)
    if not unique_candidates:
        return 'no_replacement'

    for candidate in unique_candidates:
        job_id, submitted_title, disposition = _submit_and_confirm_replacement(
            candidate, item.get('title', ''), item=item)
        if disposition in ('failed', 'rejected'):
            record_failed_candidate(item_id, candidate, job_id or '')
            logger.warning(
                '[NZBPlayback] Retry candidate rejected (%s); trying next distinct result: %r',
                disposition, candidate.get('title'),
            )
            continue
        best = dict(candidate)
        best.setdefault('original_title', best.get('title', ''))
        if submitted_title:
            best['title'] = submitted_title
        if not set_playback_candidate(item_id, best, job_id, submitted_title or best.get('title', '')):
            record_failed_candidate(item_id, best, job_id)
            logger.error('[NZBPlayback] Could not persist retry candidate %s for item %s', job_id, item_id)
            return 'persist_failed'
        if not _update_db_for_repair(item, job_id, best, unique_candidates, preserve_location=True):
            logger.error('[NZBPlayback] Could not move retry candidate %s into Adding for item %s', job_id, item_id)
            return 'db_update_failed'
        logger.info(
            '[NZBPlayback] Fresh candidate %s accepted for item %s after prior replacement also failed',
            job_id, item_id,
        )
        return 'submitted'
    return 'submission_failed'


def _move_to_wanted(item: dict) -> None:
    """Move item back to Wanted without touching Plex or provider."""
    try:
        from database.database_writing import update_media_item_state, update_media_item
        item_id = item['id']
        update_media_item_state(
            item_id, 'Wanted',
            filled_by_torrent_id=None,
            filled_by_file=None,
            filled_by_title=None,
            debrid_folder_name=None,
        )
        update_media_item(item_id, location_on_disk=None, fall_back_to_single_scraper=False)
        logger.info(f'[NZBRepair] Moved item {item_id} to Wanted for re-scrape')
    except Exception as e:
        logger.error(f'[NZBRepair] _move_to_wanted error for item {item.get("id")}: {e}')


# Item IDs that were just moved to Wanted by a manual retry, consumed
# (popped) by scraping_queue's season-pack coalescing check on that item's
# very next scrape pass. Lets a manual retry skip coalescing for exactly one
# attempt — e.g. so it can't loop forever re-coalescing into a sibling's
# ghost job the way it was retried to get away from — without touching
# coalescing behavior for every other (non-retried) item that scrapes.
_manual_retry_pending: set = set()
_manual_retry_pending_lock = _threading.Lock()


def retry_exhausted_item(item_id: int, broken_nzb_id: str = '') -> dict:
    """Manually retry an item stuck at 'skipped_max_attempts' (give_up).

    The give_up gate short-circuits before Plex/provider deletion ever runs,
    so the dead entry is likely still sitting there — clean it up here,
    blacklist it so re-scrape can't just pick the same dead release again,
    then move the item back to Wanted. Also logs a fresh activity row with
    repair_attempts=0 so a later get_repair_state() lookup for this
    broken_nzb_id derives give_up=False again, in case it resurfaces.
    """
    item = _find_db_item_by_id(item_id)
    if not item:
        return {'outcome': 'error', 'message': f'No DB item found for id {item_id}'}

    nzb_url = item.get('filled_by_magnet', '')
    if nzb_url:
        _blacklist_broken_nzb(nzb_url, item.get('nzb_segment_id', '') or '')

    # Symlinked/Local mode: deliberately NOT calling _delete_from_plex(item).
    # get_symlink_path is deterministic (based on title/season/episode/version,
    # not the specific release), so a replacement always lands at the exact
    # same path as before — a normal rescan updates the existing Plex item in
    # place (keeping addedAt/watch history) once the new symlink exists.
    # Deleting first just orphans that match, so the replacement shows up
    # as a fresh "recently added" item instead. ffprobe's rejection path
    # (_reject_unplayable_source) never touches Plex for the same reason.
    #
    # Plex mode is different: per _symlink_matches (database/
    # nzb_playback_repair.py), location_on_disk there is the real mounted
    # file path as Plex's own API reports it — no cli_debrid-owned symlink
    # or deterministic path to key off, so a different replacement release
    # genuinely is a different path from Plex's perspective. Skipping the
    # delete there risks leaving the dead entry orphaned alongside the new
    # one, so Plex mode keeps the original delete-then-recreate behavior.
    if get_setting('File Management', 'file_collection_management') != 'Symlinked/Local':
        _delete_from_plex(item)

    # broken_nzb_id (from the activity row logged back when this item first
    # failed) is often just the release title, not a real provider job ID —
    # deleting by it silently no-ops. The item's own filled_by_torrent_id
    # ("nzb:<uuid>") is the CURRENT, authoritative job ID for whatever's
    # still sitting on the mount right now, so prefer that.
    #
    # But that torrent_id may be a season pack shared with sibling episodes
    # (coalescing assigns the same job to every episode in the pack) — unlike
    # the automated NZBRepair path, which only ever deletes an entry that
    # cli_mount's own health check already independently confirmed dead,
    # nothing here has verified this job is actually broken beyond "the one
    # episode the user retried looked stuck." Deleting it unconditionally
    # would take the whole pack down for every sibling still relying on it.
    # Match the same sibling check the manual "Delete item" UI route uses
    # before it removes anything from cli_mount.
    torrent_id = item.get('filled_by_torrent_id') or ''
    provider_job_id = torrent_id[4:] if torrent_id.startswith('nzb:') else (torrent_id or broken_nzb_id)
    if provider_job_id and torrent_id:
        conn = get_db_connection()
        try:
            sibling_count = conn.execute(
                "SELECT COUNT(*) FROM media_items "
                "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                (torrent_id, item_id)
            ).fetchone()[0]
        finally:
            conn.close()
        if sibling_count == 0:
            _delete_from_provider(provider_job_id, provider_job_id)
        else:
            logger.info(
                f'[NZBRepair] Manual retry: skipping provider delete for {provider_job_id!r} — '
                f'{sibling_count} sibling(s) still rely on it'
            )
    elif provider_job_id:
        _delete_from_provider(provider_job_id, provider_job_id)

    _move_to_wanted(item)

    with _manual_retry_pending_lock:
        _manual_retry_pending.add(item_id)

    # _move_to_wanted resets identity fields but leaves collected_at from the
    # OLD (broken) collection in place. The Adding queue's season-pack dedup
    # check (run_program.py's "self_collected" shortcut) sees that stale
    # timestamp, assumes this item already has a file in place from earlier
    # in the same cycle, and marks it Collected without ever running the
    # Checking queue's symlink creation — leaving it 'Collected' in the DB
    # with location_on_disk still None and nothing actually playable. Only
    # clearing it here (not in _move_to_wanted itself, which the automated
    # repair loop also calls) keeps this scoped to manual retries.
    try:
        from database.database_writing import update_media_item
        update_media_item(item_id, collected_at=None)
    except Exception as e:
        logger.warning(f'[NZBRepair] Could not clear stale collected_at for item {item_id}: {e}')

    log_repair_activity(
        item_id=item_id,
        title=item.get('title'),
        media_type=item.get('type'),
        season_number=item.get('season_number'),
        episode_number=item.get('episode_number'),
        broken_nzb_id=broken_nzb_id,
        broken_nzb_title=item.get('filled_by_file', ''),
        outcome='manual_retry',
        triggered_by='manual',
        repair_attempts=0,
    )
    logger.info(f'[NZBRepair] Manual retry: item {item_id} reset to Wanted (was skipped_max_attempts)')
    return {'outcome': 'ok', 'message': 'Item reset to Wanted for re-scrape'}


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_available_versions() -> list:
    try:
        versions = get_setting('Scraping', 'versions', {})
        return sorted(versions.keys()) if versions else []
    except Exception:
        return []


def get_repair_version() -> str:
    return get_setting('Usenet Provider', 'repair_version', '') or ''


def set_repair_version(version: str) -> None:
    from utilities.settings import set_setting
    set_setting('Usenet Provider', 'repair_version', version or '')


# ---------------------------------------------------------------------------
# Single-entry repair
# ---------------------------------------------------------------------------

def repair_single_entry(entry_name: str, version_override: str = None) -> dict:
    """Repair a single broken entry by name. Returns outcome dict."""
    broken_entries = fetch_broken_items()
    entry = next((e for e in broken_entries if _entry_name(e) == entry_name), None)
    if not entry:
        return {'outcome': 'not_found', 'message': f'Entry {entry_name!r} not found in broken list'}
    if version_override is None:
        saved = get_repair_version()
        if saved:
            version_override = saved
    # Re-use inner loop logic by calling _run_repair_inner on just this entry
    with _repair_lock:
        return _repair_single_entry_inner(entry, version_override=version_override)


def _repair_single_entry_inner(entry: dict, version_override: str = None) -> dict:
    """Run repair logic for one entry. Must be called with _repair_lock held."""
    entry_name = _entry_name(entry)
    info_hash = entry.get('info_hash') or entry.get('hash') or ''
    if not info_hash and entry_name:
        info_hash = _resolve_info_hash_from_provider(entry_name)

    try:
        db_items = []
        if info_hash:
            single = _find_db_item_by_info_hash(info_hash)
            if single:
                db_items = [single]
        if not db_items and entry_name and not entry.get('hash_is_authoritative'):
            db_items = _find_db_items_by_entry_name(entry_name)
        if db_items is AMBIGUOUS:
            return {'outcome': 'ambiguous', 'message': f'Multiple versions ambiguously match {entry_name!r} — refusing to guess which to repair'}
        db_items = [i for i in db_items if i.get('state') in ('Collected', 'Checking', 'Upgrading', 'Adding')]
        if not db_items:
            return {'outcome': 'not_found', 'message': f'No matching DB item for {entry_name!r}'}
        # Run repair on just this entry using the full inner loop
        from usenet.repair_engine import _run_repair_inner
        # Pass entry directly — we borrow the inner logic via a single-entry broken list
        _orig_fetch = None
        try:
            import usenet.repair_engine as _re_mod
            _orig_fetch = _re_mod.fetch_broken_items
            _re_mod.fetch_broken_items = lambda: [entry]
            result = _run_repair_inner(triggered_by='manual_single', version_override=version_override)
            return {'outcome': 'ok', 'summary': result}
        finally:
            if _orig_fetch is not None:
                _re_mod.fetch_broken_items = _orig_fetch
    except Exception as e:
        logger.error(f'[NZBRepair] single entry repair error for {entry_name!r}: {e}', exc_info=True)
        return {'outcome': 'error', 'message': str(e)}


# ---------------------------------------------------------------------------
# Main repair loop
# ---------------------------------------------------------------------------

def run_repair(triggered_by: str = 'scheduled', version_override: str = None) -> dict:
    """
    Full repair cycle. Returns summary dict with counts.
    Uses a module-level lock to prevent concurrent runs from the automated
    health check and manual UI trigger processing the same items simultaneously.

    Correct operation order (never delete before confirming replacement):
      1. Find replacement → submit → confirm in queue
      2. Delete from Plex (ms_item_id first, location_on_disk fallback)
      3. Delete from provider
      4. Update DB to Adding with new torrent_id
    """
    if not _repair_lock.acquire(blocking=False):
        logger.info(f'[NZBRepair] Repair already in progress — skipping {triggered_by!r} trigger')
        return {'skipped': 'already_running'}

    try:
        return _run_repair_inner(triggered_by=triggered_by, version_override=version_override)
    finally:
        _repair_lock.release()


def _run_repair_inner(triggered_by: str = 'scheduled', version_override: str = None) -> dict:
    """Inner repair logic — called only when _repair_lock is held."""
    if version_override is None:
        saved = get_repair_version()
        if saved:
            version_override = saved
            logger.info(f'[NZBRepair] Using saved repair version: {version_override!r}')

    summary = {
        'broken_found': 0,
        'matched': 0,
        'replaced': 0,
        'no_replacement': 0,
        'submission_failed': 0,
        'skipped_backoff': 0,
        'skipped_max_attempts': 0,
        'not_found': 0,
        'errors': 0,
    }

    broken_entries = fetch_broken_items()
    summary['broken_found'] = len(broken_entries)

    if not broken_entries:
        logger.info('[NZBRepair] No broken items found — nothing to do')
        return summary

    logger.info(f'[NZBRepair] Processing {len(broken_entries)} broken entries')

    for entry in broken_entries:
        entry_name = _entry_name(entry)
        info_hash = entry.get('info_hash') or entry.get('hash') or ''
        nzb_url = entry.get('nzb_url') or entry.get('url') or ''
        playback_target = _exact_playback_target(entry)

        # Resolve job UUID from provider if not in health response
        if not info_hash and entry_name:
            info_hash = _resolve_info_hash_from_provider(entry_name)
            if info_hash:
                logger.info(f'[NZBRepair] Resolved job UUID={info_hash!r} for {entry_name!r}')

        logger.info(f'[NZBRepair] Processing broken entry: {entry_name!r} hash={info_hash!r}')

        try:
            # --- Step 1: Match to CLI DB ---
            db_items = []
            if playback_target:
                exact_item = _find_db_item_by_id(playback_target['cli_debrid_id'])
                if exact_item:
                    current = str(exact_item.get('filled_by_torrent_id') or '')
                    if current in (info_hash, f'nzb:{info_hash}'):
                        db_items = [exact_item]
                    else:
                        logger.warning(
                            '[NZBPlayback] Item %s no longer owns old UUID %s; refusing fuzzy repair',
                            playback_target['cli_debrid_id'], info_hash,
                        )
            elif info_hash:
                single = _find_db_item_by_info_hash(info_hash)
                if single:
                    db_items = [single]

            # Fuzzy fallback only when hash is not authoritative (nzbdav sets hash_is_authoritative)
            if not playback_target and not db_items and entry_name and not entry.get('hash_is_authoritative'):
                db_items = _find_db_items_by_entry_name(entry_name)

            if db_items is AMBIGUOUS:
                # Multiple versions ambiguously matched — do NOT fall through to the
                # orphan-delete branch below, which would delete a cli_mount entry
                # that a live (but unidentified) row still depends on.
                logger.warning(f'[NZBRepair] {entry_name!r} — ambiguous multi-version match, skipping without deleting')
                log_repair_activity(
                    broken_nzb_id=info_hash or entry_name,
                    broken_nzb_title=entry_name,
                    outcome='ambiguous',
                    triggered_by=triggered_by,
                )
                summary['errors'] += 1
                continue

            # Only repair items in live, repairable states
            db_items = [i for i in db_items
                        if i.get('state') in ('Collected', 'Checking', 'Upgrading', 'Adding')]

            # For fuzzy-matched Collected items, skip any whose current filled_by_torrent_id
            # does NOT match the broken entry's job ID. This means the item was already
            # successfully re-collected under a different NZB — the stale debrid_folder_name
            # just happens to match the broken entry name.
            if info_hash and db_items:
                def _matches_broken(item):
                    if item.get('state') != 'Collected':
                        return True
                    torrent_id = item.get('filled_by_torrent_id') or ''
                    # filled_by_torrent_id is stored as "nzb:<uuid>"
                    return torrent_id == info_hash or torrent_id == f'nzb:{info_hash}'
                filtered = [i for i in db_items if _matches_broken(i)]
                if len(filtered) < len(db_items):
                    skipped = [i.get('id') for i in db_items if not _matches_broken(i)]
                    logger.info(f'[NZBRepair] Skipping already-replaced Collected items {skipped} for {entry_name!r} — torrent_id mismatch')
                    db_items = filtered

            if not db_items:
                # Orphan — no CLI DB match.
                # Only attempt provider delete if we have a hash — name-search deletes
                # are slow (one API call per orphan) and orphans with no hash are usually
                # already gone from the provider queue.
                if playback_target:
                    logger.warning(
                        '[NZBPlayback] Exact item %s is unavailable or changed; leaving mounted file untouched',
                        playback_target['cli_debrid_id'],
                    )
                    if _has_pending_exact_playback_repair(playback_target, info_hash):
                        logger.info(
                            '[NZBPlayback] Exact old file already belongs to an active repair; '
                            'retaining its canonical activity and suppressing duplicate Not Found '
                            '(item=%s uuid=%s file=%s)',
                            playback_target['cli_debrid_id'], info_hash,
                            playback_target['file_name'],
                        )
                        continue
                    # No repair still owns this old UUID (finished or never tracked) — the
                    # provider will otherwise keep re-reporting this exact stale job as
                    # broken on every future sweep forever, spamming 'not_found' for an
                    # entry nothing can ever act on. Clear it the same way the orphan
                    # branch below does — but first check no *other* live item still
                    # references this exact old UUID (e.g. a sibling episode from the
                    # same season-pack NZB that hasn't been repaired yet). Same guard
                    # as the manual-retry provider delete above; unlike that path,
                    # cli_mount's own health check already confirmed this UUID is dead.
                    if info_hash:
                        conn = get_db_connection()
                        try:
                            sibling_count = conn.execute(
                                "SELECT COUNT(*) FROM media_items "
                                "WHERE filled_by_torrent_id IN (?, ?) "
                                "AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                (info_hash, f'nzb:{info_hash}', playback_target['cli_debrid_id']),
                            ).fetchone()[0]
                        finally:
                            conn.close()
                        if sibling_count == 0:
                            logger.info(
                                f'[NZBPlayback] Stale broken entry for superseded item '
                                f'{playback_target["cli_debrid_id"]} — deleting {info_hash!r} from provider'
                            )
                            _delete_from_provider(info_hash, entry_name)
                        else:
                            logger.info(
                                f'[NZBPlayback] Skipping provider delete for {info_hash!r} — '
                                f'{sibling_count} sibling(s) still rely on it'
                            )
                elif info_hash:
                    logger.warning(f'[NZBRepair] No repairable DB items for {entry_name!r} — orphan, deleting from provider')
                    _delete_from_provider(info_hash, entry_name)
                else:
                    logger.debug(f'[NZBRepair] No repairable DB items for {entry_name!r} — orphan with no hash, skipping provider delete')
                log_repair_activity(
                    broken_nzb_id=info_hash or entry_name,
                    broken_nzb_title=entry_name,
                    outcome='not_found',
                    triggered_by=triggered_by,
                )
                summary['not_found'] += 1
                continue

            # An active exact-identity playback repair already owns this item —
            # let it keep working the candidate it already submitted rather
            # than launching a second, uncoordinated repair through this
            # general pipeline. Without this, whichever candidate happens to
            # collect first silently overwrites filled_by_torrent_id and
            # orphans the other — leaving the minimal repair's own tracking
            # row spinning on candidate_source_changed indefinitely, and
            # wasting a full duplicate download in the meantime. Once the
            # minimal repair finishes (success or eventual give-up), its row
            # moves to status='complete' and has_active_exact_repair stops
            # matching, so this falls through to the general pipeline again
            # as a natural fallback.
            if playback_target and _has_pending_exact_playback_repair(playback_target, info_hash):
                logger.info(
                    '[NZBPlayback] Item %s already has an active exact playback repair; '
                    'skipping general repair to avoid a competing candidate (uuid=%s file=%s)',
                    playback_target['cli_debrid_id'], info_hash, playback_target['file_name'],
                )
                continue

            # Skip if all items already in Adding (repair already in progress)
            if all(i.get('state') == 'Adding' for i in db_items):
                logger.info(f'[NZBRepair] {entry_name!r} — all items already in Adding, skipping')
                continue

            # Filter out items already in Adding
            db_items = [i for i in db_items if i.get('state') != 'Adding']
            if not db_items:
                continue

            summary['matched'] += len(db_items)

            # --- Step 1b: Verify file is actually unreadable before repairing ---
            # cli_mount's NNTP STAT check can produce false positives (server hiccups,
            # temporary routing issues). Read the first 1MB via subprocess with a hard
            # timeout — safe on FUSE mounts, won't block the main process.
            #
            # IMPORTANT: Only check readability when location_on_disk actually points
            # to the broken NZB entry. If location_on_disk was overwritten by a stale
            # repair or upgrade (pointing to a different healthy version), the check
            # would incorrectly report the file as readable and skip a real repair.
            # Guard: verify the broken entry name is referenced in location_on_disk.
            _loc = db_items[0].get('location_on_disk', '') if db_items else ''
            _failure_reason = entry.get('failure_reason', '')
            # Skip readability check when cli_mount explicitly reports usenet_segment_missing
            # — that is a definitive diagnosis, not a transient server hiccup.
            _skip_readability = bool(playback_target) or _failure_reason == 'usenet_segment_missing'
            if not _skip_readability:
                # Also skip when location_on_disk points to a different version.
                # Compare the parent folder against entry_name — mismatch means
                # location_on_disk was overwritten by a stale repair/upgrade.
                import os as _os_rep
                _loc_folder = _os_rep.path.basename(_os_rep.path.dirname(_loc)) if _loc else ''
                _loc_matches_entry = bool(_loc_folder and entry_name and _loc_folder == entry_name)
                if _loc and not _loc_matches_entry:
                    logger.info(
                        f'[NZBRepair] {entry_name!r} — location_on_disk folder differs from '
                        f'entry name, skipping readability check.'
                    )
                    _skip_readability = True
            if _loc and not _skip_readability:
                if _verify_file_readable(_loc):
                    logger.info(
                        f'[NZBRepair] {entry_name!r} — marked broken by provider but file '
                        f'is readable on mount. Skipping repair (false positive).'
                    )
                    summary.setdefault('skipped_playable', 0)
                    summary['skipped_playable'] += 1
                    continue
            if _skip_readability:
                logger.info(
                    f'[NZBRepair] {entry_name!r} — bypassing readability check '
                    f'(reason: {_failure_reason or "location mismatch"}).'
                )

            # --- Step 2: Check backoff/attempt limit ---
            repair_state = ({'attempts': 0, 'give_up': False}
                            if playback_target else get_repair_state(info_hash or entry_name))
            attempts = repair_state['attempts']

            if not playback_target and repair_state['give_up']:
                logger.warning(
                    f'[NZBRepair] {entry_name!r} — reached max attempts ({MAX_REPAIR_ATTEMPTS}), '
                    f'requires manual intervention'
                )
                for db_item in db_items:
                    log_repair_activity(
                        item_id=db_item.get('id'),
                        title=db_item.get('title'),
                        media_type=db_item.get('type'),
                        season_number=db_item.get('season_number'),
                        episode_number=db_item.get('episode_number'),
                        broken_nzb_id=info_hash or entry_name,
                        broken_nzb_title=entry_name,
                        outcome='skipped_max_attempts',
                        triggered_by=triggered_by,
                        repair_attempts=attempts,
                    )
                summary['skipped_max_attempts'] += 1
                continue

            if not playback_target and is_in_backoff(info_hash or entry_name):
                logger.info(f'[NZBRepair] {entry_name!r} — in backoff period, skipping this cycle')
                summary['skipped_backoff'] += 1
                continue

            new_attempts = attempts + 1

            # --- Step 3: Blacklist broken NZB URL + segment ID ---
            # segment_id read from DB — no extra HTTP fetch needed
            _broken_segment_id = db_items[0].get('nzb_segment_id', '') if db_items else ''
            _blacklist_broken_nzb(nzb_url, segment_id=_broken_segment_id)

            # --- Step 4: Scrape for replacement ---
            rep = db_items[0]
            broken_nzb_title = rep.get('debrid_folder_name') or rep.get('filled_by_file') or entry_name
            candidates = _scrape_for_replacement(rep, broken_nzb_title, version_override=version_override)

            if playback_target:
                repair_id = begin_playback_repair(rep, playback_target, triggered_by=triggered_by)
                if not repair_id:
                    logger.error('[NZBPlayback] State persistence failed; submitting nothing for item %s', rep.get('id'))
                    summary['errors'] += 1
                    continue
                unique_candidates = []
                seen_keys = set()
                for candidate in candidates:
                    keys = candidate_keys(candidate)
                    if not keys or keys & seen_keys:
                        continue
                    seen_keys |= keys
                    if candidate_is_excluded(rep['id'], candidate):
                        logger.info('[NZBPlayback] Excluding original/failed candidate %r', candidate.get('title'))
                        continue
                    unique_candidates.append(candidate)
                candidates = unique_candidates

            if not candidates:
                # No replacement found — leave Collected items in place (file may still be
                # playable) and let the repair backoff handle retrying. Moving Collected items
                # to Wanted would trigger the normal scraping queue to hit indexers immediately,
                # doubling the spam. Non-Collected items (Checking/Adding) have no file on disk
                # yet so they are moved to Wanted for a fresh scrape.
                next_repair_at = calculate_next_repair_at(new_attempts)
                if playback_target:
                    logger.warning('[NZBPlayback] No distinct candidate for item %s; exact original remains mounted', rep['id'])
                    summary['no_replacement'] += 1
                    continue
                for db_item in db_items:
                    if db_item.get('state') == 'Collected':
                        logger.warning(f'[NZBRepair] No replacement found for {entry_name!r} — keeping Collected, backoff until {next_repair_at}')
                    else:
                        _move_to_wanted(db_item)
                        logger.warning(f'[NZBRepair] No replacement found for {entry_name!r} — moving to Wanted (no file on disk)')
                    log_repair_activity(
                        item_id=db_item.get('id'),
                        title=db_item.get('title'),
                        media_type=db_item.get('type'),
                        season_number=db_item.get('season_number'),
                        episode_number=db_item.get('episode_number'),
                        broken_nzb_id=info_hash or entry_name,
                        broken_nzb_title=broken_nzb_title,
                        outcome='no_replacement',
                        triggered_by=triggered_by,
                        repair_attempts=new_attempts,
                        next_repair_at=next_repair_at,
                    )
                summary['no_replacement'] += 1
                continue

            # --- Step 5: Submit replacement and confirm in queue ---
            best = None
            new_job_id = None
            named_title = ''
            candidates_to_try = candidates if playback_target else candidates[:1]
            for candidate in candidates_to_try:
                job_id, submitted_title, candidate_disposition = _submit_and_confirm_replacement(
                    candidate, rep.get('title', ''), item=rep)
                if playback_target and candidate_disposition in ('failed', 'rejected'):
                    record_failed_candidate(rep['id'], candidate, job_id or '')
                    logger.warning(
                        '[NZBPlayback] Candidate rejected (%s); trying next distinct result: %r',
                        candidate_disposition, candidate.get('title'),
                    )
                    continue
                best = candidate
                new_job_id = job_id
                named_title = submitted_title
                break

            if best is not None and named_title:
                best = dict(best)
                # cli_mount may sanitize/rename the submitted release.  Keep
                # the indexer's original title as the durable exclusion key so
                # a later repair cycle cannot submit the same NZB again under
                # its pre-submission name.
                best.setdefault('original_title', best.get('title', ''))
                best['title'] = named_title

            if not new_job_id:
                if playback_target:
                    logger.warning('[NZBPlayback] All distinct candidates failed submission for item %s', rep['id'])
                    summary['submission_failed'] += 1
                    continue
                # Submission failed — keep item in Collected state (file still exists/plays).
                # Moving to Wanted here causes re-scrape → re-submit → new cli_mount entry
                # created each cycle, producing duplicate entries in the mount.
                # The backoff system will retry on the next scheduled repair cycle.
                logger.warning(f'[NZBRepair] Replacement submission failed for {entry_name!r} — keeping Collected, will retry with backoff')
                next_repair_at = calculate_next_repair_at(new_attempts)
                for db_item in db_items:
                    log_repair_activity(
                        item_id=db_item.get('id'),
                        title=db_item.get('title'),
                        media_type=db_item.get('type'),
                        season_number=db_item.get('season_number'),
                        episode_number=db_item.get('episode_number'),
                        broken_nzb_id=info_hash or entry_name,
                        broken_nzb_title=broken_nzb_title,
                        outcome='submission_failed',
                        triggered_by=triggered_by,
                        repair_attempts=new_attempts,
                        next_repair_at=next_repair_at,
                    )
                summary['submission_failed'] += 1
                continue

            if playback_target:
                # Submission/collection is provisional.  Keep Plex, its symlink,
                # and every broken mounted file until the completion worker proves
                # this exact UUID healthy and finishes exact acknowledgements.
                if not set_playback_candidate(rep['id'], best, new_job_id, named_title or best.get('title', '')):
                    record_failed_candidate(rep['id'], best, new_job_id)
                    logger.error('[NZBPlayback] Could not persist accepted candidate %s; DB item unchanged', new_job_id)
                    summary['errors'] += 1
                    continue
                if not _update_db_for_repair(rep, new_job_id, best, candidates, preserve_location=True):
                    logger.error('[NZBPlayback] Could not move accepted candidate %s into Adding', new_job_id)
                    summary['errors'] += 1
                    continue
                summary.setdefault('pending_verification', 0)
                summary['pending_verification'] += 1
                logger.info(
                    '[NZBPlayback] Candidate %s accepted provisionally for item %s; old file and Plex retained',
                    new_job_id, rep['id'],
                )
                continue

            # --- REPLACEMENT CONFIRMED — now safe to delete broken ---

            # When more than one db item was matched to this one broken entry (a
            # season-pack-scope repair), the candidate search above was still scoped
            # to just rep's single episode — so `best` can be a single-episode file
            # that only actually covers one of the matched items, not all of them.
            # Applying it to every matched item would silently mark siblings
            # "replaced" while pointing at the wrong episode's content. Narrow the
            # update to only the item(s) `best` genuinely covers; a real season-pack
            # candidate (no specific episode numbers) still satisfies everyone, and
            # any item left out here simply stays untouched for its own repair on a
            # future cycle — never guessed at.
            matched_items = db_items
            if len(db_items) > 1:
                _best_parsed = best.get('parsed_info', {}) or {}
                _best_is_pack = bool(_best_parsed.get('seasons')) and not _best_parsed.get('episodes')
                if not _best_is_pack:
                    _covered_eps = set(_best_parsed.get('episodes') or [])
                    if _covered_eps:
                        _scoped = [d for d in db_items if d.get('episode_number') in _covered_eps]
                        matched_items = _scoped or [rep]
                    else:
                        matched_items = [rep]
                    if len(matched_items) < len(db_items):
                        logger.info(
                            f'[NZBRepair] {best.get("title")!r} only covers item(s) '
                            f'{[d["id"] for d in matched_items]} of the {len(db_items)} matched to '
                            f'{entry_name!r} — leaving the rest untouched for their own repair cycle'
                        )
            db_items = matched_items

            # Step 6: Bulk delete all items from Plex in ONE request (Symlinked/Local only)
            # Step 7: Delete broken from provider (once per entry)
            # Step 8: Update DB to Adding with new torrent_id
            #
            # Symlinked/Local mode: get_symlink_path is deterministic
            # (title/season/episode/version, not the specific release), so a
            # replacement always lands at the exact same symlink path as the
            # broken original — the rescan the Checking queue already
            # triggers once the new symlink exists updates the existing Plex
            # item in place (keeping addedAt/watch history) instead of it
            # reappearing as a fresh "recently added" entry. Deleting first
            # only orphans that match. Same reasoning already applied to
            # retry_exhausted_item's manual-retry path.
            #
            # Plex mode is NOT the same: per _symlink_matches (database/
            # nzb_playback_repair.py), location_on_disk there is the real
            # mounted file path as Plex's own API reports it — there is no
            # cli_debrid-owned symlink or deterministic path to key off, so a
            # different replacement release genuinely is a different path
            # from Plex's perspective. Skipping the delete there would risk
            # leaving the dead entry orphaned alongside the new one instead
            # of fixing anything, so Plex mode keeps the existing
            # delete-then-recreate behavior.
            symlinked_local = get_setting('File Management', 'file_collection_management') == 'Symlinked/Local'

            if not symlinked_local:
                plex_results = _bulk_delete_from_plex(db_items)
            else:
                plex_results = {}

            # Step 7: Delete from provider (once, after Plex bulk delete)
            #
            # info_hash here is whatever cli_mount's health check reported for
            # THIS specific broken entry — but that can be a season-pack job
            # hash shared by files cli_mount never flagged as broken (e.g. one
            # file in a multi-episode NZB loses its segments while the rest
            # of the post is still intact). db_items only covers the item(s)
            # this specific entry matched; other live items can still be
            # relying on the exact same torrent_id. Confirmed live: deleting
            # unconditionally took out an entire otherwise-healthy 10-episode
            # season pack over one flagged file. Same sibling check already
            # applied to retry_exhausted_item's manual-retry path.
            _repair_ids = {d['id'] for d in db_items}
            _sibling_count = 0
            if info_hash:
                conn = get_db_connection()
                try:
                    _placeholders = ','.join('?' * len(_repair_ids)) if _repair_ids else '0'
                    _sibling_count = conn.execute(
                        f"SELECT COUNT(*) FROM media_items WHERE filled_by_torrent_id = ? "
                        f"AND state IN ('Collected','Upgrading','Checking','Adding') AND id NOT IN ({_placeholders})",
                        (f'nzb:{info_hash}', *_repair_ids)
                    ).fetchone()[0]
                finally:
                    conn.close()

            if _sibling_count == 0:
                provider_deleted = _delete_from_provider(info_hash, entry_name)
                if not provider_deleted:
                    logger.warning(f'[NZBRepair] Provider delete failed for {entry_name!r} — continuing anyway')
            else:
                logger.info(
                    f'[NZBRepair] Skipping provider delete for {info_hash!r} — '
                    f'{_sibling_count} sibling(s) still rely on it'
                )

            for db_item in db_items:
                item_id = db_item['id']
                if not symlinked_local:
                    plex_deleted = plex_results.get(item_id, False)
                    if not plex_deleted:
                        logger.warning(f'[NZBRepair] Plex delete failed/skipped for item {item_id} ({db_item.get("title")!r})')

                # Step 8: Update DB in-place
                _update_db_for_repair(db_item, new_job_id, best, candidates[1:])

                log_repair_activity(
                    item_id=item_id,
                    title=db_item.get('title'),
                    media_type=db_item.get('type'),
                    season_number=db_item.get('season_number'),
                    episode_number=db_item.get('episode_number'),
                    broken_nzb_id=info_hash or entry_name,
                    broken_nzb_title=broken_nzb_title,
                    replacement_nzb_id=new_job_id,
                    replacement_title=best.get('title'),
                    outcome='replaced',
                    triggered_by=triggered_by,
                    repair_attempts=new_attempts,
                )
                summary['replaced'] += 1
                logger.info(
                    f'[NZBRepair] ✓ Repaired item {item_id} ({db_item.get("title")!r}) '
                    f'→ {best.get("title")!r} (job_id={new_job_id})'
                )

        except Exception as e:
            logger.error(f'[NZBRepair] Unhandled error for entry {entry_name!r}: {e}', exc_info=True)
            summary['errors'] += 1

    logger.info(f'[NZBRepair] Repair cycle complete: {summary}')
    return summary
