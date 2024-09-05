from flask import Blueprint, jsonify, request, render_template, Response, current_app
from settings import load_config, validate_url
from settings_schema import SETTINGS_SCHEMA
import logging
from config_manager import add_scraper, clean_notifications, get_content_source_settings, update_content_source, get_version_settings, add_content_source, delete_content_source, save_config
from routes.models import admin_required, onboarding_required
import traceback
import json

settings_bp = Blueprint('settings', __name__)


def is_user_system_enabled():
    config = load_config()
    return config.get('UI Settings', {}).get('enable_user_system', True)

@settings_bp.route('/content-sources/content')
def content_sources_content():
    config = load_config()
    source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    return render_template('settings_tabs/content_sources.html', 
                           settings=config, 
                           source_types=source_types, 
                           settings_schema=SETTINGS_SCHEMA)

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
            'title': notification_title
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

@settings_bp.route('/notifications/content', methods=['GET'])
def notifications_content():
    try:
        config = load_config()
        notification_settings = config.get('Notifications', {})
        
        # Sort notifications by type and then by number
        sorted_notifications = sorted(
            notification_settings.items(),
            key=lambda x: (x[1]['type'], int(x[0].split('_')[-1]))
        )
        
        html_content = render_template(
            'settings_tabs/notifications_content.html',
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
                               settings_schema=SETTINGS_SCHEMA)
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
            'Plex': {
                'url': config.get('Plex', {}).get('url', ''),
                'token': config.get('Plex', {}).get('token', '')
            },
            'Overseerr': {
                'url': config.get('Overseerr', {}).get('url', ''),
                'api_key': config.get('Overseerr', {}).get('api_key', '')
            },
            'RealDebrid': {
                'api_key': config.get('RealDebrid', {}).get('api_key', '')
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

        def update_nested_dict(current, new):
            for key, value in new.items():
                if isinstance(value, dict):
                    if key not in current or not isinstance(current[key], dict):
                        current[key] = {}
                    update_nested_dict(current[key], value)
                else:
                    if key.lower().endswith('url'):
                        value = validate_url(value)
                    current[key] = value

        update_nested_dict(config, new_settings)
        
        save_config(config)
        
        return jsonify({"status": "success", "message": "Settings updated successfully"})
    except Exception as e:
        logging.error(f"Error updating settings: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

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
        'max_size_gb': ''
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
            versions[new_name] = versions.pop(old_name)
            save_config(config)
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

    config['Scraping']['versions'][new_version_id] = config['Scraping']['versions'][version_id].copy()
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
    config['Overseerr']['url'] = form_data.get('overseerr_url')
    config['Overseerr']['api_key'] = form_data.get('overseerr_api_key')
    config['RealDebrid']['api_key'] = form_data.get('realdebrid_api_key')
    save_config(config)