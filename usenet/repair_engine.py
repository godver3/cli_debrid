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
)
from utilities.settings import get_setting

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

    # Filter out debrid torrent entries when debrid naming is enabled,
    # as renaming causes false positives in cli_mount health checks.
    try:
        from utilities.settings import get_setting as _gs
        if _gs('Debrid Provider', 'enable_debrid_naming', False):
            before = len(items)
            items = [e for e in items if (e.get('protocol') or '').lower() != 'torrent']
            if len(items) < before:
                logger.info(f'[NZBRepair] Skipped {before - len(items)} debrid torrent entries (debrid naming enabled)')
    except Exception:
        pass

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
        from database.not_wanted_magnets import add_to_not_wanted_nzb_guid, add_to_not_wanted_nzb_segment
        if nzb_url:
            add_to_not_wanted_nzb_guid(nzb_url)
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

def _submit_and_confirm_replacement(result: dict, title: str, item: dict = None) -> Tuple[Optional[str], str]:
    """
    Submit replacement NZB to provider.
    Polls up to 30s to confirm it appears in the provider queue.
    Returns (job_id, release_title) or (None, '') on failure.
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
            return None, ''

        if not job_id:
            logger.warning(f'[NZBRepair] Provider rejected submission for {release_title!r}')
            return None, ''

        # Poll up to 30s to confirm job appears in provider queue
        confirmed = False
        for attempt in range(6):
            time.sleep(5)
            try:
                status = client.get_job_status(job_id)
                if status and status.get('state') not in ('failed', 'error', 'unknown'):
                    confirmed = True
                    break
                if status and status.get('state') in ('failed', 'error'):
                    logger.warning(f'[NZBRepair] Replacement job {job_id} immediately failed: {status}')
                    break
            except Exception:
                pass

        if confirmed:
            logger.info(f'[NZBRepair] Replacement confirmed in queue: job_id={job_id} title={release_title!r}')
            return job_id, release_title
        else:
            # Job submitted but couldn't confirm — treat as submitted anyway
            # (provider may have completed instantly if already cached)
            logger.info(f'[NZBRepair] Replacement submitted (unconfirmed): job_id={job_id} title={release_title!r}')
            return job_id, release_title

    except Exception as e:
        logger.error(f'[NZBRepair] _submit_and_confirm_replacement error: {e}')
        return None, ''


# ---------------------------------------------------------------------------
# DB update for repaired item
# ---------------------------------------------------------------------------

def _update_db_for_repair(item: dict, new_job_id: str, replacement_result: dict, all_results: list) -> bool:
    """Update DB item in-place: state=Adding, new torrent_id, clear location.
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
        remaining = [_strip_prefetched(r) for r in all_results if r.get('title') != replacement_result.get('title')]

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
        update_media_item(item_id, location_on_disk=None, fall_back_to_single_scraper=False)
        logger.info(f'[NZBRepair] DB updated item {item_id}: state=Adding torrent_id={new_torrent_id} segment={new_segment_id!r}')
        return True
    except Exception as e:
        logger.error(f'[NZBRepair] _update_db_for_repair error: {e}')
        return False


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

        # Resolve job UUID from provider if not in health response
        if not info_hash and entry_name:
            info_hash = _resolve_info_hash_from_provider(entry_name)
            if info_hash:
                logger.info(f'[NZBRepair] Resolved job UUID={info_hash!r} for {entry_name!r}')

        logger.info(f'[NZBRepair] Processing broken entry: {entry_name!r} hash={info_hash!r}')

        try:
            # --- Step 1: Match to CLI DB ---
            db_items = []
            if info_hash:
                single = _find_db_item_by_info_hash(info_hash)
                if single:
                    db_items = [single]

            # Fuzzy fallback only when hash is not authoritative (nzbdav sets hash_is_authoritative)
            if not db_items and entry_name and not entry.get('hash_is_authoritative'):
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
                if info_hash:
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
            _skip_readability = _failure_reason == 'usenet_segment_missing'
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
            repair_state = get_repair_state(info_hash or entry_name)
            attempts = repair_state['attempts']

            if repair_state['give_up']:
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

            if is_in_backoff(info_hash or entry_name):
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

            if not candidates:
                # No replacement found — leave Collected items in place (file may still be
                # playable) and let the repair backoff handle retrying. Moving Collected items
                # to Wanted would trigger the normal scraping queue to hit indexers immediately,
                # doubling the spam. Non-Collected items (Checking/Adding) have no file on disk
                # yet so they are moved to Wanted for a fresh scrape.
                next_repair_at = calculate_next_repair_at(new_attempts)
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
            best = candidates[0]
            new_job_id, named_title = _submit_and_confirm_replacement(best, rep.get('title', ''), item=rep)

            if named_title:
                best = dict(best)
                best['title'] = named_title

            if not new_job_id:
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

            # --- REPLACEMENT CONFIRMED — now safe to delete broken ---

            # Step 6: Bulk delete all items from Plex in ONE request (avoids WAL bloat)
            # Step 7: Delete broken from provider (once per entry)
            # Step 8: Update DB to Adding with new torrent_id

            plex_results = _bulk_delete_from_plex(db_items)

            # Step 7: Delete from provider (once, after Plex bulk delete)
            provider_deleted = _delete_from_provider(info_hash, entry_name)
            if not provider_deleted:
                logger.warning(f'[NZBRepair] Provider delete failed for {entry_name!r} — continuing anyway')

            for db_item in db_items:
                item_id = db_item['id']
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
