"""Manual assignment: prepare, show, rematch, fill gaps."""

from flask import request, render_template, flash, redirect, url_for, jsonify
from debrid import get_debrid_provider
from ..models import admin_required
import logging
import os
from ._common import _assignment_store, _assignment_store_lock, _order_change_blocked, _requested_episode_order, _save_episode_order, _saved_episode_order, magnet_bp
from .assignment_build import _AssignmentError, _build_assignment, _episode_list, _store_assignment, _video_files

@magnet_bp.route('/prepare_manual_assignment', methods=['POST'])
@admin_required
def prepare_manual_assignment():
    """Prepare the data for the manual file assignment screen."""
    # Get form data (same as original 'assign' action)
    tmdb_id = request.form.get('tmdb_id')
    media_type = request.form.get('media_type')
    magnet_link = request.form.get('magnet_link', '').strip()
    title = request.form.get('title')
    year = request.form.get('year')
    version = request.form.get('version')
    selection_type = request.form.get('selection_type')
    selected_seasons = request.form.get('selected_seasons', '').split(',') if request.form.get('selected_seasons') else []
    season = request.form.get('season')
    episode = request.form.get('episode')
    requested_order = _requested_episode_order(request.form.get('episode_order')) if media_type in ['tv', 'show'] else None
    
    # Check for torrent file upload
    torrent_file = request.files.get('torrent_file')

    # Basic validation
    if not all([tmdb_id, media_type, title, year, version]):
        # Return JSON error for fetch request
        return jsonify({'success': False, 'error': 'Missing required information'}), 400
    
    # Validate that either magnet link or torrent file is provided, but not both
    if not magnet_link and not torrent_file:
        return jsonify({'success': False, 'error': 'Please provide either a magnet link or a torrent file'}), 400

    if magnet_link and torrent_file:
        return jsonify({'success': False, 'error': 'Please provide either a magnet link OR a torrent file, not both'}), 400

    # Detect NZB by file extension (.nzb) or HTTP URL (anything not magnet: and not .torrent)
    is_nzb_file = torrent_file and torrent_file.filename.lower().endswith('.nzb')
    is_nzb_url = (magnet_link and not magnet_link.startswith('magnet:')
                  and magnet_link.startswith('http')
                  and not magnet_link.lower().endswith('.torrent'))

    if is_nzb_url or is_nzb_file:
        from routes.scraper_routes import _add_nzb_to_usenet
        if requested_order and tmdb_id:
            # The season/episode were picked in this order's numbering: save it
            # on the show first (there's no file-matching step for NZBs).
            from metadata.metadata import get_imdb_id_if_missing
            order_imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': 'show'})
            if order_imdb_id and requested_order != _saved_episode_order(order_imdb_id):
                blocked = _order_change_blocked(order_imdb_id, requested_order)
                if blocked:
                    return jsonify({'success': False, 'error': blocked}), 409
                _save_episode_order(order_imdb_id, tmdb_id, requested_order)
        try:
            season_num = int(season) if season and season.lower() != 'null' else None
        except (ValueError, TypeError):
            season_num = None
        try:
            episode_num = int(episode) if episode and episode.lower() != 'null' else None
        except (ValueError, TypeError):
            episode_num = None

        # Seasons / whole show / an episode list: one library item per episode,
        # all on this NZB. Without this a pack became a single item with no
        # season or episode, and only the one episode it resolved to got
        # assigned (The Traitors (IN) S02, 2026-11-25).
        episode_list = None
        if media_type in ['tv', 'show'] and selection_type in ('seasons', 'all', 'episode_list'):
            if selection_type == 'episode_list':
                episode_list = _episode_list(selected_seasons)
            else:
                try:
                    from metadata.metadata import get_metadata
                    seasons_meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type).get('seasons') or {}
                except Exception as _me:
                    logging.error(f"[NZB] Could not load seasons for manual assign: {_me}")
                    seasons_meta = {}
                wanted = None if selection_type == 'all' else {int(x) for x in selected_seasons if str(x).strip().isdigit()}
                episode_list = []
                for s_key in sorted(seasons_meta, key=lambda k: int(k)):
                    if wanted is not None and int(s_key) not in wanted:
                        continue
                    eps = (seasons_meta[s_key] or {}).get('episodes') or {}
                    episode_list += [(int(s_key), int(e)) for e in sorted(eps, key=lambda k: int(k))]
            if not episode_list:
                return jsonify({'success': False, 'error': 'No episodes found for the selected seasons'}), 400
            season_num = episode_num = None

        try:
            nzb_content = None
            nzb_title = None
            if is_nzb_file:
                nzb_content = torrent_file.read().decode('utf-8', errors='replace')
                nzb_title = torrent_file.filename
            _nzb_resp = _add_nzb_to_usenet(
                nzb_url=None if is_nzb_file else magnet_link,
                title=title, year=year, media_type=media_type,
                season=season_num, episode=episode_num,
                version=version, tmdb_id=tmdb_id,
                original_scraped_torrent_title=nzb_title,
                nzb_content=nzb_content, episode_list=episode_list,
            )
            # Inject redirect_url into the response so the assign page can navigate
            try:
                _nzb_data = _nzb_resp.get_json()
                if _nzb_data and _nzb_data.get('success'):
                    _nzb_data['redirect_url'] = f'/library/show/{tmdb_id}' if tmdb_id else '/library'
                    return jsonify(_nzb_data)
            except Exception:
                pass
            return _nzb_resp
        except Exception as _ne:
            logging.error(f"NZB handling failed: {_ne}", exc_info=True)
            return jsonify({'success': False, 'error': str(_ne)}), 500

    try:
        # Handle torrent file upload if provided
        temp_file_path = None
        actual_magnet_link = magnet_link

        if torrent_file:
            # Save uploaded torrent file to temporary location
            import tempfile
            temp_fd, temp_file_path = tempfile.mkstemp(suffix='.torrent')
            os.close(temp_fd)
            torrent_file.save(temp_file_path)
            logging.info(f"Saved uploaded torrent file to: {temp_file_path}")
            actual_magnet_link = None  # Use None for magnet link when using torrent file
        
        # Get file list, filename, and torrent_id from debrid provider
        debrid_provider = get_debrid_provider()
        
        try:
            if actual_magnet_link:
                result = debrid_provider.get_torrent_file_list(actual_magnet_link)
                if result is None:
                    error_msg = 'Failed to retrieve file list from debrid service. Torrent might be invalid or provider error.'
                    logging.error(f"get_torrent_file_list returned None for magnet: {actual_magnet_link[:60]}")
                    return jsonify({'success': False, 'error': error_msg}), 503
                
                files, torrent_filename, torrent_id = result
            else:
                # For torrent files, we need to add it first, then get file list
                torrent_id = debrid_provider.add_torrent(None, temp_file_path)
                if not torrent_id:
                    error_msg = 'Failed to add torrent file to debrid service.'
                    logging.error(f"add_torrent returned None for torrent file: {temp_file_path}")
                    return jsonify({'success': False, 'error': error_msg}), 503
                
                # Wait a moment for RD to process the torrent before getting info
                import time
                time.sleep(3)
                
                # Get torrent info which contains the file list
                torrent_info = debrid_provider.get_torrent_info(torrent_id)
                if not torrent_info:
                    error_msg = 'Failed to get torrent info from debrid service.'
                    logging.error(f"get_torrent_info returned None for torrent ID: {torrent_id}")
                    return jsonify({'success': False, 'error': error_msg}), 503
                
                files = torrent_info.get('files', [])
                torrent_filename = torrent_info.get('filename', 'Unknown Filename')
                
                # Ensure files is a list
                if isinstance(files, dict):
                    files = list(files.values())
                elif not isinstance(files, list):
                    files = []
            
            logging.info(f"Retrieved {len(files)} files for manual assignment. Torrent ID: {torrent_id}, Filename: {torrent_filename}")
            
        finally:
            # Clean up temporary file if it was created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    logging.info(f"Cleaned up temporary torrent file: {temp_file_path}")
                except Exception as cleanup_error:
                    logging.warning(f"Failed to clean up temporary file {temp_file_path}: {cleanup_error}")

        video_files = _video_files(files)

        if not video_files:
            error_msg = 'No video files found in the magnet link.'
            logging.warning(f"No video files found for magnet: {magnet_link[:60]}...")
            # Return JSON error
            return jsonify({'success': False, 'error': error_msg}), 400

        logging.info(f"Filtered down to {len(video_files)} video files.")

        context = {
            'tmdb_id': tmdb_id, 'media_type': media_type, 'title': title, 'year': year,
            'version': version, 'selection_type': selection_type, 'selected_seasons': selected_seasons,
            'season': season, 'episode': episode, 'torrent_id': torrent_id,
            'magnet_link': actual_magnet_link or '', 'torrent_filename': torrent_filename,
            'is_torrent_file': torrent_file is not None, 'video_files': video_files,
        }
        try:
            target_items, episode_order = _build_assignment(
                video_files, tmdb_id, media_type, title, year, version, torrent_id, actual_magnet_link or '',
                selection_type, selected_seasons, season, episode, requested_order)
        except _AssignmentError as e:
            return jsonify({'success': False, 'error': str(e)}), e.status
        imdb_id = next((i.get('imdb_id') for i in target_items if i.get('imdb_id')), None)
        token = _store_assignment(target_items, episode_order, requested_order, imdb_id, video_files, context)
        return jsonify({'success': True, 'redirect_url': url_for('magnet.show_manual_assignment', token=token)})

    except Exception as e:
        # Catch-all for unexpected errors
        error_msg = f"An unexpected error occurred while preparing assignment: {str(e)}"
        logging.error(error_msg, exc_info=True)
        # Return JSON error
        return jsonify({'success': False, 'error': 'An internal server error occurred. Please check logs.'}), 500

@magnet_bp.route('/show_manual_assignment', methods=['GET'])
@admin_required
def show_manual_assignment():
    """Display the manual assignment page using data stored in the server-side store."""
    token = request.args.get('token')
    assignment_data = None
    if token:
        with _assignment_store_lock:
            assignment_data = _assignment_store.pop(token, None)

    if not assignment_data:
        flash('No assignment data found. Please start the process again.', 'warning')
        return redirect(url_for('magnet.assign_magnet'))

    return render_template('manual_assignment.html', page_data=_assignment_page_data(assignment_data),
                           **assignment_data)

def _assignment_page_data(assignment_data):
    """What the assignment page's script needs, without the full item dicts."""
    items = [{
        'key': item.get('item_key'),
        'type': item.get('type'),
        'title': item.get('title'),
        'year': item.get('year'),
        'season': item.get('season_number'),
        'episode': item.get('episode_number'),
        'episode_title': item.get('episode_title'),
        'absolute': item.get('absolute_number'),
        'in_library': bool(item.get('in_library')),
        'file': item.get('suggested_file_path') or '',
        'reason': item.get('match_reason') or '',
        'score': item.get('match_score'),
    } for item in assignment_data.get('target_items') or []]
    files = [{k: f.get(k) for k in ('filename', 'path', 'bytes', 'label', 'seasons', 'episodes',
                                    'absolutes', 'skip_reason')}
             for f in assignment_data.get('video_files') or []]
    # The script sends `files` back as the context's video_files on a rematch
    context = {k: v for k, v in (assignment_data.get('rematch_context') or {}).items() if k != 'video_files'}
    return {
        'items': items,
        'files': files,
        'tmdb_id': context.get('tmdb_id'),
        'is_show': context.get('media_type') in ['tv', 'show'] and any(i['type'] == 'episode' for i in items),
        'current_order': assignment_data.get('current_episode_order') or 'default',
        'context': context,
    }

@magnet_bp.route('/rematch_manual_assignment', methods=['POST'])
@admin_required
def rematch_manual_assignment():
    """Rebuild the assignment screen in another episode order, reusing the
    file list the screen was built from (nothing is re-added to the debrid)."""
    data = request.get_json(silent=True) or {}
    context = data.get('context') or {}
    requested_order = _requested_episode_order(data.get('episode_order'))
    if not requested_order or context.get('media_type') not in ['tv', 'show']:
        return jsonify({'success': False, 'error': 'Pick an episode order for a show.'}), 400
    required = ('tmdb_id', 'title', 'year', 'version', 'torrent_id', 'torrent_filename', 'video_files')
    if not all(context.get(k) for k in required) or not isinstance(context['video_files'], list):
        return jsonify({'success': False, 'error': 'The assignment data is incomplete; start again from the assign page.'}), 400
    video_files = [{'id': f.get('id'), 'path': f.get('path'), 'filename': f.get('filename'),
                    'bytes': f.get('bytes', 0)}
                   for f in context['video_files'] if isinstance(f, dict) and f.get('path') and f.get('filename')]
    selected_seasons = context.get('selected_seasons') or []
    if not isinstance(selected_seasons, list):
        selected_seasons = []
    context = {
        'tmdb_id': str(context['tmdb_id']), 'media_type': context['media_type'],
        'title': context['title'], 'year': context['year'], 'version': context['version'],
        'selection_type': context.get('selection_type'), 'selected_seasons': [str(s) for s in selected_seasons],
        'season': context.get('season'), 'episode': context.get('episode'),
        'torrent_id': context['torrent_id'], 'magnet_link': context.get('magnet_link') or '',
        'torrent_filename': context['torrent_filename'],
        'is_torrent_file': bool(context.get('is_torrent_file')), 'video_files': video_files,
    }
    try:
        target_items, episode_order = _build_assignment(
            video_files, context['tmdb_id'], context['media_type'], context['title'], context['year'],
            context['version'], context['torrent_id'], context['magnet_link'], context['selection_type'],
            context['selected_seasons'], context['season'], context['episode'], requested_order)
    except _AssignmentError as e:
        return jsonify({'success': False, 'error': str(e)}), e.status
    except Exception as e:
        logging.error(f"Rematching manual assignment in order {requested_order} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'An internal server error occurred. Please check logs.'}), 500
    imdb_id = next((i.get('imdb_id') for i in target_items if i.get('imdb_id')), None)
    token = _store_assignment(target_items, episode_order, requested_order, imdb_id, video_files, context)
    return jsonify({'success': True, 'redirect_url': url_for('magnet.show_manual_assignment', token=token)})

@magnet_bp.route('/fill_gaps', methods=['POST'])
@admin_required
def fill_gaps():
    """An assignment screen for just the episodes another torrent left without a
    file, matched against the files of a second magnet."""
    data = request.get_json(silent=True) or {}
    context = data.get('context') or {}
    magnet_link = (data.get('magnet_link') or '').strip()
    episodes = [f'{s}:{e}' for s, e in _episode_list(data.get('episodes'))]
    if not magnet_link.startswith('magnet:'):
        return jsonify({'success': False, 'error': 'Paste a magnet link (magnet:?xt=...).'}), 400
    if not episodes or context.get('media_type') not in ['tv', 'show']:
        return jsonify({'success': False, 'error': 'There are no episodes left to fill.'}), 400
    if not all(context.get(k) for k in ('tmdb_id', 'title', 'year', 'version')):
        return jsonify({'success': False, 'error': 'The assignment data is incomplete; start again from the assign page.'}), 400
    try:
        result = get_debrid_provider().get_torrent_file_list(magnet_link)
        if result is None:
            return jsonify({'success': False, 'error': 'Failed to retrieve file list from debrid service. Torrent might be invalid or provider error.'}), 503
        files, torrent_filename, torrent_id = result
        video_files = _video_files(files)
        if not video_files:
            return jsonify({'success': False, 'error': 'No video files found in that magnet link.'}), 400
        requested_order = _requested_episode_order(data.get('episode_order'))
        context = {
            'tmdb_id': str(context['tmdb_id']), 'media_type': context['media_type'],
            'title': context['title'], 'year': context['year'], 'version': context['version'],
            'selection_type': 'episode_list', 'selected_seasons': episodes, 'season': None, 'episode': None,
            'torrent_id': torrent_id, 'magnet_link': magnet_link, 'torrent_filename': torrent_filename,
            'is_torrent_file': False, 'video_files': video_files,
        }
        target_items, episode_order = _build_assignment(
            video_files, context['tmdb_id'], context['media_type'], context['title'], context['year'],
            context['version'], torrent_id, magnet_link, 'episode_list', episodes, None, None, requested_order)
    except _AssignmentError as e:
        return jsonify({'success': False, 'error': str(e)}), e.status
    except Exception as e:
        logging.error(f"Preparing a gap-fill assignment failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'An internal server error occurred. Please check logs.'}), 500
    imdb_id = next((i.get('imdb_id') for i in target_items if i.get('imdb_id')), None)
    notice = str(data.get('notice') or '')[:1000]
    token = _store_assignment(target_items, episode_order, requested_order, imdb_id, video_files, context, notice)
    return jsonify({'success': True, 'redirect_url': url_for('magnet.show_manual_assignment', token=token)})
