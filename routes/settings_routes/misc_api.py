"""Schema, popups/disclaimers, source order, version usage, default MDBLists, virtual folder."""

from flask import jsonify, request
from utilities.settings import load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from queues.config_manager import get_enabled_content_sources
from routes.models import admin_required
import json
import os
from datetime import datetime
from content_checkers.trakt import fetch_liked_trakt_lists_details
from ._common import settings_bp

@settings_bp.route('/api/settings_schema', methods=['GET'])
def api_settings_schema():
    try:
        return jsonify(SETTINGS_SCHEMA)
    except Exception as e:
        return jsonify({"error": "An error occurred while loading settings schema."}), 500

@settings_bp.route('/api/scrapers', methods=['GET'])
@admin_required
def api_get_scrapers():
    """API endpoint to get scrapers configuration as JSON"""
    try:
        config = load_config()
        return jsonify(config.get('Scrapers', {}))
    except Exception as e:
        logging.error(f"Error getting scrapers: {str(e)}", exc_info=True)
        return jsonify({"error": "An error occurred while loading scrapers."}), 500

@settings_bp.route('/api/phalanx-disclaimer-status')
def get_phalanx_disclaimer_status():
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        disclaimer_file = os.path.join(db_content_dir, 'phalanx_disclaimer.json')
        if os.path.exists(disclaimer_file):
            with open(disclaimer_file, 'r') as f:
                status = json.load(f)
                # Only return True if they've actually made a choice (accepted or declined)
                return jsonify({'hasSeenDisclaimer': 'accepted' in status})
        return jsonify({'hasSeenDisclaimer': False})
    except Exception as e:
        logging.error(f"Error checking Phalanx disclaimer status: {str(e)}")
        return jsonify({'hasSeenDisclaimer': False})

@settings_bp.route('/api/phalanx-disclaimer-accept', methods=['POST'])
@admin_required
def accept_phalanx_disclaimer():
    try:
        data = request.json
        accepted = data.get('accepted', False)
        print(f"Accepted: {accepted}")
        
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the disclaimer status
        disclaimer_file = os.path.join(db_content_dir, 'phalanx_disclaimer.json')
        
        # Create the file with proper permissions
        with open(disclaimer_file, 'w', encoding='utf-8') as f:
            json.dump({
                'accepted': accepted,
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        # Update the UI Settings based on user choice
        config = load_config()
        if 'UI Settings' not in config:
            config['UI Settings'] = {}
        config['UI Settings']['enable_phalanx_db'] = accepted
        save_config(config)
        
        logging.info(f"Phalanx disclaimer status saved: accepted={accepted}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving Phalanx disclaimer status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# Support modal status endpoints
@settings_bp.route('/api/support-modal-status')
def get_support_modal_status():
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
                # Return both seen status and page views
                return jsonify({
                    'hasSeenSupport': status.get('seen', False),
                    'pageViews': status.get('pageViews', 0),
                    'neverShowAgain': status.get('neverShowAgain', False)
                })
        return jsonify({
            'hasSeenSupport': False,
            'pageViews': 0,
            'neverShowAgain': False
        })
    except Exception as e:
        logging.error(f"Error checking support modal status: {str(e)}")
        return jsonify({
            'hasSeenSupport': False,
            'pageViews': 0,
            'neverShowAgain': False
        })

@settings_bp.route('/api/support-modal-seen', methods=['POST'])
@admin_required
def mark_support_modal_seen():
    try:
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the status
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        
        # Load existing status to preserve page views
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                existing_status = json.load(f)
                current_page_views = existing_status.get('pageViews', 0)
        else:
            current_page_views = 0
        
        # Create the file with proper permissions
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump({
                'seen': True,
                'pageViews': current_page_views,  # Preserve page views when marked as seen
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        logging.info("Support modal marked as seen")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving support modal status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/support-modal-never-show', methods=['POST'])
@admin_required
def mark_support_modal_never_show():
    try:
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the status
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        
        # Create the file with proper permissions
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump({
                'seen': True,
                'neverShowAgain': True,
                'pageViews': 0,  # Reset page views when marked as never show
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        logging.info("Support modal marked as never show again")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving support modal never show status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/support-modal-pageview', methods=['POST'])
@admin_required
def increment_pageview():
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        
        # Load existing status or create new
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
        else:
            status = {
                'seen': False,
                'pageViews': 0,
                'timestamp': datetime.now().isoformat()
            }
        
        # Only increment if not marked as never show again
        if not status.get('neverShowAgain', False):
            status['pageViews'] = status.get('pageViews', 0) + 1
            
            # Save updated status
            with open(status_file, 'w', encoding='utf-8') as f:
                json.dump(status, f, indent=4)
        
        return jsonify({
            'success': True,
            'pageViews': status['pageViews'],
            'hasSeenSupport': status.get('seen', False),
            'neverShowAgain': status.get('neverShowAgain', False)
        })
    except Exception as e:
        logging.error(f"Error incrementing page views: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/enabled_content_sources', methods=['GET'])
def get_enabled_content_sources_route():
    try:
        enabled_sources = get_enabled_content_sources()
        
        # Manually add content_requester and overseerr_webhook to the list for UI purposes
        enabled_sources.append({
            'id': 'content_requester',
            'type': 'Internal', # Assign a placeholder type
            'display_name': 'Content Requester'
        })

        enabled_sources.append({
            'id': 'overseerr_webhook',
            'type': 'Internal', # Assign a placeholder type
            'display_name': 'Overseerr Webhook'
        })
        
        return jsonify({'success': True, 'sources': enabled_sources})
    except Exception as e:
        logging.error(f"Error getting enabled content sources: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/save_content_source_order', methods=['POST'])
@admin_required
def save_content_source_order():
    try:
        source_order_from_request = request.json.get('order', [])
        if not isinstance(source_order_from_request, list):
             return jsonify({'success': False, 'error': 'Invalid order format provided'}), 400

        # Filter out 'content_requester' as it's not a real content source to be saved
        filtered_source_order = [source_id for source_id in source_order_from_request if source_id not in ('content_requester', 'content_requestor')]
        
        if not filtered_source_order:
            logging.info("Content source priority order is empty after filtering.")
            # Decide if you want to save an empty string or handle differently
            # Saving empty string for now:
            order_string = ""
        else:
            # Join the filtered list into a comma-separated string
            order_string = ','.join(filtered_source_order)
        
        # Save to config
        config = load_config()
        if 'Queue' not in config:
            config['Queue'] = {}
        config['Queue']['content_source_priority'] = order_string
        save_config(config)
        
        logging.info(f"Saved filtered content source priority: {order_string}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving content source order: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/check_usage/<version_id>', methods=['GET'])
def check_version_usage(version_id):
    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if any media items use this version
        cursor.execute("SELECT COUNT(*) FROM media_items WHERE version = ?", (version_id,))
        count = cursor.fetchone()[0]
        conn.close()

        in_use = count > 0
        is_last = False
        alternatives = []

        if in_use:
            # If in use, check if it's the last version and get alternatives
            config = load_config()
            all_versions = list(config.get('Scraping', {}).get('versions', {}).keys())
            alternatives = [v for v in all_versions if v != version_id]
            is_last = len(all_versions) <= 1

        return jsonify({
            'success': True,
            'in_use': in_use,
            'is_last': is_last,
            'alternatives': alternatives
        })

    except Exception as e:
        logging.error(f"Error checking version usage for {version_id}: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/trakt/import_liked_lists', methods=['POST'])
@admin_required
def get_liked_trakt_lists_for_import():
    """Fetches liked lists details from Trakt to be used by the frontend."""
    try:
        liked_lists = fetch_liked_trakt_lists_details()
        if not liked_lists:
            # Return success but indicate no lists found
            return jsonify({'success': True, 'lists': []}) 

        # Just return the fetched list details
        return jsonify({'success': True, 'lists': liked_lists})

    except Exception as e:
        logging.error(f"Error fetching liked Trakt lists details: {str(e)}", exc_info=True)
        # Return failure if there was an error during fetch
        return jsonify({'success': False, 'error': f'Failed to fetch liked lists details: {str(e)}'}), 500

@settings_bp.route('/api/mdblist-popup-status')
def get_mdblist_popup_status():
    try:
        # Check if we're in limited environment mode
        from utilities.set_supervisor_env import is_limited_environment
        limited_env = is_limited_environment()
        
        if not limited_env:
            return jsonify({'shouldShow': False})
        
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        status_file = os.path.join(db_content_dir, 'mdblist_popup.json')
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
                return jsonify({'shouldShow': not status.get('seen', False)})
        return jsonify({'shouldShow': True})
    except Exception as e:
        logging.error(f"Error checking MDBList popup status: {str(e)}")
        return jsonify({'shouldShow': False})

@settings_bp.route('/api/mdblist-popup-seen', methods=['POST'])
@admin_required
def mark_mdblist_popup_seen():
    try:
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the status
        status_file = os.path.join(db_content_dir, 'mdblist_popup.json')
        
        # Create the file with proper permissions
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump({
                'seen': True,
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        logging.info("MDBList popup marked as seen")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving MDBList popup status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/add-default-mdblists', methods=['POST'])
@admin_required
def add_default_mdblists():
    try:
        config = load_config()
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Check if MDBList sources already exist
        existing_mdblists = [source_id for source_id in config['Content Sources'].keys() 
                           if source_id.startswith('MDBList_')]
        
        if existing_mdblists:
            return jsonify({'success': False, 'error': 'MDBList sources already exist'}), 400
        
        # Get available versions from config
        available_versions = list(config.get('Scraping', {}).get('versions', {}).keys())
        if not available_versions:
            # If no versions configured, create a default 1080p version
            if 'Scraping' not in config:
                config['Scraping'] = {}
            if 'versions' not in config['Scraping']:
                config['Scraping']['versions'] = {}
            
            config['Scraping']['versions']['1080p'] = {
                'enable_hdr': False,
                'max_resolution': '1080p',
                'resolution_wanted': '<=',
                'resolution_weight': 3,
                'hdr_weight': 3,
                'similarity_weight': 3,
                'size_weight': 3,
                'bitrate_weight': 3,
                'min_size_gb': 0.01,
                'max_size_gb': '',
                'min_bitrate_mbps': 0.01,
                'max_bitrate_mbps': '',
                'similarity_threshold': 0.85,
                'similarity_threshold_anime': 0.80,
                'year_match_weight': 3,
                'foreign_language_penalty': 30,
                'audio_description_penalty': 10000,
                'wake_count': None,
                'fallback_version': 'None',
                'anime_filter_mode': 'None',
                'preferred_filter_in': [],
                'preferred_filter_out': [],
                'filter_in': [],
                'filter_out': [],
                'require_physical_release': False,
                'language_code': 'en'
            }
            available_versions = ['1080p']
            logging.info("Created default 1080p version for MDBLists")
        
        # Use the first available version (or 1080p as fallback)
        default_version = available_versions[0] if available_versions else '1080p'
        
        # Default MDBList configurations
        default_mdblists = {
            "MDBList_1": {
                "enabled": True,
                "source_mode": "json_url",
                "urls": "https://mdblist.com/lists/hdlists/top-ten-pirated-movies-of-the-week-torrent-freak-com",
                "versions": {
                    default_version: True
                },
                "type": "MDBList",
                "media_type": "All",
                "display_name": "New Movies",
                "allow_specials": False,
                "custom_symlink_subfolder": "",
                "cutoff_date": "",
                "exclude_genres": []
            },
            "MDBList_2": {
                "enabled": True,
                "source_mode": "json_url",
                "urls": "https://mdblist.com/lists/godver3/top-10-shows",
                "versions": {
                    default_version: True
                },
                "type": "MDBList",
                "media_type": "All",
                "display_name": "New Shows",
                "allow_specials": False,
                "custom_symlink_subfolder": "",
                "cutoff_date": "",
                "exclude_genres": []
            }
        }
        
        # Add the default MDBLists to the config
        for source_id, source_config in default_mdblists.items():
            config['Content Sources'][source_id] = source_config
        
        save_config(config)
        
        logging.info(f"Default MDBList sources added successfully using version: {default_version}")
        return jsonify({'success': True, 'message': f'Default MDBList sources added successfully using version: {default_version}'})
    except Exception as e:
        logging.error(f"Error adding default MDBLists: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/create_virtual_folder', methods=['POST'])
@admin_required
def create_virtual_folder():
    """Create cli_mount virtual folder(s) from tags in config.json."""
    try:
        import json as _json
        import os as _os

        data = request.json or {}
        folder_name = (data.get('folder_name') or '').strip()
        tags = data.get('tags', [])  # list of tag strings
        mode = data.get('mode', 'per_tag')  # 'per_tag' or 'combined'

        if not folder_name and mode == 'combined':
            return jsonify({'success': False, 'error': 'Folder name required'}), 400
        if not tags:
            return jsonify({'success': False, 'error': 'At least one tag required'}), 400

        # Get cli_mount data_path
        config = load_config()
        data_path = (config.get('Usenet Provider', {}).get('data_path') or '').strip()
        if not data_path:
            return jsonify({'success': False, 'error': 'cli_mount data path not configured in Usenet Provider settings'}), 400

        dc_config_path = _os.path.join(data_path, 'config.json')
        if not _os.path.exists(dc_config_path):
            return jsonify({'success': False, 'error': f'cli_mount config.json not found at {dc_config_path}'}), 400

        with open(dc_config_path, 'r') as f:
            dc_config = _json.load(f)

        if 'custom_folders' not in dc_config:
            dc_config['custom_folders'] = {}

        created = []
        skipped = []

        if mode == 'per_tag':
            # One folder per tag, folder name = tag name
            for tag in tags:
                tag = tag.strip()
                if not tag:
                    continue
                fname = tag
                if fname in dc_config['custom_folders']:
                    skipped.append(fname)
                    continue
                dc_config['custom_folders'][fname] = {
                    'filters': {
                        'tags': tag
                    }
                }
                created.append(fname)
        else:
            # One combined folder for all selected tags
            if folder_name in dc_config['custom_folders']:
                return jsonify({'success': False, 'error': f'Folder "{folder_name}" already exists'}), 400
            tag_value = ','.join(tag.strip() for tag in tags if tag.strip())
            dc_config['custom_folders'][folder_name] = {
                'filters': {
                    'tags': tag_value
                }
            }
            created.append(folder_name)

        with open(dc_config_path, 'w') as f:
            _json.dump(dc_config, f, indent=2)

        msg = f'Created: {", ".join(created)}'
        if skipped:
            msg += f'. Skipped (already exist): {", ".join(skipped)}'
        return jsonify({'success': True, 'message': msg, 'created': created, 'skipped': skipped})

    except Exception as e:
        logging.error(f'create_virtual_folder error: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
