"""
Tools → NFO Files: write Kodi-style NFO files into the symlinked TV library
(utilities/nfo_writer.py) and point Plex's TV libraries at an NFO-first
agent (utilities/plex_nfo_agent.py).
"""

import logging
import threading

from flask import Blueprint, jsonify, render_template, request

from routes.models import admin_required
from utilities.settings import get_setting, set_setting

nfo_bp = Blueprint('nfo', __name__)


def _media_server_is_plex() -> bool:
    from utilities.silo_functions import is_silo_selected
    return (get_setting('File Management', 'media_server_type', 'plex') or 'plex') == 'plex' and not is_silo_selected()


def _plex_step(action: str):
    """Run the Plex agent step after a library write; the result lands in the job."""
    from utilities import nfo_writer, plex_nfo_agent
    if not _media_server_is_plex():
        nfo_writer._set_job(plex={'skipped': 'media server is not Plex (Jellyfin/Emby/Kodi read the NFOs by themselves)'})
        return
    try:
        result = plex_nfo_agent.apply(refresh=True) if action == 'apply' else plex_nfo_agent.restore(refresh=True)
        nfo_writer._set_job(plex=result)
    except Exception as e:
        logging.error(f"[NFO] Plex agent {action} failed: {e}", exc_info=True)
        nfo_writer._set_job(plex={'error': str(e)})


@nfo_bp.route('')
@admin_required
def page():
    return render_template('nfo_files.html')


@nfo_bp.route('/api/status')
@admin_required
def api_status():
    from utilities import nfo_writer
    out = {'enabled': bool(get_setting(nfo_writer.SECTION, 'enabled', False)),
           'symlinked_mode': nfo_writer.symlinked_mode(),
           'library_root': nfo_writer.library_root(),
           'media_server_plex': _media_server_is_plex(),
           'job': nfo_writer.job_status()}
    if request.args.get('plex') == '1' and _media_server_is_plex():
        from utilities import plex_nfo_agent
        out['plex'] = plex_nfo_agent.status()
    return jsonify(out)


@nfo_bp.route('/api/enable', methods=['POST'])
@admin_required
def api_enable():
    """Turn NFO files on (write the library, then switch Plex to the NFO
    agent) or off (restore Plex's previous agent, then remove our NFOs)."""
    from utilities import nfo_writer
    on = bool((request.get_json(silent=True) or {}).get('on'))
    if on and not nfo_writer.symlinked_mode():
        return jsonify({'success': False, 'error': 'NFO files need Symlinked/Local file management.'}), 400
    if nfo_writer.job_status().get('running'):
        return jsonify({'success': False, 'error': 'A run is already going; wait for it to finish.'}), 409
    set_setting(nfo_writer.SECTION, 'enabled', on)
    logging.info(f"[NFO] NFO files turned {'on' if on else 'off'}")
    if on:
        nfo_writer.write_library(on_done=lambda: _plex_step('apply'))
        return jsonify({'success': True, 'message': 'Writing NFO files for the library; Plex is switched when that finishes.'})

    def off():
        if _media_server_is_plex():
            _plex_step('restore')
        nfo_writer._set_job(removed=nfo_writer.remove_library())
    threading.Thread(target=off, name='nfo-off', daemon=True).start()
    return jsonify({'success': True, 'message': "Restoring Plex's previous agent and removing cli_debrid's NFO files."})


@nfo_bp.route('/api/convert', methods=['POST'])
@admin_required
def api_convert():
    """Rewrite every show's NFOs, then (re)apply the Plex agent and refresh."""
    from utilities import nfo_writer
    if not nfo_writer.enabled():
        return jsonify({'success': False, 'error': 'Turn NFO files on first.'}), 400
    with_plex = bool((request.get_json(silent=True) or {}).get('plex', True))
    started = nfo_writer.write_library(on_done=(lambda: _plex_step('apply')) if with_plex else None)
    if not started:
        return jsonify({'success': False, 'error': 'A run is already going.'}), 409
    return jsonify({'success': True})


@nfo_bp.route('/api/plex/<action>', methods=['POST'])
@admin_required
def api_plex(action):
    from utilities import plex_nfo_agent
    if action not in ('apply', 'restore'):
        return jsonify({'success': False, 'error': 'unknown action'}), 404
    try:
        refresh = bool((request.get_json(silent=True) or {}).get('refresh', True))
        result = plex_nfo_agent.apply(refresh=refresh) if action == 'apply' else plex_nfo_agent.restore(refresh=refresh)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        logging.error(f"[NFO] Plex agent {action} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
