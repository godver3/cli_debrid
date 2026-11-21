"""Manual blacklist page and per-show season list."""

from flask import jsonify, render_template, request
from database.manual_blacklist import add_to_manual_blacklist, remove_from_manual_blacklist, get_manual_blacklist, save_manual_blacklist
from database.core import add_db_notification
import logging
from routes import admin_required
from cli_battery.app.direct_api import DirectAPI
import json
from ._common import debug_bp

@debug_bp.route('/manual_blacklist', methods=['GET', 'POST'])
@admin_required
def manual_blacklist():
    from metadata.metadata import get_tmdb_id_and_media_type # Import the function to determine media type

    if request.method == 'POST':
        action = request.form.get('action')
        imdb_id = request.form.get('imdb_id')

        if not imdb_id:
            return jsonify({'success': False, 'error': 'IMDb ID is required'}), 400

        blacklist = get_manual_blacklist()
        direct_api = DirectAPI()

        if action == 'add':
            try:
                logging.info(f"Attempting to add IMDb ID '{imdb_id}' to manual blacklist.")
                # 1. Determine media type — use caller-supplied type if present,
                #    otherwise check our own DB, then fall back to TMDB API.
                passed_type = request.form.get('media_type')  # 'movie' or 'episode'
                if passed_type in ('movie', 'episode'):
                    actual_media_type = 'tv' if passed_type == 'episode' else 'movie'
                    tmdb_id = None
                else:
                    from database.core import get_db_connection as _get_db
                    _conn = _get_db()
                    _row = _conn.execute(
                        "SELECT type FROM media_items WHERE imdb_id=? LIMIT 1", (imdb_id,)
                    ).fetchone()
                    _conn.close()
                    if _row:
                        actual_media_type = 'tv' if _row[0] == 'episode' else 'movie'
                        tmdb_id = None
                    else:
                        tmdb_id, actual_media_type = get_tmdb_id_and_media_type(imdb_id)

                if not actual_media_type:
                    return jsonify({'success': False, 'error': f'Could not determine media type for IMDb ID {imdb_id}. Cannot add to blacklist.'}), 400

                # 2. Fetch metadata based on the determined type
                metadata = None
                if actual_media_type == 'tv':
                    metadata_tuple = direct_api.get_show_metadata(imdb_id)
                    if metadata_tuple: metadata = metadata_tuple[0]
                elif actual_media_type == 'movie':
                    metadata_tuple = direct_api.get_movie_metadata(imdb_id)
                    if metadata_tuple: metadata = metadata_tuple[0]

                # Ensure metadata is a dictionary if found
                if metadata and isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = None

                if not metadata or not isinstance(metadata, dict):
                    return jsonify({'success': False, 'error': f'Unable to fetch metadata for IMDb ID {imdb_id} (Type: {actual_media_type}). Cannot add to blacklist.'}), 400

                # 3. Determine the media type to store in the blacklist file
                media_type_to_store = 'episode' if actual_media_type == 'tv' else 'movie'

                # 4. Add to blacklist with the intended (potentially 'episode') media type
                logging.info(f"Calling add_to_manual_blacklist with: imdb_id='{imdb_id}', media_type='{media_type_to_store}', title='{metadata.get('title', 'Unknown Title')}', year='{str(metadata.get('year', ''))}'")
                add_to_manual_blacklist(
                    imdb_id=imdb_id,
                    media_type=media_type_to_store,
                    title=metadata.get('title', 'Unknown Title'),
                    year=str(metadata.get('year', '')),
                )
                msg = f'Successfully added {metadata.get("title", "Item")} ({actual_media_type}) to blacklist as type "{media_type_to_store}"'
                add_db_notification('Blacklist', msg, 'info')
                return jsonify({'success': True, 'message': msg})

            except Exception as e:
                logging.error(f"Error adding to blacklist: {str(e)}", exc_info=True)
                return jsonify({'success': False, 'error': f'Error adding to blacklist: {str(e)}'}), 500

        elif action == 'update_seasons':
            try:
                if imdb_id in blacklist:
                    item = blacklist[imdb_id]
                    # REVERT: Check against 'episode' type here
                    if item['media_type'] == 'episode':
                        all_seasons = request.form.get('all_seasons') == 'on'

                        if all_seasons:
                            item['seasons'] = []
                        else:
                            selected_seasons = request.form.getlist('seasons')
                            item['seasons'] = sorted([int(s) for s in selected_seasons if s.isdigit()])

                        save_manual_blacklist(blacklist)
                        return jsonify({'success': True, 'message': 'Successfully updated seasons'})
                    else:
                        # This branch should technically not be hit for TV shows if 'add' stores them as 'episode'
                        return jsonify({'success': False, 'error': 'Only items stored as type "episode" can have seasons updated'}), 400
                else:
                    return jsonify({'success': False, 'error': 'Item not found in blacklist'}), 404
            except Exception as e:
                logging.error(f"Error updating seasons via AJAX: {str(e)}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        elif action == 'remove':
            try:
                remove_from_manual_blacklist(imdb_id)
                add_db_notification('Blacklist', f'Removed {imdb_id} from blacklist', 'info')
                return jsonify({'success': True, 'message': 'Successfully removed from blacklist'})
            except Exception as e:
                logging.error(f"Error removing from blacklist: {str(e)}", exc_info=True)
                return jsonify({'success': False, 'error': f'Error removing from blacklist: {str(e)}'}), 500

        return jsonify({'success': False, 'error': 'Unknown action'}), 400

    # --- GET Request Logic ---
    blacklist = get_manual_blacklist()

    def get_sort_key(item):
        try:
            title = item[1].get('title', '')
            if not isinstance(title, str):
                title = str(title) if title is not None else ''
            return title.lower()
        except Exception:
            return ''
    sorted_blacklist = dict(sorted(blacklist.items(), key=get_sort_key))
    # Seasons are no longer pre-fetched here — loaded lazily via /api/manual_blacklist/<id>/seasons
    return render_template('manual_blacklist.html', blacklist=sorted_blacklist)

@debug_bp.route('/api/manual_blacklist/<imdb_id>/seasons', methods=['GET'])
@admin_required
def manual_blacklist_seasons(imdb_id):
    """Lazy-load available seasons for a blacklisted show."""
    direct_api = DirectAPI()
    try:
        seasons_data, _ = direct_api.get_show_seasons(imdb_id)
        if seasons_data:
            if isinstance(seasons_data, str):
                seasons_data = json.loads(seasons_data)
            if isinstance(seasons_data, dict) and all(str(k).isdigit() for k in seasons_data.keys()):
                available_seasons = sorted([int(s) for s in seasons_data.keys()])
                season_episodes = {int(s): d.get('episode_count', 0) for s, d in seasons_data.items()}
            else:
                available_seasons = sorted([int(s['season_number']) for s in seasons_data.get('seasons', [])
                                            if str(s.get('season_number')).isdigit()])
                season_episodes = {}
            return jsonify({'success': True, 'seasons': available_seasons, 'season_episodes': season_episodes})
        return jsonify({'success': True, 'seasons': [], 'season_episodes': {}})
    except Exception as e:
        logging.error(f"manual_blacklist_seasons error for {imdb_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
