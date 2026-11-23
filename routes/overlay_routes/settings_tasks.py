"""Overlay settings and task triggers."""

from flask import jsonify, request
from routes.models import admin_required
from ._common import logger, overlay_bp

# ============================================
# Settings Endpoints
# ============================================

@overlay_bp.route('/api/overlays/plex-data-path-check', methods=['GET'])
@admin_required
def check_plex_data_path_route():
    """Check a Plex data path for Plex's Metadata folder (Overlay Settings save)."""
    from overlays.scheduled_tasks import check_plex_data_path
    return jsonify(check_plex_data_path(request.args.get('path', ''))), 200


@overlay_bp.route('/api/overlays/settings', methods=['GET'])
def get_overlay_settings():
    """Get overlay system settings."""
    try:
        from utilities.settings import load_config

        config = load_config()
        overlay_settings = config.get('Overlays', {})

        # Provide defaults if section doesn't exist yet
        if not overlay_settings:
            overlay_settings = {
                'overlays_enabled': False,
                'auto_generate': True,
                'sync_interval': 3600,
                'cleanup_enabled': False,
                'cleanup_interval': 86400,
                'cleanup_mode': 'move',
                'active_movie_layout_id': None,
                'active_tv_layout_id': None
            }

        return jsonify({
            'success': True,
            'settings': overlay_settings
        }), 200

    except Exception as e:
        logger.error(f"Error getting overlay settings: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@overlay_bp.route('/api/overlays/settings', methods=['POST'])
def update_overlay_settings():
    """Update overlay system settings."""
    try:
        from utilities.settings import load_config, save_config

        data = request.get_json()

        if not data:
            return jsonify({
                'success': False,
                'error': 'No data provided'
            }), 400

        config = load_config()

        # Initialize Overlays section if it doesn't exist
        if 'Overlays' not in config:
            config['Overlays'] = {}

        # Update settings
        for key, value in data.items():
            config['Overlays'][key] = value
            logger.info(f"Updated overlay setting: {key} = {value}")

        # Save configuration
        save_config(config)

        # If overlays were enabled/disabled, log it
        if 'overlays_enabled' in data:
            status = "enabled" if data['overlays_enabled'] else "disabled"
            logger.info(f"Overlays {status}")

        return jsonify({
            'success': True,
            'message': 'Settings updated successfully',
            'settings': config['Overlays']
        }), 200

    except Exception as e:
        logger.error(f"Error updating overlay settings: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ============================================
# Scheduled Task Endpoints
# ============================================

@overlay_bp.route('/api/overlays/tasks/sync', methods=['POST'])
def trigger_overlay_sync():
    """Manually trigger overlay sync task."""
    try:
        from overlays.scheduled_tasks import task_overlay_sync
        result = task_overlay_sync()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to trigger overlay sync: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/tasks/cleanup', methods=['POST'])
def trigger_overlay_cleanup():
    """Manually trigger overlay cleanup (DB housekeeping + Plex poster pruning)."""
    import threading
    try:
        from overlays.scheduled_tasks import task_overlay_cleanup
        # Run in background — the Plex poster cleanup phase can be slow.
        t = threading.Thread(target=task_overlay_cleanup, daemon=True)
        t.start()
        return jsonify({'success': True, 'message': 'Cleanup started'})
    except Exception as e:
        logger.error(f"Failed to trigger overlay cleanup: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/tasks/full-sync', methods=['POST'])
def trigger_full_sync():
    """Manually trigger full overlay sync task."""
    try:
        data = request.get_json() or {}
        force = data.get('force', False)

        from overlays.scheduled_tasks import task_overlay_full_sync
        result = task_overlay_full_sync(force=force)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to trigger full sync: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
