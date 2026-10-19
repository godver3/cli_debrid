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
    return jsonify({'success': True, 'series_type': choice, 'is_anime': is_anime, 'episodes_updated': changed})
