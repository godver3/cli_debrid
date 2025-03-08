from flask import Blueprint, jsonify, request, render_template, Response, current_app
from settings import load_config, validate_url
from settings_schema import SETTINGS_SCHEMA
import logging
from config_manager import add_scraper, clean_notifications, get_content_source_settings, update_content_source, get_version_settings, add_content_source, delete_content_source, save_config
from routes.models import admin_required, onboarding_required
from .utils import is_user_system_enabled
import traceback
import json
import os
import platform
from datetime import datetime
from notifications import (
    send_telegram_notification, 
    send_discord_notification, 
    send_ntfy_notification, 
    send_email_notification
)
import re
import time

settings_bp = Blueprint('settings', __name__)

@settings_bp.route('/content-sources/content')
def content_sources_content():
    config = load_config()
    source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    return render_template('settings_tabs/content_sources.html', 
                           settings=config, 
                           source_types=source_types, 
                           settings_schema=SETTINGS_SCHEMA)

@settings_bp.route('/content-sources/types')
def content_sources_types():
    config = load_config()
    source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    return jsonify({
        'source_types': source_types,
        'settings': SETTINGS_SCHEMA['Content Sources']['schema']
    })

@settings_bp.route('/content-sources/trakt-friends')
def get_trakt_friends():
    """Get a list of authorized Trakt friends for the dropdown"""
    try:
        friends = []
        trakt_friends_dir = os.environ.get('USER_CONFIG', '/user/config')
        trakt_friends_dir = os.path.join(trakt_friends_dir, 'trakt_friends')
        
        # List all files in the trakt_friends_dir
        if os.path.exists(trakt_friends_dir):
            for filename in os.listdir(trakt_friends_dir):
                if filename.endswith('.json'):
                    try:
                        # Extract auth_id from filename
                        auth_id = filename.replace('.json', '')
                        
                        with open(os.path.join(trakt_friends_dir, filename), 'r') as f:
                            state = json.load(f)
                        
                        # Only include authorized accounts
                        if state.get('status') == 'authorized':
                            friends.append({
                                'auth_id': auth_id,
                                'friend_name': state.get('friend_name', 'Unknown Friend'),
                                'username': state.get('username', ''),
                                'display_name': f"{state.get('friend_name', 'Unknown Friend')}'s Watchlist"
                            })
                    except Exception as e:
                        logging.error(f"Error reading friend state file {filename}: {str(e)}")
        
        return jsonify({
            'success': True,
            'friends': friends
        })
    
    except Exception as e:
        logging.error(f"Error listing Trakt friends: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/content_sources/add', methods=['POST'])
def add_content_source_route():
    try:
        if request.is_json:
            source_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        source_type = source_config.pop('type', None)
        if not source_type:
            return jsonify({'success': False, 'error': 'No source type provided'}), 400
        
        # Ensure versions is a list
        if 'versions' in source_config:
            if isinstance(source_config['versions'], bool):
                source_config['versions'] = []
            elif isinstance(source_config['versions'], str):
                source_config['versions'] = [source_config['versions']]
        
        new_source_id = add_content_source(source_type, source_config)
        
        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        logging.error(f"Error adding content source: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    
@settings_bp.route('/content_sources/delete', methods=['POST'])
def delete_content_source_route():
    source_id = request.json.get('source_id')
    if not source_id:
        return jsonify({'success': False, 'error': 'No source ID provided'}), 400

    logging.info(f"Attempting to delete content source: {source_id}")
    
    success = delete_content_source(source_id)
    
    if success:
        # Update the config in web_server.py
        config = load_config()
        if 'Content Sources' in config and source_id in config['Content Sources']:
            del config['Content Sources'][source_id]
            save_config(config)
        
        logging.info(f"Content source {source_id} deleted successfully")
        return jsonify({'success': True})
    else:
        logging.warning(f"Failed to delete content source: {source_id}")
        return jsonify({'success': False, 'error': 'Source not found or already deleted'}), 404

@settings_bp.route('/scrapers/add', methods=['POST'])
def add_scraper_route():
    logging.info(f"Received request to add scraper. Content-Type: {request.content_type}")
    logging.info(f"Request data: {request.data}")
    try:
        if request.is_json:
            scraper_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        logging.info(f"Parsed data: {scraper_config}")
        
        if not scraper_config:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        scraper_type = scraper_config.pop('type', None)
        if not scraper_type:
            return jsonify({'success': False, 'error': 'No scraper type provided'}), 400
        
        new_scraper_id = add_scraper(scraper_type, scraper_config)
        
        # Log the updated config after adding the scraper
        updated_config = load_config()
        logging.info(f"Updated config after adding scraper: {updated_config}")
        
        return jsonify({'success': True, 'scraper_id': new_scraper_id})
    except Exception as e:
        logging.error(f"Error adding scraper: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    
@settings_bp.route('/scrapers/content')
def scrapers_content():
    try:
        settings = load_config()
        scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
        scraper_settings = {scraper: list(SETTINGS_SCHEMA["Scrapers"]["schema"][scraper].keys()) for scraper in SETTINGS_SCHEMA["Scrapers"]["schema"]}
        return render_template('settings_tabs/scrapers.html', settings=settings, scraper_types=scraper_types, scraper_settings=scraper_settings)
    except Exception as e:
        return jsonify({'error': 'An error occurred while loading scraper settings'}), 500

@settings_bp.route('/scrapers/get', methods=['GET'])
def get_scrapers():
    config = load_config()
    scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
    return render_template('settings_tabs/scrapers.html', settings=config, scraper_types=scraper_types)

@settings_bp.route('/get_content_source_types', methods=['GET'])
def get_content_source_types():
    content_sources = SETTINGS_SCHEMA['Content Sources']['schema']
    return jsonify({
        'source_types': list(content_sources.keys()),
        'settings': content_sources
    })

@settings_bp.route('/scrapers/delete', methods=['POST'])
def delete_scraper():
    data = request.json
    scraper_id = data.get('scraper_id')
    
    if not scraper_id:
        return jsonify({'success': False, 'error': 'No scraper ID provided'}), 400

    config = load_config()
    scrapers = config.get('Scrapers', {})
    
    if scraper_id in scrapers:
        del scrapers[scraper_id]
        config['Scrapers'] = scrapers
        save_config(config)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Scraper not found'}), 404
    
@settings_bp.route('/notifications/delete', methods=['POST'])
def delete_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' in config and notification_id in config['Notifications']:
            del config['Notifications'][notification_id]
            save_config(config)
            logging.info(f"Notification {notification_id} deleted successfully")
            return jsonify({'success': True})
        else:
            logging.warning(f"Failed to delete notification: {notification_id}")
            return jsonify({'success': False, 'error': 'Notification not found'}), 404
    except Exception as e:
        logging.error(f"Error deleting notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/add', methods=['POST'])
def add_notification():
    try:
        notification_data = request.json
        if not notification_data or 'type' not in notification_data:
            return jsonify({'success': False, 'error': 'Invalid notification data'}), 400

        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        notification_type = notification_data['type']
        existing_count = sum(1 for key in config['Notifications'] if key.startswith(f"{notification_type}_"))
        notification_id = f"{notification_type}_{existing_count + 1}"

        notification_title = notification_type.replace('_', ' ').title()

        config['Notifications'][notification_id] = {
            'type': notification_type,
            'enabled': True,
            'title': notification_title,
            'notify_on': {
                'collected': True,
                'wanted': False,
                'scraping': False,
                'adding': False,
                'checking': False,
                'sleeping': False,
                'unreleased': False,
                'blacklisted': False,
                'pending_uncached': False,
                'upgrading': False,
                'program_stop': True,
                'program_crash': True,
                'program_start': True,
                'program_pause': True,
                'program_resume': True,
                'queue_pause': True,
                'queue_resume': True,
                'queue_start': True,
                'queue_stop': True
            }
        }

        # Add default values based on the notification type
        if notification_type == 'Telegram':
            config['Notifications'][notification_id].update({
                'bot_token': '',
                'chat_id': ''
            })
        elif notification_type == 'Discord':
            config['Notifications'][notification_id].update({
                'webhook_url': ''
            })
        elif notification_type == 'NTFY':
            config['Notifications'][notification_id].update({
                'host': '',
                'topic': '',
                'api_key': '',
                'priority': ''
            })
        elif notification_type == 'Email':
            config['Notifications'][notification_id].update({
                'smtp_server': '',
                'smtp_port': 587,
                'smtp_username': '',
                'smtp_password': '',
                'from_address': '',
                'to_address': ''
            })

        save_config(config)

        logging.info(f"Notification {notification_id} added successfully")
        return jsonify({'success': True, 'notification_id': notification_id})
    except Exception as e:
        logging.error(f"Error adding notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def ensure_notification_defaults(notification_config):
    """Ensure notification config has all required default fields."""
    default_categories = {
        'collected': True,
        'wanted': False,
        'scraping': False,
        'adding': False,
        'checking': False,
        'sleeping': False,
        'unreleased': False,
        'blacklisted': False,
        'pending_uncached': False,
        'upgrading': False,
        'program_stop': True,
        'program_crash': True,
        'program_start': True,
        'program_pause': True,
        'program_resume': True,
        'queue_pause': True,
        'queue_resume': True,
        'queue_start': True,
        'queue_stop': True
    }

    # If notify_on is missing or empty, set it to the default values
    if 'notify_on' not in notification_config or not notification_config['notify_on']:
        notification_config['notify_on'] = default_categories.copy()
    else:
        # Ensure all categories exist in notify_on
        for category, default_value in default_categories.items():
            if category not in notification_config['notify_on']:
                notification_config['notify_on'][category] = default_value

    return notification_config

@settings_bp.route('/notifications/content', methods=['GET'])
def notifications_content():
    try:
        config = load_config()
        notification_settings = config.get('Notifications', {})
        
        # Ensure all notifications have the required defaults
        for notification_id, notification_config in notification_settings.items():
            if notification_config is not None:
                notification_settings[notification_id] = ensure_notification_defaults(notification_config)
        
        # Always save the config to ensure defaults are persisted
        config['Notifications'] = notification_settings
        save_config(config)
        
        # Sort notifications by type and then by number
        sorted_notifications = sorted(
            notification_settings.items(),
            key=lambda x: (x[1]['type'], int(x[0].split('_')[-1]))
        )
        
        html_content = render_template(
            'settings_tabs/notifications.html',
            notification_settings=dict(sorted_notifications),
            settings_schema=SETTINGS_SCHEMA
        )
        
        return jsonify({
            'status': 'success',
            'html': html_content
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'An error occurred while generating notifications content: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500

@settings_bp.route('/', methods=['GET'])
@admin_required
@onboarding_required
def index():
    try:
        config = load_config()
        config = clean_notifications(config)  # Clean notifications before rendering
        scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
        source_types = list(SETTINGS_SCHEMA["Content Sources"]["schema"].keys())        
        scraper_settings = {scraper: list(SETTINGS_SCHEMA["Scrapers"]["schema"][scraper].keys()) for scraper in SETTINGS_SCHEMA["Scrapers"]["schema"]}

        # Check if platform is Windows
        is_windows = platform.system() == 'Windows'

        # Fetch content source settings
        content_source_settings_response = get_content_source_settings_route()
        if isinstance(content_source_settings_response, Response):
            content_source_settings = content_source_settings_response.get_json()
        else:
            content_source_settings = content_source_settings_response        
            
        # Fetch scraping versions
        scraping_versions_response = get_scraping_versions()
        if isinstance(scraping_versions_response, Response):
            scraping_versions = scraping_versions_response.get_json()['versions']
        else:
            scraping_versions = scraping_versions_response['versions']

        # Ensure 'Scrapers' exists in the config
        if 'Scrapers' not in config:
            config['Scrapers'] = {}
        
        # Only keep the scrapers that are actually configured
        configured_scrapers = {}
        for scraper, scraper_config in config['Scrapers'].items():
            scraper_type = scraper.split('_')[0]  # Assuming format like 'Zilean_1'
            if scraper_type in scraper_settings:
                configured_scrapers[scraper] = scraper_config
        
        config['Scrapers'] = configured_scrapers

        # Ensure 'UI Settings' exists in the config
        if 'UI Settings' not in config:
            config['UI Settings'] = {}

        if 'Sync Deletions' not in config:
            config['Sync Deletions'] = {}
        
        # Ensure 'enable_user_system' exists in 'UI Settings'
        if 'enable_user_system' not in config['UI Settings']:
            config['UI Settings']['enable_user_system'] = True  # Default to True
        
        
        # Ensure 'Content Sources' exists in the config
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Ensure each content source is a dictionary
        for source, source_config in config['Content Sources'].items():
            if not isinstance(source_config, dict):
                config['Content Sources'][source] = {}

        # Initialize notification_settings
        if 'Notifications' not in config:
            config['Notifications'] = {
                'Telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
                'Discord': {'enabled': False, 'webhook_url': ''},
                'NTFY': {'enabled': False, 'host': '', 'topic': '', 'api_key': '', 'priority': ''},
                'Email': {
                    'enabled': False,
                    'smtp_server': '',
                    'smtp_port': 587,
                    'smtp_username': '',
                    'smtp_password': '',
                    'from_address': '',
                    'to_address': ''
                }
            }

        return render_template('settings_base.html', 
                               settings=config, 
                               notification_settings=config['Notifications'],
                               scraper_types=scraper_types, 
                               scraper_settings=scraper_settings,
                               source_types=source_types,
                               content_source_settings=content_source_settings,
                               scraping_versions=scraping_versions,
                               settings_schema=SETTINGS_SCHEMA,
                               is_windows=is_windows)
    except Exception as e:
        current_app.logger.error(f"Error in settings route: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return render_template('error.html', error_message="An error occurred while loading settings."), 500
    
@settings_bp.route('/api/program_settings', methods=['GET'])
@admin_required
def api_program_settings():
    try:
        config = load_config()
        program_settings = {
            'Scrapers': config.get('Scrapers', {}),
            'Content Sources': config.get('Content Sources', {}),
            'Debug': config.get('Debug', {}),
            'Plex': {
                'url': config.get('Plex', {}).get('url', ''),
                'token': config.get('Plex', {}).get('token', '')
            },
            'Metadata Battery': {
                'url': config.get('Metadata Battery', {}).get('url', '')
            },
            'Debrid Provider': {
                'provider': config.get('Debrid Provider', {}).get('provider', ''),
                'api_key': config.get('Debrid Provider', {}).get('api_key', '')
            }
        }
        return jsonify(program_settings)
    except Exception as e:
        return jsonify({"error": "An error occurred while loading program settings."}), 500
    
@settings_bp.route('/scraping/get')
def get_scraping_settings():
    config = load_config()
    scraping_settings = config.get('Scraping', {})
    return jsonify(scraping_settings)

@settings_bp.route('/api/settings', methods=['POST'])
def update_settings():
    try:
        new_settings = request.json
        config = load_config()
        
        logging.info("Received settings update:")
        logging.info(f"File Management: {json.dumps(new_settings.get('File Management', {}), indent=2)}")
        logging.info(f"Plex: {json.dumps(new_settings.get('Plex', {}), indent=2)}")

        # Validate Plex libraries if Plex is selected
        file_management = new_settings.get('File Management', {})
        if file_management.get('file_collection_management') == 'Plex':
            plex_settings = new_settings.get('Plex', {})
            movie_libraries = plex_settings.get('movie_libraries', '').strip()
            show_libraries = plex_settings.get('shows_libraries', '').strip()
            
            logging.info(f"Validating Plex libraries - Movie: '{movie_libraries}', Shows: '{show_libraries}'")
            
            if not movie_libraries or not show_libraries:
                error_msg = "When using Plex as your library management system, you must specify both a movie library and a TV show library."
                logging.error(f"Settings validation failed: {error_msg}")
                return jsonify({
                    "status": "error",
                    "message": error_msg
                }), 400

        def update_nested_dict(current, new):
            for key, value in new.items():
                if isinstance(value, dict):
                    if key not in current or not isinstance(current[key], dict):
                        current[key] = {}
                    if key == 'Content Sources':
                        for source_id, source_config in value.items():
                            if source_id in current[key]:
                                # Don't save config here, just update the dictionary
                                current[key][source_id].update(source_config)
                            else:
                                # Don't save config here, just add to the dictionary
                                current[key][source_id] = source_config
                    else:
                        update_nested_dict(current[key], value)
                else:
                    current[key] = value

        update_nested_dict(config, new_settings)
        
        # Update content source check periods
        if 'Debug' in new_settings and 'content_source_check_period' in new_settings['Debug']:
            config['Debug']['content_source_check_period'] = {
                source: float(period) for source, period in new_settings['Debug']['content_source_check_period'].items()
            }
        
        # Handle Reverse Parser settings
        if 'Reverse Parser' in new_settings:
            reverse_parser = new_settings['Reverse Parser']
            config['Reverse Parser'] = {
                'version_terms': reverse_parser['version_terms'],
                'default_version': reverse_parser['default_version'],
                'version_order': reverse_parser['version_order']
            }

        save_config(config)
        
        # Save config only once at the end
        from debrid import reset_provider
        reset_provider()
        from queue_manager import QueueManager
        QueueManager().reinitialize_queues()
        from run_program import ProgramRunner
        ProgramRunner().reinitialize()

        return jsonify({"status": "success", "message": "Settings updated successfully"})
    except Exception as e:
        logging.error(f"Error updating settings: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@settings_bp.route('/api/reverse_parser_settings', methods=['GET'])
def get_reverse_parser_settings():
    config = load_config()
    reverse_parser_settings = config.get('Reverse Parser', {})
    
    # Get all scraping versions
    all_scraping_versions = set(config.get('Scraping', {}).get('versions', {}).keys())
    
    # Get the current version order, or initialize it if it doesn't exist
    version_order = reverse_parser_settings.get('version_order', [])
    
    # Ensure version_terms exists
    version_terms = reverse_parser_settings.get('version_terms', {})
    
    # Create a new ordered version_terms dictionary
    ordered_version_terms = {}
    
    # First, add versions in the order specified by version_order
    for version in version_order:
        if version in all_scraping_versions:
            ordered_version_terms[version] = version_terms.get(version, [])
            all_scraping_versions.remove(version)
    
    # Then, add any remaining versions that weren't in version_order
    for version in all_scraping_versions:
        ordered_version_terms[version] = version_terms.get(version, [])
    
    # Update version_order to include any new versions
    version_order = list(ordered_version_terms.keys())
    
    # Update the settings
    reverse_parser_settings['version_terms'] = ordered_version_terms
    reverse_parser_settings['version_order'] = version_order
    
    # Ensure default_version is set and valid
    if 'default_version' not in reverse_parser_settings or reverse_parser_settings['default_version'] not in ordered_version_terms:
        reverse_parser_settings['default_version'] = next(iter(ordered_version_terms), None)
    
    return jsonify(reverse_parser_settings)

def update_nested_settings(current, new):
    for key, value in new.items():
        if isinstance(value, dict):
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            if key == 'Content Sources':
                for source_id, source_config in value.items():
                    if source_id in current[key]:
                        update_content_source(source_id, source_config)
                    else:
                        add_content_source(source_config['type'], source_config)
            else:
                update_nested_settings(current[key], value)
        else:
            current[key] = value

@settings_bp.route('/versions/add', methods=['POST'])
def add_version():
    data = request.json
    version_name = data.get('name')
    if not version_name:
        return jsonify({'success': False, 'error': 'No version name provided'}), 400

    config = load_config()
    if 'Scraping' not in config:
        config['Scraping'] = {}
    if 'versions' not in config['Scraping']:
        config['Scraping']['versions'] = {}

    if version_name in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version already exists'}), 400

    # Add the new version with default settings
    config['Scraping']['versions'][version_name] = {
        'enable_hdr': False,
        'max_resolution': '1080p',
        'resolution_wanted': '<=',
        'resolution_weight': 3,
        'hdr_weight': 3,
        'similarity_weight': 3,
        'size_weight': 3,
        'bitrate_weight': 3,
        'preferred_filter_in': [],
        'preferred_filter_out': [],
        'filter_in': [],
        'filter_out': [],
        'min_size_gb': 0.01,
        'max_size_gb': '',
        'wake_count': None,
        'require_physical_release': False  # Add default require_physical_release setting
    }

    save_config(config)
    return jsonify({'success': True, 'version_id': version_name})

@settings_bp.route('/versions/delete', methods=['POST'])
def delete_version():
    data = request.json
    version_id = data.get('version_id')
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    config = load_config()
    if 'Scraping' in config and 'versions' in config['Scraping'] and version_id in config['Scraping']['versions']:
        del config['Scraping']['versions'][version_id]
        save_config(config)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Version not found'}), 404

@settings_bp.route('/versions/import_defaults', methods=['POST'])
def import_default_versions():
    try:
        # Read the default versions from the JSON file
        with open('optional_default_versions.json', 'r') as f:
            default_versions = json.load(f)
        
        if not isinstance(default_versions, dict) or 'versions' not in default_versions:
            return jsonify({'success': False, 'error': 'Invalid default versions format'}), 400
            
        # Load current config
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
            
        # Add each default version with a unique name
        for version_name, version_config in default_versions['versions'].items():
            base_name = version_name
            counter = 1
            new_name = base_name
            
            # Find a unique name for this version
            while new_name in config['Scraping']['versions']:
                new_name = f"{base_name} {counter}"
                counter += 1
                
            config['Scraping']['versions'][new_name] = version_config
        
        # Save the updated config
        save_config(config)
        
        return jsonify({'success': True, 'message': 'Default versions imported successfully'})
    except FileNotFoundError:
        return jsonify({'success': False, 'error': 'Default versions file not found'}), 404
    except json.JSONDecodeError:
        return jsonify({'success': False, 'error': 'Invalid JSON in default versions file'}), 400
    except Exception as e:
        logging.error(f"Error importing default versions: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/rename', methods=['POST'])
def rename_version():
    data = request.json
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    
    if not old_name or not new_name:
        return jsonify({'success': False, 'error': 'Missing old_name or new_name'}), 400

    config = load_config()
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions = config['Scraping']['versions']
        if old_name in versions:
            # Update version name in config
            versions[new_name] = versions.pop(old_name)
            save_config(config)
            
            # Update version name in database
            from database.database_writing import update_version_name
            updated_count = update_version_name(old_name, new_name)
            logging.info(f"Updated {updated_count} media items in database from version '{old_name}' to '{new_name}'")
            
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Version not found'}), 404
    else:
        return jsonify({'success': False, 'error': 'Scraping versions not found in config'}), 404

@settings_bp.route('/versions/duplicate', methods=['POST'])
def duplicate_version():
    data = request.json
    version_id = data.get('version_id')
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    config = load_config()
    if 'Scraping' not in config or 'versions' not in config['Scraping'] or version_id not in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version not found'}), 404

    new_version_id = f"{version_id} Copy"
    counter = 1
    while new_version_id in config['Scraping']['versions']:
        new_version_id = f"{version_id} Copy {counter}"
        counter += 1

    # Create a deep copy of the version settings
    original_settings = config['Scraping']['versions'][version_id]
    new_settings = original_settings.copy()
    
    # Ensure require_physical_release is included in the copy
    if 'require_physical_release' not in new_settings:
        new_settings['require_physical_release'] = False

    config['Scraping']['versions'][new_version_id] = new_settings
    config['Scraping']['versions'][new_version_id]['display_name'] = new_version_id

    save_config(config)
    return jsonify({'success': True, 'new_version_id': new_version_id})

@settings_bp.route('/scraping/content')
def scraping_content():
    config = load_config()
    return render_template('settings_tabs/scraping.html', settings=config, settings_schema=SETTINGS_SCHEMA)

@settings_bp.route('/get_scraping_versions', methods=['GET'])
def get_scraping_versions_route():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@settings_bp.route('/get_content_source_settings', methods=['GET'])
def get_content_source_settings_route():
    try:
        content_source_settings = get_content_source_settings()
        return jsonify(content_source_settings)
    except Exception as e:
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@settings_bp.route('/get_scraping_versions', methods=['GET'])
def get_scraping_versions():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@settings_bp.route('/get_version_settings')
def get_version_settings_route():
    try:
        version = request.args.get('version')
        if not version:
            return jsonify({'error': 'No version provided'}), 400
        
        version_settings = get_version_settings(version)
        if not version_settings:
            return jsonify({'error': f'No settings found for version: {version}'}), 404
        
        # Ensure max_resolution is included in the settings
        if 'max_resolution' not in version_settings:
            version_settings['max_resolution'] = '1080p'  # or whatever the default should be
        
        return jsonify({version: version_settings})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@settings_bp.route('/save_version_settings', methods=['POST'])
def save_version_settings():
    data = request.json
    version = data.get('version')
    settings = data.get('settings')

    if not version or not settings:
        return jsonify({'success': False, 'error': 'Invalid data provided'}), 400

    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Handle wake_count conversion
        if 'wake_count' in settings:
            if settings['wake_count'] == '' or settings['wake_count'] == 'None' or settings['wake_count'] is None:
                settings['wake_count'] = None
            else:
                try:
                    settings['wake_count'] = int(settings['wake_count'])
                except (ValueError, TypeError):
                    settings['wake_count'] = None
        
        config['Scraping']['versions'][version] = settings
        save_config(config)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def update_required_settings(form_data):
    config = load_config()
    config['Plex']['url'] = form_data.get('plex_url')
    config['Plex']['token'] = form_data.get('plex_token')
    config['Plex']['shows_libraries'] = form_data.get('shows_libraries')
    config['Plex']['movies_libraries'] = form_data.get('movies_libraries')
    config['RealDebrid']['api_key'] = form_data.get('realdebrid_api_key')
    config['Metadata Battery']['url'] = form_data.get('metadata_battery_url')
    save_config(config)

@settings_bp.route('/notifications/enabled', methods=['GET'])
def get_enabled_notifications():
    try:
        config = load_config()
        notifications = config.get('Notifications', {})
        
        enabled_notifications = {}
        for notification_id, notification_config in notifications.items():
            # Ensure defaults are present
            notification_config = ensure_notification_defaults(notification_config)
            
            if notification_config.get('enabled', False):
                # Only include notifications that are enabled and have non-empty required fields
                if notification_config['type'] == 'Discord':
                    if notification_config.get('webhook_url'):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Email':
                    if all([
                        notification_config.get('smtp_server'),
                        notification_config.get('smtp_port'),
                        notification_config.get('smtp_username'),
                        notification_config.get('smtp_password'),
                        notification_config.get('from_address'),
                        notification_config.get('to_address')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Telegram':
                    if all([
                        notification_config.get('bot_token'),
                        notification_config.get('chat_id')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'NTFY':
                    if all([
                        notification_config.get('host'),
                        notification_config.get('topic')
                    ]):
                        enabled_notifications[notification_id] = notification_config
        
        return jsonify({
            'success': True,
            'enabled_notifications': enabled_notifications
        })
    except Exception as e:
        logging.error(f"Error getting enabled notifications: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/enabled_for_category/<category>', methods=['GET'])
def get_enabled_notifications_for_category(category):
    try:
        config = load_config()
        notifications = config.get('Notifications', {})
        
        enabled_notifications = {}
        for notification_id, notification_config in notifications.items():
            # Ensure defaults are present
            notification_config = ensure_notification_defaults(notification_config)
            
            if notification_config.get('enabled', False):
                # Check if the notification is enabled for this category
                notify_on = notification_config.get('notify_on', {})
                if not notify_on.get(category, False):
                    continue

                # Only include notifications that are enabled and have non-empty required fields
                if notification_config['type'] == 'Discord':
                    if notification_config.get('webhook_url'):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Email':
                    if all([
                        notification_config.get('smtp_server'),
                        notification_config.get('smtp_port'),
                        notification_config.get('smtp_username'),
                        notification_config.get('smtp_password'),
                        notification_config.get('from_address'),
                        notification_config.get('to_address')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Telegram':
                    if all([
                        notification_config.get('bot_token'),
                        notification_config.get('chat_id')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'NTFY':
                    if all([
                        notification_config.get('host'),
                        notification_config.get('topic')
                    ]):
                        enabled_notifications[notification_id] = notification_config

        return jsonify({
            'success': True,
            'enabled_notifications': enabled_notifications
        })
    except Exception as e:
        logging.error(f"Error getting enabled notifications for category {category}: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@settings_bp.route('/notifications/update_defaults', methods=['POST'])
def update_notification_defaults():
    try:
        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        # Force update all notifications with proper defaults
        for notification_id, notification_config in config['Notifications'].items():
            if notification_config is not None:
                # Remove empty notify_on if it exists
                if 'notify_on' in notification_config and not notification_config['notify_on']:
                    del notification_config['notify_on']
                
                # Apply defaults
                notification_config = ensure_notification_defaults(notification_config)
                config['Notifications'][notification_id] = notification_config

        save_config(config)
        return jsonify({'success': True, 'message': 'Notification defaults updated successfully'})
    except Exception as e:
        logging.error(f"Error updating notification defaults: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/add_default', methods=['POST'])
def add_default_version():
    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}

        # Get the default version settings from the schema
        version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
        default_version = {
            'enable_hdr': version_schema['enable_hdr']['default'],
            'max_resolution': version_schema['max_resolution']['default'],
            'resolution_wanted': version_schema['resolution_wanted']['default'],
            'resolution_weight': version_schema['resolution_weight']['default'],
            'hdr_weight': version_schema['hdr_weight']['default'],
            'similarity_weight': version_schema['similarity_weight']['default'],
            'similarity_threshold': version_schema['similarity_threshold']['default'],
            'similarity_threshold_anime': version_schema['similarity_threshold_anime']['default'],
            'size_weight': version_schema['size_weight']['default'],
            'bitrate_weight': version_schema['bitrate_weight']['default'],
            'preferred_filter_in': version_schema['preferred_filter_in']['default'],
            'preferred_filter_out': version_schema['preferred_filter_out']['default'],
            'filter_in': version_schema['filter_in']['default'],
            'filter_out': version_schema['filter_out']['default'],
            'min_size_gb': version_schema['min_size_gb']['default'],
            'max_size_gb': version_schema['max_size_gb']['default'],
            'min_bitrate_mbps': version_schema['min_bitrate_mbps']['default'],
            'max_bitrate_mbps': version_schema['max_bitrate_mbps']['default'],
            'wake_count': version_schema['wake_count']['default'],
            'require_physical_release': version_schema['require_physical_release']['default']
        }

        # Add the default version while preserving existing versions
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Find a unique name for the default version
        version_name = 'Default'
        counter = 1
        while version_name in config['Scraping']['versions']:
            version_name = f'Default {counter}'
            counter += 1
            
        config['Scraping']['versions'][version_name] = default_version
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/add_separate_versions', methods=['POST'])
def add_separate_versions():
    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}

        # Get the default version settings from the schema
        version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
        base_version = {
            'resolution_wanted': version_schema['resolution_wanted']['default'],
            'resolution_weight': version_schema['resolution_weight']['default'],
            'hdr_weight': version_schema['hdr_weight']['default'],
            'similarity_weight': version_schema['similarity_weight']['default'],
            'similarity_threshold': version_schema['similarity_threshold']['default'],
            'similarity_threshold_anime': version_schema['similarity_threshold_anime']['default'],
            'size_weight': version_schema['size_weight']['default'],
            'bitrate_weight': version_schema['bitrate_weight']['default'],
            'preferred_filter_in': [],
            'preferred_filter_out': [],
            'filter_in': [],
            'filter_out': [],
            'min_size_gb': version_schema['min_size_gb']['default'],
            'max_size_gb': version_schema['max_size_gb']['default'],
            'min_bitrate_mbps': version_schema['min_bitrate_mbps']['default'],
            'max_bitrate_mbps': version_schema['max_bitrate_mbps']['default'],
            'wake_count': version_schema['wake_count']['default'],
            'require_physical_release': version_schema['require_physical_release']['default']
        }

        # Create 1080p version
        version_1080p = base_version.copy()
        version_1080p.update({
            'enable_hdr': False,
            'max_resolution': '1080p',
            'preferred_filter_in': [
                [
                    'REMUX',
                    100
                ],
                [
                    'WebDL',
                    50
                ],
                [
                    'Web-DL',
                    50
                ]
            ],
            'preferred_filter_out': [
                [
                    '720p',
                    5
                ],
                [
                    'TrueHD',
                    3
                ],
                [
                    'SDR',
                    5
                ]
            ],
            'filter_out': [
                'Telesync',
                '3D',
                '(?i)\\bHDTS\\b',
                'HD-TS',
                '\\.TS\\.',
                '\\.CAM\\.',
                'HDCAM',
                'Telecine',
                '(?i).*\\bTS\\b$'
            ]
        })

        # Create 4K version
        version_4k = base_version.copy()
        version_4k.update({
            'enable_hdr': True,
            'max_resolution': '2160p',
            'resolution_wanted': '==',
            'wake_count': 6,
            'preferred_filter_in': [
                [
                    'REMUX',
                    100
                ],
                [
                    'WebDL',
                    50
                ],
                [
                    'Web-DL',
                    50
                ]
            ],
            'preferred_filter_out': [
                [
                    '720p',
                    5
                ],
                [
                    'TrueHD',
                    3
                ],
                [
                    'SDR',
                    5
                ]
            ],
            'filter_out': [
                'Telesync',
                '3D',
                '(?i)\\bHDTS\\b',
                'HD-TS',
                '\\.TS\\.',
                '\\.CAM\\.',
                'HDCAM',
                'Telecine',
                '(?i).*\\bTS\\b$'
            ]
        })

        # Add the new versions while preserving existing versions
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Find unique names for the versions
        version_1080p_name = '1080p'
        version_4k_name = '2160p'
        counter_1080p = 1
        counter_4k = 1
        
        while version_1080p_name in config['Scraping']['versions']:
            version_1080p_name = f'1080p {counter_1080p}'
            counter_1080p += 1
            
        while version_4k_name in config['Scraping']['versions']:
            version_4k_name = f'2160p {counter_4k}'
            counter_4k += 1
            
        # Add the new versions
        config['Scraping']['versions'][version_1080p_name] = version_1080p
        config['Scraping']['versions'][version_4k_name] = version_4k
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/clear_all', methods=['POST'])
def clear_all_versions():
    try:
        config = load_config()
        if 'Scraping' in config:
            config['Scraping']['versions'] = {}
            save_config(config)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error clearing versions: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/test', methods=['POST'])
def test_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' not in config or notification_id not in config['Notifications']:
            return jsonify({'success': False, 'error': 'Notification not found'}), 404

        notification_config = config['Notifications'][notification_id]
        
        # Create a test notification
        test_notification = {
            'title': 'Test Notification',
            'message': f'This is a test notification from {notification_config["title"]}',
            'type': 'info',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Get the notification type
        notification_type = notification_config['type']
        
        # Send the test notification based on the type
        success = False
        message = "Test notification sent successfully"
        
        try:
            if notification_type == 'Telegram':
                if not notification_config.get('bot_token') or not notification_config.get('chat_id'):
                    return jsonify({'success': False, 'error': 'Missing Telegram configuration'}), 400
                
                content = f"<b>Test Notification</b>\n\nThis is a test message from CLI Debrid. If you're seeing this, your Telegram notifications are working correctly!"
                send_telegram_notification(
                    notification_config['bot_token'],
                    notification_config['chat_id'],
                    content
                )
                success = True
                
            elif notification_type == 'Discord':
                if not notification_config.get('webhook_url'):
                    return jsonify({'success': False, 'error': 'Missing Discord webhook URL'}), 400
                
                content = "**Test Notification**\n\nThis is a test message from CLI Debrid. If you're seeing this, your Discord notifications are working correctly!"
                send_discord_notification(
                    notification_config['webhook_url'],
                    content
                )
                success = True
                
            elif notification_type == 'NTFY':
                if not notification_config.get('host') or not notification_config.get('topic'):
                    return jsonify({'success': False, 'error': 'Missing NTFY configuration'}), 400
                
                content = "Test Notification\n\nThis is a test message from CLI Debrid. If you're seeing this, your NTFY notifications are working correctly!"
                send_ntfy_notification(
                    notification_config['host'],
                    notification_config.get('api_key', ''),
                    notification_config.get('priority', 'low'),
                    notification_config['topic'],
                    content
                )
                success = True
                
            elif notification_type == 'Email':
                required_fields = ['smtp_server', 'smtp_port', 'smtp_username', 'smtp_password', 'from_address', 'to_address']
                missing_fields = [field for field in required_fields if not notification_config.get(field)]
                
                if missing_fields:
                    return jsonify({'success': False, 'error': f'Missing Email configuration: {", ".join(missing_fields)}'}), 400
                
                content = """
                <html>
                <body>
                <h2>Test Notification</h2>
                <p>This is a test message from CLI Debrid. If you're seeing this, your Email notifications are working correctly!</p>
                </body>
                </html>
                """
                
                smtp_config = {
                    'smtp_server': notification_config['smtp_server'],
                    'smtp_port': notification_config['smtp_port'],
                    'smtp_username': notification_config['smtp_username'],
                    'smtp_password': notification_config['smtp_password'],
                    'from_address': notification_config['from_address'],
                    'to_address': notification_config['to_address']
                }
                
                send_email_notification(smtp_config, content)
                success = True
            
            else:
                return jsonify({'success': False, 'error': f'Unknown notification type: {notification_type}'}), 400
                
            if success:
                logging.info(f"Test notification sent successfully for {notification_id}")
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'error': 'Failed to send test notification'}), 500
                
        except Exception as e:
            logging.error(f"Error sending test notification: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': f'Error sending test notification: {str(e)}'}), 500
            
    except Exception as e:
        logging.error(f"Error testing notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
