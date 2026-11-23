"""Plex token check, torrent tracking/verification, Trakt token, Emby scan, cache files."""

from flask import jsonify, render_template, request, redirect, url_for, flash
from utilities.settings import get_all_settings
import logging
from routes import admin_required
from database.torrent_tracking import get_recent_additions, get_torrent_history
import os
import glob
from datetime import datetime
from debrid import get_debrid_provider
from database.symlink_verification import get_unverified_files, get_verification_stats
from ._common import debug_bp

# --- Helper function to get cache files ---
def get_cache_files():
    """Returns a dict with content source cache files and other cache files."""
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    user_config_dir = os.environ.get('USER_CONFIG', '/user/config')

    # ── Content source cache files ────────────────────────────────────────────
    content_source_files = []
    try:
        if os.path.isdir(db_content_dir):
            pattern = os.path.join(db_content_dir, 'content_source_*.pkl')
            cache_files = [os.path.basename(f) for f in glob.glob(pattern)]
            content_sources = get_all_settings().get('Content Sources', {})
            for filename in sorted(cache_files):
                if filename.startswith('content_source_') and filename.endswith('_cache.pkl'):
                    source_id = filename[15:-10]
                    display_label = None
                    for source_key, source_config in content_sources.items():
                        safe_source_key = source_key.replace('/', '_').replace('\\', '_')
                        if source_id == safe_source_key or source_id.startswith(safe_source_key + '_'):
                            if isinstance(source_config, dict):
                                display_name = source_config.get('display_name', source_key)
                                display_label = f"{display_name} ({source_id})"
                                break
                    if not display_label:
                        display_label = f"{source_id} ({source_id})"
                    content_source_files.append((filename, display_label))
    except Exception as e:
        logging.error(f"Error getting content source cache files: {e}", exc_info=True)

    # ── Other cache files ─────────────────────────────────────────────────────
    OTHER_CACHE_FILES = [
        # (filename, base_dir, display_label, description)
        ('plex_collection_state.json', user_config_dir,
         'Plex Collection State',
         'Clears sync state for all Plex collections — forces full re-sync on next trigger'),
        ('plex_smart_collection_state.json', user_config_dir,
         'Plex Smart Collection State',
         'Clears Smart Collection Posters state — forces poster re-apply on next trigger'),
        ('plex_boxsets_state.json', user_config_dir,
         'Plex Box Sets State',
         'Clears Box Sets fingerprints — forces poster re-apply for all box sets on next trigger'),
        ('trakt_lists_cache.pkl', db_content_dir,
         'Trakt Lists Cache',
         'Cached Trakt list data — forces re-fetch from Trakt API'),
        ('trakt_imdb_id_cache.pkl', db_content_dir,
         'Trakt IMDb ID Cache',
         'IMDb↔Trakt ID mappings — re-fetches on next Trakt API call'),
        ('trakt_watchlist_cache.pkl', db_content_dir,
         'Trakt Watchlist Cache',
         'Cached Trakt watchlist — forces re-sync with Trakt'),
        ('adaptive_list_imdb_cache.pkl', db_content_dir,
         'Adaptive List IMDb Cache',
         'Cached IMDb data for Adaptive Lists — forces re-fetch'),
        ('poster_cache.pkl', db_content_dir,
         'Poster Cache',
         'Cached TMDB poster URLs — forces fresh API calls for artwork'),
        ('failed_upgrades.pkl', db_content_dir,
         'Failed Upgrades History',
         'Failed upgrade attempt history — allows retry of previously failed items'),
    ]

    other_files = []
    for filename, base_dir, label, desc in OTHER_CACHE_FILES:
        exists = os.path.exists(os.path.join(base_dir, filename))
        other_files.append({
            'filename': filename,
            'base_dir': base_dir,
            'label': label,
            'description': desc,
            'exists': exists,
        })

    return {'content_source': content_source_files, 'other': other_files}

@debug_bp.route('/validate_plex_tokens', methods=['GET', 'POST'])
@admin_required
def validate_plex_tokens_route():
    """Route to validate and refresh Plex tokens"""
    from content_checkers.plex_watchlist import validate_plex_tokens
    from content_checkers.plex_token_manager import get_token_status
    
    try:
        if request.method == 'POST':
            # For POST requests, perform a fresh validation
            token_status = validate_plex_tokens()
        else:
            # For GET requests, return the stored status
            token_status = get_token_status()
            if not token_status:
                # If no stored status exists, perform a fresh validation
                token_status = validate_plex_tokens()
        
        # Ensure all datetime objects are serialized
        for username, status in token_status.items():
            if isinstance(status.get('expires_at'), datetime):
                status['expires_at'] = status['expires_at'].isoformat()
            if isinstance(status.get('last_checked'), datetime):
                status['last_checked'] = status['last_checked'].isoformat()
        
        return jsonify({
            'success': True,
            'token_status': token_status
        })
    except Exception as e:
        logging.error(f"Error in validate_plex_tokens route: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@debug_bp.route('/torrent_tracking')
@admin_required
def torrent_tracking():
    """View the torrent tracking history."""
    try:
        # Get the most recent 1000 entries
        entries = get_recent_additions(1000)
        
        # Convert the entries to a list of dictionaries for easier template handling
        formatted_entries = []
        if entries:  # Check if entries exist
            for entry in entries:
                formatted_entry = {
                    'id': entry[0],
                    'torrent_hash': entry[1],
                    'timestamp': entry[2],
                    'trigger_source': entry[3],
                    'trigger_details': entry[4],
                    'rationale': entry[5],
                    'item_data': entry[6],
                    'is_still_present': bool(entry[7]),
                    'removal_reason': entry[8],
                    'removal_timestamp': entry[9],
                    'additional_metadata': entry[10]
                }
                formatted_entries.append(formatted_entry)
        
        # Always render the template, even with empty entries
        return render_template('torrent_tracking.html', entries=formatted_entries)
    except Exception as e:
        logging.error(f"Error in torrent tracking view: {e}")
        flash(f"Error loading torrent tracking data: {str(e)}", 'error')
        return redirect(url_for('debug.debug_functions'))

@debug_bp.route('/verify_torrent/<hash_value>')
@admin_required
def verify_torrent(hash_value):
    """Verify if a torrent is still present in Real-Debrid and get its status"""
    try:
        debrid_provider = get_debrid_provider()
        
        # Get all active torrents from Real-Debrid
        logging.info("Fetching list of active torrents from Real-Debrid")
        try:
            active_torrents = debrid_provider.list_active_torrents()
            logging.info(f"Found {len(active_torrents)} active torrents")
        except Exception as e:
            if "429" in str(e):
                logging.warning("Rate limit hit while fetching active torrents")
                return jsonify({
                    'error': 'Rate limit exceeded. Please try again in a few seconds.',
                    'is_present': None,
                    'status': 'rate_limited'
                }), 429
            else:
                logging.error(f"Error fetching active torrents: {str(e)}")
                return jsonify({
                    'error': f"Failed to fetch active torrents: {str(e)}",
                    'is_present': None,
                    'status': 'error'
                }), 500
        
        # Find matching torrent
        logging.info(f"Searching for torrent with hash {hash_value}")
        matching_torrent = None
        for torrent in active_torrents:
            if torrent.get('hash', '').lower() == hash_value.lower():
                matching_torrent = torrent
                logging.info(f"Found matching torrent with ID: {torrent.get('id')}")
                break
        
        # Get any removal reason if it exists
        logging.info("Checking torrent history for removal information")
        history = get_torrent_history(hash_value)
        removal_reason = None
        
        if history and not history[0]['is_still_present']:
            removal_reason = history[0]['removal_reason']
            logging.info(f"Found removal reason in history: {removal_reason}")
        
        if matching_torrent:
            # Get detailed torrent info to check status
            logging.info(f"Getting detailed info for torrent ID: {matching_torrent['id']}")
            try:
                torrent_info = debrid_provider.get_torrent_info(matching_torrent['id'])
                if torrent_info:
                    status = torrent_info.get('status', '')
                    logging.info(f"Torrent status: {status}")
                    
                    if status == 'downloaded':
                        logging.info("Torrent is present and downloaded")
                        return jsonify({
                            'is_present': True,
                            'status': status,
                            'removal_reason': None
                        })
                    elif status in ['magnet_error', 'error', 'virus', 'dead']:
                        logging.warning(f"Torrent has error status: {status}")
                        return jsonify({
                            'is_present': False,
                            'status': status,
                            'removal_reason': f"Torrent error: {status}"
                        })
                    else:
                        logging.info(f"Torrent is present with status: {status}")
                        return jsonify({
                            'is_present': True,
                            'status': status,
                            'removal_reason': None
                        })
            except Exception as e:
                if "429" in str(e):
                    logging.warning("Rate limit hit while fetching torrent info")
                    return jsonify({
                        'error': 'Rate limit exceeded. Please try again in a few seconds.',
                        'is_present': None,
                        'status': 'rate_limited'
                    }), 429
                else:
                    logging.error(f"Error getting torrent info: {str(e)}")
                    return jsonify({
                        'error': f"Failed to get torrent info: {str(e)}",
                        'is_present': None,
                        'status': 'error'
                    }), 500
        
        # If we get here, the torrent was not found
        logging.info("Torrent not found in active torrents")
        return jsonify({
            'is_present': False,
            'status': 'not_found',
            'removal_reason': removal_reason
        })
        
    except Exception as e:
        logging.error(f"Error verifying torrent {hash_value}: {str(e)}", exc_info=True)
        return jsonify({
            'error': f"Verification failed: {str(e)}",
            'is_present': None,
            'status': 'error'
        }), 500

@debug_bp.route('/api/trakt_token_status', methods=['GET'])
@admin_required
def get_trakt_token_status():
    try:
        from cli_battery.app import trakt_auth
        from utilities.settings import get_setting

        access_token = get_setting('Trakt', 'access_token', default='')
        expires_at = get_setting('Trakt', 'expires_at', default='')
        last_refresh = get_setting('Trakt', 'last_refresh', default='')
        token_data = {
            'access_token': access_token[:10] + '...' if access_token else None,
            'expires_at': expires_at,
            'last_refresh': last_refresh,
        }

        logging.debug(f"Trakt token status - Token Data: {token_data}")
        logging.debug(f"Trakt token status - Last Refresh: {last_refresh}")
        logging.debug(f"Trakt token status - Expires At: {expires_at}")

        status = {
            'is_authenticated': trakt_auth.is_authenticated(),
            'token_data': token_data,
            'last_refresh': last_refresh,
            'expires_at': expires_at
        }
        
        logging.debug(f"Trakt token status response: {status}")
        
        return jsonify({
            'success': True,
            'status': status
        })
    except Exception as e:
        logging.error(f"Error getting Trakt token status: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@debug_bp.route('/get_verification_queue', methods=['GET'])
@admin_required
def get_verification_queue():
    """Get the contents of the symlink verification queue."""
    try:
        # Get unverified files (limit to 500 to prevent overwhelming the UI)
        unverified_files = get_unverified_files(limit=500)
        
        # Format the data for display
        formatted_files = []
        for file in unverified_files:
            if file['type'] == 'episode':
                s_num, e_num = file['season_number'], file['episode_number']
                se = (f"S{s_num:02d}E{e_num:02d}" if s_num is not None and e_num is not None
                      else f"S{s_num if s_num is not None else '?'}E{e_num if e_num is not None else '?'}")
                title = f"{file['title']} - {se}"
                if file['episode_title']:
                    title += f" - {file['episode_title']}"
            else:
                title = file['title']
                
            formatted_files.append({
                'id': file['verification_id'],
                'title': title,
                'filename': file['filename'],
                'full_path': file['full_path'],
                'media_item_id': file['media_item_id'],
                'added_at': file['added_at'],
                'attempts': file['verification_attempts'],
                'last_attempt': file['last_attempt'],
                'type': file['type']
            })
        
        # Keep the real unverified count; the list itself is capped at 500. Replacing
        # the count with the number of rows shown made a 10,000-file queue read 500.
        stats = get_verification_stats()
        stats['shown'] = len(formatted_files)
        stats.pop('multiple_attempts', None)  # Hide the multiple attempts stat
        
        return jsonify({
            'success': True,
            'stats': stats,
            'files': formatted_files
        })
    except Exception as e:
        logging.error(f"Error getting verification queue: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@debug_bp.route('/api/direct_emby_scan', methods=['POST'])
@admin_required
def direct_emby_scan():
    """Reads the whole library of the symlink media server (Emby/Jellyfin, or
    Silo when it is selected) and adds the items to the database."""
    from utilities.emby_functions import get_collected_from_emby
    from utilities.silo_functions import is_silo_selected, is_silo_mode, get_collected_from_silo
    from database.collected_items import add_collected_items
    server = 'Silo' if is_silo_selected() else 'Emby/Jellyfin'
    logging.info(f"Received request for direct {server} scan and collection.")
    try:
        if server == 'Silo':
            if not is_silo_mode():
                return jsonify({'success': False, 'error': 'Silo URL or API key not set.'}), 400
            collected_data = get_collected_from_silo()
        else:
            collected_data = get_collected_from_emby(bypass=True)  # bypass=True to scan all configured libs

        if collected_data is None:
            logging.error(f"Failed to retrieve data from {server}.")
            return jsonify({'success': False, 'error': f'Failed to retrieve data from {server}.'}), 500

        movies = collected_data.get('movies', [])
        episodes = collected_data.get('episodes', [])
        combined_items = movies + episodes

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from {server}.")

        if not combined_items:
             logging.warning(f"No items collected from {server} scan.")
             return jsonify({'success': True, 'message': f'No items collected from {server} scan.'}), 200

        # 2. Add collected items to the database
        logging.info(f"Adding collected {server} items to the database...")
        add_collected_items(combined_items, recent=True)  # additive only
        logging.info(f"Successfully added {server} items to the database.")

        return jsonify({'success': True, 'message': f'Successfully processed {len(combined_items)} items from {server}.'}), 200

    except Exception as e:
        logging.error(f"Error during direct {server} scan: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'}), 500

# --- New route to delete cache files ---
@debug_bp.route('/api/delete_cache_files', methods=['POST'])
@admin_required
def delete_cache_files_route():
    """API endpoint to delete selected cache files (content source + other cache files)."""
    selected_files = request.form.getlist('selected_files')
    if not selected_files:
        return jsonify({'success': False, 'error': 'No cache files selected'}), 400

    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    user_config_dir = os.environ.get('USER_CONFIG', '/user/config')

    # Allowed other cache files with their base directories
    OTHER_ALLOWED = {
        'plex_collection_state.json': user_config_dir,
        'plex_smart_collection_state.json': user_config_dir,
        'plex_boxsets_state.json': user_config_dir,
        'trakt_lists_cache.pkl': db_content_dir,
        'trakt_imdb_id_cache.pkl': db_content_dir,
        'trakt_watchlist_cache.pkl': db_content_dir,
        'adaptive_list_imdb_cache.pkl': db_content_dir,
        'poster_cache.pkl': db_content_dir,
        'failed_upgrades.pkl': db_content_dir,
    }

    deleted_count = 0
    errors = []

    for filename in selected_files:
        # Determine file path based on type
        if filename.startswith('content_source_') and filename.endswith('_cache.pkl'):
            file_path = os.path.join(db_content_dir, filename)
        elif filename in OTHER_ALLOWED:
            file_path = os.path.join(OTHER_ALLOWED[filename], filename)
        else:
            errors.append(f"Invalid cache filename skipped: {filename}")
            continue

        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted_count += 1
                logging.info(f"Deleted cache file: {file_path}")
            else:
                logging.warning(f"Cache file not found, skipping: {file_path}")
        except OSError as e:
            logging.error(f"Error deleting cache file {file_path}: {e}")
            errors.append(f"Failed to delete {filename}: {e.strerror}")
        except Exception as e:
            logging.error(f"Unexpected error deleting cache file {file_path}: {e}")
            errors.append(f"Failed to delete {filename}: {str(e)}")

    if not errors:
        return jsonify({'success': True, 'message': f'Successfully deleted {deleted_count} cache file(s).'})
    else:
        error_message = f'Deleted {deleted_count} cache file(s). Errors: {"; ".join(errors)}'
        return jsonify({'success': True, 'message': error_message, 'errors': errors})
