"""
cli_mount → CLI DB sync.

Polls GET /api/sync/changes?since={unix_ts} on cli_mount and updates
the following media_items columns:

Both NZB and torrent:
  filled_by_torrent_id           ← provider_id
  debrid_folder_name             ← folder_name
  location_basename              ← folder_name
  original_scraped_torrent_title ← original_filename
  filled_by_file                 ← matched per-file name (by size)

Torrent only:
  filled_by_magnet               ← magnet

NZB only:
  nzb_segment_id                 ← nzb_segment_id (delta sync only)

New fields from cli_mount:
  bad        — if True, CLI triggers re-insertion immediately
  file_count — number of media files in entry
  files[]    — {name, size} per file; matched by size to CLI rows for filled_by_file

Match priority:
  1. NZB:     filled_by_torrent_id = provider_id  (exact "nzb:{uuid}" match)
  2. Torrent: extract infohash from filled_by_magnet, compare to info_hash
  3. Both:    debrid_folder_name = folder_name
  4. Both:    original_scraped_torrent_title = original_filename
  5. Both:    location_on_disk folder = folder_name (Plex-scanned items)
  6. Both:    location_on_disk folder extraction — orphan items with no torrent_id
              but with location_on_disk set (Plex-scanned items)
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

_LIVE_STATES = "('Collected','Checking','Upgrading')"
_LAST_SYNC_SETTING_KEY = 'last_climount_sync'


def _get_last_sync_ts() -> int:
    try:
        from utilities.settings import get_setting
        val = get_setting('Usenet Provider', _LAST_SYNC_SETTING_KEY, 0)
        return int(val) if val else 0
    except Exception:
        return 0


def _set_last_sync_ts(ts: int) -> None:
    try:
        from utilities.settings import set_setting
        set_setting('Usenet Provider', _LAST_SYNC_SETTING_KEY, str(ts))
    except Exception as e:
        logger.debug(f'[CMSync] Could not save last sync timestamp: {e}')


def _fetch_changes(since_ts: int) -> list:
    try:
        from usenet.climount_client import get_climount_client
        from routes.api_tracker import api
        client = get_climount_client()
        if not client or not client.is_enabled():
            return []
        url = f'{client.base_url}/api/sync/changes'
        if since_ts > 0:
            url += f'?since={since_ts}'
        r = api.get(url, headers=client._headers(), timeout=60)
        if r.status_code != 200:
            logger.warning(f'[CMSync] /api/sync/changes returned HTTP {r.status_code}')
            return []
        return r.json() or []
    except Exception as e:
        logger.warning(f'[CMSync] fetch_changes error: {e}')
        return []


def _extract_infohash_from_magnet(magnet: str) -> str:
    """Extract infohash from a magnet link."""
    if not magnet:
        return ''
    m = re.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, re.IGNORECASE)
    return m.group(1).lower() if m else ''


def _find_item_ids(conn, entry: dict, infohash_map: dict = None, location_map: dict = None) -> list:
    """
    Find ALL media_item ids matching a cli_mount entry.
    Returns a list of ids — multiple rows for season packs (one per episode).
    """
    protocol = entry.get('protocol', '')
    provider_id = entry.get('provider_id', '') or ''
    info_hash = entry.get('info_hash', '') or ''
    folder_name = entry.get('folder_name', '') or ''
    original_filename = entry.get('original_filename', '') or ''

    # Collect all matched item IDs across strategies.
    # Strategies 1-4 find already-linked items; strategy 5 finds orphan duplicates
    # (same content, null filled_by_torrent_id) via location_on_disk folder name.
    # We combine all so orphans get updated alongside their linked counterparts.
    matched_ids: list = []
    seen: set = set()

    def _add(ids):
        for i in ids:
            if i not in seen:
                seen.add(i)
                matched_ids.append(i)

    # Strategy 0: cli_debrid_ids direct lookup.
    # Collected but don't early-return — other strategies may find additional items
    # (e.g. duplicates or items collected via different paths) so we accumulate all.
    cli_debrid_ids = entry.get('cli_debrid_ids') or {}
    if cli_debrid_ids:
        _add([v for v in cli_debrid_ids.values() if isinstance(v, int) and v > 0])

    # Strategy 1: NZB — exact filled_by_torrent_id match
    if protocol == 'nzb' and provider_id:
        if infohash_map is not None and provider_id in infohash_map:
            _add(infohash_map[provider_id])
        else:
            rows = conn.execute(
                f"SELECT id FROM media_items WHERE filled_by_torrent_id = ? AND state IN {_LIVE_STATES}",
                (provider_id,)
            ).fetchall()
            _add([r[0] for r in rows])

    # Strategy 2: Torrent — infohash from magnet
    if protocol == 'torrent' and info_hash:
        if infohash_map is not None:
            _add(infohash_map.get(info_hash.lower(), []))
        else:
            rows = conn.execute(
                f"SELECT id, filled_by_magnet FROM media_items WHERE filled_by_magnet IS NOT NULL AND state IN {_LIVE_STATES}"
            ).fetchall()
            _add([r[0] for r in rows if _extract_infohash_from_magnet(r[1] or '') == info_hash.lower()])

    # Strategy 3: debrid_folder_name match
    if folder_name:
        rows = conn.execute(
            f"SELECT id FROM media_items WHERE debrid_folder_name = ? AND state IN {_LIVE_STATES}",
            (folder_name,)
        ).fetchall()
        _add([r[0] for r in rows])

    # Strategy 4: original_scraped_torrent_title match
    if original_filename:
        rows = conn.execute(
            f"SELECT id FROM media_items WHERE original_scraped_torrent_title = ? AND state IN {_LIVE_STATES}",
            (original_filename,)
        ).fetchall()
        _add([r[0] for r in rows])

    # Strategy 5: location_on_disk folder match — catches orphan duplicates with
    # null filled_by_torrent_id that share the same cli_mount folder name.
    if folder_name and location_map is not None:
        _add(location_map.get(folder_name, []))

    return matched_ids


def _build_update(entry: dict, existing: dict = None, file_name: str = None) -> tuple:
    """
    Build (SET clause, params list) for the UPDATE statement.
    All fields are always overwritten when cli_mount has a non-empty value.
    file_name: per-row matched filename for filled_by_file (matched by size).
    """
    protocol = entry.get('protocol', '')
    sets = []
    params = []

    provider_id = entry.get('provider_id', '') or ''
    folder_name = entry.get('folder_name', '') or ''
    original_filename = entry.get('original_filename', '') or ''
    magnet = entry.get('magnet', '') or ''
    nzb_segment_id = entry.get('nzb_segment_id', '') or ''

    # Both: filled_by_torrent_id — always overwrite
    if provider_id:
        sets.append('filled_by_torrent_id = ?')
        params.append(provider_id)

    # Both: debrid_folder_name — always overwrite (cli_mount is ground truth)
    if folder_name:
        sets.append('debrid_folder_name = ?')
        params.append(folder_name)

    # Both: location_basename — folder name, always overwrite
    if folder_name:
        sets.append('location_basename = ?')
        params.append(folder_name)

    # Both: original_scraped_torrent_title — always overwrite
    if original_filename:
        sets.append('original_scraped_torrent_title = ?')
        params.append(original_filename)

    # Both: filled_by_file — per-row matched filename, only set if currently NULL
    # The Plex scan and Adding queue set this correctly per-episode.
    # The sync's size-based matching can be imprecise for season packs,
    # so only fill when empty to avoid overwriting correct values.
    if file_name:
        sets.append('filled_by_file = CASE WHEN (filled_by_file IS NULL OR filled_by_file = "") THEN ? ELSE filled_by_file END')
        params.append(file_name)

    # Both: original_filename — immutable raw filename, never overwrite once set
    # SQL CASE ensures it's only written when currently NULL/empty
    if file_name:
        sets.append('original_filename = CASE WHEN (original_filename IS NULL OR original_filename = "") THEN ? ELSE original_filename END')
        params.append(file_name)

    # Torrent only: filled_by_magnet — always overwrite
    if protocol == 'torrent' and magnet:
        sets.append('filled_by_magnet = ?')
        params.append(magnet)

    # NZB: clear any stale magnet left over from a previous debrid grab
    if protocol == 'nzb':
        sets.append('filled_by_magnet = NULL')

    # NZB only: nzb_segment_id — always overwrite when available
    if protocol == 'nzb' and nzb_segment_id:
        sets.append('nzb_segment_id = ?')
        params.append(nzb_segment_id)

    return sets, params


def sync_changes_from_climount(force_full: bool = False) -> dict:
    """
    Poll cli_mount for changes since last sync and update CLI media_items.
    Pass force_full=True to reset the timestamp and re-process all entries.
    Returns summary dict with counts.
    """
    summary = {'fetched': 0, 'matched': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

    since_ts = 0 if force_full else _get_last_sync_ts()
    now_ts = int(time.time())

    changes = _fetch_changes(since_ts)
    summary['fetched'] = len(changes)

    if not changes:
        _set_last_sync_ts(now_ts)
        return summary

    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            # Build lookup maps once — avoids repeated full-table scans per entry
            # torrent_map: infohash → [item_ids]
            # nzb_map: filled_by_torrent_id → [item_ids]
            torrent_map: dict = {}
            rows = conn.execute(
                f"SELECT id, filled_by_magnet FROM media_items WHERE filled_by_magnet IS NOT NULL AND state IN {_LIVE_STATES}"
            ).fetchall()
            for row in rows:
                ih = _extract_infohash_from_magnet(row[1] or '')
                if ih:
                    torrent_map.setdefault(ih, []).append(row[0])

            nzb_map: dict = {}
            rows = conn.execute(
                f"SELECT id, filled_by_torrent_id FROM media_items WHERE filled_by_torrent_id LIKE 'nzb:%' AND state IN {_LIVE_STATES}"
            ).fetchall()
            for row in rows:
                nzb_map.setdefault(row[1], []).append(row[0])

            # location_map: folder_name → [item_ids] for items with no filled_by_torrent_id.
            # location_on_disk format: /debrid/movies/FolderName/FolderName.mkv
            # The folder name is always the second-to-last path segment.
            # Only include items whose type matches the path — movies from /debrid/movies/,
            # episodes from /debrid/shows/ — to prevent cross-type contamination.
            location_map: dict = {}
            rows = conn.execute(
                f"SELECT id, type, location_on_disk FROM media_items "
                f"WHERE (filled_by_torrent_id IS NULL OR filled_by_torrent_id = '') "
                f"AND location_on_disk IS NOT NULL AND location_on_disk != '' "
                f"AND state IN {_LIVE_STATES}"
            ).fetchall()
            for row in rows:
                item_id, item_type, loc = row[0], row[1], row[2]
                parts = (loc or '').rstrip('/').split('/')
                if len(parts) >= 2:
                    folder = parts[-2]
                    if not folder:
                        continue
                    # Guard: skip episode rows whose path is in /movies/ and vice versa
                    if item_type == 'episode' and '/movies/' in loc:
                        continue
                    if item_type == 'movie' and '/shows/' in loc:
                        continue
                    location_map.setdefault(folder, []).append(item_id)

            logger.info(f'[CMSync] Built maps — torrent: {len(torrent_map)}, nzb: {len(nzb_map)}, location: {len(location_map)}')

            _BATCH_SIZE = 500
            _batch_count = 0

            for entry in changes:
                try:
                    lookup_map = nzb_map if entry.get('protocol') == 'nzb' else torrent_map
                    item_ids = _find_item_ids(conn, entry, infohash_map=lookup_map, location_map=location_map)
                    if not item_ids:
                        summary['skipped'] += 1
                        logger.debug(f'[CMSync] No match for {entry.get("protocol")} entry: {entry.get("folder_name","?")}')
                        continue

                    # Safety: if matched rows span more than one distinct version, this
                    # is not one logical item — applying the same update (esp.
                    # filled_by_torrent_id) to all of them would converge different
                    # versions onto the same job id (e.g. from a since-fixed dedup bug
                    # or other stale data), risking one version's file being deleted
                    # later when the other's lifecycle acts on that shared id.
                    if len(item_ids) > 1:
                        _versions = set()
                        for _iid in item_ids:
                            _vrow = conn.execute('SELECT version FROM media_items WHERE id = ?', (_iid,)).fetchone()
                            _versions.add((_vrow[0] or '').rstrip('*') if _vrow else '')
                        if len(_versions) > 1:
                            summary['skipped'] += 1
                            logger.warning(
                                f'[CMSync] Matched {len(item_ids)} rows spanning multiple versions '
                                f'{sorted(_versions)} for {entry.get("folder_name","?")} — skipping update to avoid cross-version collapse'
                            )
                            continue

                    summary['matched'] += len(item_ids)
                    logger.debug(f'[CMSync] Matched {len(item_ids)} row(s) for {entry.get("folder_name","?")}')

                    # Build size→filename map for filled_by_file matching
                    files = entry.get('files') or []
                    size_to_file: dict = {f['size']: f['name'] for f in files if f.get('size') and f.get('name')}
                    single_file = files[0]['name'] if len(files) == 1 else None

                    # Handle bad flag — trigger immediate re-insertion if cli_mount marked entry bad
                    if entry.get('bad'):
                        try:
                            from usenet.debrid_repair_engine import reinsert_entry
                            folder_name = entry.get('folder_name', '')
                            info_hash = entry.get('info_hash', '')
                            logger.info(f'[CMSync] Entry marked bad by cli_mount, triggering re-insertion: {folder_name}')
                            reinsert_entry(folder_name, info_hash)
                        except Exception as be:
                            logger.debug(f'[CMSync] Bad flag re-insertion error: {be}')

                    # DB field updates — unchanged from original, runs per item_id
                    for item_id in item_ids:
                        file_name = single_file
                        if file_name is None and size_to_file:
                            row_size = conn.execute(
                                'SELECT size FROM media_items WHERE id = ?', (item_id,)
                            ).fetchone()
                            if row_size and row_size[0]:
                                size_bytes = int(float(row_size[0]) * 1024 * 1024 * 1024)
                                file_name = size_to_file.get(size_bytes)
                                if not file_name:
                                    for sz, fn in size_to_file.items():
                                        if abs(sz - size_bytes) / max(sz, 1) < 0.01:
                                            file_name = fn
                                            break
                        sets, params = _build_update(entry, {}, file_name=file_name)
                        if not sets:
                            continue
                        params.append(item_id)
                        conn.execute(
                            f"UPDATE media_items SET {', '.join(sets)} WHERE id = ?",
                            params
                        )
                        summary['updated'] += 1
                        _batch_count += 1

                    # Build {filename: item_id} map using the same logic as register_cli_ids_for_item:
                    # query all siblings by filled_by_torrent_id using filled_by_file.
                    # Try each matched item until we find one with a filled_by_torrent_id —
                    # Strategy 0 may return stale/duplicate items without a torrent_id.
                    import os as _os_sync
                    _VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}
                    _torrent_id_val = ''
                    for _iid in item_ids:
                        _row = conn.execute(
                            'SELECT filled_by_torrent_id FROM media_items WHERE id = ?', (_iid,)
                        ).fetchone()
                        if _row and _row[0]:
                            _torrent_id_val = _row[0]
                            break
                    _sibs = conn.execute(
                        "SELECT id, filled_by_file FROM media_items "
                        "WHERE filled_by_torrent_id = ? "
                        "AND state IN ('Checking','Collected','Upgrading')",
                        (_torrent_id_val,)
                    ).fetchall() if _torrent_id_val else []
                    _cli_ids_to_register = {
                        s[1]: s[0] for s in _sibs
                        if s[1] and _os_sync.path.splitext(s[1])[1].lower() in _VIDEO_EXTS
                    }
                    # Fallback: use filled_by_file directly from each matched item_id
                    if not _cli_ids_to_register:
                        for _iid in item_ids:
                            _row = conn.execute(
                                'SELECT filled_by_file FROM media_items WHERE id = ?', (_iid,)
                            ).fetchone()
                            if _row and _row[0] and _os_sync.path.splitext(_row[0])[1].lower() in _VIDEO_EXTS:
                                _cli_ids_to_register[_row[0]] = _iid
                    # Final fallback for single-file entries not yet collected
                    if not _cli_ids_to_register and single_file and item_ids:
                        _cli_ids_to_register = {single_file: item_ids[0]}

                    # Register cli_debrid IDs with cli_mount — always send the complete map
                    _info_hash = entry.get('info_hash') or ''
                    if _cli_ids_to_register and _info_hash:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_sync
                            _dc_sync = _get_dc_sync()
                            if _dc_sync and _dc_sync.is_enabled():
                                _dc_sync.register_cli_ids(_info_hash, _cli_ids_to_register)
                        except Exception as _reg_err:
                            logger.debug(f'[CMSync] cli_ids registration error for {_info_hash}: {_reg_err}')

                    # Push tags to cli_mount — Plex mode only, checked inside push_tags().
                    # Uses the first matched item's tags column (comma-separated string).
                    if _info_hash and item_ids:
                        try:
                            _tags_row = conn.execute(
                                'SELECT tags FROM media_items WHERE id = ?', (item_ids[0],)
                            ).fetchone()
                            if _tags_row and _tags_row[0]:
                                from usenet.climount_client import get_climount_client as _get_dc_tags
                                _dc_tags = _get_dc_tags()
                                if _dc_tags and _dc_tags.is_enabled():
                                    _tag_ok = _dc_tags.push_tags(_info_hash, _tags_row[0])
                                    if _tag_ok:
                                        summary['tags_pushed'] = summary.get('tags_pushed', 0) + 1
                                        logger.info(f"[CMSync] Pushed tags '{_tags_row[0]}' for {_info_hash}")
                                        from datetime import datetime as _dt_tags
                                        conn.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            (_dt_tags.now(), item_ids[0])
                                        )
                                    else:
                                        logger.warning(f"[CMSync] Tag push returned false for {_info_hash} (tags='{_tags_row[0]}')")
                                else:
                                    logger.warning(f'[CMSync] Tag push skipped for {_info_hash}: cli_mount client disabled/not configured')
                        except Exception as _tag_err:
                            logger.warning(f'[CMSync] Tag push error for {_info_hash}: {_tag_err}')

                    # Commit every _BATCH_SIZE updates to release write lock
                    if _batch_count >= _BATCH_SIZE:
                        conn.commit()
                        _batch_count = 0

                except Exception as e:
                    logger.debug(f'[CMSync] Error processing entry {entry.get("info_hash","?")}: {e}')
                    summary['errors'] += 1

            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f'[CMSync] DB error: {e}', exc_info=True)
        summary['errors'] += 1

    _set_last_sync_ts(now_ts)

    # --- Orphan pass: find Collected items missing torrent_id that aren't on NAS ---
    # Runs after every delta sync to catch items that were never matched.
    # Fetches all cli_mount entries only if orphans actually exist.
    try:
        from utilities.settings import get_setting as _gs
        from database.core import get_db_connection as _get_dbc

        # NAS path prefixes from settings — these items live on NAS, not cli_mount
        nas_paths = _gs('Debug', 'nas_paths', []) or []
        if isinstance(nas_paths, str):
            import json as _json
            try:
                nas_paths = _json.loads(nas_paths)
            except Exception:
                nas_paths = []

        _conn = _get_dbc()
        try:
            orphan_rows = _conn.execute(
                f"SELECT id, location_on_disk FROM media_items "
                f"WHERE (filled_by_torrent_id IS NULL OR filled_by_torrent_id = '') "
                f"AND location_on_disk IS NOT NULL AND location_on_disk != '' "
                f"AND state IN {_LIVE_STATES}"
            ).fetchall()

            # Exclude NAS items — they can't be matched via cli_mount
            if nas_paths:
                orphan_rows = [
                    r for r in orphan_rows
                    if not any((r[1] or '').startswith(p) for p in nas_paths)
                ]

            if orphan_rows:
                logger.info(f'[CMSync] Orphan pass: {len(orphan_rows)} items missing torrent_id — fetching all cli_mount entries')
                all_entries = _fetch_changes(0)
                if all_entries:
                    folder_to_entry = {
                        (e.get('folder_name') or '').strip(): e
                        for e in all_entries
                        if (e.get('folder_name') or '').strip()
                    }
                    orphan_matched = orphan_updated = 0
                    _obatch = 0
                    for oid, loc in orphan_rows:
                        parts = (loc or '').rstrip('/').split('/')
                        folder = parts[-2] if len(parts) >= 2 else ''
                        if not folder or folder not in folder_to_entry:
                            continue
                        entry = folder_to_entry[folder]
                        sets, params = _build_update(entry, {})
                        if not sets:
                            continue
                        params.append(oid)
                        _conn.execute(
                            f"UPDATE media_items SET {', '.join(sets)} WHERE id = ?",
                            params
                        )
                        orphan_matched += 1
                        orphan_updated += 1
                        _obatch += 1
                        if _obatch >= 500:
                            _conn.commit()
                            _obatch = 0
                    _conn.commit()
                    if orphan_matched:
                        logger.info(f'[CMSync] Orphan pass: matched={orphan_matched}, updated={orphan_updated}')
                        summary['matched'] += orphan_matched
                        summary['updated'] += orphan_updated
        finally:
            _conn.close()
    except Exception as _oe:
        logger.debug(f'[CMSync] Orphan pass error: {_oe}')

    logger.info(
        f'[CMSync] Sync complete — fetched={summary["fetched"]}, '
        f'matched={summary["matched"]}, updated={summary["updated"]}, '
        f'skipped={summary["skipped"]}, errors={summary["errors"]}'
    )
    return summary


def push_pending_tags() -> dict:
    """
    Push tags for any media_items row whose tags column was set/changed since
    the last successful push to cli_mount. Independent of the decypharr-side
    delta sync above — that mechanism only re-fetches entries decypharr itself
    has changed, so a cli_debrid-only tags edit (e.g. via the Database "Assign
    Tags" bulk action) would never be picked up by sync_changes_from_climount
    alone. Uses tags_pushed_at (NULL, or older than last_updated) to find rows
    needing a push, so already-pushed rows aren't re-sent every cycle.
    """
    summary = {'candidates': 0, 'pushed': 0, 'skipped_no_hash': 0, 'errors': 0}
    try:
        from database.core import get_db_connection
        from usenet.climount_client import get_climount_client
        from datetime import datetime as _dt

        client = get_climount_client()
        if not client or not client.is_enabled():
            return summary

        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items "
                "WHERE tags IS NOT NULL AND tags != '' "
                "AND (tags_pushed_at IS NULL OR last_updated > tags_pushed_at) "
                f"AND state IN {_LIVE_STATES}"
            ).fetchall()
            summary['candidates'] = len(rows)

            for row in rows:
                info_hash = ''
                torrent_id = str(row['filled_by_torrent_id'] or '')
                if torrent_id.startswith('nzb:'):
                    info_hash = torrent_id[4:]
                else:
                    magnet = row['filled_by_magnet'] or ''
                    m = _extract_infohash_from_magnet(magnet)
                    if m:
                        info_hash = m
                if not info_hash:
                    summary['skipped_no_hash'] += 1
                    continue

                try:
                    if client.push_tags(info_hash, row['tags']):
                        conn.execute(
                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                            (_dt.now(), row['id'])
                        )
                        conn.commit()
                        summary['pushed'] += 1
                        logger.info(f"[CMSync] push_pending_tags: pushed tags '{row['tags']}' for {info_hash}")
                    else:
                        summary['errors'] += 1
                        logger.warning(f"[CMSync] push_pending_tags: push failed for {info_hash} (tags='{row['tags']}')")
                except Exception as _pe:
                    summary['errors'] += 1
                    logger.warning(f'[CMSync] push_pending_tags: error for {info_hash}: {_pe}')
        finally:
            conn.close()
    except Exception as e:
        logger.error(f'[CMSync] push_pending_tags: DB error: {e}', exc_info=True)
        summary['errors'] += 1

    if summary['candidates']:
        logger.info(
            f"[CMSync] push_pending_tags complete — candidates={summary['candidates']}, "
            f"pushed={summary['pushed']}, skipped_no_hash={summary['skipped_no_hash']}, "
            f"errors={summary['errors']}"
        )
    return summary
