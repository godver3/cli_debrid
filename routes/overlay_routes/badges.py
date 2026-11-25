"""Badge types, variations and assets."""

from flask import jsonify, request, send_file, render_template, abort
from pathlib import Path
from database.core import get_db_connection as _get_db_connection
from routes.models import admin_required
from ._common import _BADGE_SAMPLE_PREFERRED_KEYS, logger, overlay_bp, overlay_page_bp

# ============================================
# Badge Library Routes
# ============================================

def _get_badge_manager():
    """Get a BadgeManager instance."""
    from overlays.badge_manager import BadgeManager
    return BadgeManager(None)

@overlay_page_bp.route('/overlays/badges')
@admin_required
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
