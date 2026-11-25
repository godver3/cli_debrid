"""Content source and scraper add/delete/reorder endpoints."""

from flask import jsonify, request, render_template
from utilities.settings import load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from queues.config_manager import add_scraper, add_content_source, delete_content_source
from routes.models import admin_required
import json
import os
from ._common import _reload_components_after_settings_change, migrate_scraper_priorities, settings_bp

# --- END Hardcoded Default Versions ---

@settings_bp.route('/content-sources/content')
@admin_required
def content_sources_content():
    import json as _json
    config = load_config()
    source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())

    # Normalise plex_collection.libraries — may have been saved as a JSON string by older code
    for src_cfg in config.get('Content Sources', {}).values():
        if isinstance(src_cfg, dict):
            pc = src_cfg.get('plex_collection')
            if isinstance(pc, dict):
                libs = pc.get('libraries', [])
                if isinstance(libs, str):
                    try:
                        pc['libraries'] = _json.loads(libs)
                    except Exception:
                        pc['libraries'] = []

    template_name = 'settings_tabs/content_sources.html'

    # Debug logging

    return render_template(template_name,
                          settings=config,
                          source_types=source_types,
                          settings_schema=SETTINGS_SCHEMA)

@settings_bp.route('/content-sources/types')
def content_sources_types():
    config = load_config()
    source_types_from_schema = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
         
    return jsonify({
        'source_types': source_types_from_schema, 
        'settings': SETTINGS_SCHEMA['Content Sources']['schema']
    })

@settings_bp.route('/content-sources/trakt-friends')
@admin_required
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

@settings_bp.route('/content-sources/scrob-lists')
@admin_required
def get_scrob_lists():
    """Get the user's Scrob lists (id + name) for the multi-select picker."""
    try:
        from content_checkers.scrob import get_scrob_config, _scrob_get

        if not get_scrob_config():
            return jsonify({'success': False, 'error': 'Scrob URL/API key not configured in Settings → Tracking accounts → Scrob'}), 400

        data = _scrob_get('/lists')
        if data is None:
            return jsonify({'success': False, 'error': 'Failed to reach Scrob. Check the URL/API key in Settings → Tracking accounts → Scrob.'}), 502

        lists = [{'id': lst['id'], 'name': lst.get('name', f"List {lst['id']}"), 'item_count': lst.get('item_count', 0)}
                 for lst in data.get('lists', [])]
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"Error fetching Scrob lists: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/content-sources/overseerr-users')
@admin_required
def get_overseerr_users():
    from utilities.settings import get_all_settings
    source_id = request.args.get('source_id', '')
    try:
        all_settings = get_all_settings()
        source_data = all_settings.get('Content Sources', {}).get(source_id, {})
        url = source_data.get('url', '').rstrip('/')
        api_key = source_data.get('api_key', '')
        if not url or not api_key:
            return jsonify({'success': False, 'error': 'URL or API key not configured', 'users': []})
        import requests as req
        resp = req.get(
            f"{url}/api/v1/user?take=100&skip=0",
            headers={'X-Api-Key': api_key},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        users = data.get('results', [])
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'users': []})

@settings_bp.route('/content_sources/add', methods=['POST'])
@admin_required
def add_content_source_route():
    try:
        if request.is_json:
            source_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        source_type = source_config.pop('type', None)
        if not source_type:
            return jsonify({'success': False, 'error': 'No source type provided'}), 400

        # Ensure versions is a dictionary (new standard)
        if 'versions' in source_config:
            # Convert list to dict if necessary (handle old format)
            if isinstance(source_config['versions'], list):
                 source_config['versions'] = {v: True for v in source_config['versions']}
            # Ensure it's a dict, otherwise default to empty dict
            elif not isinstance(source_config['versions'], dict):
                 logging.warning(f"Invalid 'versions' format for {source_type}: {source_config['versions']}. Resetting to empty dict.")
                 source_config['versions'] = {}

        new_source_id = add_content_source(source_type, source_config)

        # Reload components so the change takes effect mid-run
        _reload_components_after_settings_change('content_source')

        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        logging.error(f"Error adding content source: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/content_sources/delete', methods=['POST'])
@admin_required
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

        # Reload components so the change takes effect mid-run
        _reload_components_after_settings_change('content_source')

        logging.info(f"Content source {source_id} deleted successfully")
        return jsonify({'success': True})
    else:
        logging.warning(f"Failed to delete content source: {source_id}")
        return jsonify({'success': False, 'error': 'Source not found or already deleted'}), 404

@settings_bp.route('/scrapers/add', methods=['POST'])
@admin_required
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

        # Reload components so the change takes effect mid-run
        _reload_components_after_settings_change('scraper')

        return jsonify({'success': True, 'scraper_id': new_scraper_id})
    except Exception as e:
        logging.error(f"Error adding scraper: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/scrapers/content')
@admin_required
def scrapers_content():
    try:
        # Run migration to ensure all scrapers have priority field
        migrate_scraper_priorities()

        settings = load_config()
        scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())
        scraper_settings = {scraper: list(SETTINGS_SCHEMA["Scrapers"]["schema"][scraper].keys()) for scraper in SETTINGS_SCHEMA["Scrapers"]["schema"]}

        template_name = 'settings_tabs/scrapers.html'

        # Debug logging

        return render_template(template_name, settings=settings, scraper_types=scraper_types, scraper_settings=scraper_settings)
    except Exception as e:
        return jsonify({'error': 'An error occurred while loading scraper settings'}), 500

@settings_bp.route('/scrapers/get', methods=['GET'])
@admin_required
def get_scrapers():
    config = load_config()
    scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())

    template_name = 'settings_tabs/scrapers.html'


    return render_template(template_name, settings=config, scraper_types=scraper_types)

@settings_bp.route('/get_content_source_types', methods=['GET'])
def get_content_source_types():
    content_sources = SETTINGS_SCHEMA['Content Sources']['schema']
    return jsonify({
        'source_types': list(content_sources.keys()),
        'settings': content_sources
    })

@settings_bp.route('/scrapers/delete', methods=['POST'])
@admin_required
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

        # Reload components so the change takes effect mid-run
        _reload_components_after_settings_change('scraper')

        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Scraper not found'}), 404

@settings_bp.route('/scrapers/reorder', methods=['POST'])
@admin_required
def reorder_scrapers():
    """
    Reorder scrapers and recalculate priorities.
    Expects: {scraper_order: ['scraper1', 'scraper2', ...]}
    """
    try:
        data = request.json
        scraper_order = data.get('scraper_order', [])

        if not scraper_order:
            return jsonify({'success': False, 'error': 'No scraper order provided'}), 400

        config = load_config()
        scrapers = config.get('Scrapers', {})

        # Create new ordered dict based on the provided order
        reordered_scrapers = {}
        for scraper_id in scraper_order:
            if scraper_id in scrapers:
                reordered_scrapers[scraper_id] = scrapers[scraper_id]

        # Add any scrapers not in the order list at the end (shouldn't happen, but safety check)
        for scraper_id, scraper_config in scrapers.items():
            if scraper_id not in reordered_scrapers:
                reordered_scrapers[scraper_id] = scraper_config

        # Recalculate priorities: last enabled = 0, increment by 10 going up
        enabled_scrapers_in_order = [(sid, cfg) for sid, cfg in reordered_scrapers.items() if cfg.get('enabled', False)]

        # Reset all priorities to 0 first
        for scraper_id in reordered_scrapers:
            reordered_scrapers[scraper_id]['priority'] = 0

        # Assign priorities to enabled scrapers
        if enabled_scrapers_in_order:
            for index, (scraper_id, _) in enumerate(reversed(enabled_scrapers_in_order)):
                priority = index * 10
                reordered_scrapers[scraper_id]['priority'] = priority
                logging.info(f"Reordered: Assigned priority {priority} to scraper '{scraper_id}'")

        # Save the reordered scrapers
        config['Scrapers'] = reordered_scrapers
        save_config(config)

        # Reload components so the change takes effect mid-run
        _reload_components_after_settings_change('scraper')

        return jsonify({'success': True, 'message': 'Scrapers reordered successfully'})

    except Exception as e:
        logging.error(f"Error reordering scrapers: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
