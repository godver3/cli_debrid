"""Collection poster previews, Plex smart collections and box sets."""

from flask import jsonify, request
from utilities.settings import load_config, save_config
import logging
from routes.models import admin_required
from ._common import settings_bp

# ── Plex Collection Poster Routes ─────────────────────────────────────────────

@settings_bp.route('/content-sources/collection-poster/previews')
def get_collection_poster_previews():
    """Return metadata for all 9 poster design previews (0=default, 1-8=custom)."""
    try:
        from database.collection_poster_renderer import DESIGNS, PREVIEW_DIR, generate_preview_images
    except Exception as e:
        logging.error(f"[CollectionPoster] Failed to import renderer: {e}")
        return jsonify({'success': False, 'error': str(e), 'designs': []}), 200
    try:
        # Only trigger generation if design previews are missing (not on every call)
        missing = any(
            not (PREVIEW_DIR / info['preview']).exists()
            for did, info in DESIGNS.items() if did > 0
        )
        if missing:
            import threading
            t = threading.Thread(target=generate_preview_images, daemon=True)
            t.start()
        designs = []
        for design_id, info in DESIGNS.items():
            preview_file = PREVIEW_DIR / info['preview']
            designs.append({
                'id': design_id,
                'name': info['name'],
                'preview_url': f"/settings/content-sources/collection-poster/preview/{design_id}",
                'available': preview_file.exists(),
            })
    except Exception as e:
        logging.error(f"[CollectionPoster] previews route error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e), 'designs': []}), 200
    return jsonify({'success': True, 'designs': designs})

@settings_bp.route('/content-sources/collection-poster/preview/<int:design_id>')
def serve_collection_poster_preview(design_id):
    """Serve a poster design preview image."""
    from flask import send_file
    from database.collection_poster_renderer import DESIGNS, PREVIEW_DIR
    if design_id not in DESIGNS:
        return jsonify({'error': 'Invalid design ID'}), 404
    preview_file = PREVIEW_DIR / DESIGNS[design_id]['preview']
    if not preview_file.exists() and design_id > 0:
        # Generate just this one design
        try:
            from database.collection_poster_renderer import DESIGN_RENDERERS, get_source_icon
            renderer = DESIGN_RENDERERS.get(design_id)
            if renderer:
                img = renderer(
                    collection_name='My Collection',
                    eyebrow='COLLECTION',
                    accent='#E6A800',
                    icon_path=get_source_icon('Trakt Lists', ''),
                    movie_thumbs=[None, None, None, None],
                    source_type='Trakt Lists',
                )
                import io
                buf = io.BytesIO()
                img.convert('RGB').save(buf, format='JPEG', quality=88)
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                preview_file.write_bytes(buf.getvalue())
        except Exception as e:
            logging.error(f"[CollectionPoster] On-demand preview gen failed for design {design_id}: {e}", exc_info=True)
    if preview_file.exists():
        return send_file(str(preview_file), mimetype='image/jpeg')
    return jsonify({'error': 'Preview not available'}), 404

@settings_bp.route('/api/plex-smart-collections', methods=['GET'])
@admin_required
def get_plex_smart_collections():
    """Get all smart collections from Plex with enabled state and shared poster settings."""
    try:
        from database.plex_smart_collections import get_all_smart_collections
        collections = get_all_smart_collections()
        _psc = load_config().get('Plex Smart Collections', {})
        saved_colls = _psc.get('collections', {}) if isinstance(_psc, dict) else {}
        if not isinstance(saved_colls, dict):
            saved_colls = {}
        for coll in collections:
            rk = coll['ratingKey']
            coll['enabled'] = saved_colls.get(rk, {}).get('enabled', False)
        return jsonify({'success': True, 'collections': collections})
    except Exception as e:
        logging.error(f"Failed to get Plex smart collections: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-smart-collections', methods=['POST'])
@admin_required
def save_plex_smart_collections():
    """Save per-collection enabled state to config.json under Plex Smart Collections.collections."""
    try:
        from queues.config_manager import save_config
        data = request.json or {}
        config = load_config()
        if 'Plex Smart Collections' not in config:
            config['Plex Smart Collections'] = {}
        existing_colls = config['Plex Smart Collections'].get('collections', {})
        for rk, cfg in data.get('collections', {}).items():
            existing_colls[rk] = {
                'enabled': cfg.get('enabled', False),
                'title': cfg.get('title', existing_colls.get(rk, {}).get('title', '')),
                'section': cfg.get('section', existing_colls.get(rk, {}).get('section', '')),
                'type': cfg.get('type', existing_colls.get(rk, {}).get('type', 'movie')),
            }
        config['Plex Smart Collections']['collections'] = existing_colls
        save_config(config)
        # Clear cached hash so task re-renders on next run
        try:
            from database.plex_smart_collections import _load_state, _save_state, _migrate_state
            state = _migrate_state(_load_state())
            state.setdefault('shared', {})['poster_hash'] = ''
            _save_state(state)
        except Exception:
            pass
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Failed to save Plex smart collections: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-smart-collections/settings', methods=['POST'])
@admin_required
def save_plex_smart_collection_settings():
    """Save shared poster design settings to config.json."""
    try:
        from queues.config_manager import save_config
        data = request.json or {}
        config = load_config()
        if 'Plex Smart Collections' not in config:
            config['Plex Smart Collections'] = {}
        config['Plex Smart Collections'].update({
            'poster_design': int(data.get('poster_design', 0)),
            'poster_accent': data.get('poster_accent', ''),
            'poster_eyebrow': data.get('poster_eyebrow', ''),
            'poster_icon': data.get('poster_icon', ''),
            'poster_overlay_opacity': int(data.get('poster_overlay_opacity', 60)),
            'poster_glow_opacity': int(data.get('poster_glow_opacity', 80)),
            'poster_glow_radius': int(data.get('poster_glow_radius', 55)),
        })
        save_config(config)
        # Clear cached hash so task re-renders on next run
        try:
            from database.plex_smart_collections import _load_state, _save_state, _migrate_state
            state = _migrate_state(_load_state())
            state.setdefault('shared', {})['poster_hash'] = ''
            _save_state(state)
        except Exception:
            pass
        logging.info(f"[SmartCollections] Saved poster settings: design={data.get('poster_design')}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Failed to save smart collection settings: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-smart-collections/run', methods=['POST'])
@admin_required
def run_plex_smart_collections():
    """Manually trigger the smart collection poster task."""
    try:
        import threading
        from database.plex_smart_collections import apply_smart_collection_posters
        t = threading.Thread(target=apply_smart_collection_posters, daemon=True)
        t.start()
        return jsonify({'success': True, 'message': 'Smart collection poster task started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-smart-collections/status', methods=['GET'])
@admin_required
def get_plex_smart_collections_status():
    """Return enabled smart collections list for the Manage Posters modal."""
    try:
        from utilities.settings import get_all_settings as _gas
        _psc = _gas().get('Plex Smart Collections', {})
        colls = _psc.get('collections', {}) if isinstance(_psc, dict) else {}
        if not isinstance(colls, dict):
            colls = {}
        collections = [
            {'id': rk, 'name': cfg.get('title', rk)}
            for rk, cfg in colls.items()
            if isinstance(cfg, dict) and cfg.get('enabled', False)
        ]
        collections.sort(key=lambda x: x['name'].lower())
        return jsonify({'success': True, 'collections': collections})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-smart-collections/reset-collection', methods=['POST'])
@admin_required
def reset_smart_collection_poster():
    """Immediately reapply the poster for a single smart collection."""
    try:
        data = request.get_json() or {}
        rk = str(data.get('collection_id', '')).strip()
        if not rk:
            return jsonify({'success': False, 'error': 'collection_id required'}), 400
        from database.plex_smart_collections import reapply_single_collection_poster
        result = reapply_single_collection_poster(rk)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@settings_bp.route('/api/plex-boxsets/status', methods=['GET'])
@admin_required
def get_plex_boxsets_status():
    """Return current box sets state: last run time, collections managed, and per-collection list."""
    try:
        import json, os
        state_file = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'plex_boxsets_state.json')
        state = {}
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        from utilities.settings import get_setting as _gs
        fingerprints = state.get('collection_fingerprints', {})
        names = state.get('collection_names', {})
        collections = [
            {'id': cid, 'name': names.get(cid, cid)}
            for cid in fingerprints
        ]
        collections.sort(key=lambda x: x['name'].lower())
        return jsonify({
            'success': True,
            'last_run': state.get('last_run'),
            'collections_managed': len(fingerprints),
            'collections': collections,
            'enabled': _gs('Plex Movie Box Sets', 'enabled', False),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-boxsets/reset-collection', methods=['POST'])
@admin_required
def reset_boxset_collection_poster():
    """Immediately reapply the poster for a single collection."""
    try:
        data = request.get_json() or {}
        coll_id = str(data.get('collection_id', '')).strip()
        if not coll_id:
            return jsonify({'success': False, 'error': 'collection_id required'}), 400
        from database.plex_movie_boxsets import reapply_single_collection_poster
        result = reapply_single_collection_poster(coll_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@settings_bp.route('/api/plex-boxsets/run', methods=['POST'])
@admin_required
def run_plex_movie_boxsets():
    """Manually trigger the Plex movie box sets task."""
    try:
        import threading
        from database.plex_movie_boxsets import run_plex_movie_boxsets as _run
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({'success': True, 'message': 'Plex movie box sets task started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-boxsets/settings', methods=['POST'])
@admin_required
def save_plex_boxsets_settings():
    """Save Plex Movie Box Sets settings."""
    try:
        data = request.get_json() or {}
        config = load_config()
        if 'Plex Movie Box Sets' not in config:
            config['Plex Movie Box Sets'] = {}
        for key in ('enabled', 'grab_missing', 'grab_version', 'collection_name_pattern'):
            if key in data:
                config['Plex Movie Box Sets'][key] = data[key]
        save_config(config)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-preset-collections/status', methods=['GET'])
@admin_required
def get_plex_preset_collections_status():
    """Last run time and result for the preset collections."""
    try:
        from database.plex_preset_collections import get_status
        return jsonify({'success': True, **get_status()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-preset-collections/run', methods=['POST'])
@admin_required
def run_plex_preset_collections():
    """Sync the preset collections now, in the background."""
    try:
        import threading
        from database.plex_preset_collections import run_preset_collections
        threading.Thread(target=run_preset_collections, daemon=True, name='plex-preset-collections').start()
        return jsonify({'success': True, 'message': 'Preset collections sync started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/plex-libraries', methods=['GET'])
@admin_required
def get_plex_libraries():
    """Return all Plex movie/show library sections for the library picker UI."""
    try:
        from database.plex_collections import _get_plex_credentials, _get_library_sections
        plex_url, plex_token = _get_plex_credentials()
        sections = _get_library_sections(plex_url, plex_token)
        libraries = [
            {'key': key, 'title': info['title'], 'type': info['type']}
            for key, info in sorted(sections.items(), key=lambda x: int(x[0]))
        ]
        return jsonify({'success': True, 'libraries': libraries})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/content-sources/collection-poster/regenerate', methods=['POST'])
def regenerate_collection_poster_previews():
    """Force regeneration of all poster preview images."""
    try:
        from database.collection_poster_renderer import regenerate_all_previews
        import threading
        t = threading.Thread(target=regenerate_all_previews, daemon=True)
        t.start()
        return jsonify({'success': True, 'message': 'Preview regeneration started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
