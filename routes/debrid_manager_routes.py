import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, jsonify, request
from debrid import get_debrid_provider, get_provider_display_name, get_debrid_providers
from database.torrent_tracking import get_recent_additions
from .models import admin_required
from utilities.jellyfin_auth import jellyfin_auth_headers

debrid_manager_bp = Blueprint('debrid_manager', __name__)

# ---------------------------------------------------------------------------
# Library cache — progressive loading pattern
#
# First load: fetch INITIAL_PAGES synchronously (fast, ~2-3s), return
# immediately with loading=True. Background thread fetches remaining pages
# and accumulates into the cache.  Frontend polls while loading=True.
#
# Subsequent loads within TTL: instant from stable cache.
# After TTL: serve stale snapshot instantly, re-fetch in background.
# Force refresh: re-fetch everything from scratch.
# ---------------------------------------------------------------------------
_PAGE_SIZE    = 500       # items per RD API call
_INITIAL_PAGES = 3        # pages fetched synchronously before first response
_LIB_TTL      = 86400 * 7  # 7 days — background refresh only triggers after this

# _stable: last complete snapshot dict  {torrents, total, total_bytes, fetched_at}
# _partial: list accumulating during an in-progress fetch
# _gen:     monotonically-increasing counter; background threads abort if theirs
#           doesn't match, preventing stale writes after a force-refresh.
_lib = {
    'stable':  None,
    'partial': [],
    'loading': False,
    'gen':     0,
    'lock':    threading.Lock(),
}
_lib_db_loaded = False  # track whether we've attempted a DB load this process
_lib_last_accessed = 0.0  # timestamp of last debrid manager visit — used for idle eviction


def _save_lib_cache_to_db(stable_data: dict) -> None:
    """Persist the full RD library to SQLite so it survives restarts."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rd_library_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data_json TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO rd_library_cache (id, data_json, fetched_at) VALUES (1, ?, ?)",
            (json.dumps({
                'torrents':    stable_data['torrents'],
                'total':       stable_data['total'],
                'total_bytes': stable_data['total_bytes'],
            }), stable_data['fetched_at'])
        )
        conn.commit()
        conn.close()
        logging.debug(f"[LibCache] Saved {stable_data['total']} torrents to DB")
    except Exception as e:
        logging.warning(f"[LibCache] Could not save library to DB: {e}")


def _load_lib_cache_from_db() -> None:
    """Load a previously persisted RD library into the in-memory cache (once per process)."""
    global _lib_db_loaded
    if _lib_db_loaded:
        return
    _lib_db_loaded = True
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        row = conn.execute(
            "SELECT data_json, fetched_at FROM rd_library_cache WHERE id=1"
        ).fetchone()
        conn.close()
        if row:
            data = json.loads(row[0])
            with _lib['lock']:
                if _lib['stable'] is None:  # don't overwrite a fresher in-memory copy
                    _lib['stable'] = {
                        'torrents':    data.get('torrents', []),
                        'total':       data.get('total', 0),
                        'total_bytes': data.get('total_bytes', 0),
                        'fetched_at':  row[1],
                    }
            logging.info(f"[LibCache] Loaded {data.get('total', 0)} torrents from DB "
                         f"(age: {int(time.time() - row[1])}s)")
    except Exception as e:
        logging.debug(f"[LibCache] Could not load library from DB: {e}")


def _fetch_all_bg(gen):
    """Background thread for providers that return all torrents in one call (e.g. AllDebrid, Premiumize).
    For Premiumize, uses list_completed_torrents() so the library/reconcile only
    shows finished items — pending downloads are excluded.
    Aborts silently if a newer fetch generation has started."""
    try:
        provider = get_debrid_provider()
        # Premiumize: use completed-only list for the library snapshot
        if hasattr(provider, 'list_completed_torrents'):
            result = provider.list_completed_torrents()
        else:
            result = provider.list_active_torrents()
        if not isinstance(result, list):
            result = []

        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            _lib['partial'].extend(result)

    except Exception as e:
        logging.error(f"Library bg fetch error (all-at-once): {e}")


def _fetch_pages_bg(api_key, start_page, gen):
    """Background thread for RD-style paginated /torrents endpoint.
    Aborts silently if a newer fetch generation has started."""
    from debrid.real_debrid.api import make_request
    page = start_page
    try:
        while True:
            with _lib['lock']:
                if _lib['gen'] != gen:
                    return  # superseded by a newer fetch
            try:
                result = make_request('GET', '/torrents', api_key,
                                      params={'limit': _PAGE_SIZE, 'page': page})
            except Exception as e:
                logging.error(f"Library bg fetch error at page {page}: {e}")
                break

            if not isinstance(result, list) or not result:
                break

            with _lib['lock']:
                if _lib['gen'] != gen:
                    return
                _lib['partial'].extend(result)

            if len(result) < _PAGE_SIZE:
                break
            page += 1

        # Enrich with DB metadata before promoting to stable (done outside lock — it's a read-only query)
        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            all_torrents = list(_lib['partial'])

        _enrich_with_db(all_torrents)

        # Promote partial → stable
        new_stable = {
            'torrents':    all_torrents,
            'total':       len(all_torrents),
            'total_bytes': sum(t.get('bytes', 0) or 0 for t in all_torrents),
            'fetched_at':  time.time(),
        }
        with _lib['lock']:
            if _lib['gen'] != gen:
                return
            _lib['stable'] = new_stable
            _lib['partial'] = []
            _lib['loading'] = False
        _save_lib_cache_to_db(new_stable)
        logging.info(f"Library cache complete: {len(all_torrents)} torrents")

    except Exception as e:
        logging.error(f"Library bg fetch unexpected error: {e}")
        with _lib['lock']:
            if _lib['gen'] == gen:
                _lib['loading'] = False


def _enrich_with_db(torrents):
    """Annotate each torrent with _db metadata from media_items.filled_by_torrent_id.
    Single bulk query per cache build — zero per-request cost."""
    if not torrents:
        return torrents
    id_map = {t['id']: t for t in torrents if t.get('id')}
    if not id_map:
        return torrents
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            ids = list(id_map.keys())
            placeholders = ','.join(['?'] * len(ids))
            rows = conn.execute(
                f"SELECT filled_by_torrent_id, title, year, type, season_number, episode_number "
                f"FROM media_items WHERE filled_by_torrent_id IN ({placeholders})",
                ids
            ).fetchall()
        finally:
            conn.close()
        # Count how many media_items rows share each torrent ID
        torrent_row_counts = {}
        for row in rows:
            tid = row[0]
            if tid:
                torrent_row_counts[tid] = torrent_row_counts.get(tid, 0) + 1

        seen = set()
        for row in rows:
            tid = row[0]
            if tid and tid in id_map and tid not in seen:
                seen.add(tid)
                is_pack = torrent_row_counts.get(tid, 1) > 1
                id_map[tid]['_db'] = {
                    'title':          row[1],
                    'year':           row[2],
                    'type':           row[3],
                    'season_number':  row[4],
                    # Omit episode number for season packs (multiple items share this torrent)
                    'episode_number': None if is_pack else row[5],
                }
        logging.debug(f"Library DB enrichment: {len(rows)}/{len(ids)} matched")
    except Exception as e:
        logging.warning(f"Library DB enrichment skipped: {e}")
    return torrents


def _invalidate_library_cache():
    """Mark cache stale after a mutation (delete / reinsert)."""
    with _lib['lock']:
        if _lib['stable']:
            _lib['stable']['fetched_at'] = 0.0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@debrid_manager_bp.route('/')
@admin_required
def index():
    from routes.debrid_manager.torrent_audits import _get_bad_folder_path  # imported when called
    from utilities.settings import get_setting
    file_mgmt = get_setting('File Management', 'file_collection_management', default='Plex')
    media_server = get_setting('File Management', 'media_server_type', default='plex')
    show_plex_trash = (
        file_mgmt == 'Plex' or
        (file_mgmt == 'Symlinked/Local' and media_server == 'plex')
    )
    symlink_mode = (file_mgmt == 'Symlinked/Local')
    provider_name = get_provider_display_name()
    # Derive abbreviation from the backup prefix map (single source of truth)
    # prefix → display name is defined in _bkPfxLabel in the template;
    # here we reverse it: display name → prefix.upper()
    _prefix_map = {
        'Real-Debrid':  'RD',
        'AllDebrid':    'AD',
        'Premiumize':   'PM',
        'Torbox':       'TB',
        'Debrid-Link':  'DL',
    }
    provider_abbrev = _prefix_map.get(provider_name, provider_name[:2].upper())
    climount_mode = _get_bad_folder_path() is not None
    # Show cli_mount tab only when a cli_mount/usenet provider is configured and enabled
    try:
        from usenet.climount_client import get_climount_client
        _dc = get_climount_client()
        show_climount_tab = _dc.is_enabled()
    except Exception:
        show_climount_tab = False
    # Build provider list for reinsert target selector
    try:
        all_providers = [{'index': i, 'name': p.PROVIDER_NAME} for i, p in enumerate(get_debrid_providers())]
    except Exception:
        all_providers = [{'index': 0, 'name': provider_name}]
    return render_template(
        'debrid_manager.html',
        show_plex_trash=show_plex_trash,
        symlink_mode=symlink_mode,
        climount_mode=climount_mode,
        show_climount_tab=show_climount_tab,
        media_server_type=media_server,
        debrid_provider_name=provider_name,
        debrid_provider_abbrev=provider_abbrev,
        debrid_all_providers=all_providers,
    )


_plex_trash_items_cache = {'items': None, 'ts': 0}


@debrid_manager_bp.route('/api/active')
@admin_required
def api_active():
    try:
        provider = get_debrid_provider()
        all_torrents = provider.list_active_torrents()
        if not isinstance(all_torrents, list):
            all_torrents = []

        _error_statuses = {'error', 'magnet_error', 'virus', 'dead'}
        _no_files_statuses = {'waiting_files_selection', 'magnet_conversion', 'compressing', 'uploading'}
        _active_statuses = {'downloading', 'active', 'queued'}

        torrents = [t for t in all_torrents if (t.get('status') or '').lower() in _active_statuses]
        errors   = [t for t in all_torrents if (t.get('status') or '').lower() in _error_statuses]
        no_files = [t for t in all_torrents if (t.get('status') or '').lower() in _no_files_statuses]

        active_count, max_downloads = provider.get_active_downloads()
        return jsonify({
            'success': True,
            'active_count': active_count,
            'max_downloads': max_downloads,
            'torrents': torrents,
            'errors': errors,
            'no_files': no_files,
        })
    except Exception as e:
        logging.error(f"Debrid Manager active API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/active_files')
@admin_required
def api_active_files():
    """
    Return currently open/active files from cli_mount.
    DFS mode:    active_streams from /debug/stats  (entry_name, file_name, started_at, cli_debrid_ids)
    Rclone mode: mount.detail.transferring from /debug/stats (name, bytes, size, speed, progress)
    """
    try:
        import requests as _req
        from usenet.climount_client import get_climount_client
        dc = get_climount_client()
        if not dc or not dc.is_enabled():
            return jsonify({'success': False, 'error': 'cli_mount not enabled'}), 400

        base = dc.base_url.rstrip('/')
        resp = _req.get(f'{base}/debug/stats', timeout=8)
        if resp.status_code != 200:
            return jsonify({'success': False, 'error': f'cli_mount stats returned {resp.status_code}'}), 502

        data = resp.json()
        now = int(time.time())

        # DFS / WebDAV mode — active_streams
        active_streams_raw = (data.get('active_streams') or {}).get('streams') or []
        streams_out = []
        for s in active_streams_raw:
            started_at = s.get('started_at') or 0
            duration_s = max(0, now - started_at) if started_at else 0
            cli_debrid_ids = s.get('cli_debrid_ids') or {}
            # Resolve cli_debrid_id for this specific file_name
            file_name = s.get('file_name', '')
            cli_debrid_id = cli_debrid_ids.get(file_name) if cli_debrid_ids else None

            streams_out.append({
                'mode': 'dfs',
                'entry_name': s.get('entry_name', ''),
                'file_name': file_name,
                'file_size': s.get('file_size', 0),
                'source': s.get('source', ''),
                'debrid': s.get('debrid', ''),
                'client': s.get('client', ''),
                'started_at': started_at,
                'last_active': s.get('last_active', 0),
                'duration_seconds': duration_s,
                'cli_debrid_id': cli_debrid_id,
                'cli_debrid_ids': cli_debrid_ids,
            })

        # Rclone mode — mount.detail.core.transferring
        # Path format: "<root_folder>/<EntryName>/<filename>"
        mount_detail = (data.get('mount') or {}).get('detail') or {}
        transferring = (mount_detail.get('core') or {}).get('transferring') or []
        rclone_out = []
        for t in transferring:
            full_path = t.get('name', '')
            # Strip leading root folder (movies/ or shows/) and split into entry+file
            parts = full_path.split('/', 2)
            entry_name = parts[1] if len(parts) >= 3 else ''
            file_name = parts[2] if len(parts) >= 3 else full_path
            rclone_out.append({
                'mode': 'rclone',
                'name': file_name,
                'entry_name': entry_name,
                'full_path': full_path,
                'bytes': t.get('bytes', 0),
                'size': t.get('size', 0),
                'speed': t.get('speed', 0),
                'progress': t.get('progress', 0),
                'eta': t.get('eta', 0),
            })

        mount_type = (data.get('mount') or {}).get('type', 'unknown')
        return jsonify({
            'success': True,
            'mount_type': mount_type,
            'dfs_streams': streams_out,
            'rclone_transfers': rclone_out,
        })
    except Exception as e:
        logging.warning(f'[ActiveFiles] cli_mount unreachable or error: {type(e).__name__}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/active_files/delete', methods=['POST'])
@admin_required
def api_active_files_delete():
    """
    Delete an active file from cli_mount — handles season packs by resetting ALL siblings.
    Body (DFS):    { cli_debrid_ids: {filename: id, ...}, entry_name: str }
    Body (rclone): { entry_name: str }
    Steps:
      1. Collect all item IDs — from cli_debrid_ids map (DFS) or DB lookup by entry_name (rclone)
      2. Move every item to Wanted + delete each from Plex
      3. Remove the entry from cli_mount (always — all siblings are being reset)
    """
    try:
        from database.database_writing import update_media_item_state
        from database.database_reading import get_media_item_by_id
        from usenet.repair_engine import _delete_from_plex
        from usenet.climount_client import get_climount_client
        import re as _re

        data = request.get_json(silent=True) or {}
        entry_name = data.get('entry_name', '')
        # cli_debrid_ids: {filename: item_id} — all siblings in the season pack
        cli_debrid_ids = data.get('cli_debrid_ids') or {}

        # Collect all item IDs to reset
        item_ids = list({int(v) for v in cli_debrid_ids.values() if v}) if cli_debrid_ids else []

        # Fallback when cli_debrid_ids not populated (entry not yet registered):
        # Find one item by entry_name match, then expand to ALL siblings via filled_by_torrent_id
        if not item_ids and entry_name:
            from database.core import get_db_connection as _gdb_af
            _conn_af = _gdb_af()
            try:
                # Step 1: find any one item matching this entry name
                seed = _conn_af.execute(
                    "SELECT id, filled_by_torrent_id FROM media_items "
                    "WHERE (debrid_folder_name = ? OR filled_by_title = ? OR original_scraped_torrent_title = ?) "
                    "AND state IN ('Collected','Upgrading','Checking') LIMIT 1",
                    (entry_name, entry_name, entry_name)
                ).fetchone()
                if seed:
                    torrent_id_seed = seed[1] or ''
                    if torrent_id_seed:
                        # Step 2: get ALL items with same filled_by_torrent_id (whole season pack)
                        rows = _conn_af.execute(
                            "SELECT id FROM media_items WHERE filled_by_torrent_id = ? "
                            "AND state IN ('Collected','Upgrading','Checking')",
                            (torrent_id_seed,)
                        ).fetchall()
                        item_ids = [r[0] for r in rows]
                    else:
                        item_ids = [seed[0]]
            finally:
                _conn_af.close()

        if not item_ids:
            return jsonify({'success': False, 'error': 'No items found to delete'}), 404

        results = {'wanted': 0, 'plex': 0, 'cli_mount': False}

        # Get infohash from the first item (all siblings share the same entry)
        first_item = dict(get_media_item_by_id(item_ids[0]) or {})
        info_hash = ''
        torrent_id = first_item.get('filled_by_torrent_id', '')
        if torrent_id.startswith('nzb:'):
            info_hash = torrent_id[4:]
        else:
            m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', first_item.get('filled_by_magnet', ''), _re.IGNORECASE)
            info_hash = m.group(1).lower() if m else ''

        # 1+2. Reset all items to Wanted and delete from Plex
        for item_id in item_ids:
            item = get_media_item_by_id(item_id)
            if not item:
                continue
            item = dict(item)
            try:
                update_media_item_state(item_id, 'Wanted')
                results['wanted'] += 1
            except Exception as e:
                logging.warning(f'[ActiveFiles] DB reset failed for {item_id}: {e}')
            try:
                # For episodes, skip ms_item_id (show-level key — deletes entire show)
                # and use location_on_disk path instead to target only this episode.
                _item_for_plex = dict(item)
                if _item_for_plex.get('type') == 'episode':
                    _item_for_plex['ms_item_id'] = None
                if _delete_from_plex(_item_for_plex):
                    results['plex'] += 1
            except Exception as e:
                logging.warning(f'[ActiveFiles] Plex delete failed for {item_id}: {e}')

        # 3. cli_mount removal intentionally skipped — the entry_name from an active
        # stream maps to one cli_mount entry which may contain multiple seasons or
        # episodes that share it. Removing the entry would delete files for all of
        # them. Items reset to Wanted will re-scrape and re-add naturally.

        logging.info(f'[ActiveFiles] Deleted entry {entry_name!r}: wanted={results["wanted"]}, plex={results["plex"]}, cli_mount={results["cli_mount"]}')
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logging.error(f'[ActiveFiles] Delete error: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/<torrent_id>', methods=['GET'])
@admin_required
def api_torrent_detail(torrent_id):
    try:
        provider = get_debrid_provider()
        detail = provider.get_torrent_info(torrent_id)
        return jsonify({'success': True, 'torrent': detail})
    except Exception as e:
        logging.error(f"Debrid Manager torrent detail error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/<torrent_id>', methods=['DELETE'])
@admin_required
def api_delete_torrent(torrent_id):
    try:
        provider = get_debrid_provider()
        provider.remove_torrent(torrent_id, removal_reason='Manual removal via Debrid Manager')
        _invalidate_library_cache()
        _reconcile_cache['data'] = None
        with _lib['lock']:
            if _lib['stable']:
                _lib['stable']['torrents'] = [
                    t for t in _lib['stable']['torrents'] if t.get('id') != torrent_id
                ]
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Debrid Manager delete error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/torrent/batch-delete', methods=['POST'])
@admin_required
def api_batch_delete():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'error': 'No IDs provided'}), 400

    provider = get_debrid_provider()
    results = {'deleted': [], 'failed': []}

    def delete_one(tid):
        try:
            provider.remove_torrent(tid, removal_reason='Batch removal via Debrid Manager')
            return tid, None
        except Exception as e:
            return tid, str(e)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(delete_one, tid): tid for tid in ids}
        for future in as_completed(futures):
            tid, err = future.result()
            if err:
                results['failed'].append({'id': tid, 'error': err})
            else:
                results['deleted'].append(tid)

    if results['deleted']:
        _invalidate_library_cache()
        _reconcile_cache['data'] = None
        deleted_set = set(results['deleted'])
        with _lib['lock']:
            if _lib['stable']:
                _lib['stable']['torrents'] = [
                    t for t in _lib['stable']['torrents'] if t.get('id') not in deleted_set
                ]

    return jsonify({'success': True, **results})


@debrid_manager_bp.route('/api/torrent/batch-reinsert', methods=['POST'])
@admin_required
def api_batch_reinsert():
    data = request.get_json(silent=True) or {}
    torrents = data.get('torrents', [])  # list of {id, hash, filename}
    if not torrents:
        return jsonify({'success': False, 'error': 'No torrents provided'}), 400

    provider_index = int(data.get('provider_index', 0))
    try:
        all_providers = get_debrid_providers()
        provider = all_providers[provider_index] if provider_index < len(all_providers) else all_providers[0]
    except Exception:
        provider = get_debrid_provider()
    results = {'reinserted': [], 'failed': []}

    def _hash_exists_on_provider(h):
        try:
            if hasattr(provider, '_find_existing_torrent'):
                ex = provider._find_existing_torrent(h)
                return str(ex.get('id', '')) if ex else None
            elif hasattr(provider, '_find_by_hash'):
                ex = provider._find_by_hash(h)
                return str(ex.get('id', '')) if ex else None
            elif hasattr(provider, 'get_torrents'):
                for t in (provider.get_torrents() or []):
                    if (t.get('hash') or '').lower() == h.lower():
                        return str(t.get('id', ''))
        except Exception:
            pass
        return None

    def reinsert_one(t):
        tid = t.get('id', '')
        h   = t.get('hash', '').lower()
        fn  = t.get('filename', '')
        if not h:
            return tid, 'missing hash'
        try:
            from urllib.parse import quote
            # Pre-check: skip add if already on target provider
            existing_id = _hash_exists_on_provider(h)
            if existing_id:
                logging.info(f'[Reinsert] Hash already on {provider.PROVIDER_NAME} as {existing_id}, skipping add')
                return tid, None
            magnet = f'magnet:?xt=urn:btih:{h}'
            if fn:
                magnet += f'&dn={quote(fn)}'
            new_id = provider.add_torrent(magnet)
            if new_id is None:
                # Provider found it already exists (e.g. RD duplicate 404)
                logging.info(f'[Reinsert] add_torrent returned None (already exists) for hash {h}')
                return tid, None
            if tid and str(new_id) != str(tid):
                try:
                    provider.remove_torrent(tid)
                except Exception:
                    pass
            time.sleep(0.5)
            return tid, None
        except Exception as e:
            return tid, str(e)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(reinsert_one, t): t for t in torrents}
        for future in as_completed(futures):
            tid, err = future.result()
            if err:
                results['failed'].append({'id': tid, 'error': err})
            else:
                results['reinserted'].append(tid)

    if results['reinserted']:
        _invalidate_library_cache()

    return jsonify({'success': True, **results})


@debrid_manager_bp.route('/api/torrent/<torrent_id>/reinsert', methods=['POST'])
@admin_required
def api_reinsert_torrent(torrent_id):
    data = request.get_json(silent=True) or {}
    torrent_hash = data.get('hash', '')
    filename = data.get('filename', '')
    if not torrent_hash:
        return jsonify({'success': False, 'error': 'hash required'}), 400
    try:
        provider_index = int(data.get('provider_index', 0))
        try:
            all_providers = get_debrid_providers()
            provider = all_providers[provider_index] if provider_index < len(all_providers) else all_providers[0]
        except Exception:
            provider = get_debrid_provider()
        from urllib.parse import quote
        torrent_hash = torrent_hash.lower()
        # Pre-check: if already on target provider, skip add
        existing_id = None
        try:
            if hasattr(provider, '_find_existing_torrent'):
                ex = provider._find_existing_torrent(torrent_hash)
                existing_id = str(ex.get('id', '')) if ex else None
            elif hasattr(provider, '_find_by_hash'):
                ex = provider._find_by_hash(torrent_hash)
                existing_id = str(ex.get('id', '')) if ex else None
            elif hasattr(provider, 'get_torrents'):
                for t in (provider.get_torrents() or []):
                    if (t.get('hash') or '').lower() == torrent_hash:
                        existing_id = str(t.get('id', ''))
                        break
        except Exception:
            pass
        if existing_id:
            logging.info(f'[Reinsert] Hash already on {provider.PROVIDER_NAME} as {existing_id}, skipping add')
            _invalidate_library_cache()
            return jsonify({'success': True, 'new_id': existing_id, 'already_existed': True})
        magnet = f'magnet:?xt=urn:btih:{torrent_hash}'
        if filename:
            magnet += f'&dn={quote(filename)}'
        new_id = provider.add_torrent(magnet)
        if new_id is None:
            logging.info(f'[Reinsert] add_torrent returned None (already exists) for {torrent_id}')
            _invalidate_library_cache()
            return jsonify({'success': True, 'new_id': torrent_id, 'already_existed': True})
        if str(new_id) != str(torrent_id):
            try:
                provider.remove_torrent(torrent_id)
            except Exception:
                pass
        _invalidate_library_cache()
        return jsonify({'success': True, 'new_id': new_id})
    except Exception as e:
        logging.error(f"Debrid Manager reinsert error for {torrent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/nzb/reinsert', methods=['POST'])
@admin_required
def api_nzb_reinsert():
    """
    Re-submit a Plex-trashed NZB item to cli_mount.

    Step 1 — try the stored NZB URL from scrape_results.
    Step 2 — if that fails, run a targeted re-scrape and submit the best result.

    Body: { item_id: int, nzb_url: str, nzb_title: str, rating_key: str }
    """
    try:
        data = request.get_json(force=True) or {}
        item_id = data.get('item_id')
        nzb_url = (data.get('nzb_url') or '').strip()
        nzb_title = (data.get('nzb_title') or '').strip()
        rating_key = data.get('rating_key', '')

        if not item_id:
            return jsonify({'success': False, 'error': 'item_id required'}), 400

        from database.core import get_db_connection as _gdbc
        conn = _gdbc()
        row = conn.execute(
            "SELECT id, title, year, type, imdb_id, tmdb_id, season_number, "
            "episode_number, version, genres, filled_by_torrent_id, scrape_results "
            "FROM media_items WHERE id = ?", (item_id,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'error': 'Item not found in database'}), 404

        item = dict(zip(
            ['id', 'title', 'year', 'type', 'imdb_id', 'tmdb_id', 'season_number',
             'episode_number', 'version', 'genres', 'filled_by_torrent_id', 'scrape_results'],
            row
        ))

        from usenet import get_usenet_client, reset_usenet_client
        reset_usenet_client()
        client = get_usenet_client()

        new_job_id = None
        used_url = None
        method = None
        new_release_title = nzb_title  # updated if rescrape finds a different release

        # ── Step 1: try stored NZB URL ──────────────────────────────────────
        if nzb_url:
            try:
                import requests as _req
                resp = _req.get(nzb_url, timeout=15, allow_redirects=True,
                                headers={'User-Agent': 'Sabnzbd/3.0.0'})
                if resp.status_code == 200 and '<nzb' in resp.text.lower():
                    new_job_id = client.add_nzb_content(
                        nzb_content=resp.text,
                        title=nzb_title or item['title']
                    )
                    if new_job_id:
                        used_url = nzb_url
                        method = 'url'
                        logging.info(f"[ReNZB] item={item_id} re-submitted via stored URL, job={new_job_id}")
            except Exception as _e1:
                logging.warning(f"[ReNZB] item={item_id} Step 1 failed: {_e1}")

        # ── Step 2: fallback — re-scrape and submit best result ─────────────
        if not new_job_id:
            logging.info(f"[ReNZB] item={item_id} falling back to re-scrape")
            try:
                from usenet.repair_engine import (
                    _scrape_for_replacement, _submit_and_confirm_replacement,
                    _update_db_for_repair, _is_junk_replacement_result,
                )
                results = _scrape_for_replacement(item, broken_nzb_title=nzb_title)
                for candidate in results or []:
                    if _is_junk_replacement_result(candidate):
                        continue
                    job_id, submitted_title, disposition = _submit_and_confirm_replacement(
                        candidate, item['title'], item=item)
                    if disposition in ('failed', 'rejected') or not job_id:
                        logging.warning(f"[ReNZB] item={item_id} candidate {disposition}: {candidate.get('title')!r}")
                        continue
                    new_job_id = job_id
                    best = dict(candidate)
                    if submitted_title:
                        best['title'] = submitted_title
                    method = 'rescrape'
                    new_release_title = best.get('title') or nzb_title
                    logging.info(f"[ReNZB] item={item_id} re-scraped and submitted, job={new_job_id}")
                    _update_db_for_repair(item, new_job_id, best, results)
                    _plex_trash_items_cache['items'] = None
                    return jsonify({'success': True, 'job_id': new_job_id, 'method': method, 'new_release_title': new_release_title})
            except Exception as _e2:
                logging.error(f"[ReNZB] item={item_id} Step 2 failed: {_e2}", exc_info=True)

        if not new_job_id:
            return jsonify({'success': False, 'error': 'Failed to re-submit NZB — URL expired and re-scrape found no results'}), 502

        # Update DB with new job ID (URL path — state stays Collected, just refresh torrent ID)
        try:
            from database.database_writing import update_media_item as _umi
            _umi(item_id, filled_by_torrent_id=f'nzb:{new_job_id}', fall_back_to_single_scraper=False)
        except Exception as _dbe:
            logging.warning(f"[ReNZB] DB update failed for item {item_id}: {_dbe}")

        _plex_trash_items_cache['items'] = None
        return jsonify({'success': True, 'job_id': new_job_id, 'method': method, 'new_release_title': new_release_title})

    except Exception as e:
        logging.error(f"[ReNZB] Unexpected error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/library')
@admin_required
def api_library():
    global _lib_last_accessed
    force = request.args.get('force') == '1'
    now   = time.time()
    _lib_last_accessed = now  # track for idle eviction

    with _lib['lock']:
        stable   = _lib['stable']
        is_loading = _lib['loading']
        partial  = list(_lib['partial'])       # snapshot to avoid holding lock

    stable_age = int(now - stable['fetched_at']) if stable else None
    is_fresh   = stable is not None and stable_age < _LIB_TTL

    # ── 1. Complete fresh cache ─────────────────────────────────────────────
    if is_fresh and not is_loading and not force:
        return jsonify({
            'success': True, 'loading': False,
            'cache_status': 'fresh', 'cache_age': stable_age,
            'torrents': stable['torrents'], 'total': stable['total'],
            'total_bytes': stable['total_bytes'],
        })

    # ── 2. Already loading ──────────────────────────────────────────────────
    if is_loading and not force:
        if stable:
            # Stale refresh — serve old complete snapshot while new one builds
            return jsonify({
                'success': True, 'loading': True,
                'cache_status': 'refreshing', 'cache_age': stable_age,
                'torrents': stable['torrents'], 'total': stable['total'],
                'total_bytes': stable['total_bytes'],
            })
        else:
            # First load — serve whatever partial data we have so far
            total_bytes = sum(t.get('bytes', 0) or 0 for t in partial)
            return jsonify({
                'success': True, 'loading': True,
                'cache_status': 'partial', 'cache_age': 0,
                'torrents': partial, 'total': len(partial),
                'total_bytes': total_bytes,
            })

    provider = get_debrid_provider()
    _uses_pagination = (provider.PROVIDER_NAME == 'Real-Debrid')

    # ── 3. Complete but stale — serve stale, trigger background re-fetch ────
    if stable and not is_fresh and not is_loading and not force:
        with _lib['lock']:
            _lib['gen'] += 1
            gen = _lib['gen']
            _lib['loading'] = True
            _lib['partial'] = []
        if _uses_pagination:
            threading.Thread(target=_fetch_pages_bg,
                             args=(provider.api_key, 1, gen), daemon=True).start()
        else:
            threading.Thread(target=_fetch_all_bg,
                             args=(gen,), daemon=True).start()
        return jsonify({
            'success': True, 'loading': True,
            'cache_status': 'stale', 'cache_age': stable_age,
            'torrents': stable['torrents'], 'total': stable['total'],
            'total_bytes': stable['total_bytes'],
        })

    # ── 4. No cache or force refresh — fetch synchronously ──────────────────
    with _lib['lock']:
        _lib['gen'] += 1
        gen = _lib['gen']
        _lib['stable']  = None
        _lib['partial'] = []
        _lib['loading'] = True

    initial = []
    try:
        if not _uses_pagination:
            # Non-paginated providers: Premiumize uses completed-only list for
            # the library snapshot; others use list_active_torrents
            if hasattr(provider, 'list_completed_torrents'):
                initial = provider.list_completed_torrents() or []
            else:
                initial = provider.list_active_torrents() or []
            # Ensure status is always a plain string (enum .value may be int for UNKNOWN)
            for t in initial:
                if 'status' in t and not isinstance(t['status'], str):
                    t['status'] = str(t['status'])
            _enrich_with_db(initial)
            new_stable = {
                'torrents':    initial,
                'total':       len(initial),
                'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
                'fetched_at':  time.time(),
            }
            with _lib['lock']:
                if _lib['gen'] == gen:
                    _lib['stable'] = new_stable
                    _lib['partial'] = []
                    _lib['loading'] = False
            _save_lib_cache_to_db(new_stable)
            return jsonify({
                'success': True, 'loading': False,
                'cache_status': 'live', 'cache_age': 0,
                'torrents': initial, 'total': len(initial),
                'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
            })
        else:
            # RD: fetch initial pages synchronously; if all fit, cache and return.
            # If more pages exist, fall through to background thread below.
            from debrid.real_debrid.api import make_request
            api_key = provider.api_key
            all_fit = False
            for page in range(1, _INITIAL_PAGES + 1):
                result = make_request('GET', '/torrents', api_key,
                                      params={'limit': _PAGE_SIZE, 'page': page})
                if not isinstance(result, list) or not result:
                    all_fit = True
                    break
                initial.extend(result)
                if len(result) < _PAGE_SIZE:
                    # Partial page — all torrents fetched
                    all_fit = True
                    break
            if all_fit:
                _enrich_with_db(initial)
                new_stable = {
                    'torrents':    initial,
                    'total':       len(initial),
                    'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
                    'fetched_at':  time.time(),
                }
                with _lib['lock']:
                    if _lib['gen'] == gen:
                        _lib['stable'] = new_stable
                        _lib['partial'] = []
                        _lib['loading'] = False
                _save_lib_cache_to_db(new_stable)
                return jsonify({
                    'success': True, 'loading': False,
                    'cache_status': 'live', 'cache_age': 0,
                    'torrents': initial, 'total': len(initial),
                    'total_bytes': sum(t.get('bytes', 0) or 0 for t in initial),
                })
            # More pages exist — fall through to background thread
    except Exception as e:
        logging.error(f"Library initial fetch error: {e}")
        with _lib['lock']:
            if _lib['gen'] == gen:
                _lib['loading'] = False
        return jsonify({'success': False, 'error': str(e)}), 500

    # Seed partial with initial pages, launch background for the rest
    with _lib['lock']:
        if _lib['gen'] == gen:
            _lib['partial'] = initial

    if _uses_pagination:
        threading.Thread(target=_fetch_pages_bg,
                         args=(provider.api_key, _INITIAL_PAGES + 1, gen), daemon=True).start()

    total_bytes = sum(t.get('bytes', 0) or 0 for t in initial)
    return jsonify({
        'success': True, 'loading': True,
        'cache_status': 'partial', 'cache_age': 0,
        'torrents': initial, 'total': len(initial),
        'total_bytes': total_bytes,
    })


# ---------------------------------------------------------------------------
# Backup / Restore API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cleanup API
# ---------------------------------------------------------------------------


@debrid_manager_bp.route('/api/usage')
@admin_required
def api_usage():
    """Return account info + traffic details for the Usage tab."""
    try:
        from datetime import datetime
        provider = get_debrid_provider()
        if not provider:
            return jsonify({'error': 'No debrid provider configured'}), 503
        sub = provider.get_subscription_status()

        # Get traffic data via provider abstraction (handles RD and AllDebrid differences)
        traffic = {}
        try:
            traffic = provider.get_user_traffic() or {}
        except Exception as e:
            logging.warning(f"Usage traffic fetch failed: {e}")

        # RD returns date-keyed traffic history; AllDebrid doesn't have this concept
        traffic_details = traffic.get('traffic_details', {})

        today_utc   = datetime.utcnow().strftime("%Y-%m-%d")
        today_data  = traffic_details.get(today_utc, {})
        today_bytes = today_data.get('bytes', 0)

        history = []
        for date_str in sorted(traffic_details.keys(), reverse=True):
            day = traffic_details[date_str]
            b   = day.get('bytes', 0)
            hosters_raw = day.get('host', {}) or day.get('hosters', {}) or {}
            hosters = []
            for name, info in hosters_raw.items():
                if isinstance(info, dict):
                    hb = info.get('bytes', 0) or info.get('downloaded', 0)
                else:
                    hb = int(info or 0)
                hosters.append({'name': name, 'bytes': hb,
                                'gb': round(hb / (1024 ** 3), 2)})
            history.append({
                'date':    date_str,
                'bytes':   b,
                'gb':      round(b / (1024 ** 3), 2),
                'hosters': hosters,
            })

        total_all_bytes = sum(
            traffic_details[d].get('bytes', 0) for d in traffic_details
        )
        # Fallback: providers without traffic history may expose all-time bytes directly
        if not total_all_bytes:
            total_all_bytes = (
                traffic.get('total_bytes_downloaded', 0) or
                sub.get('total_bytes_downloaded', 0) or 0
            )

        try:
            active_count, max_dl = provider.get_active_downloads()
        except Exception:
            active_count, max_dl = 0, 0

        return jsonify({
            'success': True,
            'has_traffic_history': bool(traffic_details),
            'user': {
                'username':       sub.get('username', ''),
                'email':          sub.get('email', ''),
                'type':           sub.get('type', ''),
                'premium':        bool(sub.get('premium', False)),
                'expiration':     sub.get('expiration', ''),
                'days_remaining': sub.get('days_remaining'),
                'points':         sub.get('points', 0),  # None hides the row in JS
                'locale':         sub.get('locale', ''),
                # Premiumize-specific extras (ignored by other providers)
                'space_used':     sub.get('space_used'),
                'limit_used':     sub.get('limit_used'),
                # Torbox-specific extras (ignored by other providers)
                'created_at':     sub.get('created_at'),
                'cooldown_until': sub.get('cooldown_until'),
            },
            'today': {
                'date':          today_utc,
                'downloaded_gb': round(today_bytes / (1024 ** 3), 2),
                'limit_gb':      2000,
            },
            'totals': {
                'all_time_gb':   round(total_all_bytes / (1024 ** 3), 2),
                'active_slots':  active_count,
                'max_slots':     max_dl,
            },
            'history': history,
            # Debrid-Link specific — raw limits dict + live speeds (None for all other providers)
            'dl_limits':         traffic.get('limits', {}) if 'limits' in traffic else {},
            'dl_download_speed': traffic.get('download_speed') if 'download_speed' in traffic else None,
            'dl_upload_speed':   traffic.get('upload_speed')   if 'upload_speed'   in traffic else None,
            'dl_peers':          traffic.get('peers_connected') if 'peers_connected' in traffic else None,
        })
    except Exception as e:
        logging.error(f"Usage API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/history')
@admin_required
def api_history():
    try:
        raw = get_recent_additions(1000)
        entries = []
        for row in raw:
            e = dict(row)
            item_data = {}
            if e.get('item_data'):
                try:
                    item_data = json.loads(e['item_data'])
                except Exception:
                    pass

            title = (
                item_data.get('filled_by_title')
                or item_data.get('title', '')
            )
            year = item_data.get('year', '')
            season = item_data.get('season_number')
            episode = item_data.get('episode_number')
            media_type = item_data.get('type') or item_data.get('media_type', '')

            label = title
            if year:
                label += f' ({year})'
            if season is not None:
                label += f' S{str(season).zfill(2)}'
                if episode is not None:
                    label += f'E{str(episode).zfill(2)}'

            entries.append({
                'id': e.get('id'),
                'torrent_hash': e.get('torrent_hash', ''),
                'timestamp': str(e.get('timestamp', '')),
                'trigger_source': e.get('trigger_source', ''),
                'rationale': e.get('rationale', ''),
                'is_still_present': e.get('is_still_present', True),
                'removal_reason': e.get('removal_reason', ''),
                'removal_timestamp': str(e.get('removal_timestamp', '') or ''),
                'label': label,
                'media_type': media_type,
                'item_data': e.get('item_data', ''),
                'trigger_details': e.get('trigger_details', ''),
                'additional_metadata': e.get('additional_metadata', ''),
            })
        return jsonify({'success': True, 'entries': entries})
    except Exception as e:
        logging.error(f"Debrid Manager history API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
_reconcile_cache = {'data': None, 'ts': 0}


@debrid_manager_bp.route('/api/clear_cache', methods=['POST'])
@admin_required
def api_clear_cache():
    """Clear the RD library cache and reconcile cache, forcing a full re-fetch."""
    global _lib_db_loaded
    with _lib['lock']:
        _lib['stable']  = None
        _lib['partial'] = []
        _lib['loading'] = False
        _lib['gen']    += 1
    _lib_db_loaded = False
    _reconcile_cache['data'] = None
    _reconcile_cache['ts']   = 0
    # Also clear the persisted RD library cache in DB so stale data isn't reloaded
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        conn.execute("DELETE FROM rd_library_cache")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f'[LibCache] Could not clear rd_library_cache table: {e}')
    logging.info('[LibCache] Cache cleared manually via Debrid Manager')
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Symlink Audit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Junk Symlink Audit (bad/split/NZB-mismatch files in symlink mode)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Battery Audit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bad Torrent Audit (climount __bad__ folder)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# All-folder audit (__all__) — same ffprobe logic, different folder + state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Usenet Repair API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Debrid (RealDebrid torrent) Health & Repair API
# ---------------------------------------------------------------------------


# The rest of the Debrid Manager routes, one module per area. Imported last: they import
# the blueprint and shared state from this module.
from routes.debrid_manager import plex_trash  # noqa: E402,F401
from routes.debrid_manager import backups  # noqa: E402,F401
from routes.debrid_manager import symlink_audits  # noqa: E402,F401
from routes.debrid_manager import reconcile  # noqa: E402,F401
from routes.debrid_manager import battery_audit  # noqa: E402,F401
from routes.debrid_manager import torrent_audits  # noqa: E402,F401
from routes.debrid_manager import repair  # noqa: E402,F401
