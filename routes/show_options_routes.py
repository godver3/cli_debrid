"""Per-show options on the library show page: series type and episode order.

See database/show_overrides.py for what is stored, utilities/anime_detection.py
for how the series type is used.
"""
import logging

from flask import Blueprint, jsonify, request

from .models import admin_required, user_required

show_options_bp = Blueprint('show_options', __name__)


def _show_metadata(imdb_id):
    from cli_battery.app.direct_api import DirectAPI
    metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    return metadata or {}


def _auto_is_anime(imdb_id, metadata):
    from utilities.anime_detection import is_anime
    tmdb_id = (metadata.get('ids') or {}).get('tmdb')
    return is_anime(metadata.get('genres') or [], imdb_id=imdb_id, tmdb_id=tmdb_id,
                    media_type='tv', use_override=False)


@show_options_bp.route('/<imdb_id>', methods=['GET'])
@user_required
def get_show_options(imdb_id):
    from database.show_overrides import get_show_override
    override = get_show_override(imdb_id)
    try:
        metadata = _show_metadata(imdb_id)
        auto_anime = _auto_is_anime(imdb_id, metadata)
    except Exception as e:
        logging.warning(f"[ShowOptions] Could not run anime detection for {imdb_id}: {e}")
        auto_anime = None
    series_type = override.get('series_type')
    return jsonify({
        'success': True,
        'imdb_id': imdb_id,
        'series_type': series_type or 'auto',
        'auto_is_anime': auto_anime,
        'is_anime': (series_type == 'anime') if series_type else auto_anime,
        'episode_order': override.get('episode_order'),
    })


@show_options_bp.route('/<imdb_id>/series_type', methods=['POST'])
@admin_required
def set_series_type(imdb_id):
    """Force a show to anime / standard, or back to automatic detection.

    The show's existing episode rows are re-tagged at once, since everything
    downstream reads is-anime from each row's genres.
    """
    from database.show_overrides import apply_series_type_to_episodes, set_show_override
    data = request.get_json(silent=True) or {}
    choice = data.get('series_type')
    if choice not in ('auto', 'anime', 'standard'):
        return jsonify({'success': False, 'error': "series_type must be 'auto', 'anime' or 'standard'"}), 400

    metadata = {}
    try:
        metadata = _show_metadata(imdb_id)
    except Exception as e:
        logging.warning(f"[ShowOptions] No metadata for {imdb_id}: {e}")
    tmdb_id = (metadata.get('ids') or {}).get('tmdb')

    if not set_show_override(imdb_id, tmdb_id=tmdb_id, series_type=None if choice == 'auto' else choice):
        return jsonify({'success': False, 'error': 'Could not save the setting'}), 500

    is_anime = choice == 'anime' if choice != 'auto' else bool(_auto_is_anime(imdb_id, metadata))
    changed = apply_series_type_to_episodes(imdb_id, is_anime)
    symlink_note = _move_symlinks_for_series_type(imdb_id) if changed else None
    return jsonify({'success': True, 'series_type': choice, 'is_anime': is_anime, 'episodes_updated': changed,
                    'symlink_note': symlink_note})


def _move_symlinks_for_series_type(imdb_id):
    """With separate anime folders, re-file the show's collected episodes so the
    whole show follows its new series type instead of only episodes added later."""
    from utilities.settings import get_setting
    if (get_setting('File Management', 'file_collection_management') != 'Symlinked/Local'
            or not get_setting('Debug', 'enable_separate_anime_folders', False)):
        return None
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        item_ids = [r['id'] for r in conn.execute(
            "SELECT id FROM media_items WHERE imdb_id = ? AND type = 'episode' "
            "AND state IN ('Collected', 'Upgrading')", (imdb_id,))]
    finally:
        conn.close()
    if not item_ids:
        return None
    from utilities.local_library_scan import resync_symlinks_with_new_settings
    try:
        result = resync_symlinks_with_new_settings(item_ids=item_ids)
    except Exception as e:
        logging.error(f"[ShowOptions] Moving symlinks for {imdb_id} failed: {e}", exc_info=True)
        return f"Could not move the show's symlinks: {e}"
    if not isinstance(result, dict):
        return None
    return (f"Symlinks: {result.get('symlinks_updated_count', 0)} moved, "
            f"{result.get('symlinks_created_count', 0)} created, {result.get('error_count', 0)} errors")


@show_options_bp.route('/<imdb_id>/episode_orders', methods=['GET'])
@user_required
def list_orders(imdb_id):
    """The episode orders this show can use (TVDB season types, TMDB episode groups)."""
    from cli_battery.app.episode_order import list_episode_orders, show_episode_order
    try:
        metadata = _show_metadata(imdb_id)
        tmdb_id = (metadata.get('ids') or {}).get('tmdb')
        orders = list_episode_orders(imdb_id, tmdb_id)
    except Exception as e:
        logging.error(f"[ShowOptions] Listing episode orders for {imdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Could not list episode orders'}), 500
    return jsonify({'success': True, 'current': show_episode_order(imdb_id) or 'default', 'orders': orders})


def _order_from_request():
    data = request.get_json(silent=True) or {}
    order = (data.get('order') or '').strip()
    if not order or not (order == 'default' or order.startswith('tvdb:') or order.startswith('tmdb_group:')):
        return None, data
    return order, data


@show_options_bp.route('/<imdb_id>/episode_order/preview', methods=['POST'])
@admin_required
def preview_episode_order(imdb_id):
    """What switching to `order` would do to the show's library rows. Changes nothing."""
    from utilities.episode_order_change import preview_order_change
    order, _ = _order_from_request()
    if not order:
        return jsonify({'success': False, 'error': 'An episode order is required'}), 400
    try:
        return jsonify(preview_order_change(imdb_id, order))
    except Exception as e:
        logging.error(f"[ShowOptions] Episode order preview for {imdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@show_options_bp.route('/<imdb_id>/episode_order/apply', methods=['POST'])
@admin_required
def apply_episode_order(imdb_id):
    """Switch the show to `order`: move its rows, rebuild its metadata, rename
    symlinks and (optionally) set Plex's ordering."""
    from utilities.episode_order_change import apply_order_change
    order, data = _order_from_request()
    if not order:
        return jsonify({'success': False, 'error': 'An episode order is required'}), 400
    try:
        result = apply_order_change(imdb_id, order, sync_plex=bool(data.get('sync_plex', True)),
                                    add_missing=bool(data.get('add_missing', False)))
    except Exception as e:
        logging.error(f"[ShowOptions] Applying episode order for {imdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify(result), (200 if result.get('success') else 409)
