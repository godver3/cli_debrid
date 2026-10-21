"""
Overlay System API Routes

REST API endpoints for overlay management.
"""

import logging
import os
import re
import sqlite3
from flask import Blueprint, jsonify, request, send_file, render_template, abort
from pathlib import Path
from database.core import get_db_connection as _get_db_connection

# Logo library directories (two-tier: user first, system fallback)
_SYSTEM_LOGO_DIR = Path(__file__).parent.parent / 'overlays' / 'assets' / 'logos'
_USER_LOGO_DIR = Path('/user/config/overlay_assets/logos')

from routes.models import admin_required
from overlays import OverlayManager, LayoutManager, LayoutValidator
from overlays.element_definitions import get_all_elements_by_category
from overlays.activity_logger import log_activity
from overlays.utils import is_jellyfin_mode, get_jellyfin_url, get_jellyfin_token
from utilities.settings import get_setting

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Poster thumbnail disk cache
# Caches the resized JPEG served by poster_applied and season_thumb so that
# the overlay page never hammers Plex with hundreds of simultaneous downloads.
# Cache lives at /user/config/poster_thumb_cache/ (persisted across restarts).
# ---------------------------------------------------------------------------
_THUMB_CACHE_DIR = Path('/user/config/poster_thumb_cache')


def _thumb_cache_path(key: str, prefix: str = '') -> Path:
    """Return the cache file path for a given ms_item_id / season key."""
    safe = key.replace('/', '_').replace('\\', '_')
    return _THUMB_CACHE_DIR / f"{prefix}{safe}.jpg"


def _serve_from_thumb_cache(cache_path: Path):
    """Return a Flask Response from a cached JPEG file, or None if not cached."""
    try:
        if not cache_path.exists():
            return None
        age = __import__('time').time() - cache_path.stat().st_mtime
        if age > 86400:  # 24-hour TTL
            return None
        from io import BytesIO
        from flask import Response
        data = cache_path.read_bytes()
        if not data:
            return None
        return Response(data, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception:
        return None


def _write_thumb_cache(cache_path: Path, jpeg_bytes: bytes) -> None:
    """Atomically write JPEG bytes to cache (safe under concurrent requests)."""
    if not jpeg_bytes:
        return
    try:
        _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix('.tmp')
        tmp.write_bytes(jpeg_bytes)
        tmp.rename(cache_path)
    except Exception as e:
        logger.debug(f"poster thumb cache write failed for {cache_path.name}: {e}")


def invalidate_poster_thumb_cache(ms_item_id: str) -> None:
    """Delete the cached thumbnail for a movie/show after its poster changes."""
    try:
        p = _thumb_cache_path(ms_item_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def invalidate_season_thumb_cache(season_key: str) -> None:
    """Delete the cached thumbnail for a season after its poster changes."""
    try:
        p = _thumb_cache_path(season_key, prefix='season_')
        if p.exists():
            p.unlink()
    except Exception:
        pass


# Preferred variation keys for the badge sample-asset endpoint, ordered by desirability.
_BADGE_SAMPLE_PREFERRED_KEYS = {
    'audio_codec':    ['truehd_atmos', 'atmos', 'truehd', 'dts_hd_ma'],
    'audio_combo':    ['truehd_atmos_7.1', 'atmos_7.1', 'dts_hd_ma_7.1'],
    'resolution':     ['4k', '1080p', '2k'],
    'hdr':            ['dolby_vision', 'hdr', 'dolby_vision_hdr'],
    'resolution_hdr': ['4k_dv_hdr', '4k_plain', '1080p_hdr'],
}

# Create two blueprints - one for HTML routes, one for API
overlay_page_bp = Blueprint('overlay_page', __name__)
overlay_bp = Blueprint('overlay_api', __name__)

# HTML routes
@overlay_page_bp.route('/overlays')
@admin_required
def overlays_page():
    """Serve the overlay management UI."""
    return render_template('overlays.html')

@overlay_page_bp.route('/overlays/builder')
@admin_required
def layout_builder_page():
    """Serve the layout builder UI."""
    from utilities.settings import load_config
    _textless = load_config().get('Overlay Settings', {}).get('textless_posters', False)
    return render_template('layout_builder.html', textless_posters_enabled=_textless)

# Initialize managers (will be configured on first use)
_overlay_manager = None
_layout_manager = None


def _get_overlay_manager():
    """Get or create OverlayManager instance (recreated when media server mode changes)."""
    global _overlay_manager
    if _overlay_manager is None:
        plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        _overlay_manager = OverlayManager(None, plex_url, plex_token)
    return _overlay_manager


def _reset_overlay_manager():
    """Force recreation of the OverlayManager on next use (e.g. after settings change)."""
    global _overlay_manager
    _overlay_manager = None


def _get_media_client():
    """Get a media server client (PlexClient or JellyfinClient) using app settings."""
    if is_jellyfin_mode():
        from overlays.jellyfin_client import JellyfinClient
        return JellyfinClient(get_jellyfin_url(), get_jellyfin_token())
    from overlays.plex_client import PlexClient
    plex_url   = get_setting('Plex', 'url',   default='').rstrip('/')
    plex_token = get_setting('Plex', 'token', default='')
    return PlexClient(plex_url, plex_token)


def _get_plex_client():
    """Backward-compatible alias for _get_media_client (used by existing route code)."""
    return _get_media_client()


def _get_layout_manager():
    """Get or create LayoutManager instance."""
    global _layout_manager
    if _layout_manager is None:
        _layout_manager = LayoutManager(None)
    return _layout_manager


# ============================================
# Element Definitions
# ============================================

@overlay_bp.route('/api/overlays/element-definitions', methods=['GET'])
def get_element_definitions():
    """
    Get all available pre-defined element types.

    Returns element definitions grouped by category:
    - basic: Text, Variable Text, Image, SVG, Shapes
    - rating_badges: IMDb, TMDb, Trakt, Rotten Tomatoes
    - quick_badges: Resolution, HDR, Audio, Format, Network, Studio
    """
    try:
        elements = get_all_elements_by_category()
        return jsonify({
            'success': True,
            'elements': elements
        })
    except Exception as e:
        logger.error(f"Failed to get element definitions: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Template CRUD Endpoints
# ============================================

@overlay_bp.route('/api/overlays/layouts', methods=['GET'])
def list_layouts():
    """
    List all overlay templates.

    Query params:
        media_type: Filter by media type (movie, tv, both)
        active_only: Only return active templates (true/false)
    """
    try:
        manager = _get_layout_manager()

        media_type = request.args.get('media_type')
        active_only = request.args.get('active_only', 'false').lower() == 'true'

        templates = manager.list_layouts(media_type=media_type, active_only=active_only)

        return jsonify({
            'success': True,
            'count': len(templates),
            'layouts': templates
        })

    except Exception as e:
        logger.error(f"Failed to list templates: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/<int:layout_id>', methods=['GET'])
def get_layout(layout_id):
    """Get a specific template by ID."""
    try:
        manager = _get_layout_manager()
        template = manager.get_layout(layout_id)

        if not template:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        return jsonify({
            'success': True,
            'layout': template
        })

    except Exception as e:
        logger.error(f"Failed to get template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts', methods=['POST'])
def create_layout():
    """
    Create a new template.

    Request body:
        name: Template name (required)
        description: Template description (optional)
        media_type: 'movie', 'tv', or 'both' (required)
        layout_data: Template JSON structure (required)
        is_default: Whether template is active (optional, default: true)
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400

        # Validate required fields
        required = ['name', 'media_type', 'layout_json']
        missing = [f for f in required if f not in data]
        if missing:
            return jsonify({
                'success': False,
                'error': f'Missing required fields: {", ".join(missing)}'
            }), 400

        manager = _get_layout_manager()
        template = manager.create_layout(
            name=data['name'],
            description=data.get('description', ''),
            media_type=data['media_type'],
            layout_json=data['layout_json'],
            is_default=data.get('is_default', True)
        )

        log_activity('layout_create', title=f"Layout created: \"{template['name']}\" ({template.get('media_type', '')})",
                     stats={'layout_id': template['id'], 'name': template['name'], 'media_type': template.get('media_type')})
        return jsonify({
            'success': True,
            'message': 'Template created successfully',
            'layout': template
        }), 201

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to create template: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/<int:layout_id>', methods=['PUT'])
def update_layout(layout_id):
    """
    Update an existing template.

    Request body (all optional):
        name: New template name
        description: New description
        layout_data: New template structure
        is_default: New active status
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400

        manager = _get_layout_manager()
        success = manager.update_layout(
            layout_id,
            name=data.get('name'),
            description=data.get('description'),
            media_type=data.get('media_type'),
            layout_json=data.get('layout_json'),
            is_default=data.get('is_default')
        )

        if not success:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        updated_layout = manager.get_layout(layout_id)
        log_activity('layout_update', title=f"Layout updated: \"{updated_layout['name']}\" ({updated_layout.get('media_type', '')})",
                     stats={'layout_id': layout_id, 'name': updated_layout['name'], 'media_type': updated_layout.get('media_type')})
        return jsonify({
            'success': True,
            'message': 'Template updated successfully',
            'layout': updated_layout
        })

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to update template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/<int:layout_id>', methods=['DELETE'])
def delete_layout(layout_id):
    """Delete a template."""
    try:
        manager = _get_layout_manager()
        success = manager.delete_layout(layout_id)

        if not success:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        log_activity('layout_delete', title=f"Layout deleted (id={layout_id})",
                     stats={'layout_id': layout_id})
        return jsonify({
            'success': True,
            'message': 'Template deleted successfully'
        })

    except Exception as e:
        logger.error(f"Failed to delete template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/<int:layout_id>/duplicate', methods=['POST'])
def duplicate_layout(layout_id):
    """Duplicate an existing layout, creating a copy with 'Copy of ' prefix."""
    try:
        manager = _get_layout_manager()
        original = manager.get_layout(layout_id)
        if not original:
            return jsonify({'success': False, 'error': 'Layout not found'}), 404

        import json as _json
        lj = original.get('layout_json')
        if isinstance(lj, str):
            lj = _json.loads(lj)

        new_layout = manager.create_layout(
            name=f"Copy of {original['name']}",
            description=original.get('description', ''),
            media_type=original.get('media_type', 'both'),
            layout_json=lj,
            is_default=False,  # duplicate starts inactive
        )

        log_activity('layout_create', title=f"Layout duplicated: \"{new_layout['name']}\"",
                     stats={'layout_id': new_layout['id'], 'name': new_layout['name'], 'duplicated_from': layout_id})
        return jsonify({
            'success': True,
            'message': 'Layout duplicated successfully',
            'layout': new_layout,
        }), 201

    except Exception as e:
        logger.error(f"Failed to duplicate layout {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/validate', methods=['POST'])
def validate_layout():
    """
    Validate a template structure without saving.

    Request body:
        layout_data: Template JSON to validate
    """
    try:
        data = request.get_json()

        if not data or 'layout_data' not in data:
            return jsonify({'success': False, 'error': 'No layout_data provided'}), 400

        validator = LayoutValidator()
        is_valid, errors = validator.validate_layout(data['layout_data'])
        refs_valid, warnings = validator.validate_layout_references(data['layout_data'])

        return jsonify({
            'success': is_valid,
            'valid': is_valid,
            'errors': errors,
            'warnings': warnings
        })

    except Exception as e:
        logger.error(f"Failed to validate template: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/<int:layout_id>/export', methods=['GET'])
def export_layout(layout_id):
    """Export template to JSON file."""
    try:
        manager = _get_layout_manager()
        template = manager.get_layout(layout_id)

        if not template:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        # Create temp file for export
        import tempfile
        import json

        export_data = {
            'name': template['name'],
            'description': template.get('description', ''),
            'media_type': template.get('media_type', 'both'),
            'layout_json': template['layout_json'],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(export_data, f, indent=2)
            temp_path = f.name

        filename = f"{template['name'].replace(' ', '_')}.json"

        return send_file(
            temp_path,
            mimetype='application/json',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        logger.error(f"Failed to export template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/import', methods=['POST'])
def import_layout():
    """
    Import layout from JSON body.

    Request body:
        name: Layout name (required)
        description: Layout description (optional)
        media_type: 'movie', 'tv', or 'both' (optional, default: 'both')
        elements: Layout elements array (required)
        is_default: Whether layout should be active (optional, default: true)
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400

        name = data.get('name', 'Imported Layout')
        description = data.get('description', 'Imported layout')
        media_type = data.get('media_type', 'both')
        is_default = data.get('is_default', True)

        # Support both wrapped format {name, layout_json, ...} and raw layout_json blob
        layout_json = data.get('layout_json', data)

        manager = _get_layout_manager()
        template = manager.create_layout(
            name=name,
            description=description,
            media_type=media_type,
            layout_json=layout_json,
            is_default=is_default
        )

        log_activity('layout_create', title=f"Layout imported: \"{template['name']}\" ({template.get('media_type', '')})",
                     stats={'layout_id': template['id'], 'name': template['name'], 'media_type': template.get('media_type')})
        return jsonify({
            'success': True,
            'message': 'Layout imported successfully',
            'layout': template
        }), 201

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to import layout: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/layouts/load_defaults', methods=['POST'])
def load_default_layouts():
    """
    Import any missing bundled default layouts.
    Layouts whose names already exist in the DB are skipped.
    """
    try:
        manager = _get_layout_manager()
        result = manager.load_default_layouts(skip_existing=True)
        if result['errors']:
            logger.warning(f"load_default_layouts errors: {result['errors']}")
        for name in []:  # log each loaded layout
            pass
        if result['loaded']:
            log_activity('layout_create',
                         title=f"Loaded {result['loaded']} default layout(s)",
                         stats=result)
        return jsonify({
            'success': True,
            'loaded': result['loaded'],
            'skipped': result['skipped'],
            'errors': result['errors'],
        })
    except Exception as e:
        logger.error(f"Failed to load default layouts: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Overlay Generation Endpoints
# ============================================

@overlay_bp.route('/api/overlays/generate/<int:media_item_id>', methods=['POST'])
def generate_overlay(media_item_id):
    """
    Generate overlay for a specific media item.

    Request body (optional):
        layout_id: Template ID to use (optional)
        force: Force regeneration (optional, default: false)
    """
    try:
        data = request.get_json() or {}

        manager = _get_overlay_manager()
        result = manager.generate_overlay_for_item(
            media_item_id,
            force=data.get('force', False),
            layout_id=data.get('layout_id')
        )

        _st = result.get('status', 'error')
        if _st not in ('skipped',):
            log_activity('generate', result='success' if result.get('success') else 'failed',
                         title=f"Generate overlay: {result.get('message', _st)} (item {media_item_id})",
                         stats={'media_item_id': media_item_id, 'status': _st})
        status_code = 200 if result['success'] else 400
        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"Failed to generate overlay for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/generate/regenerate/<int:media_item_id>', methods=['POST'])
def regenerate_single_overlay(media_item_id):
    """
    Regenerate overlay for a single item using the currently-selected Plex poster as the base.
    This deletes the existing backup so a fresh poster is downloaded from Plex (the user's
    current selection), then re-applies the overlay on top of it.
    """
    try:
        data = request.get_json() or {}
        manager = _get_overlay_manager()
        result = manager.generate_overlay_for_item(
            media_item_id,
            force=True,
            layout_id=data.get('layout_id'),
            force_fresh_poster=True
        )
        _st = result.get('status', 'error')
        log_activity('regenerate', result='success' if result.get('success') else 'failed',
                     title=f"Regenerate overlay: {result.get('message', _st)} (item {media_item_id})",
                     stats={'media_item_id': media_item_id, 'status': _st})
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Failed to regenerate overlay for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/poster_list/<int:media_item_id>', methods=['GET'])
def get_item_poster_list(media_item_id):
    """Return list of available clean (non-overlay) Plex posters for a media item."""
    try:
        if is_jellyfin_mode():
            return jsonify({'success': False, 'error': 'Poster picker only available for Plex'}), 400
        conn = _get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT ms_item_id, title FROM media_items WHERE id = ?', (media_item_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row['ms_item_id']:
            return jsonify({'success': False, 'error': 'Item not found or no Plex ID'}), 404
        ms_item_id = row['ms_item_id']
        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        from overlays.plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)
        all_posters = client.get_poster_list(ms_item_id)
        # Filter out upload:// (our overlays) to prevent overlay-on-overlay
        clean = [p for p in all_posters if not p.get('ratingKey', '').startswith('upload://')]
        posters = [
            {
                'index': idx,
                'rating_key': p.get('ratingKey', ''),
                'selected': bool(p.get('selected')),
                'provider': p.get('provider', ''),
                'thumb_url': f"/api/overlays/poster_thumb/{ms_item_id}/{idx}",
            }
            for idx, p in enumerate(clean)
        ]
        return jsonify({'success': True, 'posters': posters, 'ms_item_id': ms_item_id})
    except Exception as e:
        logger.error(f"Failed to get poster list for item {media_item_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/poster_thumb/<ms_item_id>/<int:index>', methods=['GET'])
def proxy_poster_thumb(ms_item_id, index):
    """Proxy a specific Plex poster thumbnail to the browser."""
    try:
        if is_jellyfin_mode():
            abort(400)
        from io import BytesIO
        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        from overlays.plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)
        all_posters = client.get_poster_list(ms_item_id)
        clean = [p for p in all_posters if not p.get('ratingKey', '').startswith('upload://')]
        logger.debug(f"poster_thumb {ms_item_id}/{index}: {len(clean)} clean posters available")
        if index >= len(clean):
            logger.warning(f"poster_thumb: index {index} out of range ({len(clean)} posters)")
            abort(404)
        poster = clean[index]
        rating_key = poster.get('ratingKey', '')
        logger.debug(f"poster_thumb: fetching ratingKey={rating_key!r} thumb={poster.get('thumb','')!r}")
        # Use download_poster_by_rating_key which handles all ratingKey formats and auth
        img_bytes = client.download_poster_by_rating_key(ms_item_id, rating_key)
        if not img_bytes:
            logger.warning(f"poster_thumb: download_poster_by_rating_key returned empty for {rating_key!r}")
            abort(404)
        return send_file(BytesIO(img_bytes), mimetype='image/jpeg')
    except Exception as e:
        logger.warning(f"Failed to proxy poster thumb {ms_item_id}/{index}: {e}")
        abort(404)


@overlay_bp.route('/api/overlays/apply_poster/<int:media_item_id>', methods=['POST'])
def apply_specific_poster(media_item_id):
    """Download a chosen Plex poster, save as backup, and apply overlay on top."""
    try:
        if is_jellyfin_mode():
            return jsonify({'success': False, 'error': 'Poster picker only available for Plex'}), 400
        data = request.get_json() or {}
        poster_rating_key = data.get('rating_key')
        if not poster_rating_key:
            return jsonify({'success': False, 'error': 'rating_key required'}), 400
        # Safety: never allow using an uploaded (overlay) poster as source
        if poster_rating_key.startswith('upload://'):
            return jsonify({'success': False, 'error': 'Cannot use an uploaded poster as source'}), 400
        conn = _get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT ms_item_id, title FROM media_items WHERE id = ?', (media_item_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row['ms_item_id']:
            return jsonify({'success': False, 'error': 'Item not found or no Plex ID'}), 404
        ms_item_id = row['ms_item_id']
        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        from overlays.plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)
        # Download the specific clean poster
        poster_bytes = client.download_poster_by_rating_key(ms_item_id, poster_rating_key)
        if not poster_bytes:
            return jsonify({'success': False, 'error': 'Failed to download poster from Plex'}), 500
        # Save as backup so generate_overlay_for_item picks it up as the source
        from overlays.cache_cleanup import PosterCacheManager
        PosterCacheManager(None).backup_poster(ms_item_id, poster_bytes)
        # Reset overlay state to 'pending' so the generate path uses the backup directly
        # (avoids the 'removed' branch which would try to compare with current Plex poster)
        _conn = _get_db_connection()
        _conn.execute(
            "UPDATE media_overlay_state SET status='pending', reason='poster pick',"
            " updated_at=CURRENT_TIMESTAMP WHERE media_item_id=?",
            (media_item_id,)
        )
        _conn.commit()
        _conn.close()
        # Apply overlay on top of the chosen poster
        manager = _get_overlay_manager()
        result = manager.generate_overlay_for_item(media_item_id, force=True)
        if result.get('success'):
            invalidate_poster_thumb_cache(ms_item_id)
        log_activity('poster_pick',
                     result='success' if result.get('success') else 'failed',
                     title=f"Poster picked: {row['title']} (item {media_item_id})",
                     stats={'media_item_id': media_item_id})
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.error(f"Failed to apply specific poster for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/generate/batch', methods=['POST'])
def batch_generate_overlays():
    """Start a background generate job for selected items."""
    import threading
    from overlays.overlay_manager import get_generate_status, _update_gen
    from datetime import datetime as _dt
    try:
        data = request.get_json()
        if not data or 'item_ids' not in data:
            return jsonify({'success': False, 'error': 'No item_ids provided'}), 400
        item_ids = data['item_ids']
        if not isinstance(item_ids, list) or not item_ids:
            return jsonify({'success': False, 'error': 'item_ids must be a non-empty array'}), 400
        if get_generate_status().get('running'):
            return jsonify({'success': False, 'error': 'A generate job is already running'}), 409

        label = f"Generate selected ({len(item_ids)} item{'s' if len(item_ids) != 1 else ''})"
        _update_gen(running=True, total=len(item_ids), done=0, applied=0, failed=0, skipped=0,
                    current='', errors=[], label=label,
                    started_at=_dt.utcnow().isoformat(), finished_at=None)

        def _bg():
            from overlays.overlay_manager import _update_gen
            from datetime import datetime as _dt2
            try:
                layout_id = data.get('layout_id')
                force = data.get('force', False)
                layout_media_type = None
                if layout_id:
                    lm = _get_layout_manager()
                    layout_obj = lm.get_layout(layout_id)
                    if layout_obj:
                        layout_media_type = layout_obj.get('media_type')

                manager = _get_overlay_manager()

                if layout_media_type == 'season':
                    # Resolve the selected item_ids → ms_item_ids (show-level Plex keys)
                    _sel_conn = _get_db_connection()
                    _sel_cur  = _sel_conn.cursor()
                    _sel_cur.execute(
                        f"SELECT DISTINCT ms_item_id FROM media_items "
                        f"WHERE type='episode' AND ms_item_id IS NOT NULL "
                        f"AND id IN ({','.join('?' * len(item_ids))})",
                        item_ids)
                    _selected_show_keys = {r[0] for r in _sel_cur.fetchall()}
                    _sel_conn.close()

                    all_seasons = []
                    try:
                        if is_jellyfin_mode():
                            for _sk in _selected_show_keys:
                                all_seasons.extend(manager.client.get_show_seasons(_sk) or [])
                        else:
                            # Fetch all sections, collect seasons, filter to selected shows only
                            sections = manager.plex.get_all_sections()
                            for section in sections:
                                if section.get('type') == 'show':
                                    for _s in manager.plex.get_all_seasons_for_section(section['key']):
                                        if _s.get('parentRatingKey', '') in _selected_show_keys:
                                            all_seasons.append(_s)
                    except Exception as _sec_exc:
                        logger.warning(f"Failed to fetch seasons from media server: {_sec_exc}")

                    # Correct the total so the progress bar reflects seasons, not shows
                    from overlays.overlay_manager import get_generate_status as _ggs
                    if _ggs().get('running'):
                        _update_gen(total=len(all_seasons), done=0)

                    # Build show-title lookup for progress display and failure names
                    _show_title_map = {}
                    try:
                        _stm_conn = _get_db_connection()
                        for _stm_row in _stm_conn.execute(
                                "SELECT DISTINCT ms_item_id, title FROM media_items "
                                "WHERE type='episode' AND ms_item_id IS NOT NULL AND ms_item_id != ''"):
                            if _stm_row['ms_item_id']:
                                _show_title_map[_stm_row['ms_item_id']] = _stm_row['title']
                        _stm_conn.close()
                    except Exception:
                        pass

                    season_results = []
                    for s in all_seasons:
                        show_key = s.get('parentRatingKey', '')
                        season_key = s.get('ratingKey', '')
                        season_num = s.get('index', 0)
                        season_title = s.get('title', f'Season {season_num}')
                        if not show_key or not season_key:
                            continue
                        _st = 'failed'
                        _show_name = _show_title_map.get(show_key, show_key)
                        # Pre-check: skip already-applied seasons without hitting the media server
                        if not force:
                            _pre_state = manager._get_season_overlay_state(season_key)
                            if _pre_state and _pre_state.get('status') in ('applied', 'no_poster', 'removed'):
                                _st = 'skipped'
                                season_results.append({'show_key': show_key, 'season_number': season_num,
                                                       'status': _st, 'label': f"{_show_name} - {season_title}"})
                                _gs2 = _ggs()
                                if _gs2.get('running'):
                                    _update_gen(done=_gs2['done'] + 1,
                                                skipped=_gs2['skipped'] + 1,
                                                current=f"{_show_name} - {season_title}")
                                continue
                        try:
                            res = manager.generate_season_overlay(
                                show_plex_rating_key=show_key,
                                season_plex_rating_key=season_key,
                                season_number=season_num,
                                force=force,
                                layout_id=layout_id,
                            )
                            _st = res.get('status', 'failed')
                            season_results.append({'show_key': show_key, 'season_number': season_num,
                                                   'status': _st, 'label': f"{_show_name} - {season_title}"})
                        except Exception as _se:
                            season_results.append({'show_key': show_key, 'season_number': season_num,
                                                   'status': 'failed', 'label': f"{_show_name} - {season_title}"})
                            logger.warning(f"Season overlay generation failed for show {show_key} season {season_num}: {_se}")

                        # Per-item progress update
                        _gs2 = _ggs()
                        if _gs2.get('running'):
                            _update_gen(
                                done=_gs2['done'] + 1,
                                applied=_gs2['applied'] + (1 if _st == 'applied' else 0),
                                failed=_gs2['failed'] + (1 if _st not in ('applied', 'skipped', 'analyzing') else 0),
                                skipped=_gs2['skipped'] + (1 if _st in ('skipped', 'analyzing') else 0),
                                current=f"{_show_name} - {season_title}",
                            )

                    _s_applied = sum(1 for r in season_results if r.get('status') == 'applied')
                    _s_failed  = sum(1 for r in season_results if r.get('status') not in ('applied', 'skipped', 'analyzing'))
                    _s_fail_names = [r['label'] for r in season_results
                                     if r.get('status') not in ('applied', 'skipped', 'analyzing')]
                    _s_fail_stats = {'failures': _s_fail_names} if 0 < _s_failed <= 10 else {}
                    log_activity('generate',
                                 title=f"Generate selected (season layout): {_s_applied} applied, {_s_failed} failed",
                                 stats={'applied': _s_applied, 'failed': _s_failed,
                                        'total': len(season_results), **_s_fail_stats})
                else:
                    results = manager.batch_generate_overlays(item_ids, force=force, layout_id=layout_id)
                    _applied = results.get('applied', 0) if isinstance(results, dict) else 0
                    _failed  = results.get('failed', 0)  if isinstance(results, dict) else 0
                    log_activity('generate',
                                 title=f"Generate selected: {_applied} applied, {_failed} failed",
                                 stats={'applied': _applied, 'failed': _failed, 'total': len(item_ids)})
            except Exception as _e:
                logger.error(f"Batch generate background job failed: {_e}", exc_info=True)
                from overlays.overlay_manager import get_generate_status
                s = get_generate_status()
                _update_gen(errors=list(s.get('errors', [])) + [str(_e)])
            finally:
                _update_gen(running=False, current='', finished_at=_dt2.utcnow().isoformat())

        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({'success': True, 'started': True, 'total': len(item_ids), 'label': label})

    except Exception as e:
        logger.error(f"Failed to batch generate overlays: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _estimate_generate_total(data: dict) -> int:
    """Quickly estimate how many items a generate-all job will process."""
    try:
        media_type = data.get('media_type', 'movie')
        db_type = 'episode' if media_type == 'tv' else media_type
        conn = _get_db_connection()
        cursor = conn.cursor()
        if db_type == 'episode':
            cursor.execute(
                "SELECT COUNT(DISTINCT ms_item_id) FROM media_items "
                "WHERE type='episode' AND state IN ('Collected','Upgrading') AND ms_item_id IS NOT NULL"
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM media_items "
                "WHERE type=? AND state IN ('Collected','Upgrading') AND ms_item_id IS NOT NULL",
                (db_type,)
            )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _run_generate_all_bg(data: dict):
    """Background worker for generate/regenerate-all. No Flask context needed."""
    from overlays.overlay_manager import _update_gen, get_generate_status
    from datetime import datetime as _dt
    try:
        _run_generate_all_work(data)
    except Exception as _e:
        logger.error(f"Background generate-all job failed: {_e}", exc_info=True)
        s = get_generate_status()
        _update_gen(errors=list(s.get('errors', [])) + [str(_e)])
    finally:
        _update_gen(running=False, current='',
                    finished_at=_dt.utcnow().isoformat())


def _run_generate_all_work(data: dict):
    """
    Core worker for generate_all / regenerate_all — no Flask context required.
    Updates the shared _gen_job state as items are processed.
    """
    media_type = data.get('media_type')
    layout_id = data.get('layout_id')
    force = data.get('force', False)
    force_fresh_poster = data.get('force_fresh_poster', False)
    seasons_only = data.get('seasons_only', False)

    if not media_type:
        return

    # Determine what the selected layout is for, so we don't apply the wrong
    # layout type to the wrong items (e.g. a season layout to show posters).
    layout_media_type = None
    if layout_id:
        lm = _get_layout_manager()
        layout_obj = lm.get_layout(layout_id)
        if layout_obj:
            layout_media_type = layout_obj.get('media_type')  # 'movie', 'tv', 'season', 'both'

    # If a season layout is selected on the TV tab, only generate season overlays.
    season_layout_only = (layout_media_type == 'season') or seasons_only
    # If a tv/movie layout is selected, skip season generation (use None to let it auto-select).
    skip_seasons = (layout_media_type in ('tv', 'movie'))

    manager = _get_overlay_manager()
    results = []
    item_ids = []

    if not season_layout_only:
        conn = _get_db_connection()
        cursor = conn.cursor()
        # Map UI 'tv' to DB 'episode'; 'movie' stays 'movie'
        db_type = 'episode' if media_type == 'tv' else media_type
        # In Jellyfin mode, exclude legacy Plex integer IDs (they haven't been synced yet)
        if is_jellyfin_mode():
            _has_valid_id = "(ms_item_id IS NOT NULL AND LENGTH(ms_item_id) >= 20)"
        else:
            _has_valid_id = "ms_item_id IS NOT NULL"
        # For shows: use one representative episode per show (min id), deduplicated by ms_item_id
        # For movies: just all movies with a ms_item_id
        if db_type == 'episode':
            cursor.execute(
                f"""SELECT MIN(id) as id FROM media_items
                   WHERE type = 'episode' AND state IN ('Collected', 'Upgrading') AND {_has_valid_id}
                   GROUP BY ms_item_id""",
            )
        else:
            cursor.execute(
                f"SELECT id FROM media_items WHERE type = ? AND state IN ('Collected', 'Upgrading') AND {_has_valid_id}",
                (db_type,)
            )
        item_ids = [row[0] for row in cursor.fetchall()]
        conn.close()

        if item_ids:
            results = manager.batch_generate_overlays(
                item_ids, force=force, layout_id=layout_id,
                force_fresh_poster=force_fresh_poster)

    # For TV: also generate season overlays for all processed shows
    # Skip if a non-season layout was explicitly selected (user wants shows only).
    season_results = []
    if media_type == 'tv' and not skip_seasons:
        # Use season layout_id if that's what was selected; otherwise pass None (auto-select)
        season_layout_id = layout_id if season_layout_only else None

        # Get all seasons for all tracked shows.
        # For Plex: use the batch sections API.
        # For Jellyfin: get unique show keys from DB, fetch seasons per show.
        all_seasons = []
        try:
            if is_jellyfin_mode():
                _conn_s2 = _get_db_connection()
                _cur_s2  = _conn_s2.cursor()
                _cur_s2.execute(
                    "SELECT DISTINCT ms_item_id FROM media_items "
                    "WHERE type='episode' AND ms_item_id IS NOT NULL AND ms_item_id != '' "
                    "AND LENGTH(ms_item_id) >= 20 "
                    "AND state IN ('Collected', 'Upgrading')"
                )
                _all_show_keys = [r[0] for r in _cur_s2.fetchall()]
                _conn_s2.close()
                for _sk in _all_show_keys:
                    for _season in (manager.client.get_show_seasons(_sk) or []):
                        # Inject parentRatingKey so the loop below can read show_key
                        _season.setdefault('parentRatingKey', _sk)
                        all_seasons.append(_season)
            else:
                sections = manager.plex.get_all_sections()
                for section in sections:
                    if section.get('type') == 'show':
                        all_seasons.extend(
                            manager.plex.get_all_seasons_for_section(section['key'])
                        )
        except Exception as _sec_exc:
            logger.warning(f"Failed to fetch seasons from media server: {_sec_exc}")

        # Build show-title lookup for progress display and failure names
        _show_title_map = {}
        try:
            _stm_conn = _get_db_connection()
            for _stm_row in _stm_conn.execute(
                    "SELECT DISTINCT ms_item_id, title FROM media_items "
                    "WHERE type='episode' AND ms_item_id IS NOT NULL AND ms_item_id != ''"):
                if _stm_row['ms_item_id']:
                    _show_title_map[_stm_row['ms_item_id']] = _stm_row['title']
            _stm_conn.close()
        except Exception:
            pass

        # Reset progress for the season phase so the bar doesn't stay frozen at
        # 100% (shows done) while seasons are processing.  Always do this when
        # there are seasons to process — not just for season-only jobs.
        if all_seasons:
            from overlays.overlay_manager import _update_gen, get_generate_status as _ggs
            if _ggs().get('running'):
                _update_gen(
                    total=len(all_seasons), done=0,
                    label=f"Applying season overlays\u2026 ({len(all_seasons)} seasons)",
                    current='',
                )

        for s in all_seasons:
            show_key = s.get('parentRatingKey', '')
            season_key = s.get('ratingKey', '')
            season_num = s.get('index', 0)
            season_title = s.get('title', f'Season {season_num}')
            if not show_key or not season_key:
                continue
            _show_name = _show_title_map.get(show_key, show_key)
            # Pre-check: skip already-applied seasons without hitting the media server
            if not force:
                _pre_state = manager._get_season_overlay_state(season_key)
                if _pre_state and _pre_state.get('status') == 'applied':
                    _st = 'skipped'
                    season_results.append({'show_key': show_key, 'season_number': season_num,
                                           'status': _st, 'label': f"{_show_name} - {season_title}"})
                    from overlays.overlay_manager import _update_gen, get_generate_status as _ggs2
                    _s2 = _ggs2()
                    if _s2.get('running'):
                        _update_gen(done=_s2['done'] + 1, skipped=_s2['skipped'] + 1,
                                    current=f"{_show_name} - {season_title}")
                    continue
            try:
                res = manager.generate_season_overlay(
                    show_plex_rating_key=show_key,
                    season_plex_rating_key=season_key,
                    season_number=season_num,
                    force=force,
                    layout_id=season_layout_id,
                    force_fresh_poster=force_fresh_poster,
                )
                _st = res.get('status')
                season_results.append({
                    'show_key': show_key,
                    'season_number': season_num,
                    'status': _st,
                    'label': f"{_show_name} - {season_title}",
                })
            except Exception as _se:
                _st = 'failed'
                season_results.append({'show_key': show_key, 'season_number': season_num,
                                       'status': _st, 'label': f"{_show_name} - {season_title}"})
                logger.warning(f"Season overlay generation failed for show {show_key} season {season_num}: {_se}")

            # Update progress counter so the frontend shows real-time season progress
            from overlays.overlay_manager import _update_gen, get_generate_status as _ggs2
            _s2 = _ggs2()
            if _s2.get('running'):
                _update_gen(
                    done=_s2['done'] + 1,
                    applied=_s2['applied'] + (1 if _st == 'applied' else 0),
                    failed=_s2['failed'] + (1 if _st not in ('applied', 'skipped', 'analyzing') else 0),
                    skipped=_s2['skipped'] + (1 if _st in ('skipped', 'analyzing') else 0),
                    current=f"{_show_name} - {season_title}",
                )

    _applied   = results.get('applied', 0) if isinstance(results, dict) else 0
    _failed    = results.get('failed', 0)  if isinstance(results, dict) else 0
    _s_applied = sum(1 for r in season_results if r.get('status') == 'applied') if season_results else 0
    _s_failed  = sum(1 for r in season_results if r.get('status') not in ('applied', 'skipped', 'analyzing')) if season_results else 0
    _action = 'regenerate_all' if force else 'generate_all'
    if season_layout_only:
        _title = (f"{'Regenerate' if force else 'Generate'} all seasons ({media_type}): "
                  f"{_s_applied} applied, {_s_failed} failed")
    else:
        _title = (f"{'Regenerate' if force else 'Generate'} all ({media_type}): "
                  f"{_applied} applied, {_failed} failed"
                  + (f", {_s_applied} seasons applied" if _s_applied else ""))
    _s_fail_names = [r['label'] for r in season_results
                     if r.get('status') not in ('applied', 'skipped', 'analyzing') and r.get('label')]
    _s_fail_stats = {'failures': _s_fail_names} if 0 < _s_failed <= 10 else {}
    log_activity(_action,
                 title=_title,
                 stats={'media_type': media_type, 'applied': _applied + _s_applied,
                        'failed': _failed + _s_failed,
                        'seasons_applied': _s_applied, 'total': len(item_ids),
                        **_s_fail_stats})


@overlay_bp.route('/api/overlays/generate/all', methods=['POST'])
def generate_all_overlays():
    """Start background generate-all job for a given media type."""
    import threading
    from overlays.overlay_manager import get_generate_status, _update_gen
    try:
        data = request.get_json() or {}
        if get_generate_status().get('running'):
            return jsonify({'success': False, 'error': 'A generate job is already running'}), 409
        media_type = data.get('media_type', 'movie')
        label = f"Generate all {'movies' if media_type == 'movie' else 'TV shows'}"
        total = _estimate_generate_total(data)
        _update_gen(running=True, total=total, done=0, applied=0, failed=0, skipped=0,
                    current='', errors=[], label=label,
                    started_at=__import__('datetime').datetime.utcnow().isoformat(),
                    finished_at=None)
        t = threading.Thread(target=_run_generate_all_bg, args=(data,), daemon=True)
        t.start()
        return jsonify({'success': True, 'started': True, 'total': total, 'label': label})
    except Exception as e:
        logger.error(f"Failed to start generate-all job: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/generate/regenerate-all', methods=['POST'])
def regenerate_all_overlays():
    """Start background regenerate-all job for a given media type."""
    import threading
    from overlays.overlay_manager import get_generate_status, _update_gen
    try:
        data = request.get_json() or {}
        data['force'] = True
        data['force_fresh_poster'] = True
        if get_generate_status().get('running'):
            return jsonify({'success': False, 'error': 'A generate job is already running'}), 409
        media_type = data.get('media_type', 'movie')
        label = f"Regenerate all {'movies' if media_type == 'movie' else 'TV shows'}"
        total = _estimate_generate_total(data)
        _update_gen(running=True, total=total, done=0, applied=0, failed=0, skipped=0,
                    current='', errors=[], label=label,
                    started_at=__import__('datetime').datetime.utcnow().isoformat(),
                    finished_at=None)
        t = threading.Thread(target=_run_generate_all_bg, args=(data,), daemon=True)
        t.start()
        return jsonify({'success': True, 'started': True, 'total': total, 'label': label})
    except Exception as e:
        logger.error(f"Failed to start regenerate-all job: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/generate/status', methods=['GET'])
def generate_status():
    """Poll the progress of the running/last generate job."""
    try:
        from overlays.overlay_manager import get_generate_status
        s = get_generate_status()
        pct = 0
        if s['total'] > 0:
            pct = round(s['done'] / s['total'] * 100)
        return jsonify({
            'success': True,
            'running':  s['running'],
            'total':    s['total'],
            'done':     s['done'],
            'applied':  s['applied'],
            'failed':   s['failed'],
            'skipped':  s['skipped'],
            'current':  s['current'],
            'errors':   s['errors'],
            'percent':  pct,
            'label':    s.get('label', ''),
            'finished_at': s.get('finished_at'),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Track background sync state
_library_sync_state = {
    'running': False,
    'progress': '',
    'updated_movies': 0,
    'updated_episodes': 0,
    'errors': [],
    'done': False,
}


def _run_library_sync():
    """Background thread: fetch Plex items and populate ms_item_id in DB."""
    import time as _time
    global _library_sync_state

    _library_sync_state.update({
        'running': True, 'done': False, 'progress': 'Starting…',
        'updated_movies': 0, 'updated_episodes': 0, 'errors': [],
    })

    try:
        client = _get_media_client()
        conn = _get_db_connection()
        cursor = conn.cursor()
        _jf_mode = is_jellyfin_mode()
        # In Jellyfin mode also overwrite legacy Plex integer IDs; in Plex mode only fill NULLs
        if _jf_mode:
            _needs_id = "(ms_item_id IS NULL OR ms_item_id = '' OR LENGTH(ms_item_id) < 20)"
        else:
            _needs_id = "ms_item_id IS NULL"

        # --- Sync movies ---
        _library_sync_state['progress'] = 'Fetching movies from media server…'
        logger.info("sync_library: fetching movies from media server...")
        try:
            if _jf_mode:
                ms_movies = client.get_all_items_with_guids(media_type='Movie')
            else:
                ms_movies = client.get_all_items_with_guids(plex_type=1)
            logger.info(f"sync_library: got {len(ms_movies)} movies from media server")
            _library_sync_state['progress'] = f'Matching {len(ms_movies)} movies…'

            for item in ms_movies:
                rk = item['ratingKey']
                imdb_id = item.get('imdb_id')
                tmdb_id = item.get('tmdb_id')
                if not rk:
                    continue
                updated = 0
                if imdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='movie' AND imdb_id=? AND {_needs_id}",
                        (rk, imdb_id))
                    updated = cursor.rowcount
                if not updated and tmdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='movie' AND tmdb_id=? AND {_needs_id}",
                        (rk, tmdb_id))
                    updated = cursor.rowcount
                # Fallback: match by title + year, only for Plex items with no external IDs at all
                if not updated and not imdb_id and not tmdb_id and item.get('title') and item.get('year'):
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='movie' AND LOWER(title)=LOWER(?) AND year=? AND {_needs_id}",
                        (rk, item['title'], item['year']))
                    updated = cursor.rowcount
                _library_sync_state['updated_movies'] += updated

        except Exception as e:
            msg = f"Movie sync error: {e}"
            logger.error(f"sync_library: {msg}", exc_info=True)
            _library_sync_state['errors'].append(msg)

        # Small pause between movie and show fetch
        _time.sleep(1)

        # --- Sync TV shows ---
        _library_sync_state['progress'] = 'Fetching TV shows from media server…'
        logger.info("sync_library: fetching TV shows from media server...")
        try:
            if _jf_mode:
                ms_shows = client.get_all_items_with_guids(media_type='Series')
            else:
                ms_shows = client.get_all_items_with_guids(plex_type=2)
            logger.info(f"sync_library: got {len(ms_shows)} shows from media server")
            _library_sync_state['progress'] = f'Matching {len(ms_shows)} shows…'

            for item in ms_shows:
                rk = item['ratingKey']
                imdb_id = item.get('imdb_id')
                tmdb_id = item.get('tmdb_id')
                if not rk:
                    continue
                updated = 0
                if imdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='episode' AND imdb_id=? AND {_needs_id}",
                        (rk, imdb_id))
                    updated = cursor.rowcount
                if not updated and tmdb_id:
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='episode' AND tmdb_id=? AND {_needs_id}",
                        (rk, tmdb_id))
                    updated = cursor.rowcount
                # Fallback: match by title + year, only for Plex items with no external IDs at all
                if not updated and not imdb_id and not tmdb_id and item.get('title') and item.get('year'):
                    cursor.execute(
                        f"UPDATE media_items SET ms_item_id=? WHERE type='episode' AND LOWER(title)=LOWER(?) AND year=? AND {_needs_id}",
                        (rk, item['title'], item['year']))
                    updated = cursor.rowcount
                _library_sync_state['updated_episodes'] += updated

        except Exception as e:
            msg = f"TV show sync error: {e}"
            logger.error(f"sync_library: {msg}", exc_info=True)
            _library_sync_state['errors'].append(msg)

        conn.commit()
        conn.close()

        # Clean up season_overlay_state rows whose show ratingKey is no longer valid.
        # This happens when Plex rescans and assigns new ratingKeys to shows — the old
        # season rows become orphaned and would 404 on the next overlay sync. Removing
        # them here lets the overlay sync re-register the seasons with fresh ratingKeys.
        try:
            _conn2 = _get_db_connection()
            _conn2.execute('''
                DELETE FROM season_overlay_state
                WHERE show_ms_item_id NOT IN (
                    SELECT DISTINCT ms_item_id FROM media_items
                    WHERE ms_item_id IS NOT NULL
                )
            ''')
            _stale = _conn2.total_changes
            _conn2.commit()
            _conn2.close()
            if _stale:
                logger.info(f"sync_library: removed {_stale} stale season_overlay_state row(s)")
        except Exception as _se:
            logger.warning(f"sync_library: could not clean stale season rows: {_se}")

        m = _library_sync_state['updated_movies']
        ep = _library_sync_state['updated_episodes']
        logger.info(f"sync_library complete: {m} movies, {ep} episodes updated")
        _library_sync_state['progress'] = f'Done — {m} movies, {ep} shows matched'
        log_activity('sync_library',
                     title=f"Sync Library: {m} movies, {ep} shows matched",
                     stats={'movies_updated': m, 'episodes_updated': ep})

    except Exception as e:
        logger.error(f"sync_library background error: {e}", exc_info=True)
        _library_sync_state['errors'].append(str(e))
        _library_sync_state['progress'] = f'Error: {e}'

    finally:
        _library_sync_state['running'] = False
        _library_sync_state['done'] = True


@overlay_bp.route('/api/overlays/sync_library', methods=['POST'])
def sync_library():
    """Start background media-server key sync (returns immediately)."""
    global _library_sync_state

    if _library_sync_state['running']:
        return jsonify({'success': True, 'status': 'already_running',
                        'progress': _library_sync_state['progress']})

    if is_jellyfin_mode():
        jf_token = get_jellyfin_token()
        if not jf_token:
            return jsonify({'success': False, 'error': 'Jellyfin token not configured in Settings'}), 400
    else:
        plex_url = get_setting('Plex', 'url', default='')
        plex_token = get_setting('Plex', 'token', default='')
        if not plex_url or not plex_token:
            return jsonify({'success': False, 'error': 'Plex URL / token not configured in Settings'}), 400

    import threading
    t = threading.Thread(target=_run_library_sync, daemon=True)
    t.start()

    return jsonify({'success': True, 'status': 'started',
                    'message': 'Sync started in background — check /api/overlays/sync_library/status'})


@overlay_bp.route('/api/overlays/sync_library/status', methods=['GET'])
def sync_library_status():
    """Return current state of the background Plex key sync."""
    return jsonify({
        'success': True,
        'running': _library_sync_state['running'],
        'done': _library_sync_state['done'],
        'progress': _library_sync_state['progress'],
        'updated_movies': _library_sync_state['updated_movies'],
        'updated_episodes': _library_sync_state['updated_episodes'],
        'errors': _library_sync_state['errors'],
    })


@overlay_bp.route('/api/overlays/status/<int:media_item_id>', methods=['GET'])
def get_overlay_status(media_item_id):
    """Get overlay generation status for a media item."""
    try:
        manager = _get_overlay_manager()
        state = manager._get_overlay_state(media_item_id)

        if not state:
            return jsonify({
                'success': True,
                'status': 'not_started',
                'message': 'No overlay generation attempted yet'
            })

        return jsonify({
            'success': True,
            'status': state['status'],
            'reason': state.get('reason'),
            'retry_count': state.get('retry_count', 0),
            'last_retry': state.get('last_retry'),
            'overlay_applied_at': state.get('overlay_applied_at')
        })

    except Exception as e:
        logger.error(f"Failed to get overlay status for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# System Status Endpoints
# ============================================

@overlay_bp.route('/api/overlays/status', methods=['GET'])
def system_status():
    """Get overlay system status and statistics."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # ── Layout counts (tiny table, fast) ──────────────────────────────────
        cursor.execute('''
            SELECT
                COUNT(*)                                             AS total,
                SUM(CASE WHEN is_default = 1 THEN 1 ELSE 0 END)    AS active,
                SUM(CASE WHEN media_type = 'movie'  THEN 1 ELSE 0 END) AS movies,
                SUM(CASE WHEN media_type = 'tv'     THEN 1 ELSE 0 END) AS shows,
                SUM(CASE WHEN media_type = 'season' THEN 1 ELSE 0 END) AS seasons
            FROM overlay_layouts
        ''')
        lr = cursor.fetchone()
        total_layouts   = lr[0] or 0
        active_layouts  = lr[1] or 0
        layouts_movies  = lr[2] or 0
        layouts_shows   = lr[3] or 0
        layouts_seasons = lr[4] or 0

        # ── media_overlay_state aggregated in one pass ────────────────────────
        # One JOIN query that counts applied/failed per type in a single scan.
        cursor.execute('''
            SELECT
                m.type,
                o.status,
                COUNT(DISTINCT m.ms_item_id) AS cnt
            FROM media_overlay_state o
            JOIN media_items m ON m.id = o.media_item_id
            WHERE m.ms_item_id IS NOT NULL
              AND o.status IN ('applied', 'failed', 'removal_failed')
            GROUP BY m.type, o.status
        ''')
        _mos = {}  # (type, status) -> cnt
        for row in cursor.fetchall():
            _mos[(row[0], row[1])] = row[2]

        applied_movies = _mos.get(('movie',   'applied'), 0)
        applied_shows  = _mos.get(('episode', 'applied'), 0)
        failed_movies  = _mos.get(('movie',   'failed'),  0) + _mos.get(('movie',   'removal_failed'), 0)
        failed_shows   = _mos.get(('episode', 'failed'),  0) + _mos.get(('episode', 'removal_failed'), 0)

        # ── season_overlay_state aggregated in one pass ───────────────────────
        cursor.execute('''
            SELECT status, COUNT(*) FROM season_overlay_state GROUP BY status
        ''')
        _sos = dict(cursor.fetchall())
        applied_seasons = _sos.get('applied', 0)
        failed_seasons  = _sos.get('failed', 0) + _sos.get('removal_failed', 0)
        skipped_seasons = _sos.get('skipped', 0)

        applied_count = applied_movies + applied_shows + applied_seasons

        # ── Unprocessed movies ────────────────────────────────────────────────
        # Items with no overlay row at all, or in pending/removed/user_removed state
        cursor.execute('''
            SELECT COUNT(DISTINCT m.id)
            FROM media_items m
            LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE m.ms_item_id IS NOT NULL
              AND m.ms_item_id != ''
              AND m.state IN ('Collected', 'Upgrading')
              AND m.type = 'movie'
              AND (o.media_item_id IS NULL OR o.status IN ('pending', 'removed', 'user_removed'))
        ''')
        unprocessed_movies = cursor.fetchone()[0]

        # ── Unprocessed shows ─────────────────────────────────────────────────
        # A show is "unprocessed" when none of its episodes have an 'applied' row.
        # Use a subquery of applied show keys rather than a correlated NOT EXISTS.
        cursor.execute('''
            SELECT COUNT(DISTINCT m.ms_item_id)
            FROM media_items m
            WHERE m.ms_item_id IS NOT NULL
              AND m.ms_item_id != ''
              AND m.state IN ('Collected', 'Upgrading')
              AND m.type = 'episode'
              AND m.ms_item_id NOT IN (
                  SELECT DISTINCT m2.ms_item_id
                  FROM media_overlay_state o2
                  JOIN media_items m2 ON m2.id = o2.media_item_id
                  WHERE m2.type = 'episode'
                    AND o2.status = 'applied'
                    AND m2.ms_item_id IS NOT NULL
              )
        ''')
        unprocessed_shows = cursor.fetchone()[0]

        # ── Unprocessed seasons ───────────────────────────────────────────────
        # Count seasons registered in season_overlay_state that are pending/failed.
        # Using tv_shows.total_seasons (metadata count) was inaccurate because it
        # includes seasons from metadata that don't exist in the media server yet
        # (future seasons, uncollected seasons), inflating the "unprocessed" count.
        unprocessed_seasons = _sos.get('pending', 0) + _sos.get('failed', 0)

        unprocessed_count = unprocessed_movies + unprocessed_shows

        conn.close()

        return jsonify({
            'success': True,
            'layouts': {
                'total': total_layouts,
                'active': active_layouts,
                'layouts_movies': layouts_movies,
                'layouts_shows': layouts_shows,
                'layouts_seasons': layouts_seasons,
            },
            'overlays': {
                'applied': applied_count,
                'applied_movies': applied_movies,
                'applied_shows': applied_shows,
                'applied_seasons': applied_seasons,
                'unprocessed': unprocessed_count,
                'unprocessed_movies': unprocessed_movies,
                'unprocessed_shows': unprocessed_shows,
                'unprocessed_seasons': unprocessed_seasons,
                'failed': failed_movies + failed_shows + failed_seasons,
                'failed_movies': failed_movies,
                'failed_shows': failed_shows,
                'failed_seasons': failed_seasons,
                'skipped': _sos.get('skipped', 0)
            }
        })

    except Exception as e:
        logger.error(f"Failed to get system status: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Media List Endpoints
# ============================================

def _cached_poster_path(imdb_id, tmdb_id, media_type='movie'):
    """Return TMDB poster path from cache (e.g. '/abc.jpg') or None."""
    try:
        from routes.poster_cache import get_cached_poster_url
        cached = (get_cached_poster_url(imdb_id, media_type) if imdb_id else None) or \
                 (get_cached_poster_url(tmdb_id, media_type) if tmdb_id else None)
        if cached and 'image.tmdb.org' in cached:
            parts = cached.split('/t/p/')
            if len(parts) > 1:
                full_path = parts[1]
                return ('/' + full_path.split('/', 1)[1]) if '/' in full_path else None
    except Exception:
        pass
    return None


@overlay_bp.route('/api/overlays/media/movies', methods=['GET'])
def list_movies():
    """Get list of movies with overlay status."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Get movies with overlay status.
        # Group by ms_item_id when available so split Plex items (each with a unique
        # ratingKey) appear as separate cards with their own overlays. Fall back to
        # imdb_id/tmdb_id grouping only for items not yet synced (ms_item_id is NULL).
        # Merged Plex items (multiple DB rows sharing the same ms_item_id) are still
        # collapsed to one card and show a version_count badge.
        cursor.execute('''
            SELECT
                MIN(m.id) AS id,
                m.title,
                m.year,
                m.imdb_id,
                m.tmdb_id,
                m.ms_item_id,
                COUNT(*) AS version_count,
                CASE
                    WHEN MAX(CASE WHEN o.status = 'applied'         THEN 1 ELSE 0 END) = 1 THEN 'applied'
                    WHEN MAX(CASE WHEN o.status = 'pending'         THEN 1 ELSE 0 END) = 1 THEN 'pending'
                    WHEN MAX(CASE WHEN o.status = 'analyzing'       THEN 1 ELSE 0 END) = 1 THEN 'analyzing'
                    WHEN MAX(CASE WHEN o.status = 'failed'          THEN 1 ELSE 0 END) = 1 THEN 'failed'
                    WHEN MAX(CASE WHEN o.status = 'removal_failed'  THEN 1 ELSE 0 END) = 1 THEN 'failed'
                    ELSE 'not_started'
                END AS overlay_status,
                MAX(CASE WHEN o.status IN ('failed', 'removal_failed') THEN o.reason ELSE NULL END) AS overlay_reason
            FROM media_items m
            LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE m.type = 'movie'
              AND m.state IN ('Collected', 'Upgrading')
            GROUP BY
                CASE
                    WHEN m.ms_item_id IS NOT NULL AND m.ms_item_id != ''
                    THEN m.ms_item_id
                    ELSE COALESCE(NULLIF(m.imdb_id, ''), NULLIF(m.tmdb_id, ''), m.title || CAST(m.year AS TEXT))
                END
            ORDER BY m.title
        ''')

        items = []
        for row in cursor.fetchall():
            items.append({
                'id': row['id'],
                'title': row['title'],
                'year': row['year'],
                'imdb_id': row['imdb_id'],
                'tmdb_id': row['tmdb_id'],
                'ms_item_id': row['ms_item_id'],
                'overlay_status': row['overlay_status'],
                'overlay_reason': row['overlay_reason'],
                'version_count': row['version_count'],
                'poster_path': _cached_poster_path(row['imdb_id'], row['tmdb_id'], 'movie'),
            })

        conn.close()

        return jsonify({
            'success': True,
            'count': len(items),
            'items': items
        })

    except Exception as e:
        logger.error(f"Failed to list movies: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/media/shows', methods=['GET'])
def list_shows():
    """Get list of TV shows with overlay status."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Get TV shows with overlay status — driven by media_items so newly-added
        # shows appear immediately without waiting for the tv_shows background task.
        # tv_shows is LEFT JOINed for metadata (title/year fallback) only.
        cursor.execute('''
            SELECT
                ep.imdb_id,
                ep.tmdb_id,
                COALESCE(ts.title, ep.title)           AS title,
                COALESCE(ts.year,  ep.year)            AS year,
                ep.ms_item_id,
                ep.collected_episodes,
                ep.total_episodes,
                ep.rep_id,
                CASE WHEN o.status = 'removal_failed' THEN 'failed'
                     ELSE COALESCE(o.status, 'not_started') END AS overlay_status,
                CASE WHEN o.status IN ('failed', 'removal_failed') THEN o.reason ELSE NULL END AS overlay_reason,
                ss.unprocessed_season_nums
            FROM (
                SELECT
                    COALESCE(NULLIF(imdb_id, ''), NULLIF(tmdb_id, ''), title || CAST(year AS TEXT)) AS show_key,
                    MAX(ms_item_id)        AS ms_item_id,
                    MIN(id)                AS rep_id,
                    MAX(imdb_id)           AS imdb_id,
                    MAX(tmdb_id)           AS tmdb_id,
                    MAX(title)             AS title,
                    MAX(year)              AS year,
                    COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading')
                          THEN season_number || '-' || episode_number END) AS collected_episodes,
                    COUNT(DISTINCT season_number || '-' || episode_number) AS total_episodes
                FROM media_items
                WHERE type = 'episode'
                  AND (ghostlisted = 0 OR ghostlisted IS NULL)
                GROUP BY show_key
            ) ep
            LEFT JOIN tv_shows ts ON ts.imdb_id = ep.imdb_id
                                  OR ts.tmdb_id = ep.tmdb_id
            LEFT JOIN media_overlay_state o ON ep.rep_id = o.media_item_id
            LEFT JOIN (
                SELECT f.show_ms_item_id,
                       GROUP_CONCAT(f.season_number) AS unprocessed_season_nums
                FROM season_overlay_state f
                WHERE f.status IN ('not_started', 'pending', 'failed', 'removal_failed')
                  AND NOT EXISTS (
                      SELECT 1 FROM season_overlay_state a
                      WHERE a.show_ms_item_id = f.show_ms_item_id
                        AND a.season_number = f.season_number
                        AND a.status = 'applied'
                  )
                GROUP BY f.show_ms_item_id
            ) ss ON ss.show_ms_item_id = ep.ms_item_id
            WHERE ep.collected_episodes > 0
            ORDER BY COALESCE(ts.title, ep.title)
        ''')

        items = []
        for row in cursor.fetchall():
            raw = row['unprocessed_season_nums']
            if raw:
                seasons_unprocessed = sorted(set(
                    int(x) for x in raw.split(',') if x.strip().lstrip('-').isdigit()
                ))
            else:
                seasons_unprocessed = []
            items.append({
                'id': row['rep_id'],
                'title': row['title'],
                'year': row['year'],
                'imdb_id': row['imdb_id'],
                'tmdb_id': row['tmdb_id'],
                'ms_item_id': row['ms_item_id'],
                'overlay_status': row['overlay_status'],
                'overlay_reason': row['overlay_reason'],
                'collected_episodes': row['collected_episodes'] or 0,
                'total_episodes': row['total_episodes'] or 0,
                'poster_path': _cached_poster_path(row['imdb_id'], row['tmdb_id'], 'show'),
                'seasons_unprocessed': seasons_unprocessed,
            })

        conn.close()

        return jsonify({
            'success': True,
            'count': len(items),
            'items': items
        })

    except Exception as e:
        logger.error(f"Failed to list shows: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Season Overlay Endpoints
# ============================================

@overlay_bp.route('/api/overlays/poster_applied/<ms_item_id>', methods=['GET'])
def poster_applied(ms_item_id):
    """
    Proxy the currently-selected (overlay-applied) Plex poster for a movie or show.
    Returns a 300px-wide JPEG thumbnail. Results are cached to disk for 24 hours so
    that the overlay page never floods Plex with hundreds of simultaneous downloads.
    """
    from io import BytesIO
    from flask import Response
    cache_path = _thumb_cache_path(ms_item_id)
    cached = _serve_from_thumb_cache(cache_path)
    if cached:
        return cached
    try:
        manager = _get_overlay_manager()
        img = manager.client.download_poster(ms_item_id)
        if not img:
            abort(404)
        thumb_w = 300
        w, h = img.size
        if w > thumb_w:
            thumb_h = int(h * thumb_w / w)
            img = img.resize((thumb_w, thumb_h))
        buf = BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=85)
        jpeg_bytes = buf.getvalue()
        _write_thumb_cache(cache_path, jpeg_bytes)
        return Response(jpeg_bytes, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        logger.debug(f"poster_applied {ms_item_id}: {e}")
        abort(404)


@overlay_bp.route('/api/overlays/seasons/<season_key>/thumb', methods=['GET'])
def season_thumb(season_key):
    """Proxy season thumbnail from the media server to the browser (disk-cached 24 h)."""
    from io import BytesIO
    from flask import Response
    cache_path = _thumb_cache_path(season_key, prefix='season_')
    cached = _serve_from_thumb_cache(cache_path)
    if cached:
        return cached
    try:
        manager = _get_overlay_manager()
        img = manager.client.download_poster(season_key)
        if not img:
            abort(404)
        buf = BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=85)
        jpeg_bytes = buf.getvalue()
        _write_thumb_cache(cache_path, jpeg_bytes)
        return Response(jpeg_bytes, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        logger.debug(f"season_thumb {season_key}: {e}")
        abort(404)


@overlay_bp.route('/api/overlays/seasons/<show_plex_rating_key>', methods=['GET'])
def list_seasons(show_plex_rating_key):
    """
    Fetch all Plex seasons for a show and return them with their overlay status.

    Query params:
        layout_id (optional): layout to pre-select in the UI
    """
    try:
        manager = _get_overlay_manager()
        seasons = manager.client.get_show_seasons(show_plex_rating_key)

        if not seasons:
            return jsonify({'success': True, 'seasons': []}), 200

        # Attach stored overlay status for each season
        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT season_ms_item_id, status, reason, overlay_applied_at '
            'FROM season_overlay_state WHERE show_ms_item_id = ?',
            (show_plex_rating_key,)
        )
        state_map = {r['season_ms_item_id']: dict(r) for r in cursor.fetchall()}
        conn.close()

        result = []
        for s in seasons:
            rk = s['ratingKey']
            state = state_map.get(rk, {})
            result.append({
                'season_ms_item_id': rk,
                'title': s['title'],
                'season_number': s['index'],
                'thumb_url': s.get('thumb_url'),
                'overlay_status': state.get('status', 'not_started'),
                'overlay_applied_at': state.get('overlay_applied_at'),
                'reason': state.get('reason'),
            })

        return jsonify({'success': True, 'seasons': result})

    except Exception as e:
        logger.error(f"Failed to list seasons for show {show_plex_rating_key}: {e}",
                     exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/seasons/<show_plex_rating_key>/generate', methods=['POST'])
def generate_season_overlays(show_plex_rating_key):
    """
    Generate overlays for all (or selected) seasons of a show.

    Body JSON:
        season_keys (list, optional): specific season rating keys; if absent → all seasons
        layout_id (int, optional): explicit layout ID
        force (bool, optional): force regeneration even if already applied
    """
    try:
        data = request.get_json() or {}
        season_keys = data.get('season_keys')   # None → all seasons
        layout_id = data.get('layout_id')
        force = bool(data.get('force', False))

        manager = _get_overlay_manager()
        seasons = manager.client.get_show_seasons(show_plex_rating_key)

        if season_keys:
            seasons = [s for s in seasons if s['ratingKey'] in season_keys]

        if not seasons:
            return jsonify({'success': True, 'message': 'No seasons found', 'results': []})

        # Look up show title for failure labels in activity log
        _show_title = show_plex_rating_key
        try:
            _st_conn = _get_db_connection()
            _st_row = _st_conn.execute(
                "SELECT title FROM media_items WHERE ms_item_id=? AND type='episode' LIMIT 1",
                (show_plex_rating_key,)).fetchone()
            _st_conn.close()
            if _st_row:
                _show_title = _st_row['title']
        except Exception:
            pass

        results = []
        applied = failed = skipped = 0

        for s in seasons:
            res = manager.generate_season_overlay(
                show_plex_rating_key=show_plex_rating_key,
                season_plex_rating_key=s['ratingKey'],
                season_number=s['index'],
                force=force,
                layout_id=layout_id,
            )
            results.append({
                'season_number': s['index'],
                'title': s['title'],
                'status': res.get('status'),
                'message': res.get('message'),
            })
            status = res.get('status', 'error')
            if status == 'applied':
                applied += 1
            elif status == 'skipped':
                skipped += 1
            else:
                failed += 1

        _fail_names = [f"{_show_title} - {r['title']}" for r in results
                       if r.get('status') not in ('applied', 'skipped', 'analyzing')]
        _fail_stats = {'failures': _fail_names} if 0 < failed <= 10 else {}
        log_activity('season_generate',
                     title=f"Season overlays (show {show_plex_rating_key}): {applied} applied, {failed} failed, {skipped} skipped",
                     stats={'show_key': show_plex_rating_key, 'total': len(seasons),
                            'applied': applied, 'failed': failed, 'skipped': skipped,
                            **_fail_stats})
        return jsonify({
            'success': True,
            'total': len(seasons),
            'applied': applied,
            'skipped': skipped,
            'failed': failed,
            'results': results,
        })

    except Exception as e:
        logger.error(f"Failed to generate season overlays for show {show_plex_rating_key}: {e}",
                     exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route(
    '/api/overlays/seasons/<show_plex_rating_key>/<season_plex_rating_key>/regenerate',
    methods=['POST'])
def regenerate_season_overlay(show_plex_rating_key, season_plex_rating_key):
    """
    Force-regenerate the overlay for a single season using the current Plex poster.

    Body JSON:
        season_number (int, required)
        layout_id (int, optional)
    """
    try:
        data = request.get_json() or {}
        season_number = data.get('season_number', 0)
        layout_id = data.get('layout_id')

        manager = _get_overlay_manager()
        res = manager.generate_season_overlay(
            show_plex_rating_key=show_plex_rating_key,
            season_plex_rating_key=season_plex_rating_key,
            season_number=season_number,
            force=True,
            layout_id=layout_id,
            force_fresh_poster=True,
        )
        if res.get('success'):
            log_activity('season_regenerate',
                         title=f"Season {season_number} regenerated (show {show_plex_rating_key})",
                         stats={'show_key': show_plex_rating_key,
                                'season_key': season_plex_rating_key, 'season_number': season_number})
        return jsonify({
            'success': res.get('success', False),
            'status': res.get('status'),
            'message': res.get('message'),
        })

    except Exception as e:
        logger.error(
            f"Failed to regenerate season overlay {season_plex_rating_key}: {e}",
            exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route(
    '/api/overlays/seasons/<show_plex_rating_key>/<season_plex_rating_key>/remove',
    methods=['POST'])
def remove_season_overlay(show_plex_rating_key, season_plex_rating_key):
    """
    Remove overlay from a single season and restore its original poster.

    Body JSON:
        season_number (int, required)
    """
    try:
        data = request.get_json() or {}
        season_number = data.get('season_number', 0)

        manager = _get_overlay_manager()
        res = manager.remove_season_overlay(
            show_plex_rating_key=show_plex_rating_key,
            season_plex_rating_key=season_plex_rating_key,
            season_number=season_number,
            user_initiated=True,
        )
        if res.get('success'):
            invalidate_season_thumb_cache(season_plex_rating_key)
            log_activity('season_remove',
                         title=f"Season {season_number} overlay removed (show {show_plex_rating_key})",
                         stats={'show_key': show_plex_rating_key,
                                'season_key': season_plex_rating_key, 'season_number': season_number})
        return jsonify({
            'success': res.get('success', False),
            'status': res.get('status'),
            'message': res.get('message'),
        })

    except Exception as e:
        logger.error(
            f"Failed to remove season overlay {season_plex_rating_key}: {e}",
            exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Settings Endpoints
# ============================================

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


# ============================================
# Cache Cleanup Endpoints
# ============================================

@overlay_bp.route('/api/overlays/cleanup/stats', methods=['GET'])
def get_cleanup_stats():
    """Combined cleanup stats: applied overlay count, backup dir size, and cumulative totals."""
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM media_overlay_state WHERE status = 'applied'")
        applied_count = cursor.fetchone()[0]

        # Cumulative all-time cleanup counters
        cursor.execute(
            "SELECT key, value FROM overlay_sync_state WHERE key IN ('cleanup_total_posters', 'cleanup_total_bytes')"
        )
        counters = {row[0]: int(row[1] or 0) for row in cursor.fetchall()}
        conn.close()

        total_posters = counters.get('cleanup_total_posters', 0)
        total_bytes   = counters.get('cleanup_total_bytes', 0)
        total_mb      = round(total_bytes / (1024 * 1024), 2)

        # Current backup directory size + orphaned count
        from overlays.cache_cleanup import PosterCacheManager
        from pathlib import Path
        manager = PosterCacheManager(None)
        backup_stats = manager.get_backup_stats()

        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT ms_item_id FROM media_items WHERE ms_item_id IS NOT NULL')
        valid_keys = set(row[0] for row in cursor.fetchall())
        cursor.execute('''
            SELECT DISTINCT season_ms_item_id FROM season_overlay_state
            WHERE season_ms_item_id IS NOT NULL
              AND status NOT IN ('removed', 'user_removed')
        ''')
        valid_season_keys = set(row[0] for row in cursor.fetchall())
        conn.close()

        orphaned_count = 0
        for f in manager.backup_dir.glob('*_original.jpg'):
            stem = f.stem.replace('_original', '')
            if stem.startswith('season_'):
                if stem[len('season_'):] not in valid_season_keys:
                    orphaned_count += 1
            else:
                if stem not in valid_keys:
                    orphaned_count += 1

        return jsonify({
            'success': True,
            'applied_count': applied_count,
            'backup_count': backup_stats.get('backup_count', 0),
            'backup_size_mb': backup_stats.get('total_size_mb', 0),
            'backup_orphaned': orphaned_count,
            'cleaned_total_posters': total_posters,
            'cleaned_total_mb': total_mb,
        })
    except Exception as e:
        logger.error(f"Failed to get cleanup stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/cache-size', methods=['GET'])
def get_cache_size():
    """Get backup dir size (Plex cache detection is unreliable; returns backup stats instead)."""
    try:
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        result = manager.get_backup_stats()
        # Map to expected field name used by frontend
        result['total_size_gb'] = result.get('total_size_mb', 0) / 1024
        result['success'] = True
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to get cache size: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/backup-stats', methods=['GET'])
def get_backup_stats():
    """Get poster backup statistics."""
    try:
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        result = manager.get_backup_stats()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to get backup stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/remove/<int:media_item_id>', methods=['POST'])
def remove_overlay(media_item_id):
    """Remove overlay and restore original poster for a single item."""
    try:
        from overlays.cache_cleanup import PosterCacheManager

        manager = PosterCacheManager(None)

        result = manager.remove_overlay(media_item_id)
        if result.get('success'):
            # Look up ms_item_id so we can drop the thumb cache for this item
            try:
                _c = _get_db_connection()
                _row = _c.execute('SELECT ms_item_id FROM media_items WHERE id = ?',
                                  (media_item_id,)).fetchone()
                _c.close()
                if _row and _row[0]:
                    invalidate_poster_thumb_cache(_row[0])
            except Exception:
                pass
            log_activity('remove', title=f"Overlay removed (item {media_item_id})",
                         stats={'media_item_id': media_item_id})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to remove overlay for item {media_item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/remove-all', methods=['POST'])
def remove_all_overlays():
    """Start a background Remove-All job; returns immediately with total count."""
    import threading
    from overlays.cache_cleanup import get_remove_all_status, run_remove_all_job
    try:
        status = get_remove_all_status()
        if status.get('running'):
            return jsonify({'success': False, 'error': 'A remove-all job is already running'}), 409

        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MIN(o.media_item_id) as media_item_id
            FROM media_overlay_state o
            JOIN media_items m ON m.id = o.media_item_id
            WHERE o.status = 'applied' AND m.ms_item_id IS NOT NULL
            GROUP BY m.ms_item_id
        """)
        item_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute("""
            SELECT show_ms_item_id, season_ms_item_id, season_number
            FROM season_overlay_state
            WHERE status = 'applied'
        """)
        season_rows = list(cursor.fetchall())
        conn.close()

        if not item_ids and not season_rows:
            return jsonify({'success': True, 'started': False, 'total': 0,
                            'message': 'No applied overlays found'})

        overlay_manager = _get_overlay_manager()
        t = threading.Thread(
            target=run_remove_all_job,
            args=(item_ids, season_rows, overlay_manager),
            daemon=True,
        )
        t.start()

        return jsonify({'success': True, 'started': True,
                        'total': len(item_ids) + len(season_rows)})
    except Exception as e:
        logger.error(f"Failed to start remove-all job: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/remove-all/status', methods=['GET'])
def remove_all_status():
    """Poll progress of the running/last Remove-All job."""
    try:
        from overlays.cache_cleanup import get_remove_all_status
        s = get_remove_all_status()
        pct = 0
        if s['total'] > 0:
            pct = round((s['done']) / s['total'] * 100)
        return jsonify({
            'success': True,
            'running': s['running'],
            'total': s['total'],
            'done': s['done'],
            'restored': s['restored'],
            'failed': s['failed'],
            'current': s['current'],
            'errors': s['errors'],
            'percent': pct,
            'finished_at': s.get('finished_at'),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/remove-batch', methods=['POST'])
def remove_overlays_batch():
    """Remove overlays for multiple items."""
    try:
        data = request.get_json()

        if not data or 'item_ids' not in data:
            return jsonify({'success': False, 'error': 'No item_ids provided'}), 400

        item_ids = data['item_ids']
        if not isinstance(item_ids, list) or not item_ids:
            return jsonify({'success': False, 'error': 'item_ids must be a non-empty array'}), 400

        from overlays.cache_cleanup import PosterCacheManager

        cache_manager = PosterCacheManager(None)
        result = cache_manager.batch_remove_overlays(item_ids)

        # Invalidate thumb cache for all items whose poster just changed
        try:
            _ic = _get_db_connection()
            _placeholders = ','.join('?' * len(item_ids))
            _rows = _ic.execute(
                f'SELECT DISTINCT ms_item_id FROM media_items WHERE id IN ({_placeholders}) AND ms_item_id IS NOT NULL',
                item_ids
            ).fetchall()
            _ic.close()
            for _r in _rows:
                invalidate_poster_thumb_cache(_r[0])
        except Exception:
            pass

        # Also remove season overlays for any TV shows in the selection.
        # Items may be episodes (type='episode') or shows (type='show'); both share
        # the show-level ms_item_id which is what season_overlay_state indexes on.
        conn = _get_db_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(item_ids))
        cursor.execute(
            f"SELECT DISTINCT ms_item_id FROM media_items "
            f"WHERE id IN ({placeholders}) AND type IN ('episode', 'show') AND ms_item_id IS NOT NULL",
            item_ids
        )
        show_keys = [r[0] for r in cursor.fetchall()]
        if show_keys:
            key_placeholders = ','.join('?' * len(show_keys))
            cursor.execute(
                f"SELECT show_ms_item_id, season_ms_item_id, season_number "
                f"FROM season_overlay_state "
                f"WHERE show_ms_item_id IN ({key_placeholders}) AND status='applied'",
                show_keys
            )
            season_rows = cursor.fetchall()
            conn.close()

            overlay_manager = _get_overlay_manager()

            # DB-tracked seasons first
            for sr in season_rows:
                try:
                    sr_result = overlay_manager.remove_season_overlay(
                        show_plex_rating_key=sr['show_ms_item_id'],
                        season_plex_rating_key=sr['season_ms_item_id'],
                        season_number=sr['season_number'],
                    )
                    if sr_result.get('success'):
                        invalidate_season_thumb_cache(sr['season_ms_item_id'])
                        result['restored'] = result.get('restored', 0) + 1
                    elif sr_result.get('status') != 'skipped':
                        logger.warning(f"Season overlay removal failed for {sr['season_ms_item_id']}: {sr_result.get('message')}")
                        result['failed'] = result.get('failed', 0) + 1
                except Exception as _se:
                    logger.warning(f"Failed to remove season overlay {sr['season_ms_item_id']}: {_se}")
                    result['failed'] = result.get('failed', 0) + 1

            # Fallback: scan Plex directly for untracked season overlays
            tracked_season_keys = {sr['season_ms_item_id'] for sr in season_rows}
            for show_key in show_keys:
                try:
                    seasons = overlay_manager.client.get_show_seasons(show_key)
                    for season in seasons:
                        if season['ratingKey'] in tracked_season_keys:
                            continue  # already handled above
                        sr_result = overlay_manager.remove_season_overlay(
                            show_plex_rating_key=show_key,
                            season_plex_rating_key=season['ratingKey'],
                            season_number=season.get('index', 0),
                        )
                        if sr_result.get('success'):
                            invalidate_season_thumb_cache(season['ratingKey'])
                            result['restored'] = result.get('restored', 0) + 1
                        elif sr_result.get('status') == 'error':
                            result['failed'] = result.get('failed', 0) + 1
                except Exception as _scan_err:
                    logger.debug(f"Season scan failed for show {show_key}: {_scan_err}")
        else:
            conn.close()

        log_activity('remove',
                     title=f"Remove selected overlays: {result.get('restored', 0)} restored, {result.get('failed', 0)} failed",
                     stats={'restored': result.get('restored', 0), 'failed': result.get('failed', 0),
                            'count': len(item_ids)})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to batch remove overlays: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/orphaned-backups', methods=['POST'])
def cleanup_orphaned_backups():
    """Clean up orphaned poster backups."""
    try:
        from overlays.cache_cleanup import task_cleanup_orphaned_backups

        result = task_cleanup_orphaned_backups()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to cleanup orphaned backups: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/cleanup/delete-all-backups', methods=['POST'])
def delete_all_backups():
    """Delete all backup files and reset overlay states to pending."""
    try:
        from overlays.cache_cleanup import PosterCacheManager
        manager = PosterCacheManager(None)
        result = manager.delete_all_backups()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Failed to delete all backups: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/stats/reset-cleanup', methods=['POST'])
def reset_cleanup_stats():
    """Reset the cumulative cleaned-up poster counters to zero."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        conn.execute(
            "UPDATE overlay_sync_state SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key IN ('cleanup_total_posters', 'cleanup_total_bytes')"
        )
        conn.commit()
        conn.close()
        logger.info("Cleanup stats reset to zero")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Failed to reset cleanup stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/stats/reset-failed', methods=['POST'])
def reset_failed_stats():
    """Reset all failed overlay items back to pending so they are retried."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE media_overlay_state SET status = 'pending', retry_count = 0, "
            "reason = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('failed', 'removal_failed')"
        )
        movie_show_count = cursor.rowcount
        cursor.execute(
            "UPDATE season_overlay_state SET status = 'pending', retry_count = 0, "
            "reason = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('failed', 'removal_failed')"
        )
        season_count = cursor.rowcount
        conn.commit()
        conn.close()
        total = movie_show_count + season_count
        logger.info(f"Failed stats reset: {total} item(s) set back to pending")
        return jsonify({'success': True, 'reset_count': total})
    except Exception as e:
        logger.error(f"Failed to reset failed stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Poster Reset Routes (Phase 1 + Phase 2)
# ============================================

@overlay_bp.route('/api/overlays/reset/start', methods=['POST'])
def poster_reset_start():
    """
    Phase 1 — start a full scorched-earth reset job.
    Resets ALL library posters to clean TMDB originals.
    Body (optional JSON): {"reset_seasons": true}
    """
    try:
        from overlays.poster_reset import start_reset_job
        data = request.get_json(silent=True) or {}
        reset_seasons = data.get('reset_seasons', True)
        started = start_reset_job(ms_item_ids=None, reset_seasons=reset_seasons)
        if not started:
            return jsonify({'success': False, 'error': 'A reset job is already running'}), 409
        return jsonify({'success': True, 'message': 'Reset job started'})
    except Exception as e:
        logger.error(f"Failed to start poster reset: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/reset/selective', methods=['POST'])
def poster_reset_selective():
    """
    Phase 2 — reset only the selected items.
    Body: {"ms_item_ids": ["123", "456", ...], "reset_seasons": true}
    """
    try:
        from overlays.poster_reset import start_reset_job
        data = request.get_json(silent=True) or {}
        keys = data.get('ms_item_ids', []) or data.get('plex_rating_keys', [])
        if not keys:
            return jsonify({'success': False, 'error': 'No ms_item_ids provided'}), 400
        reset_seasons = data.get('reset_seasons', True)
        started = start_reset_job(ms_item_ids=keys, reset_seasons=reset_seasons)
        if not started:
            return jsonify({'success': False, 'error': 'A reset job is already running'}), 409
        return jsonify({'success': True, 'message': f'Reset job started for {len(keys)} item(s)'})
    except Exception as e:
        logger.error(f"Failed to start selective poster reset: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/reset/status', methods=['GET'])
def poster_reset_status():
    """Poll the status of the running/last reset job."""
    try:
        from overlays.poster_reset import get_job_status
        status = get_job_status()
        pct = 0
        if status['total'] > 0:
            pct = round((status['done'] + status['failed'] + status['skipped']) / status['total'] * 100)
        return jsonify({
            'success': True,
            'running': status['running'],
            'total': status['total'],
            'done': status['done'],
            'failed': status['failed'],
            'skipped': status['skipped'],
            'current': status['current'],
            'errors': status['errors'],
            'percent': pct,
            'cancelled': status['cancelled'],
        })
    except Exception as e:
        logger.error(f"Failed to get reset status: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/reset/cancel', methods=['POST'])
def poster_reset_cancel():
    """Cancel the running reset job."""
    try:
        from overlays.poster_reset import cancel_job, get_job_status
        status = get_job_status()
        if not status['running']:
            return jsonify({'success': False, 'error': 'No job running'}), 400
        cancel_job()
        return jsonify({'success': True, 'message': 'Cancel requested'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/reset/preview', methods=['GET'])
def poster_reset_preview():
    """
    Phase 2 — return all library items with poster thumbnail URLs for the review grid.
    """
    try:
        from overlays.poster_reset import get_preview_items
        items = get_preview_items()
        return jsonify({'success': True, 'count': len(items), 'items': items})
    except Exception as e:
        logger.error(f"Failed to get reset preview items: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Badge Library Routes
# ============================================

def _get_badge_manager():
    """Get a BadgeManager instance."""
    from overlays.badge_manager import BadgeManager
    return BadgeManager(None)


@overlay_page_bp.route('/overlays/badges')
def badges_page():
    """Serve the badge library UI."""
    return render_template('badges.html')


@overlay_bp.route('/api/overlays/badges/types', methods=['GET'])
def get_badge_types():
    """List all badge types, optionally filtered by ?category=audio|video."""
    try:
        bm = _get_badge_manager()
        category = request.args.get('category')
        types = bm.get_badge_types(category=category)
        # Attach variation counts
        for t in types:
            variations = bm.get_variations(t['slug'])
            t['total_variations'] = len(variations)
            t['filled_variations'] = sum(1 for v in variations if v.get('has_asset'))
        return jsonify({'success': True, 'badge_types': types})
    except Exception as e:
        logger.error(f"Failed to get badge types: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/types/<slug>/variations', methods=['GET'])
def get_badge_variations(slug):
    """Get all variations for a badge type."""
    try:
        bm = _get_badge_manager()
        badge_type = bm.get_badge_type(slug)
        if not badge_type:
            return jsonify({'success': False, 'error': 'Badge type not found'}), 404
        variations = bm.get_variations(slug)
        return jsonify({'success': True, 'badge_type': badge_type, 'variations': variations})
    except Exception as e:
        logger.error(f"Failed to get variations for {slug}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/types/<slug>/variations', methods=['POST'])
def add_badge_variation(slug):
    """Add a new custom variation slot (no asset yet)."""
    try:
        bm = _get_badge_manager()
        badge_type = bm.get_badge_type(slug)
        if not badge_type:
            return jsonify({'success': False, 'error': 'Badge type not found'}), 404
        data = request.get_json() or {}
        variation_key  = data.get('variation_key', '').strip()
        display_name   = data.get('display_name', '').strip()
        if not variation_key or not display_name:
            return jsonify({'success': False, 'error': 'variation_key and display_name are required'}), 400
        new_id = bm.add_variation(badge_type['id'], variation_key, display_name)
        if new_id is None:
            return jsonify({'success': False, 'error': 'Variation key already exists'}), 409
        return jsonify({'success': True, 'variation_id': new_id})
    except Exception as e:
        logger.error(f"Failed to add variation for {slug}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/variations/<int:variation_id>/asset', methods=['POST'])
def upload_badge_variation_asset(variation_id):
    """Upload a PNG file for a badge variation."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({'success': False, 'error': 'Empty filename'}), 400
        allowed = {'.png', '.jpg', '.jpeg', '.webp'}
        if Path(f.filename).suffix.lower() not in allowed:
            return jsonify({'success': False, 'error': 'Only PNG/JPG/WebP images are accepted'}), 400
        bm = _get_badge_manager()
        ok = bm.save_variation_asset(variation_id, f.read(), f.filename)
        if ok:
            # Reset applied overlays to pending so they regenerate with the new badge
            try:
                conn = _get_db_connection()
                conn.execute(
                    "UPDATE media_overlay_state SET status = 'pending' WHERE status = 'applied'"
                )
                reset_count = conn.total_changes
                conn.commit()
                conn.close()
                logger.info(f"Badge updated: reset {reset_count} applied overlay(s) to pending for regeneration")
            except Exception as db_err:
                logger.warning(f"Badge upload succeeded but failed to reset overlay states: {db_err}")
                reset_count = 0
            return jsonify({'success': True, 'reset_overlays': reset_count})
        return jsonify({'success': False, 'error': 'Variation not found'}), 404
    except Exception as e:
        logger.error(f"Failed to upload asset for variation {variation_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/variations/<int:variation_id>/asset', methods=['GET'])
def serve_badge_variation_asset(variation_id):
    """Serve the PNG for a badge variation — user upload first, system default fallback."""
    try:
        bm = _get_badge_manager()
        asset_path = bm.get_display_asset_for_variation(variation_id)
        if not asset_path:
            return jsonify({'success': False, 'error': 'No asset found'}), 404
        p = Path(asset_path)
        if not p.exists():
            return jsonify({'success': False, 'error': 'Asset file missing'}), 404
        mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
        return send_file(str(p), mimetype=mime)
    except Exception as e:
        logger.error(f"Failed to serve asset for variation {variation_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/variations/<int:variation_id>/asset', methods=['DELETE'])
def delete_badge_variation_asset(variation_id):
    """Remove the asset file for a variation (keeps the variation slot)."""
    try:
        bm = _get_badge_manager()
        ok = bm.delete_variation_asset(variation_id)
        return jsonify({'success': ok})
    except Exception as e:
        logger.error(f"Failed to delete asset for variation {variation_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/variations/<int:variation_id>', methods=['DELETE'])
def delete_badge_variation(variation_id):
    """Delete a custom variation (default slots cannot be deleted)."""
    try:
        bm = _get_badge_manager()
        ok = bm.delete_variation(variation_id)
        if ok:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Cannot delete: variation not found or is a default slot'}), 400
    except Exception as e:
        logger.error(f"Failed to delete variation {variation_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/stats', methods=['GET'])
def get_badge_stats():
    """Get badge library statistics."""
    try:
        bm = _get_badge_manager()
        stats = bm.get_stats()
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.error(f"Failed to get badge stats: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/badges/types/<slug>/sample_asset', methods=['GET'])
def get_badge_type_sample_asset(slug):
    """Return a representative sample PNG for a badge type for layout builder canvas preview.

    Optional query params for preview-data-driven selection:
      codec      — audio codec string (e.g. 'TrueHD Atmos', 'DTS-MA')
      channels   — audio channels string (e.g. '7.1', '5.1')
      resolution — resolution string (e.g. '2160p', '1080p')
      hdr        — HDR label (e.g. 'DV', 'HDR10+', 'HDR10', 'HDR', '' for none)
    When these are supplied the endpoint tries to return the PNG that best matches
    the requested media values before falling back to the default preferred keys.
    """
    bm = _get_badge_manager()

    # ── Optional: preview-data-driven lookup ─────────────────────────────
    pd_codec      = request.args.get('codec', '').strip()
    pd_channels   = request.args.get('channels', '').strip()
    pd_resolution = request.args.get('resolution', '').strip()
    pd_hdr        = request.args.get('hdr', '').strip()

    if pd_codec or pd_resolution or pd_hdr:
        # Build a fake media_item dict in the same shape as get_variation_asset_for_media expects
        # HDR: map the short preview-data label to ms_hdr / ms_dolby_vision flags
        hdr_lower = pd_hdr.lower()
        ms_dolby_vision = 1 if 'dv' in hdr_lower or 'dolby vision' in hdr_lower else 0
        ms_hdr = 1 if ('hdr' in hdr_lower or ms_dolby_vision) else 0

        # channels: preview UI shows '7.1', '5.1' etc — convert to int channel count
        ch_map = {'7.1': 8, '5.1': 6, '2.0': 2, '1.0': 1, 'mono': 1}
        ms_channels = ch_map.get(pd_channels, None)
        if ms_channels is None:
            try:
                ms_channels = int(float(pd_channels))
            except (ValueError, TypeError):
                ms_channels = None

        media_item = {
            'ms_audio_codec':    pd_codec,
            'ms_audio_channels': ms_channels,
            'ms_resolution':     pd_resolution,
            'ms_hdr':            ms_hdr,
            'ms_dolby_vision':   ms_dolby_vision,
        }
        asset_path = bm.get_variation_asset_for_media(slug, media_item)
        if asset_path:
            return send_file(asset_path, mimetype='image/png')
        # Fall through to default preferred-key logic if no match

    # ── Default: preferred-key order ─────────────────────────────────────
    preferred = _BADGE_SAMPLE_PREFERRED_KEYS.get(slug, [])

    # Tier 1: user uploads — preferred-key order, then any with an asset
    variations = bm.get_variations(slug)
    user_by_key = {v['variation_key']: v for v in variations if v.get('has_asset')}
    for key in preferred:
        if key in user_by_key:
            return send_file(user_by_key[key]['asset_path'])
    for v in variations:
        if v.get('has_asset'):
            return send_file(v['asset_path'])

    # Tier 2: system defaults
    system_dir = Path(bm.system_asset_dir) / slug
    if system_dir.exists():
        for key in preferred:
            p = system_dir / f"{key}.png"
            if p.exists():
                return send_file(str(p), mimetype='image/png')
        pngs = sorted(system_dir.glob('*.png'))
        if pngs:
            return send_file(str(pngs[0]), mimetype='image/png')
    abort(404)


@overlay_bp.route('/api/overlays/activity', methods=['GET'])
def get_overlay_activity():
    """Return recent overlay activity log rows (newest first).

    Query params:
        limit  — max rows to return (default 50, max 200)
        offset — pagination offset (default 0)
        type   — filter by action_type (optional)
    """
    try:
        limit  = min(int(request.args.get('limit', 50)), 200)
        offset = max(int(request.args.get('offset', 0)), 0)
        action_filter = request.args.get('type', '').strip()

        conn = _get_db_connection()
        cursor = conn.cursor()

        if action_filter:
            cursor.execute(
                '''SELECT id, action_type, triggered_by, result, title, stats_json, duration_seconds, created_at
                   FROM overlay_activity
                   WHERE action_type = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?''',
                (action_filter, limit, offset)
            )
        else:
            cursor.execute(
                '''SELECT id, action_type, triggered_by, result, title, stats_json, duration_seconds, created_at
                   FROM overlay_activity
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?''',
                (limit, offset)
            )
        rows = cursor.fetchall()

        cursor.execute('SELECT COUNT(*) FROM overlay_activity' +
                       (' WHERE action_type = ?' if action_filter else ''),
                       (action_filter,) if action_filter else ())
        total = cursor.fetchone()[0]
        conn.close()

        import json as _json
        activities = []
        for row in rows:
            stats = None
            if row['stats_json']:
                try:
                    stats = _json.loads(row['stats_json'])
                except Exception:
                    stats = None
            activities.append({
                'id':               row['id'],
                'action_type':      row['action_type'],
                'triggered_by':     row['triggered_by'],
                'result':           row['result'],
                'title':            row['title'],
                'stats':            stats,
                'duration_seconds': row['duration_seconds'],
                'created_at':       row['created_at'],
            })

        return jsonify({'success': True, 'activities': activities, 'total': total})
    except Exception as e:
        logger.error(f"Failed to get overlay activity: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@overlay_bp.route('/api/overlays/preview/posters', methods=['GET'])
def get_preview_posters():
    """Return random poster URLs from poster cache for layout builder preview cycling.

    Cache keys are formatted as "{tmdb_id}_{type}" (e.g. "12345_movie", "67890_tv").
    Values are (url, cached_at) tuples where url is a full TMDB CDN URL.
    Meta/trending entries are skipped automatically.

    When textless=1 is passed, attempts to return the null-language (textless) TMDB
    poster for each candidate instead of the cached English one.
    Also returns tmdb_id and media_type per entry so the builder can fetch clearlogos.
    """
    import random as _random
    from datetime import datetime, timedelta
    from routes.poster_cache import load_cache

    media_type = request.args.get('type', 'both')
    limit = min(int(request.args.get('limit', 20)), 50)
    textless = request.args.get('textless', '0') == '1'

    type_filters = set()
    if media_type in ('movie', 'both'):
        type_filters.add('movie')
    if media_type in ('tv', 'both'):
        type_filters.add('tv')

    cache = load_cache()
    expiry = timedelta(days=7)
    now = datetime.now()
    candidates = []

    for cache_key, value in cache.items():
        try:
            url, cached_at = value
        except (TypeError, ValueError):
            continue

        # Skip non-URL entries (meta dicts, trending response, etc.)
        if not isinstance(url, str) or not url.startswith('http'):
            continue
        if now - cached_at > expiry:
            continue

        # Match type suffix: key ends with "_movie" or "_tv"
        for t in type_filters:
            if cache_key.endswith(f'_{t}'):
                # Extract tmdb_id from key (format: "{tmdb_id}_{type}")
                tmdb_id = cache_key[: -(len(t) + 1)]
                candidates.append({
                    'poster_url': url,
                    'type': t,
                    'tmdb_id': tmdb_id,
                })
                break

    _random.shuffle(candidates)
    selected = candidates[:limit]

    # For textless mode, swap poster URLs to null-language TMDB posters
    if textless:
        from utilities.settings import get_setting as _gs
        _tmdb_key = _gs('TMDB', 'api_key', default='')
        import requests as _req
        for entry in selected:
            try:
                _mtype = 'tv' if entry['type'] == 'tv' else 'movie'
                _r = _req.get(
                    f"https://api.themoviedb.org/3/{_mtype}/{entry['tmdb_id']}/images"
                    f"?api_key={_tmdb_key}&include_image_language=null",
                    timeout=8
                )
                if _r.status_code == 200:
                    _posters = _r.json().get('posters', [])
                    if _posters:
                        # Prefer voted posters — filters out obscure foreign uploads (vote_count=0)
                        _voted = [p for p in _posters if p.get('vote_count', 0) >= 3]
                        _cands = _voted if _voted else _posters
                        _cands.sort(
                            key=lambda p: p.get('vote_count', 0) * p.get('vote_average', 0),
                            reverse=True)
                        entry['poster_url'] = (
                            f"https://image.tmdb.org/t/p/w300{_cands[0]['file_path']}")
            except Exception:
                pass  # keep original cached URL on error

    return jsonify({'posters': selected})


@overlay_bp.route('/api/overlays/preview/clearlogo', methods=['GET'])
def get_preview_clearlogo():
    """Return the best English clearlogo URL for a given TMDB ID.

    Query params: tmdb_id, type (movie|tv)
    Returns: { logo_url: str|null }
    """
    tmdb_id   = request.args.get('tmdb_id', '').strip()
    item_type = request.args.get('type', 'movie')
    if not tmdb_id:
        return jsonify({'logo_url': None})
    try:
        from utilities.settings import get_setting as _gs
        import requests as _req
        _api_key = _gs('TMDB', 'api_key', default='')
        if not _api_key:
            return jsonify({'logo_url': None})
        _mtype = 'tv' if item_type == 'tv' else 'movie'
        logo_url = None
        for _lang in ('en', 'en,null'):
            _r = _req.get(
                f"https://api.themoviedb.org/3/{_mtype}/{tmdb_id}/images"
                f"?api_key={_api_key}&include_image_language={_lang}",
                timeout=10
            )
            if _r.status_code == 200:
                _logos = _r.json().get('logos', [])
                _png = [l for l in _logos if (l.get('file_path') or '').endswith('.png')]
                _best = sorted(_png or _logos, key=lambda l: l.get('vote_average', 0), reverse=True)
                if _best:
                    logo_url = f"https://image.tmdb.org/t/p/original{_best[0]['file_path']}"
                    break
        return jsonify({'logo_url': logo_url})
    except Exception as e:
        logger.warning(f"Preview clearlogo fetch failed for {tmdb_id}: {e}")
        return jsonify({'logo_url': None})


# ── Logo Library ───────────────────────────────────────────────────────────────

def _scan_logo_dir(base: Path, is_user: bool) -> dict:
    """Scan a logo directory and return groups keyed by 'category' or 'category/subcat'."""
    groups = {}
    if not base.exists():
        return groups
    for cat_dir in sorted(base.iterdir()):
        if not cat_dir.is_dir():
            continue
        cat_name = cat_dir.name
        # Flat PNGs directly inside category dir
        flat_pngs = sorted(cat_dir.glob('*.png'), key=lambda p: p.stem.lower())
        if flat_pngs:
            key = cat_name
            if key not in groups:
                groups[key] = {'category': cat_name, 'subcat': None, 'logos': []}
            for p in flat_pngs:
                rel = f"{cat_name}/{p.name}"
                groups[key]['logos'].append({
                    'name': p.stem, 'filename': p.name,
                    'path': f"/logos/{rel}",
                    'url': f"/api/overlays/logos/serve/{rel}",
                    'is_user': is_user,
                })
        # Sub-category dirs
        for sub_dir in sorted(cat_dir.iterdir()):
            if not sub_dir.is_dir():
                continue
            sub_pngs = sorted(sub_dir.glob('*.png'), key=lambda p: p.stem.lower())
            if sub_pngs:
                key = f"{cat_name}/{sub_dir.name}"
                if key not in groups:
                    groups[key] = {'category': cat_name, 'subcat': sub_dir.name, 'logos': []}
                for p in sub_pngs:
                    rel = f"{cat_name}/{sub_dir.name}/{p.name}"
                    groups[key]['logos'].append({
                        'name': p.stem, 'filename': p.name,
                        'path': f"/logos/{rel}",
                        'url': f"/api/overlays/logos/serve/{rel}",
                        'is_user': is_user,
                    })
    return groups


@overlay_bp.route('/api/overlays/logos', methods=['GET'])
def list_logos():
    """List available logos grouped by category/subcategory.
    Optional ?category=rating|network|studio to filter."""
    category_filter = request.args.get('category')

    sys_groups  = _scan_logo_dir(_SYSTEM_LOGO_DIR, False)
    user_groups = _scan_logo_dir(_USER_LOGO_DIR,   True)

    # Merge: user logos added on top of system; same key → merge logo lists
    merged = dict(sys_groups)
    for key, ug in user_groups.items():
        if key not in merged:
            merged[key] = ug
        else:
            user_paths = {l['path'] for l in ug['logos']}
            combined = ug['logos'] + [l for l in merged[key]['logos'] if l['path'] not in user_paths]
            combined.sort(key=lambda l: l['name'].lower())
            merged[key] = dict(merged[key], logos=combined)

    # Build category tree
    categories: dict = {}
    for key, grp in merged.items():
        cat = grp['category']
        if category_filter and cat != category_filter:
            continue
        if cat not in categories:
            categories[cat] = {'name': cat, 'groups': []}
        categories[cat]['groups'].append({
            'key': key,
            'label': grp['subcat'] if grp['subcat'] else cat,
            'logos': grp['logos'],
        })

    return jsonify({
        'categories': [{'name': n, 'groups': v['groups']}
                       for n, v in sorted(categories.items())],
    })


@overlay_bp.route('/api/overlays/logos/serve/<path:filepath>', methods=['GET'])
def serve_logo(filepath):
    """Serve a logo file — user dir first, system fallback."""
    # Strip leading 'logos/' prefix if present (saved paths sometimes include it)
    if filepath.startswith('logos/'):
        filepath = filepath[len('logos/'):]
    parts = Path(filepath).parts
    if '..' in parts:
        abort(400)
    user_path = _USER_LOGO_DIR / filepath
    if user_path.exists():
        return send_file(str(user_path))
    system_path = _SYSTEM_LOGO_DIR / filepath
    if system_path.exists():
        return send_file(str(system_path))
    abort(404)


@overlay_bp.route('/api/overlays/logos/upload', methods=['POST'])
def upload_logo():
    """Upload a logo to the user logo directory."""
    f = request.files.get('file')
    category   = request.form.get('category', 'custom')
    subcategory = request.form.get('subcategory', '').strip()

    if not f or not f.filename:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    allowed = {'.png', '.jpg', '.jpeg', '.webp'}
    suffix = Path(f.filename).suffix.lower()
    if suffix not in allowed:
        return jsonify({'success': False, 'error': 'Only PNG/JPG/WebP accepted'}), 400

    safe_cat  = re.sub(r'[^\w\-]', '_', category)
    safe_sub  = re.sub(r'[^\w\-]', '_', subcategory) if subcategory else ''
    safe_name = re.sub(r'[^\w\-. ]', '_', Path(f.filename).name)

    dest_dir = _USER_LOGO_DIR / safe_cat / safe_sub if safe_sub else _USER_LOGO_DIR / safe_cat
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    f.save(str(dest))

    rel = str(dest.relative_to(_USER_LOGO_DIR))
    return jsonify({
        'success': True,
        'path': f"/logos/{rel}",
        'url': f"/api/overlays/logos/serve/{rel}",
    })


@overlay_bp.route('/api/overlays/logos/delete', methods=['POST'])
def delete_logo():
    """Delete a user-uploaded logo (cannot delete system logos)."""
    raw = (request.json or {}).get('path', '')
    if not raw:
        return jsonify({'success': False, 'error': 'No path provided'}), 400

    # Strip /logos/ prefix to get relative path
    rel = raw.lstrip('/')
    if rel.startswith('logos/'):
        rel = rel[len('logos/'):]

    parts = Path(rel).parts
    if '..' in parts or not parts:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400

    user_path = _USER_LOGO_DIR / rel
    try:
        resolved = user_path.resolve()
        base_resolved = _USER_LOGO_DIR.resolve()
        if not str(resolved).startswith(str(base_resolved)):
            return jsonify({'success': False, 'error': 'Path outside user logo directory'}), 400
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400

    if user_path.exists():
        user_path.unlink()
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'File not found or not a user logo'}), 404


@overlay_bp.route('/api/overlays/fonts/<font_name>', methods=['GET'])
def serve_font(font_name):
    """Serve a font file for use in the layout builder canvas.

    Allowed font names must be alphanumeric + dash/underscore with .ttf or .otf extension.
    Searches user asset dir first, then system fonts.
    """
    import re as _re
    if not _re.match(r'^[\w\-]+\.(ttf|otf)$', font_name, _re.IGNORECASE):
        abort(400)

    stem = Path(font_name).stem

    search_paths = [
        _USER_LOGO_DIR.parent / 'fonts' / font_name,
        Path('/user/config/overlay_assets/fonts') / font_name,
        Path(__file__).parent.parent / 'overlays' / 'fonts' / 'cache' / font_name,
        Path('/usr/share/fonts/TTF') / font_name,
        Path('/usr/share/fonts/truetype/dejavu') / font_name,
        Path(f'/usr/share/fonts/truetype/{stem.lower()}') / font_name,
    ]
    for p in search_paths:
        if p.exists():
            mime = 'font/otf' if font_name.lower().endswith('.otf') else 'font/ttf'
            return send_file(str(p), mimetype=mime)
    abort(404)
