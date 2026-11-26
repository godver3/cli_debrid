"""
Tools → NFO Files: write Kodi-style NFO files into the symlinked TV library
(utilities/nfo_writer.py) and point Plex's TV libraries at an NFO-first
agent (utilities/plex_nfo_agent.py). Also: snapshot Plex's own episode
metadata into NFO files before Plex drops TheTVDB (utilities/plex_snapshot.py).
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
    if action == 'apply' and not nfo_writer.switch_plex_agent():
        nfo_writer._set_job(plex={'skipped': "Plex's agent left as it is (NFO files are written only)"})
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
           'plex_agent': 'switch' if nfo_writer.switch_plex_agent() else 'leave',
           'job': nfo_writer.job_status()}
    try:
        from utilities import plex_snapshot
        out['snapshot'] = plex_snapshot.status()
    except Exception as e:
        out['snapshot'] = {'error': str(e)}
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
    if _busy():
        return jsonify({'success': False, 'error': 'A run is already going; wait for it to finish.'}), 409
    old = nfo_writer.mode()
    set_setting(nfo_writer.SECTION, 'mode', new)
    set_setting(nfo_writer.SECTION, 'enabled', new != 'off')
    logging.info(f"[NFO] mode {old} -> {new}")
    if new != 'off':
        nfo_writer.write_library(on_done=lambda: _plex_step('apply'))
        what = 'every show' if new == 'all' else 'shows with a changed episode order'
        then = ('Plex is set up and refreshed when that finishes.' if nfo_writer.switch_plex_agent()
                else "Plex's agent is left as it is.")
        return jsonify({'success': True, 'message': f'Writing NFO files for {what}; {then}'})

    def off():
        if _media_server_is_plex():
            _plex_step('restore')
        nfo_writer._set_job(removed=nfo_writer.remove_library())
        # NFO files made from a Plex snapshot come back where the writer's were
        try:
            from utilities import plex_snapshot
            restored = plex_snapshot.restore_all()
            if restored:
                nfo_writer._set_job(snapshot_restored=restored)
        except Exception as e:
            logging.warning(f"[NFO] snapshot NFOs not restored: {e}")
    threading.Thread(target=off, name='nfo-off', daemon=True).start()
    return jsonify({'success': True, 'message': "Restoring Plex's previous agent and removing Clyde's NFO files."})


def _busy() -> bool:
    from utilities import nfo_writer, plex_snapshot
    return bool(nfo_writer.job_status().get('running') or plex_snapshot.job_status().get('running'))


@nfo_bp.route('/api/plex_agent', methods=['POST'])
@admin_required
def api_plex_agent():
    """switch (default): turning NFO files on switches the Plex TV library to
    the NFO agent. leave: NFO files are written and Plex isn't changed."""
    from utilities import nfo_writer
    value = str((request.get_json(silent=True) or {}).get('value') or '').lower()
    if value not in ('switch', 'leave'):
        return jsonify({'success': False, 'error': 'value must be switch or leave'}), 400
    set_setting(nfo_writer.SECTION, 'plex_agent', value)
    logging.info(f"[NFO] Plex agent setting: {value}")
    return jsonify({'success': True})


@nfo_bp.route('/api/snapshot', methods=['POST'])
@admin_required
def api_snapshot():
    """Read Plex's current episode metadata for the TV library and write it
    out as NFO files (write=false: keep the snapshot only)."""
    from utilities import nfo_writer, plex_snapshot
    if not nfo_writer.symlinked_mode():
        return jsonify({'success': False, 'error': 'Needs Symlinked/Local file management.'}), 400
    if not _media_server_is_plex():
        return jsonify({'success': False, 'error': 'The media server is not Plex.'}), 400
    if _busy() or not plex_snapshot.take(write=bool((request.get_json(silent=True) or {}).get('write', True))):
        return jsonify({'success': False, 'error': 'A run is already going; wait for it to finish.'}), 409
    return jsonify({'success': True})


@nfo_bp.route('/api/snapshot/<action>', methods=['POST'])
@admin_required
def api_snapshot_action(action):
    """write: (re)write a snapshot's NFO files (body: id). remove: delete
    every snapshot NFO file (the snapshots themselves stay)."""
    from utilities import plex_snapshot
    if action not in ('write', 'remove'):
        return jsonify({'success': False, 'error': 'unknown action'}), 404
    if _busy():
        return jsonify({'success': False, 'error': 'A run is already going; wait for it to finish.'}), 409
    if action == 'write':
        try:
            sid = int((request.get_json(silent=True) or {}).get('id'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'id is required'}), 400
        if sid not in {s['id'] for s in plex_snapshot.snapshots()}:
            return jsonify({'success': False, 'error': 'no such snapshot'}), 404
        started = plex_snapshot.write_nfos(sid)
    else:
        started = plex_snapshot.remove_nfos()
    if not started:
        return jsonify({'success': False, 'error': 'A run is already going.'}), 409
    return jsonify({'success': True})


@nfo_bp.route('/api/convert', methods=['POST'])
@admin_required
def api_convert():
    """Rewrite every show's NFOs, then (re)apply the Plex agent and refresh."""
    from utilities import nfo_writer
    if not nfo_writer.enabled():
        return jsonify({'success': False, 'error': 'Turn NFO files on first.'}), 400
    with_plex = bool((request.get_json(silent=True) or {}).get('plex', True))
    if _busy():
        return jsonify({'success': False, 'error': 'A run is already going.'}), 409
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
        if action == 'apply':
            # As when NFO files are turned on: old title locks would block the NFOs
            from utilities.plex_order_metadata import release_all_for_nfo
            released = release_all_for_nfo()
            result = plex_nfo_agent.apply(refresh=refresh)
            result['released_shows'] = released
        else:
            result = plex_nfo_agent.restore(refresh=refresh)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        logging.error(f"[NFO] Plex agent {action} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@nfo_bp.route('/api/shows')
@admin_required
def api_shows():
    from utilities.nfo_check import library_shows
    return jsonify({'success': True, 'shows': library_shows(request.args.get('q', ''))})


@nfo_bp.route('/api/check')
@admin_required
def api_check():
    from utilities.nfo_check import check_show
    imdb_id = (request.args.get('imdb_id') or '').strip()
    if not imdb_id:
        return jsonify({'success': False, 'error': 'imdb_id is required'}), 400
    try:
        return jsonify({'success': True, 'result': check_show(imdb_id)})
    except Exception as e:
        logging.error(f"[NFO] check {imdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@nfo_bp.route('/api/fix', methods=['POST'])
@admin_required
def api_fix():
    from utilities.nfo_check import fix_show
    imdb_id = ((request.get_json(silent=True) or {}).get('imdb_id') or '').strip()
    if not imdb_id:
        return jsonify({'success': False, 'error': 'imdb_id is required'}), 400
    try:
        return jsonify({'success': True, 'message': fix_show(imdb_id)})
    except Exception as e:
        logging.error(f"[NFO] fix {imdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@nfo_bp.route('/api/override', methods=['POST'])
@admin_required
def api_override():
    """Per-show NFO override: auto (follow the mode), full (every episode's
    details), none (no NFO files). Applied at once: NFOs rewritten or
    removed and Plex re-reads the show."""
    from utilities import nfo_writer
    from utilities.nfo_check import fix_show
    data = request.get_json(silent=True) or {}
    imdb_id, value = (data.get('imdb_id') or '').strip(), (data.get('value') or '').strip()
    if not imdb_id or value not in nfo_writer.OVERRIDES:
        return jsonify({'success': False, 'error': 'imdb_id and value (auto, full or none) are required'}), 400
    nfo_writer.set_show_override(imdb_id, value)
    logging.info(f"[NFO] {imdb_id}: override set to {value}")
    try:
        note = fix_show(imdb_id) if nfo_writer.enabled() else ''
    except Exception as e:
        note = f'saved; applying it failed: {e}'
    return jsonify({'success': True, 'message': note or 'Saved'})
