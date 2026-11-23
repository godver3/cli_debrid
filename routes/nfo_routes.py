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
        if action == 'apply':
            # Titles locked by the pre-NFO order sync would block the NFOs
            from utilities.plex_order_metadata import release_all_for_nfo
            released = release_all_for_nfo()
            result = plex_nfo_agent.apply(refresh=True)
            result['released_shows'] = released
        else:
            result = plex_nfo_agent.restore(refresh=True)
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
    out = {'mode': nfo_writer.mode(),
           'enabled': nfo_writer.enabled(),
           'symlinked_mode': nfo_writer.symlinked_mode(),
           'library_root': nfo_writer.library_root(),
           'media_server_plex': _media_server_is_plex(),
           'job': nfo_writer.job_status()}
    if request.args.get('plex') == '1' and _media_server_is_plex():
        from utilities import plex_nfo_agent
        out['plex'] = plex_nfo_agent.status()
    return jsonify(out)


@nfo_bp.route('/api/mode', methods=['POST'])
@admin_required
def api_mode():
    """off | changed (only shows with a changed episode order) | all.
    Turning on: write the NFOs, then switch Plex to the NFO agent. Between
    the two on modes: add/remove NFOs per show and refresh Plex. Off: put
    Plex's previous agent back, delete our NFOs, re-lock chosen orders."""
    from utilities import nfo_writer
    new = str((request.get_json(silent=True) or {}).get('mode') or '').lower()
    if new not in nfo_writer.MODES:
        return jsonify({'success': False, 'error': 'mode must be off, changed or all'}), 400
    if new != 'off' and not nfo_writer.symlinked_mode():
        return jsonify({'success': False, 'error': 'NFO files need Symlinked/Local file management.'}), 400
    if nfo_writer.job_status().get('running'):
        return jsonify({'success': False, 'error': 'A run is already going; wait for it to finish.'}), 409
    old = nfo_writer.mode()
    set_setting(nfo_writer.SECTION, 'mode', new)
    set_setting(nfo_writer.SECTION, 'enabled', new != 'off')
    logging.info(f"[NFO] mode {old} -> {new}")
    if new != 'off':
        nfo_writer.write_library(on_done=lambda: _plex_step('apply'))
        what = 'every show' if new == 'all' else 'shows with a changed episode order'
        return jsonify({'success': True, 'message': f'Writing NFO files for {what}; Plex is set up and refreshed when that finishes.'})

    def off():
        if _media_server_is_plex():
            _plex_step('restore')
        nfo_writer._set_job(removed=nfo_writer.remove_library())
        if _media_server_is_plex():
            # Back to the pre-NFO way: lock the chosen orders' titles in Plex
            from utilities.plex_order_metadata import shows_with_chosen_order, sync_show
            for imdb_id, order in shows_with_chosen_order():
                try:
                    sync_show(imdb_id, order)
                except Exception as e:
                    logging.warning(f"[NFO] re-locking {imdb_id}: {e}")
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
