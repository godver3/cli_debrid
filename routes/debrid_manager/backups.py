"""Debrid backups and restores (including restore to another provider) and cleanup.

Part of the Debrid Manager blueprint (routes/debrid_manager_routes.py).
"""
from routes.models import admin_required
from flask import jsonify
import logging
from flask import request
import threading
import time
from routes.debrid_manager_routes import (
    debrid_manager_bp,
)


@debrid_manager_bp.route('/api/backup/status')
@admin_required
def api_backup_status():
    try:
        from utilities.debrid_backup import get_backup_status
        status = get_backup_status()
        return jsonify({'success': True, **status})
    except Exception as e:
        logging.error(f'Debrid backup status error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/files')
@admin_required
def api_backup_files():
    try:
        from utilities.debrid_backup import list_backup_files
        return jsonify({'success': True, 'files': list_backup_files()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/settings', methods=['POST'])
@admin_required
def api_backup_settings_save():
    data = request.get_json(silent=True) or {}
    try:
        from utilities.settings import set_setting
        set_setting('Debrid Backup', 'enabled', bool(data.get('enabled', False)))
        set_setting('Debrid Backup', 'slot_1d_hours', int(data.get('slot_1d_hours', 24)))
        set_setting('Debrid Backup', 'slot_3d_hours', int(data.get('slot_3d_hours', 72)))
        set_setting('Debrid Backup', 'slot_7d_hours', int(data.get('slot_7d_hours', 168)))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/run', methods=['POST'])
@admin_required
def api_backup_run():
    """Trigger a manual backup (force=True bypasses the enabled check)."""
    try:
        from utilities.debrid_backup import run_backup
        result = run_backup(force=True)
        return jsonify(result)
    except Exception as e:
        logging.error(f'Manual backup error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/backup/restore', methods=['POST'])
@admin_required
def api_backup_restore():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename', '')
    dry_run = bool(data.get('dry_run', False))
    if not filename:
        return jsonify({'success': False, 'error': 'filename required'}), 400
    try:
        from utilities.debrid_backup import restore_from_file
        result = restore_from_file(filename, dry_run=dry_run)
        return jsonify(result)
    except Exception as e:
        logging.error(f'Restore error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


_xp_restore_state = {
    'status': 'idle',   # idle | running | done | error
    'current': 0,
    'total': 0,
    'current_name': '',
    'result': None,
    'error': None,
    'started_at': 0,
}


_xp_restore_lock = threading.Lock()


_XP_RESTORE_STALE_AFTER = 1800  # 30 minutes


@debrid_manager_bp.route('/api/backup/restore-to-provider', methods=['POST'])
@admin_required
def api_backup_restore_to_provider():
    data = request.get_json(silent=True) or {}
    filename     = data.get('filename', '')
    provider_id  = data.get('provider_id', '')
    api_key      = data.get('api_key', '')
    dry_run      = bool(data.get('dry_run', False))
    cached_only  = bool(data.get('cached_only', False))
    if not filename:
        return jsonify({'success': False, 'error': 'filename required'}), 400
    if not provider_id:
        return jsonify({'success': False, 'error': 'provider_id required'}), 400
    if not api_key:
        return jsonify({'success': False, 'error': 'api_key required'}), 400

    with _xp_restore_lock:
        if _xp_restore_state['status'] == 'running':
            age = time.time() - _xp_restore_state.get('started_at', 0)
            if age < _XP_RESTORE_STALE_AFTER:
                return jsonify({'success': False, 'error': 'A restore is already in progress'}), 409
            logging.warning(f'[XPRestore] Stale running state ({age:.0f}s), resetting')
        _xp_restore_state.update({'status': 'running', 'current': 0, 'total': 0,
                                   'current_name': '', 'result': None, 'error': None,
                                   'started_at': time.time()})

    def _run():
        try:
            from utilities.debrid_backup import restore_from_file_to_provider
            logging.info(f'[XPRestore] Starting: {filename} -> {provider_id}, dry_run={dry_run}, cached_only={cached_only}')
            result = restore_from_file_to_provider(
                filename, provider_id, api_key,
                dry_run=dry_run, cached_only=cached_only,
                progress_cb=_xp_progress_cb,
            )
            logging.info(f'[XPRestore] Done: {result}')
            with _xp_restore_lock:
                _xp_restore_state.update({'status': 'done', 'result': result,
                                           'current_name': '', 'error': None})
        except Exception as e:
            logging.error(f'[XPRestore] Error: {e}', exc_info=True)
            with _xp_restore_lock:
                _xp_restore_state.update({'status': 'error', 'error': str(e), 'result': None})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'status': 'started'})


def _xp_progress_cb(current, total, name):
    with _xp_restore_lock:
        _xp_restore_state.update({'current': current, 'total': total, 'current_name': name})


@debrid_manager_bp.route('/api/backup/restore-to-provider/progress')
@admin_required
def api_backup_restore_to_provider_progress():
    with _xp_restore_lock:
        s = dict(_xp_restore_state)
    return jsonify(s)


@debrid_manager_bp.route('/api/backup/restore-to-provider/reset', methods=['POST'])
@admin_required
def api_backup_restore_to_provider_reset():
    with _xp_restore_lock:
        _xp_restore_state.update({'status': 'idle', 'current': 0, 'total': 0,
                                   'current_name': '', 'result': None, 'error': None, 'started_at': 0})
    return jsonify({'success': True})


@debrid_manager_bp.route('/api/cleanup/settings', methods=['POST'])
@admin_required
def api_cleanup_settings_save():
    data = request.get_json(silent=True) or {}
    try:
        from utilities.settings import set_setting
        set_setting('Debrid Cleanup', 'enabled',        bool(data.get('enabled', False)))
        set_setting('Debrid Cleanup', 'delete_errors',  bool(data.get('delete_errors', True)))
        set_setting('Debrid Cleanup', 'delete_dupes',   bool(data.get('delete_dupes', True)))
        set_setting('Debrid Cleanup', 'delete_stalled', bool(data.get('delete_stalled', False)))
        set_setting('Debrid Cleanup', 'stalled_days',   int(data.get('stalled_days', 3)))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@debrid_manager_bp.route('/api/cleanup/run', methods=['POST'])
@admin_required
def api_cleanup_run():
    """Trigger a manual cleanup (force=True bypasses the enabled check)."""
    try:
        from utilities.debrid_backup import run_cleanup
        result = run_cleanup(force=True)
        return jsonify(result)
    except Exception as e:
        logging.error(f'Manual cleanup error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500
