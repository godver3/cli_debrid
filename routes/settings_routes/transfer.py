"""Settings export / import routes (Settings → Export / Import)."""

import json
import logging
import threading
from datetime import datetime

from flask import Response, current_app, jsonify, request

from routes.models import admin_required
from utilities import settings_transfer
from utilities.version import get_app_version

from ._common import _reload_components_after_settings_change, settings_bp

# An export is ~20 KB; anything far bigger is not one.
_MAX_IMPORT_BYTES = 2 * 1024 * 1024


@settings_bp.route('/api/settings_transfer/groups', methods=['GET'])
@admin_required
def settings_transfer_groups():
    return jsonify({'groups': [{'id': g, 'label': label, 'description': desc}
                               for g, label, desc in settings_transfer.GROUPS]})


@settings_bp.route('/api/settings_transfer/export', methods=['GET'])
@admin_required
def settings_transfer_export():
    raw = request.args.get('groups', '')
    groups = [g for g in raw.split(',') if g] or None
    data = settings_transfer.build_export(groups, app_version=get_app_version())
    name = f"clyde-settings-{datetime.now().strftime('%Y%m%d')}.json"
    return Response(json.dumps(data, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


def _read_request():
    if (request.content_length or 0) > _MAX_IMPORT_BYTES:
        raise ValueError('That file is too big to be a settings export.')
    body = request.get_json(silent=True) or {}
    groups = body.get('groups')
    if not isinstance(groups, list):
        groups = list(settings_transfer.GROUP_IDS)
    return body.get('file'), [g for g in groups if isinstance(g, str)]


@settings_bp.route('/api/settings_transfer/preview', methods=['POST'])
@admin_required
def settings_transfer_preview():
    try:
        payload, groups = _read_request()
        return jsonify({'success': True, **settings_transfer.preview(payload, groups)})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error(f'Settings import preview failed: {e}', exc_info=True)
        return jsonify({'success': False, 'error': 'Could not read that file.'}), 500


@settings_bp.route('/api/settings_transfer/apply', methods=['POST'])
@admin_required
def settings_transfer_apply():
    try:
        payload, groups = _read_request()
        result = settings_transfer.apply(payload, groups)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error(f'Settings import failed: {e}', exc_info=True)
        return jsonify({'success': False, 'error': 'Import failed; nothing was changed.'}), 500

    if result['changed']:
        app = current_app._get_current_object()

        def _reload():
            with app.app_context():
                try:
                    _reload_components_after_settings_change('full')
                except Exception as e:
                    logging.error(f'Reload after settings import failed: {e}', exc_info=True)

        threading.Thread(target=_reload, daemon=True, name='settings-import-reload').start()
    return jsonify({'success': True, **result})
