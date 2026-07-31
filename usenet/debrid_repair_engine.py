"""
Debrid (RealDebrid torrent) Health & Repair Engine

Handles health monitoring and repair of broken RealDebrid torrent entries
tracked via cli_mount's /api/repair/health endpoint (protocol=torrent).

Repair workflow:
  1. Fetch broken torrent entries from cli_mount /api/repair/health
     (protocol=torrent, status=broken)
  2. If failure_reason == 'missing_provider_link': re-insert via
     /api/repair/health/{name}/check (re-triggers cli_mount's link resolver)
  3. Otherwise: scrape for a replacement torrent, delete from Plex + cli_mount,
     reset CLI DB item to Adding with new torrent info
  4. Log all outcomes to nzb_repair_activity with broken_nzb_id='debrid:{hash}'
"""

import logging
import re
import threading as _threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

# Module-level lock — prevents concurrent debrid repair runs
_repair_lock = _threading.Lock()


# ---------------------------------------------------------------------------
# cli_mount client helpers
# ---------------------------------------------------------------------------

def _get_client():
    """Return the active cli_mount client."""
    from usenet.climount_client import get_climount_client
    return get_climount_client()


def _dcy_cfg():
    from utilities.settings import get_setting
    url = get_setting('Usenet Provider', 'url', default='').rstrip('/')
    token = get_setting('Usenet Provider', 'api_token', default='')
    return url, token


def _dcy_headers():
    _, token = _dcy_cfg()
    return {'Authorization': f'Bearer {token}'} if token else {}




# ---------------------------------------------------------------------------
# Provider health API (torrent protocol only)
# ---------------------------------------------------------------------------

def fetch_broken_items() -> list:
    """Return broken torrent-protocol entries from cli_mount /api/repair/health.
    Each entry is tagged with is_ghost=True if its folder does not exist in the mount."""
    try:
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return []
        r = api.get(f'{url}/api/repair/health', headers=_dcy_headers(), timeout=60)
        if r.status_code != 200:
            logger.warning(f'[DebridRepair] /api/repair/health HTTP {r.status_code}')
            return []
        data = r.json()
        entries = data if isinstance(data, list) else data.get('entries', data.get('items', []))
        broken_torrent = [
            e for e in entries
            if (e.get('status') or '').lower() == 'broken'
            and (e.get('protocol') or '').lower() == 'torrent'
        ]

        # Tag ghosts via browse API — only entries that exist in the mount appear there
        mount_names = set()
        try:
            page = 1
            while True:
                br = api.get(f'{url}/api/browse/torrents', headers=_dcy_headers(),
                             params={'limit': 500, 'page': page}, timeout=30)
                if br.status_code != 200:
                    break
                bd = br.json()
                for e in bd.get('entries', []):
                    n = e.get('name', '')
                    if n:
                        mount_names.add(n)
                if page >= bd.get('total_pages', 1):
                    break
                page += 1
        except Exception:
            pass

        for e in broken_torrent:
            if e.get('failure_reason') == 'missing_provider_link':
                name = (e.get('entry_name') or '').strip()
                folder = name
                for ext in ('.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts'):
                    if folder.lower().endswith(ext):
                        folder = folder[:-len(ext)]
                        break
                e['is_ghost'] = folder not in mount_names
            else:
                e['is_ghost'] = False

        logger.info(f'[DebridRepair] fetch_broken_items: {len(broken_torrent)} broken torrent entries')
        return broken_torrent
    except Exception as e:
        logger.error(f'[DebridRepair] fetch_broken_items error: {e}')
        return []


def get_health_summary() -> dict:
    """Return health-state counts for torrent-protocol entries only."""
    try:
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return {}
        r = api.get(f'{url}/api/repair/health', headers=_dcy_headers(), timeout=60)
        if r.status_code != 200:
            return {}
        data = r.json()
        entries = data if isinstance(data, list) else data.get('entries', data.get('items', []))
        torrent_entries = [e for e in entries if (e.get('protocol') or '').lower() == 'torrent']
        counts: dict = {}
        for e in torrent_entries:
            s = (e.get('status') or 'unknown').lower()
            counts[s] = counts.get(s, 0) + 1
        return counts
    except Exception as e:
        logger.debug(f'[DebridRepair] get_health_summary error: {e}')
        return {}


def trigger_health_scan(full: bool = False, wait: bool = False, timeout: int = 300) -> bool:
    """POST to cli_mount /api/repair/run?protocol=torrent to scan torrent entries.
    full=True scans all files; full=False (default) only scans unchecked files.
    wait=True blocks until the scan completes (up to timeout seconds)."""
    try:
        import time as _t
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return False
        params = 'protocol=torrent'
        if full:
            params += '&ignore_last_checked=true'
        r = api.post(f'{url}/api/repair/run?{params}', headers=_dcy_headers(), timeout=30)
        if r.status_code not in (200, 202, 204):
            return False
        if not wait:
            return True
        run_id = (r.json() or {}).get('run_id', '')
        if not run_id:
            return True
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            _t.sleep(30)
            try:
                sr = api.get(f'{url}/api/repair/runs/{run_id}', headers=_dcy_headers(), timeout=10)
                if sr.status_code == 200:
                    status = (sr.json() or {}).get('status', '')
                    if status in ('completed', 'failed', 'error'):
                        logger.info(f'[DebridRepair] Torrent health scan completed: status={status}')
                        return True
            except Exception:
                pass
        logger.warning('[DebridRepair] Torrent health scan wait timed out')
        return True
    except Exception as e:
        logger.warning(f'[DebridRepair] trigger_health_scan error: {e}')
        return False


# ---------------------------------------------------------------------------
# DB lookup — same strategies as usenet repair_engine lines 291-380
# ---------------------------------------------------------------------------

# Sentinel returned by _find_db_items_by_entry_name when a fuzzy strategy matched
# multiple rows that could not be disambiguated. Callers MUST check for this
# explicitly and treat it as "skip this entry" — NOT as "no DB item / orphan".
# Falling through to orphan-delete logic on this result would delete a cli_mount
# entry that a live (but unidentified) row still depends on.
AMBIGUOUS = object()


def _disambiguate_by_hash(rows: list, info_hash: str):
    """
    When a fuzzy match strategy returns multiple rows (e.g. two versions of the
    same movie), narrow to the row(s) whose filled_by_magnet actually contains
    info_hash. If that still doesn't resolve to a single row — including when
    info_hash itself is unknown, in which case there is no way to disambiguate
    at all — return the AMBIGUOUS sentinel rather than an empty list or all rows
    unfiltered. Acting on the wrong version's row (or returning every row for the
    caller to pick db_items[0] from) can delete another live version's file.
    """
    if len(rows) <= 1:
        return rows
    if not info_hash:
        # No hash to correlate against — genuinely can't tell these rows apart.
        return AMBIGUOUS
    _hash_lower = info_hash.lower()
    _matched = [r for r in rows if _hash_lower in (r.get('filled_by_magnet') or '').lower()]
    if len(_matched) == 1:
        return _matched
    if not _matched:
        # None of the fuzzy-matched rows correlate to this hash — ambiguous, refuse to guess.
        return AMBIGUOUS
    # Multiple rows share this hash (e.g. legitimate season-pack siblings) — fine to
    # return them all; callers that need one pick db_items[0] deliberately for that case.
    return _matched


def _find_db_items_by_entry_name(entry_name: str, info_hash: str = ''):
    """
    Find all DB items whose files belong to this cli_mount entry.
    Falls through multiple strategies from most to least precise.
    Mirrors usenet repair_engine._find_db_items_by_entry_name exactly.

    info_hash (optional): when a fuzzy strategy (4 or 6) returns more than one
    row — e.g. two versions of the same movie/episode sharing an imdb_id — this
    is used to disambiguate via filled_by_magnet rather than picking arbitrarily.

    Returns a list (possibly empty = genuinely not found), OR the AMBIGUOUS
    sentinel if a fuzzy strategy matched multiple rows it couldn't tell apart.
    Callers MUST check for AMBIGUOUS explicitly and skip the entry — treating it
    as an empty list would trigger orphan-delete logic and destroy a file a live
    row still needs.
    """
    if not entry_name:
        return []
    try:
        from database.core import get_db_connection
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

            # Strategy 4: IMDB ID + episode number from entry_name pattern.
            # These match on imdb_id (+ S/E for episodes) only — if two versions
            # (e.g. 1080p/4k) are both live, this can return both rows. Disambiguate
            # via info_hash correlation rather than acting on an arbitrary one.
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
                    return _disambiguate_by_hash([dict(r) for r in rows], info_hash)
            elif imdb_match and not ep_match:
                imdb_id = imdb_match.group(1)
                rows = conn.execute(
                    f"SELECT * FROM media_items WHERE imdb_id = ? AND type = 'movie' AND state IN {_LIVE}",
                    (imdb_id,),
                ).fetchall()
                if rows:
                    return _disambiguate_by_hash([dict(r) for r in rows], info_hash)

            # Strategy 5: original_scraped_torrent_title match
            rows = conn.execute(
                f"SELECT * FROM media_items WHERE original_scraped_torrent_title = ? AND state IN {_LIVE}",
                (entry_name,),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]

            # Strategy 6: Episode SxxExx + title prefix (last resort)
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
                        return _disambiguate_by_hash([dict(r) for r in rows], info_hash)

            return []
        except Exception as e:
            logger.debug(f'[DebridRepair] DB entry lookup error for {entry_name!r}: {e}')
            return []
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f'[DebridRepair] DB connection error: {e}')
        return []


# ---------------------------------------------------------------------------
# cli_mount delete (torrent entries use DELETE /api/browse/torrents/{hash})
# ---------------------------------------------------------------------------

def _delete_from_climount(info_hash: str, entry_name: str = '') -> bool:
    """Delete a torrent entry from cli_mount via DELETE /api/browse/torrents/{hash}."""
    if not info_hash:
        logger.warning(f'[DebridRepair] _delete_from_climount: no hash for {entry_name!r}')
        return False
    try:
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return False
        r = api.delete(
            f'{url}/api/browse/torrents/{info_hash}',
            headers=_dcy_headers(),
            timeout=15,
        )
        if r.status_code in (200, 204):
            logger.info(f'[DebridRepair] Deleted torrent {info_hash} from cli_mount')
            return True
        if r.status_code == 404:
            logger.info(f'[DebridRepair] Torrent {info_hash} already gone from cli_mount (404)')
            return True
        logger.warning(f'[DebridRepair] delete torrent {info_hash} returned HTTP {r.status_code}')
        return False
    except Exception as e:
        logger.warning(f'[DebridRepair] _delete_from_climount error for {info_hash!r}: {e}')
        return False


# ---------------------------------------------------------------------------
# Ghost health record cleanup
# ---------------------------------------------------------------------------

def delete_ghost_health_records() -> dict:
    """Delete cli_mount health records for ghost entries.

    A ghost is a broken (missing_provider_link) torrent entry whose folder does NOT
    exist in the cli_mount mount. Detection uses the browse API — it only returns
    entries that actually exist in the mount, so any broken entry whose name is absent
    from browse results is a ghost.

    Returns dict: {deleted, skipped, errors}
    """
    import urllib.parse as _up
    from routes.api_tracker import api

    url, _ = _dcy_cfg()
    if not url:
        return {'deleted': 0, 'skipped': 0, 'errors': 0}

    # Fetch all broken missing_provider_link torrent entries from health API
    try:
        r = api.get(f'{url}/api/repair/health', headers=_dcy_headers(), timeout=60)
        if r.status_code != 200:
            logger.warning(f'[DebridRepair] ghost cleanup: health API returned {r.status_code}')
            return {'deleted': 0, 'skipped': 0, 'errors': 0}
        all_entries = r.json()
        if not isinstance(all_entries, list):
            all_entries = all_entries.get('entries', [])
    except Exception as e:
        logger.warning(f'[DebridRepair] ghost cleanup: failed to fetch health: {e}')
        return {'deleted': 0, 'skipped': 0, 'errors': 0}

    broken = [
        e for e in all_entries
        if (e.get('protocol') or '').lower() == 'torrent'
        and e.get('status') == 'broken'
        and e.get('failure_reason') == 'missing_provider_link'
    ]

    if not broken:
        return {'deleted': 0, 'skipped': 0, 'errors': 0}

    # Build set of names that exist in the mount via the browse API.
    # The browse API only returns entries whose folder exists in the mount,
    # so any broken entry absent from this set is a ghost.
    mount_names = set()
    try:
        page = 1
        while True:
            br = api.get(f'{url}/api/browse/torrents', headers=_dcy_headers(),
                         params={'limit': 500, 'page': page}, timeout=30)
            if br.status_code != 200:
                break
            bd = br.json()
            for e in bd.get('entries', []):
                n = e.get('name', '')
                if n:
                    mount_names.add(n)
            if page >= bd.get('total_pages', 1):
                break
            page += 1
        logger.debug(f'[DebridRepair] ghost cleanup: {len(mount_names)} entries in mount')
    except Exception as e:
        logger.warning(f'[DebridRepair] ghost cleanup: failed to fetch browse list: {e}')
        return {'deleted': 0, 'skipped': 0, 'errors': 0}

    deleted = skipped = errors = 0
    for entry in broken:
        name = (entry.get('entry_name') or '').strip()
        if not name:
            continue
        # Strip file extension — mount folders have no extension
        folder_name = name
        for ext in ('.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts'):
            if folder_name.lower().endswith(ext):
                folder_name = folder_name[:-len(ext)]
                break
        if folder_name in mount_names:
            logger.debug(f'[DebridRepair] ghost cleanup: in mount, skipping {name!r}')
            skipped += 1
            continue
        # Not in mount → ghost. Delete only the health record.
        encoded = _up.quote(name, safe='')
        try:
            d = api.delete(f'{url}/api/repair/health/{encoded}', headers=_dcy_headers(), timeout=15)
            if d.status_code == 200:
                deleted += 1
                logger.info(f'[DebridRepair] Deleted ghost health record: {name!r}')
            else:
                logger.warning(f'[DebridRepair] Failed to delete ghost health record {name!r}: HTTP {d.status_code}')
                errors += 1
        except Exception as e:
            logger.warning(f'[DebridRepair] Error deleting ghost health record {name!r}: {e}')
            errors += 1

    logger.info(f'[DebridRepair] Ghost cleanup: deleted={deleted}, skipped={skipped}, errors={errors}')
    return {'deleted': deleted, 'skipped': skipped, 'errors': errors}


# ---------------------------------------------------------------------------
# Plex delete
# ---------------------------------------------------------------------------

def _delete_from_plex(item: dict) -> bool:
    """Delete a media item from Plex. Reuses the usenet repair_engine's implementation."""
    try:
        from usenet.repair_engine import _delete_from_plex as _nzb_delete_from_plex
        return _nzb_delete_from_plex(item)
    except Exception as e:
        logger.warning(f'[DebridRepair] _delete_from_plex error for {item.get("title")!r}: {e}')
        return False


def _delete_from_plex_by_entry_name(entry_name: str) -> bool:
    """
    Delete an orphan cli_mount entry from Plex by scanning its specific folder path
    and emptying trash for that section.

    Runs in a background thread with a 15s delay after the scan to give Plex
    enough time to detect the missing file before emptying trash.
    """
    if not entry_name:
        return False
    try:
        import re as _re
        import threading as _threading
        from utilities.settings import get_setting

        is_episode = bool(_re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', entry_name))
        subfolder = 'shows' if is_episode else 'movies'
        mount = get_setting('Usenet Provider', 'mount_path', '/debrid').rstrip('/')
        folder_path = f'{mount}/{subfolder}/{entry_name}'
        section_type = 'show' if is_episode else 'movie'

        def _do_plex_cleanup():
            import time
            try:
                from utilities.plex_functions import scan_and_empty_plex_trash
                # Step 1: trigger scan
                result = scan_and_empty_plex_trash(
                    paths=[folder_path],
                    section_type=section_type,
                    empty_trash=False,  # scan only first
                )
                if not result.get('paths_scanned'):
                    logger.warning(f'[DebridRepair] Plex scan found no matching section for {folder_path!r}')
                    return
                # Step 2: wait for Plex to detect missing file
                time.sleep(6)
                # Step 3: empty trash
                result2 = scan_and_empty_plex_trash(
                    paths=[folder_path],
                    section_type=section_type,
                    empty_trash=True,
                )
                logger.info(f'[DebridRepair] Plex cleanup complete for {entry_name!r}: {result2}')
            except Exception as e:
                logger.warning(f'[DebridRepair] Plex cleanup thread error for {entry_name!r}: {e}')

        logger.info(f'[DebridRepair] Starting Plex cleanup thread for: {folder_path}')
        _threading.Thread(target=_do_plex_cleanup, daemon=True,
                         name=f'plex-cleanup-{entry_name[:30]}').start()
        return True
    except Exception as e:
        logger.warning(f'[DebridRepair] _delete_from_plex_by_entry_name error for {entry_name!r}: {e}')
        return False


# ---------------------------------------------------------------------------
# Re-insert (trigger cli_mount link re-check)
# ---------------------------------------------------------------------------

def _is_orphan_entry(db_item: dict, info_hash: str) -> bool:
    """
    Return True if the DB item is already collected via a DIFFERENT provider than
    the broken debrid entry. This means CLI has already moved on (e.g. replaced the
    old RD torrent with an NZB) — the cli_mount entry is an orphan.
    We should delete it from cli_mount but NOT touch the DB item.
    """
    if not db_item or not info_hash:
        return False
    state = db_item.get('state', '')
    if state not in ('Collected',):
        return False
    existing_id = db_item.get('filled_by_torrent_id') or ''
    # The broken entry's hash won't appear as-is in filled_by_torrent_id
    # (which stores the RD internal ID like 'I2SICZQ4GPKZK', not the infohash).
    # An orphan is detected when the existing provider is NZB (starts with 'nzb:')
    # or is a completely different torrent ID with no relation to info_hash.
    if existing_id.startswith('nzb:'):
        return True
    # If filled_by_file points to a completely different release name than entry_name,
    # it's also an orphan — but checking that is complex so NZB check covers main case.
    return False


def reinsert_entry(entry_name: str, info_hash: str) -> dict:
    """
    Re-insert a broken torrent directly to the enabled debrid provider.
    After re-insertion triggers immediate cli_mount sync via the new /sync endpoint.

    Orphan detection: if the DB item is already Collected via a different provider
    (e.g. NZB replaced this RD torrent), just delete the orphan from cli_mount
    and do NOT touch the DB item or reset to Wanted.
    """
    try:
        from database.nzb_repair_activity import log_repair_activity

        db_items = _find_db_items_by_entry_name(entry_name, info_hash)
        if db_items is AMBIGUOUS:
            logger.warning(f'[DebridRepair] reinsert_entry {entry_name!r}: ambiguous multi-version match, skipping without deleting')
            log_repair_activity(
                broken_nzb_id=f'debrid:{info_hash}',
                broken_nzb_title=entry_name,
                outcome='ambiguous',
                triggered_by='debrid_repair',
            )
            return {'outcome': 'ambiguous', 'message': 'Multiple versions ambiguously match — refusing to guess which to repair'}
        db_item = db_items[0] if db_items else {}

        # Orphan check — DB item is already collected via a different provider
        if _is_orphan_entry(db_item, info_hash):
            _delete_from_climount(info_hash, entry_name)
            _delete_from_plex_by_entry_name(entry_name)
            logger.info(f'[DebridRepair] reinsert_entry {entry_name!r}: orphan detected '
                        f'(DB item already collected via {db_item.get("filled_by_torrent_id")!r}), '
                        f'deleted from cli_mount and Plex')
            log_repair_activity(
                item_id=db_item.get('id'),
                title=db_item.get('title'),
                media_type=db_item.get('type'),
                season_number=db_item.get('season_number'),
                episode_number=db_item.get('episode_number'),
                broken_nzb_id=f'debrid:{info_hash}',
                broken_nzb_title=entry_name,
                outcome='plex_deleted',
                triggered_by='debrid_repair',
            )
            return {'outcome': 'plex_deleted', 'message': 'Orphan entry removed from cli_mount'}

        # Re-add to the enabled debrid provider directly
        from debrid import get_debrid_provider
        provider = get_debrid_provider()
        if not provider:
            return {'outcome': 'error', 'message': 'No debrid provider configured'}

        # Build magnet from hash
        magnet = db_item.get('filled_by_magnet') or ''
        if not magnet and info_hash:
            magnet = f'magnet:?xt=urn:btih:{info_hash}'
        if not magnet:
            # No magnet and no DB entry — this is an orphan with no way to re-insert
            # Delete from cli_mount and trigger Plex cleanup
            _delete_from_climount(info_hash, entry_name)
            _delete_from_plex_by_entry_name(entry_name)
            logger.info(f'[DebridRepair] reinsert_entry {entry_name!r}: no magnet/DB entry, deleted orphan from cli_mount and Plex')
            log_repair_activity(
                broken_nzb_id=f'debrid:{info_hash}',
                broken_nzb_title=entry_name,
                outcome='plex_deleted',
                triggered_by='debrid_repair',
            )
            return {'outcome': 'plex_deleted', 'message': 'No magnet available — orphan deleted from cli_mount'}

        try:
            new_id = provider.add_torrent(magnet)
        except Exception as add_err:
            err_str = str(add_err)
            if '429' in err_str:
                # RD rate-limited — back off and retry once
                logger.warning(f'[DebridRepair] RD rate limited for {entry_name!r}, retrying after 30s')
                time.sleep(30)
                new_id = provider.add_torrent(magnet)
            else:
                raise
        # None means "already exists on RD" — still trigger sync, treat as success
        already_existed = new_id is None
        outcome = 'reinserted'
        logger.info(f'[DebridRepair] reinsert_entry {entry_name!r}: reinserted '
                    f'new_id={new_id} already_existed={already_existed}')

        log_repair_activity(
            item_id=db_item.get('id'),
            title=db_item.get('title'),
            media_type=db_item.get('type'),
            season_number=db_item.get('season_number'),
            episode_number=db_item.get('episode_number'),
            broken_nzb_id=f'debrid:{info_hash}',
            broken_nzb_title=entry_name,
            outcome=outcome,
            triggered_by='debrid_repair',
        )
        # Wait for RD to report the torrent as downloaded, THEN trigger cli_mount sync.
        # Critical: if we sync before RD marks it downloaded, the 2-minute cli_mount
        # refresh cycle will see the entry missing from RD's downloaded list and DELETE
        # the entry from cli_mount storage — losing all renamed filenames permanently.
        import threading as _t
        _new_id = new_id  # capture for closure
        def _wait_then_sync():
            _url, _ = _dcy_cfg()
            if not _url or not info_hash:
                return
            try:
                from routes.api_tracker import api as _api
                rd_id = _new_id
                # If already_existed, find the current RD torrent ID by hash
                if rd_id is None:
                    try:
                        torrents = provider.list_active_torrents() or []
                        for t in torrents:
                            t_hash = (t.get('hash') or '').lower()
                            if t_hash == info_hash.lower():
                                rd_id = t.get('id')
                                break
                    except Exception as _le:
                        logger.debug(f'[DebridRepair] Could not find RD torrent ID for {info_hash}: {_le}')

                # Poll RD until torrent is downloaded (max 3 minutes)
                if rd_id:
                    for poll in range(18):  # 18 × 10s = 3 minutes
                        time.sleep(10)
                        try:
                            info = provider.get_torrent_info(rd_id)
                            status = (info or {}).get('status', '')
                            logger.debug(f'[DebridRepair] RD status poll {poll+1} for {rd_id}: {status}')
                            if status == 'downloaded':
                                logger.info(f'[DebridRepair] RD torrent {rd_id} is downloaded, triggering cli_mount sync')
                                break
                            elif status in ('error', 'magnet_error', 'dead'):
                                logger.warning(f'[DebridRepair] RD torrent {rd_id} failed with status {status!r}')
                                return
                        except Exception as _pe:
                            logger.debug(f'[DebridRepair] RD poll {poll+1} error: {_pe}')
                    else:
                        logger.warning(f'[DebridRepair] RD torrent {rd_id} not downloaded after 3 min polling, syncing anyway')
                else:
                    # No RD ID — wait 15s as best-effort fallback
                    time.sleep(15)

                # Trigger cli_mount sync using the NEW RD torrent ID so it fetches
                # fresh placement data instead of using the stale old placement ID.
                try:
                    sync_url = f'{_url}/api/torrents/{info_hash}/sync'
                    if rd_id:
                        sync_url += f'?rdId={rd_id}'
                    r = _api.post(sync_url, headers=_dcy_headers(), timeout=15)
                    if r.status_code == 200:
                        logger.info(f'[DebridRepair] cli_mount sync succeeded for {info_hash} (rdId={rd_id})')
                    else:
                        logger.warning(f'[DebridRepair] cli_mount sync returned HTTP {r.status_code} for {info_hash}')
                except Exception as _se:
                    logger.debug(f'[DebridRepair] cli_mount sync error (non-critical): {_se}')
                # Re-probe health so cli_mount re-validates the file link immediately
                # (health cache has next_check_due_at next week — recheck forces it now)
                try:
                    encoded_name = urllib.parse.quote(entry_name, safe='')
                    rr = _api.post(f'{_url}/api/repair/health/{encoded_name}/check',
                                   headers=_dcy_headers(), timeout=30)
                    logger.info(f'[DebridRepair] Recheck triggered for {entry_name!r}: HTTP {rr.status_code}')
                except Exception as _re:
                    logger.debug(f'[DebridRepair] Recheck error (non-critical): {_re}')
                # Trigger Plex scan so Plex picks up the re-inserted file
                try:
                    import re as _re2
                    from utilities.settings import get_setting as _gs
                    is_episode = bool(_re2.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', entry_name))
                    subfolder = 'shows' if is_episode else 'movies'
                    mount = _gs('Usenet Provider', 'mount_path', '/debrid').rstrip('/')
                    folder_path = f'{mount}/{subfolder}/{entry_name}'
                    section_type = 'show' if is_episode else 'movie'
                    from utilities.plex_functions import scan_and_empty_plex_trash
                    result = scan_and_empty_plex_trash(
                        paths=[folder_path],
                        section_type=section_type,
                        empty_trash=False,
                    )
                    logger.info(f'[DebridRepair] Plex scan triggered for {entry_name!r}: {result}')
                except Exception as _pe:
                    logger.debug(f'[DebridRepair] Plex scan error (non-critical): {_pe}')
            except Exception as _e:
                logger.warning(f'[DebridRepair] _wait_then_sync error for {info_hash}: {_e}')
        _t.Thread(target=_wait_then_sync, daemon=True, name=f'sync-{info_hash[:8]}').start()
        msg = 'Already on RD, sync triggered' if already_existed else f'Re-inserted (new_id={new_id})'
        return {'outcome': outcome, 'success': True, 'message': msg}
    except Exception as e:
        logger.error(f'[DebridRepair] reinsert_entry error for {entry_name!r}: {e}')
        return {'outcome': 'error', 'message': str(e)}


# ---------------------------------------------------------------------------
# Replace entry (scrape + submit + delete broken + reset DB)
# ---------------------------------------------------------------------------

def replace_entry(entry_name: str, info_hash: str, version_override: str = None) -> dict:
    """
    Replace a broken torrent entry by calling CLI's existing rescrape_item endpoint.
    This handles: delete from Plex, delete from cli_mount, reset to Wanted, re-scrape.
    Falls back to manual delete+reset if no DB item found.
    """
    try:
        from database.nzb_repair_activity import log_repair_activity

        db_items = _find_db_items_by_entry_name(entry_name, info_hash)
        if db_items is AMBIGUOUS:
            logger.warning(f'[DebridRepair] replace_entry {entry_name!r}: ambiguous multi-version match, skipping without deleting')
            log_repair_activity(
                broken_nzb_id=f'debrid:{info_hash}',
                broken_nzb_title=entry_name,
                outcome='ambiguous',
                triggered_by='debrid_repair',
            )
            return {'outcome': 'ambiguous', 'message': 'Multiple versions ambiguously match — refusing to guess which to repair'}
        db_items = [i for i in db_items if i.get('state') in ('Collected', 'Checking', 'Upgrading', 'Adding')]

        if not db_items:
            # No DB item — just delete from cli_mount
            _delete_from_climount(info_hash, entry_name)
            log_repair_activity(
                broken_nzb_id=f'debrid:{info_hash}',
                broken_nzb_title=entry_name,
                outcome='not_found',
                triggered_by='debrid_repair',
            )
            return {'outcome': 'not_found', 'message': f'No DB item found for {entry_name!r}'}

        db_item = db_items[0]
        item_id = db_item.get('id')

        # Orphan check — DB item already collected via a different provider
        if _is_orphan_entry(db_item, info_hash):
            _delete_from_climount(info_hash, entry_name)
            _delete_from_plex_by_entry_name(entry_name)
            logger.info(f'[DebridRepair] replace_entry {entry_name!r}: orphan detected, deleted from cli_mount and Plex')
            log_repair_activity(
                item_id=item_id,
                title=db_item.get('title'),
                media_type=db_item.get('type'),
                season_number=db_item.get('season_number'),
                episode_number=db_item.get('episode_number'),
                broken_nzb_id=f'debrid:{info_hash}',
                broken_nzb_title=entry_name,
                outcome='plex_deleted',
                triggered_by='debrid_repair',
            )
            return {'outcome': 'plex_deleted', 'message': 'Orphan entry removed from cli_mount'}

        # Delete from Plex and cli_mount first
        _delete_from_plex(db_item)
        _delete_from_climount(info_hash, entry_name)

        # Move to Wanted using the existing move_item_to_wanted function
        # This resets all filled_by fields and triggers re-scrape
        try:
            from routes.debug_routes import move_item_to_wanted
            move_item_to_wanted(item_id, None)
            outcome = 'replaced'
            logger.info(f'[DebridRepair] replace_entry {entry_name!r}: item {item_id} moved to Wanted')
        except Exception as reset_err:
            # Fallback: direct DB update
            try:
                from database.database_writing import update_media_item_state
                update_media_item_state(item_id, 'Wanted')
                outcome = 'replaced'
            except Exception as db_err:
                logger.warning(f'[DebridRepair] DB reset failed for {entry_name!r}: {db_err}')
                outcome = 'no_replacement'

        log_repair_activity(
            item_id=item_id,
            title=db_item.get('title'),
            media_type=db_item.get('type'),
            season_number=db_item.get('season_number'),
            episode_number=db_item.get('episode_number'),
            broken_nzb_id=f'debrid:{info_hash}',
            broken_nzb_title=entry_name,
            outcome=outcome,
            triggered_by='debrid_repair',
        )
        return {'outcome': outcome, 'success': outcome == 'replaced'}
    except Exception as e:
        logger.error(f'[DebridRepair] replace_entry error for {entry_name!r}: {e}', exc_info=True)
        return {'outcome': 'error', 'message': str(e)}


# ---------------------------------------------------------------------------
# Full repair cycle
# ---------------------------------------------------------------------------

def run_repair(triggered_by: str = 'scheduled', version_override: str = None) -> dict:
    """
    Full repair cycle for broken torrent entries.
    - missing_provider_link → reinsert_entry (re-trigger cli_mount link resolver)
    - Other reasons → replace_entry (delete + reset to Wanted)
    """
    if not _repair_lock.acquire(blocking=False):
        logger.info('[DebridRepair] Repair already in progress — skipping')
        return {'skipped': 'already_running'}

    try:
        summary = {
            'broken_found': 0,
            'reinserted': 0,
            'replaced': 0,
            'not_found': 0,
            'errors': 0,
        }

        broken = fetch_broken_items()
        summary['broken_found'] = len(broken)

        if not broken:
            logger.info('[DebridRepair] No broken torrent items found — nothing to do')
            return summary

        logger.info(f'[DebridRepair] Processing {len(broken)} broken torrent entries')

        for entry in broken:
            entry_name = entry.get('entry_name') or entry.get('name') or entry.get('title') or ''
            broken_files = entry.get('broken_files') or []
            info_hash = (entry.get('info_hash') or entry.get('hash') or
                         (broken_files[0].get('info_hash') if broken_files else '') or '')
            failure_reason = entry.get('failure_reason') or (broken_files[0].get('failure_reason') if broken_files else '') or ''

            if not entry_name:
                logger.debug('[DebridRepair] Skipping entry with no name')
                continue

            try:
                if failure_reason == 'missing_provider_link':
                    result = reinsert_entry(entry_name, info_hash)
                    if result.get('outcome') == 'reinserted':
                        summary['reinserted'] += 1
                    else:
                        summary['errors'] += 1
                else:
                    result = replace_entry(entry_name, info_hash, version_override=version_override)
                    outcome = result.get('outcome', 'error')
                    if outcome == 'replaced':
                        summary['replaced'] += 1
                    elif outcome == 'not_found':
                        summary['not_found'] += 1
                    else:
                        summary['errors'] += 1
            except Exception as e:
                logger.error(f'[DebridRepair] Error processing {entry_name!r}: {e}', exc_info=True)
                summary['errors'] += 1
            # Small delay between entries to avoid RD 429 rate limiting
            time.sleep(2)

        logger.info(f'[DebridRepair] Repair cycle complete: {summary}')
        return summary
    finally:
        _repair_lock.release()


# ---------------------------------------------------------------------------
# Delete all broken
# ---------------------------------------------------------------------------

def delete_all_broken() -> dict:
    """Delete all broken torrent entries from cli_mount and reset CLI items to Wanted."""
    try:
        from database.database_writing import update_media_item_state
        broken = fetch_broken_items()
        if not broken:
            return {'deleted_climount': 0, 'deleted_plex': 0, 'reset_db': 0}

        deleted_climount = 0
        deleted_plex = 0
        reset_db = 0

        for entry in broken:
            entry_name = entry.get('entry_name') or entry.get('name') or ''
            info_hash = entry.get('info_hash') or entry.get('hash') or ''

            if _delete_from_climount(info_hash, entry_name):
                deleted_climount += 1

            db_items = _find_db_items_by_entry_name(entry_name, info_hash) if entry_name else []
            if db_items is AMBIGUOUS:
                logger.warning(f'[DebridRepair] delete_all_broken: ambiguous multi-version match for {entry_name!r} — skipping DB reset for this entry')
                db_items = []
            for item in db_items:
                if _delete_from_plex(item):
                    deleted_plex += 1
                try:
                    update_media_item_state(item['id'], 'Wanted')
                    reset_db += 1
                except Exception as dbe:
                    logger.warning(f'[DebridRepair] DB reset failed for item {item.get("id")}: {dbe}')

        return {'deleted_climount': deleted_climount, 'deleted_plex': deleted_plex, 'reset_db': reset_db}
    except Exception as e:
        logger.error(f'[DebridRepair] delete_all_broken error: {e}', exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Activity log queries (filtered to debrid entries)
# ---------------------------------------------------------------------------

def get_repair_activity(limit: int = 25, offset: int = 0, outcome: str = None):
    """Read repair activity from nzb_repair_activity where broken_nzb_id LIKE 'debrid:%'."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            if outcome:
                total = conn.execute(
                    "SELECT COUNT(*) FROM nzb_repair_activity WHERE broken_nzb_id LIKE 'debrid:%' AND outcome = ?",
                    (outcome,),
                ).fetchone()[0]
                rows = conn.execute(
                    """SELECT * FROM nzb_repair_activity
                       WHERE broken_nzb_id LIKE 'debrid:%' AND outcome = ?
                       ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                    (outcome, limit, offset),
                ).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) FROM nzb_repair_activity WHERE broken_nzb_id LIKE 'debrid:%'",
                ).fetchone()[0]
                rows = conn.execute(
                    """SELECT * FROM nzb_repair_activity
                       WHERE broken_nzb_id LIKE 'debrid:%'
                       ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows], total
        except Exception as e:
            logger.debug(f'[DebridRepair] get_repair_activity error: {e}')
            return [], 0
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f'[DebridRepair] get_repair_activity DB connection error: {e}')
        return [], 0


def get_repair_stats(days: int = 30) -> dict:
    """Count outcomes from nzb_repair_activity for debrid entries in the last N days."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            since = f"datetime('now', '-{days} days')"
            rows = conn.execute(
                f"""SELECT outcome, COUNT(*) FROM nzb_repair_activity
                   WHERE broken_nzb_id LIKE 'debrid:%' AND created_at >= {since}
                   GROUP BY outcome"""
            ).fetchall()
            stats = {r[0]: r[1] for r in rows}
            return {
                'replaced': stats.get('replaced', 0),
                'reinserted': stats.get('reinserted', 0),
                'not_found': stats.get('not_found', 0),
                'no_replacement': stats.get('no_replacement', 0),
                'error': stats.get('error', 0),
                'total': sum(stats.values()),
            }
        except Exception as e:
            logger.debug(f'[DebridRepair] get_repair_stats error: {e}')
            return {k: 0 for k in ('replaced', 'reinserted', 'not_found', 'no_replacement', 'error', 'total')}
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f'[DebridRepair] get_repair_stats DB connection error: {e}')
        return {k: 0 for k in ('replaced', 'reinserted', 'not_found', 'no_replacement', 'error', 'total')}
