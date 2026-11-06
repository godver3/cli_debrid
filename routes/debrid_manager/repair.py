"""Usenet and debrid Health & Repair: scans, broken items, single and bulk repairs, settings.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
from flask import jsonify
import logging
from flask import request
import threading
import time
from routes.debrid_manager_routes import (
    debrid_manager_bp,
)


_repair_thread = None


_repair_progress = {}  # shared state for the running repair


_debrid_repair_thread = None


_debrid_repair_progress = {}  # shared state for the running debrid repair


@debrid_manager_bp.route('/api/usenet/repair/stats')
def usenet_repair_stats():
    """Stats for the Usenet tab: cli_mount health summary + 30-day repair activity counts + available versions."""
    try:
        from usenet.repair_engine import get_health_summary, get_available_versions, get_repair_version
        from database.nzb_repair_activity import get_repair_stats
        health = get_health_summary()
        activity = get_repair_stats(days=30, source='usenet')
        versions = get_available_versions()
        repair_version = get_repair_version()
        return jsonify(success=True, health=health, activity=activity, versions=versions, repair_version=repair_version)
    except Exception as e:
        logging.error(f'[UsenetRepair] stats error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/activity')
def usenet_repair_activity():
    """Paginated repair activity log."""
    try:
        from database.nzb_repair_activity import get_repair_activity
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        outcome = request.args.get('outcome') or None
        rows, total = get_repair_activity(limit=limit, offset=offset, outcome=outcome, source='usenet')
        return jsonify(success=True, rows=rows, total=total)
    except Exception as e:
        logging.error(f'[UsenetRepair] activity error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/activity/clear', methods=['POST'])
def usenet_repair_activity_clear():
    """Clear the NZB repair activity log."""
    try:
        from database.nzb_repair_activity import clear_repair_activity
        deleted = clear_repair_activity(source='usenet')
        return jsonify(success=True, deleted=deleted)
    except Exception as e:
        logging.error(f'[UsenetRepair] activity clear error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/scan_status')
def usenet_scan_status():
    """Check if cli_mount is currently running a health sweep (active_run != null and running)."""
    try:
        from usenet.climount_client import get_climount_client
        from routes.api_tracker import api as _api
        client = get_climount_client()
        if not client.is_enabled():
            return jsonify(success=True, is_scanning=False)
        r = _api.get(f'{client.base_url}/api/repair/status', headers=client._headers(), timeout=10)
        if r.status_code != 200:
            return jsonify(success=True, is_scanning=False)
        data = r.json()
        active_run = data.get('active_run') or data.get('value', {}).get('active_run')
        run_status = (active_run or {}).get('status', '') if active_run else ''
        is_scanning = active_run is not None and run_status in ('running', 'pending', '')
        return jsonify(success=True, is_scanning=is_scanning, active_run=active_run)
    except Exception as e:
        logging.debug(f'[UsenetRepair] scan_status error: {e}')
        return jsonify(success=True, is_scanning=False)


@debrid_manager_bp.route('/api/usenet/repair/health_scan', methods=['POST'])
def usenet_trigger_health_scan():
    """Trigger a health scan for NZB entries. ?full=true scans all, default scans unchecked only."""
    try:
        from usenet.repair_engine import trigger_health_scan
        full = request.args.get('full', 'false').lower() in ('true', '1', 'yes')
        ok = trigger_health_scan(full=full)
        scan_type = 'full' if full else 'partial'
        return jsonify(success=ok, message=f'{scan_type.capitalize()} health scan triggered' if ok else 'Failed to trigger scan')
    except Exception as e:
        logging.error(f'[UsenetRepair] health_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/stop', methods=['POST'])
def usenet_stop_health_scan():
    """Stop a running NZB health scan in cli_mount."""
    try:
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')
        r = api.post(f'{url}/api/repair/stop', headers=_dcy_headers(), timeout=10)
        if r.status_code == 200:
            return jsonify(success=True, message='Health scan stop requested')
        return jsonify(success=False, error=f'cli_mount returned HTTP {r.status_code}')
    except Exception as e:
        logging.error(f'[UsenetRepair] stop_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/broken')
def usenet_broken_items():
    """Return current broken items from cli_mount health."""
    try:
        from usenet.repair_engine import fetch_broken_items
        # annotate_mount=False for fast response — mount verification
        # is only needed by the actual repair pipeline, not display.
        items = fetch_broken_items(annotate_mount=False)
        return jsonify(success=True, items=items, count=len(items))
    except Exception as e:
        logging.error(f'[UsenetRepair] broken items error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/fix_single', methods=['POST'])
def usenet_fix_single():
    """Repair a single broken entry by entry_name."""
    try:
        body = request.get_json(silent=True) or {}
        entry_name = body.get('entry_name', '').strip()
        version_override = body.get('version_override') or None
        if not entry_name:
            return jsonify(success=False, error='entry_name required'), 400
        from usenet.repair_engine import repair_single_entry
        result = repair_single_entry(entry_name, version_override=version_override)
        return jsonify(success=True, result=result)
    except Exception as e:
        logging.error(f'[UsenetRepair] fix_single error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/retry_exhausted', methods=['POST'])
def usenet_retry_exhausted():
    """Manually retry an item stuck at skipped_max_attempts: clean up the dead
    entry, blacklist it, and move the item back to Wanted for a fresh scrape."""
    try:
        body = request.get_json(silent=True) or {}
        item_id = body.get('item_id')
        broken_nzb_id = body.get('broken_nzb_id', '') or ''
        if not item_id:
            return jsonify(success=False, error='item_id required'), 400
        from usenet.repair_engine import retry_exhausted_item
        result = retry_exhausted_item(int(item_id), broken_nzb_id)
        return jsonify(success=result.get('outcome') == 'ok', result=result)
    except Exception as e:
        logging.error(f'[UsenetRepair] retry_exhausted error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/in_flight')
def usenet_repair_in_flight():
    """Return active playback-repair rows (verification, candidate search, etc.)."""
    try:
        from database.nzb_playback_repair import list_active_playback_repairs
        items = list_active_playback_repairs()
        return jsonify(success=True, items=items, count=len(items))
    except Exception as e:
        logging.error(f'[UsenetRepair] in_flight error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/cancel', methods=['POST'])
def usenet_repair_cancel():
    """Cancel an in-flight playback repair. action=cancel|wanted."""
    try:
        body = request.get_json(silent=True) or {}
        repair_id = body.get('repair_id')
        action = (body.get('action') or 'cancel').strip().lower()
        if not repair_id:
            return jsonify(success=False, error='repair_id required'), 400
        from database.nzb_playback_repair import cancel_playback_repair
        result = cancel_playback_repair(int(repair_id), move_to_wanted=(action == 'wanted'))
        return jsonify(success=result.get('outcome') == 'ok', result=result)
    except Exception as e:
        logging.error(f'[UsenetRepair] cancel error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/run', methods=['POST'])
def usenet_run_repair():
    """Kick off a repair run in background. Accepts optional version_override in JSON body."""
    global _repair_thread, _repair_progress

    if _repair_thread and _repair_thread.is_alive():
        return jsonify(success=False, message='Repair already running')

    body = request.get_json(silent=True) or {}
    version_override = body.get('version_override') or None

    _repair_progress = {'status': 'running', 'summary': None, 'error': None, 'started_at': time.time(),
                        'version_override': version_override}

    def _run():
        global _repair_progress
        try:
            from usenet.repair_engine import run_repair
            summary = run_repair(triggered_by='manual', version_override=version_override)
            _repair_progress['status'] = 'done'
            _repair_progress['summary'] = summary
        except Exception as e:
            logging.error(f'[UsenetRepair] run_repair thread error: {e}', exc_info=True)
            _repair_progress['status'] = 'error'
            _repair_progress['error'] = str(e)

    _repair_thread = threading.Thread(target=_run, daemon=True, name='usenet-repair')
    _repair_thread.start()
    return jsonify(success=True, message='Repair started')


@debrid_manager_bp.route('/api/usenet/repair/progress')
def usenet_repair_progress():
    """Poll repair run progress."""
    global _repair_thread, _repair_progress
    is_running = bool(_repair_thread and _repair_thread.is_alive())
    return jsonify(
        success=True,
        is_running=is_running,
        **_repair_progress,
    )


@debrid_manager_bp.route('/api/usenet/repair/delete_all_broken', methods=['POST'])
def usenet_delete_all_broken():
    """Delete all broken NZBs from cli_mount, Plex, and reset CLI DB items to Wanted."""
    try:
        from usenet.repair_engine import (
            fetch_broken_items, _find_db_items_by_entry_name,
            _delete_from_provider, _delete_from_plex,
        )
        from database.database_writing import update_media_item_state
        from database.not_wanted_magnets import add_to_not_wanted_nzb_guid

        broken = fetch_broken_items()
        if not broken:
            return jsonify(success=True, message='No broken items found', deleted_cli_mount=0, deleted_plex=0, reset_db=0)

        deleted_climount = 0
        deleted_plex = 0
        reset_db = 0

        for entry in broken:
            info_hash = entry.get('info_hash', '')
            entry_name = entry.get('entry_name', '') or entry.get('name', '')

            # Delete from cli_mount first
            if _delete_from_provider(info_hash, entry_name):
                deleted_climount += 1

            # Find matching DB items
            db_items = _find_db_items_by_entry_name(entry_name) if entry_name else []

            if db_items:
                for item in db_items:
                    # Delete from Plex
                    if _delete_from_plex(item):
                        deleted_plex += 1
                    # Reset to Wanted so it re-scrapes
                    try:
                        update_media_item_state(item['id'], 'Wanted')
                        reset_db += 1
                    except Exception as _dbe:
                        logging.warning(f'[DeleteBroken] DB reset failed for item {item.get("id")}: {_dbe}')
                    # Blacklist broken NZB URL so it won't be re-submitted
                    nzb_url = item.get('filled_by_magnet') or item.get('link', '')
                    if nzb_url:
                        try:
                            add_to_not_wanted_nzb_guid(nzb_url)
                        except Exception:
                            pass

        msg = (f'Deleted {deleted_climount} from cli_mount, '
               f'{deleted_plex} from Plex, '
               f'{reset_db} items reset to Wanted.')
        logging.info(f'[DeleteBroken] {msg}')
        return jsonify(success=True, message=msg,
                       deleted_climount=deleted_climount,
                       deleted_plex=deleted_plex,
                       reset_db=reset_db)
    except Exception as e:
        logging.error(f'[DeleteBroken] Error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/settings', methods=['GET'])
def usenet_repair_settings_get():
    try:
        from usenet.repair_engine import get_repair_version
        return jsonify(success=True, repair_version=get_repair_version())
    except Exception as e:
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/usenet/repair/settings', methods=['POST'])
def usenet_repair_settings_save():
    try:
        from usenet.repair_engine import set_repair_version
        body = request.get_json(silent=True) or {}
        set_repair_version(body.get('repair_version', ''))
        return jsonify(success=True)
    except Exception as e:
        logging.error(f'[UsenetRepair] save settings error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/stats')
def debrid_repair_stats():
    """Stats for the Debrid repair tab: cli_mount torrent health summary + 30-day activity counts."""
    try:
        from usenet.debrid_repair_engine import get_health_summary, get_repair_stats
        health = get_health_summary()
        activity = get_repair_stats(days=30)
        return jsonify(success=True, health=health, activity=activity)
    except Exception as e:
        logging.error(f'[DebridRepair] stats error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/activity')
def debrid_repair_activity():
    """Paginated repair activity log for debrid entries."""
    try:
        from usenet.debrid_repair_engine import get_repair_activity
        limit = int(request.args.get('limit', 25))
        offset = int(request.args.get('offset', 0))
        outcome = request.args.get('outcome') or None
        rows, total = get_repair_activity(limit=limit, offset=offset, outcome=outcome)
        return jsonify(success=True, rows=rows, total=total)
    except Exception as e:
        logging.error(f'[DebridRepair] activity error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/activity/clear', methods=['POST'])
def debrid_repair_activity_clear():
    """Clear the debrid repair activity log. Shares the nzb_repair_activity
    table with the NZB log, scoped by the same broken_nzb_id prefix the
    debrid engine's own reads use."""
    try:
        from database.nzb_repair_activity import clear_repair_activity
        deleted = clear_repair_activity(source='debrid')
        return jsonify(success=True, deleted=deleted)
    except Exception as e:
        logging.error(f'[DebridRepair] activity clear error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/scan_status')
def debrid_scan_status():
    """Check if cli_mount is currently running a health sweep."""
    try:
        from usenet.climount_client import get_climount_client
        from routes.api_tracker import api as _api
        client = get_climount_client()
        if not client.is_enabled():
            return jsonify(success=True, is_scanning=False)
        r = _api.get(f'{client.base_url}/api/repair/status', headers=client._headers(), timeout=10)
        if r.status_code != 200:
            return jsonify(success=True, is_scanning=False)
        data = r.json()
        active_run = data.get('active_run') or data.get('value', {}).get('active_run')
        run_status = (active_run or {}).get('status', '') if active_run else ''
        is_scanning = active_run is not None and run_status in ('running', 'pending', '')
        return jsonify(success=True, is_scanning=is_scanning, active_run=active_run)
    except Exception as e:
        logging.debug(f'[DebridRepair] scan_status error: {e}')
        return jsonify(success=True, is_scanning=False)


@debrid_manager_bp.route('/api/debrid/repair/health_scan', methods=['POST'])
def debrid_trigger_health_scan():
    """Trigger a health scan for torrent entries. ?full=true scans all, default scans unchecked only."""
    try:
        from usenet.debrid_repair_engine import trigger_health_scan
        full = request.args.get('full', 'false').lower() in ('true', '1', 'yes')
        ok = trigger_health_scan(full=full)
        scan_type = 'full' if full else 'partial'
        return jsonify(success=ok, message=f'{scan_type.capitalize()} health scan triggered' if ok else 'Failed to trigger scan')
    except Exception as e:
        logging.error(f'[DebridRepair] health_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/stop', methods=['POST'])
def debrid_stop_health_scan():
    """Stop a running torrent health scan in cli_mount."""
    try:
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')
        r = api.post(f'{url}/api/repair/stop', headers=_dcy_headers(), timeout=10)
        if r.status_code == 200:
            return jsonify(success=True, message='Health scan stop requested')
        return jsonify(success=False, error=f'cli_mount returned HTTP {r.status_code}')
    except Exception as e:
        logging.error(f'[DebridRepair] stop_scan error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/broken')
def debrid_broken_items():
    """Return current broken torrent items from cli_mount health."""
    try:
        from usenet.debrid_repair_engine import fetch_broken_items
        items = fetch_broken_items()
        return jsonify(success=True, items=items, count=len(items))
    except Exception as e:
        logging.error(f'[DebridRepair] broken items error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/fix_single', methods=['POST'])
def debrid_fix_single():
    """Repair a single broken debrid entry. action: 'reinsert' | 'replace'."""
    try:
        body = request.get_json(silent=True) or {}
        entry_name = body.get('entry_name', '').strip()
        info_hash = body.get('info_hash', '').strip()
        action = body.get('action', 'replace')
        version_override = body.get('version_override') or None
        broken_files = body.get('broken_files')
        if not isinstance(broken_files, list):
            broken_files = None
        if not broken_files and (body.get('file_name') or body.get('cli_debrid_id')):
            broken_files = [{
                'file_name': body.get('file_name') or '',
                'cli_debrid_id': body.get('cli_debrid_id'),
                'info_hash': info_hash,
            }]
        if not entry_name:
            return jsonify(success=False, error='entry_name required'), 400
        from usenet.debrid_repair_engine import reinsert_entry, replace_entry
        if action == 'reinsert':
            result = reinsert_entry(entry_name, info_hash)
        else:
            result = replace_entry(
                entry_name, info_hash,
                version_override=version_override,
                broken_files=broken_files,
            )
        return jsonify(success=True, result=result)
    except Exception as e:
        logging.error(f'[DebridRepair] fix_single error: {e}')
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/run', methods=['POST'])
def debrid_run_repair():
    """Kick off a debrid repair run in background."""
    global _debrid_repair_thread, _debrid_repair_progress

    if _debrid_repair_thread and _debrid_repair_thread.is_alive():
        return jsonify(success=False, message='Debrid repair already running')

    body = request.get_json(silent=True) or {}
    version_override = body.get('version_override') or None

    _debrid_repair_progress = {'status': 'running', 'summary': None, 'error': None,
                                'started_at': time.time(), 'version_override': version_override}

    def _run():
        global _debrid_repair_progress
        try:
            # Clean ghosts before repair so they don't show up as actionable broken items
            try:
                from usenet.debrid_repair_engine import delete_ghost_health_records
                ghost_result = delete_ghost_health_records()
                if ghost_result['deleted']:
                    logging.info(f'[DebridRepair] Ghost cleanup before repair: deleted={ghost_result["deleted"]}')
            except Exception as _ge:
                logging.warning(f'[DebridRepair] Ghost cleanup failed (continuing): {_ge}')
            from usenet.debrid_repair_engine import run_repair
            summary = run_repair(triggered_by='manual', version_override=version_override)
            _debrid_repair_progress['status'] = 'done'
            _debrid_repair_progress['summary'] = summary
        except Exception as e:
            logging.error(f'[DebridRepair] run_repair thread error: {e}', exc_info=True)
            _debrid_repair_progress['status'] = 'error'
            _debrid_repair_progress['error'] = str(e)

    _debrid_repair_thread = threading.Thread(target=_run, daemon=True, name='debrid-repair')
    _debrid_repair_thread.start()
    return jsonify(success=True, message='Debrid repair started')


@debrid_manager_bp.route('/api/debrid/repair/progress')
def debrid_repair_progress():
    """Poll debrid repair run progress."""
    global _debrid_repair_thread, _debrid_repair_progress
    is_running = bool(_debrid_repair_thread and _debrid_repair_thread.is_alive())
    return jsonify(
        success=True,
        is_running=is_running,
        **_debrid_repair_progress,
    )


@debrid_manager_bp.route('/api/debrid/repair/clear_bad_flags', methods=['POST'])
def debrid_clear_bad_flags():
    """Clear Bad=true flag on all cli_mount torrent entries by calling the sync endpoint.
    This recovers entries that cli_mount marked as bad after failed repair attempts."""
    try:
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api

        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')

        # Get all torrent entries from cli_mount
        r = api.get(f'{url}/api/repair/health', headers=_dcy_headers(), timeout=60)
        if r.status_code != 200:
            return jsonify(success=False, error=f'Health API returned {r.status_code}')

        data = r.json()
        entries = data if isinstance(data, list) else data.get('entries', [])
        torrent_entries = [e for e in entries if (e.get('protocol') or '').lower() == 'torrent']

        cleared = 0
        errors = 0
        for entry in torrent_entries:
            broken_files = entry.get('broken_files') or []
            info_hash = (entry.get('info_hash') or entry.get('hash') or
                        (broken_files[0].get('info_hash') if broken_files else '') or '')
            if not info_hash:
                continue
            try:
                sr = api.post(f'{url}/api/torrents/{info_hash}/sync',
                              headers=_dcy_headers(), timeout=15)
                if sr.status_code == 200:
                    cleared += 1
                else:
                    errors += 1
            except Exception:
                errors += 1

        msg = f'Cleared Bad flag on {cleared} entries ({errors} errors)'
        logging.info(f'[DebridRepair] {msg}')
        return jsonify(success=True, message=msg, cleared=cleared, errors=errors)
    except Exception as e:
        logging.error(f'[DebridRepair] clear_bad_flags error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/delete_ghost_entries', methods=['POST'])
def debrid_delete_ghost_entries():
    """Delete all ghost entries from cli_mount's repair health records."""
    try:
        from usenet.debrid_repair_engine import delete_ghost_health_records
        result = delete_ghost_health_records()
        msg = f'Deleted {result["deleted"]} ghost health records ({result["skipped"]} real entries skipped, {result["errors"]} errors)'
        return jsonify(success=True, message=msg, **result)
    except Exception as e:
        logging.error(f'[DebridRepair] delete_ghost_entries error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/delete_ghost_single', methods=['POST'])
def debrid_delete_ghost_single():
    """Delete a single ghost health record by entry name."""
    try:
        import urllib.parse as _up
        from usenet.debrid_repair_engine import _dcy_cfg, _dcy_headers
        from routes.api_tracker import api
        body = request.get_json(silent=True) or {}
        entry_name = (body.get('entry_name') or '').strip()
        if not entry_name:
            return jsonify(success=False, error='entry_name required'), 400
        url, _ = _dcy_cfg()
        if not url:
            return jsonify(success=False, error='cli_mount not configured')
        encoded = _up.quote(entry_name, safe='')
        r = api.delete(f'{url}/api/repair/health/{encoded}', headers=_dcy_headers(), timeout=15)
        if r.status_code == 200:
            logging.info(f'[DebridRepair] Deleted ghost health record: {entry_name!r}')
            return jsonify(success=True)
        return jsonify(success=False, error=f'cli_mount returned HTTP {r.status_code}')
    except Exception as e:
        logging.error(f'[DebridRepair] delete_ghost_single error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))


@debrid_manager_bp.route('/api/debrid/repair/delete_all_broken', methods=['POST'])
def debrid_delete_all_broken():
    """Delete all broken torrent entries from cli_mount, Plex, and reset CLI DB items to Wanted."""
    try:
        from usenet.debrid_repair_engine import delete_all_broken
        result = delete_all_broken()
        msg = (f'Deleted {result["deleted_climount"]} from cli_mount, '
               f'{result["deleted_plex"]} from Plex, '
               f'{result["reset_db"]} items reset to Wanted.')
        logging.info(f'[DebridRepair] {msg}')
        return jsonify(success=True, message=msg, **result)
    except Exception as e:
        logging.error(f'[DebridRepair] delete_all_broken error: {e}', exc_info=True)
        return jsonify(success=False, error=str(e))
