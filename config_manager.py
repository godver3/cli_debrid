import json
from settings_schema import SETTINGS_SCHEMA
import logging
import uuid
import os
import sys
import shutil
from datetime import datetime
from debrid import reset_provider
from utilities.file_lock import FileLock
import importlib

# Get the base config directory from an environment variable, with a fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')

# Use os.path.join to create platform-independent paths
CONFIG_LOCK_FILE = os.path.join(CONFIG_DIR, 'config.lock')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

if sys.platform.startswith('win'):
    # Windows-specific import and file locking
    import msvcrt
    def lock_file(file):
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
    def unlock_file(file):
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
else:
    # Unix-like systems
    import fcntl
    def lock_file(file):
        fcntl.flock(file, fcntl.LOCK_EX)
    def unlock_file(file):
        fcntl.flock(file, fcntl.LOCK_UN)

def log_config_state(message, config):
    content_sources = config.get('Content Sources', {})
    #logging.debug(f"[CONFIG_STATE] {message} (Content Sources only): {json.dumps(content_sources, indent=2)}")

def acquire_lock():
    lock_file_handle = open(CONFIG_LOCK_FILE, 'w')
    lock_file(lock_file_handle)
    return lock_file_handle

def release_lock(lock_file_handle):
    unlock_file(lock_file_handle)
    lock_file_handle.close()

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, 'r') as file:
        with FileLock(file):
            return json.load(file)

def save_config(config):
    with open(CONFIG_FILE, 'w') as file:
        with FileLock(file):
            json.dump(config, file, indent=4)
            try:
                from routes.base_routes import clear_cache
                clear_cache()  # Clear the update check cache when settings are saved
                #logging.debug("Cleared update check cache after saving settings")
            except Exception as e:
                logging.error(f"Error clearing update check cache: {str(e)}")

def add_content_source(source_type, source_config):
    process_id = str(uuid.uuid4())[:8]
    logging.debug(f"[{process_id}] Starting add_content_source process for source_type: {source_type}")
    
    config = load_config()
    log_config_state(f"[{process_id}] Config before modification", config)
    
    if 'Content Sources' not in config:
        config['Content Sources'] = {}

    # Generate a new content source ID
    existing_sources = config['Content Sources']
    existing_indices = [int(key.split('_')[-1]) for key in existing_sources.keys() 
                        if key.startswith(f"{source_type}_") and key.split('_')[-1].isdigit()]
    
    if existing_indices:
        index = max(existing_indices) + 1
    else:
        index = 1
    
    new_source_id = f"{source_type}_{index}"
    logging.debug(f"[{process_id}] Generated new source ID: {new_source_id}")
    
    # Validate and set values based on the schema
    validated_config = {}
    schema = SETTINGS_SCHEMA['Content Sources']['schema'].get(source_type, {})
    for key, value in schema.items():
        if key in source_config:
            validated_config[key] = source_config[key]
        elif 'default' in value:
            validated_config[key] = value['default']
    
    # Add type, enabled, versions, and display_name
    validated_config['type'] = source_type
    validated_config['enabled'] = source_config.get('enabled', False)
    validated_config['versions'] = source_config.get('versions', [])
    if isinstance(validated_config['versions'], bool):
        validated_config['versions'] = []
    elif isinstance(validated_config['versions'], str):
        validated_config['versions'] = [validated_config['versions']]    
    validated_config['display_name'] = source_config.get('display_name', '')
    
    logging.debug(f"[{process_id}] Validated config for {new_source_id}: {validated_config}")
    
    # Add the new content source to the config
    config['Content Sources'][new_source_id] = validated_config
    
    log_config_state(f"[{process_id}] Config after adding content source", config)
    
    # Save the updated config
    save_config(config)
    
    # Verify that the changes were saved
    updated_config = load_config()
    if new_source_id in updated_config.get('Content Sources', {}):
        logging.debug(f"[{process_id}] New content source {new_source_id} successfully added and saved")
    else:
        logging.error(f"[{process_id}] Failed to save new content source {new_source_id}")
    
    log_config_state(f"[{process_id}] Final config after add_content_source", updated_config)
    
    logging.debug(f"[{process_id}] Finished add_content_source process")
    return new_source_id

def json_serializer(obj):
    """Custom JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

def fix_content_sources(config):
    if 'Content Sources' in config:
        for source_id, source_config in config['Content Sources'].items():
            if isinstance(source_config, str):
                try:
                    config['Content Sources'][source_id] = json.loads(source_config)
                except json.JSONDecodeError:
                    logging.warning(f"Invalid JSON in Content Sources for key {source_id}")
    return config

def delete_content_source(source_id):
    process_id = str(uuid.uuid4())[:8]
    logging.info(f"[{process_id}] Attempting to delete content source: {source_id}")
    
    config = load_config()
    if 'Content Sources' in config and source_id in config['Content Sources']:
        del config['Content Sources'][source_id]
        save_config(config)
        logging.info(f"[{process_id}] Content source {source_id} deleted successfully")
        return True
    else:
        logging.warning(f"[{process_id}] Content source {source_id} not found in config")
        return False

def update_content_source(source_id, source_config):
    process_id = str(uuid.uuid4())[:8]
    logging.debug(f"[{process_id}] Starting update_content_source process for source_id: {source_id}")
    
    config = load_config()
    if 'Content Sources' in config and source_id in config['Content Sources']:
        # Validate and update only the fields present in the schema
        source_type = source_id.split('_')[0]
        schema = SETTINGS_SCHEMA['Content Sources']['schema'][source_type]
        for key, value in source_config.items():
            if key in schema or key in ['versions', 'enabled', 'display_name']:
                if key == 'versions':
                    # Ensure versions is always a list
                    if isinstance(value, bool):
                        value = []
                    elif isinstance(value, str):
                        value = [value]
                    elif not isinstance(value, list):
                        value = list(value)
                config['Content Sources'][source_id][key] = value
        log_config_state(f"[{process_id}] Config after updating content source", config)
        save_config(config)
        # Explicitly reset provider and reinitialize components after content source update
        reset_provider()
        from queue_manager import QueueManager
        QueueManager().reinitialize_queues()
        from run_program import ProgramRunner
        ProgramRunner().reinitialize()
        logging.debug(f"[{process_id}] Successfully updated content source: {source_id}")
        return True
    else:
        logging.error(f"[{process_id}] Content source not found: {source_id}")
        return False

def update_all_content_sources(content_sources):
    process_id = str(uuid.uuid4())[:8]
    logging.debug(f"[{process_id}] Starting update_all_content_sources process")
    
    config = load_config()
    config['Content Sources'] = content_sources
    log_config_state(f"[{process_id}] Config after updating all content sources", config)
    save_config(config)
    
    # Explicitly reset provider and reinitialize components after updating all content sources
    reset_provider()
    from queue_manager import QueueManager
    QueueManager().reinitialize_queues()
    from run_program import ProgramRunner
    ProgramRunner().reinitialize()
    logging.debug(f"[{process_id}] Successfully updated all content sources")
    return True

def add_scraper(scraper_type, scraper_config):
    process_id = str(uuid.uuid4())[:8]  # Generate a unique ID for this process
    logging.debug(f"[{process_id}] Starting add_scraper process")
    logging.debug(f"[{process_id}] Adding scraper of type {scraper_type} with config: {scraper_config}")
    
    config = load_config()
    log_config_state(f"[{process_id}] Config before modification", config)
    
    if 'Scrapers' not in config:
        config['Scrapers'] = {}
    
    # Generate a new scraper ID
    base_name = scraper_type
    index = 1
    while f"{base_name}_{index}" in config['Scrapers']:
        index += 1
    new_scraper_id = f"{base_name}_{index}"
    logging.debug(f"[{process_id}] Generated new scraper ID: {new_scraper_id}")
    
    # Validate and set values based on the schema
    validated_config = {}
    schema = SETTINGS_SCHEMA['Scrapers']['schema'][scraper_type]
    for key, value in schema.items():
        if key in scraper_config:
            validated_config[key] = scraper_config[key]
        elif 'default' in value:
            validated_config[key] = value['default']
    logging.debug(f"[{process_id}] Validated config for {new_scraper_id}: {validated_config}")
    
    # Add this line
    validated_config['type'] = scraper_type
    
    # Add the new scraper to the 'Scrapers' section
    config['Scrapers'][new_scraper_id] = validated_config
    
    # Remove any keys that might have been accidentally added to the root
    root_keys = list(SETTINGS_SCHEMA.keys())
    config = {key: value for key, value in config.items() if key in root_keys}
    log_config_state(f"[{process_id}] Config after adding scraper", config)
    
    save_config(config)
    logging.debug(f"[{process_id}] Finished add_scraper process")
    return new_scraper_id

def delete_scraper(scraper_id):
    config = load_config()
    if 'Scrapers' in config and scraper_id in config['Scrapers']:
        del config['Scrapers'][scraper_id]
        save_config(config)
        return True
    return False

# Add this function if it doesn't exist
def save_version_settings(version, settings):
    config = load_config()
    if 'Scraping' not in config:
        config['Scraping'] = {}
    if 'versions' not in config['Scraping']:
        config['Scraping']['versions'] = {}
    
    # Ensure max_size_gb is properly handled
    if 'max_size_gb' in settings:
        if settings['max_size_gb'] == '' or settings['max_size_gb'] is None:
            settings['max_size_gb'] = float('inf')
        else:
            settings['max_size_gb'] = float(settings['max_size_gb'])
    
    config['Scraping']['versions'][version] = settings
    save_config(config)

def get_version_settings(version):
    config = load_config()
    scraping_config = config.get('Scraping', {})
    versions = scraping_config.get('versions', {})
    settings = versions.get(version, {})
    
    # Convert max_size_gb back to empty string if it's infinity
    if 'max_size_gb' in settings and settings['max_size_gb'] == float('inf'):
        settings['max_size_gb'] = ''
    
    logging.debug(f"Fetched settings for version '{version}': {settings}")
    
    if not settings:
        logging.warning(f"No settings found for version: {version}")
    
    return settings

def get_content_source_settings():
    #logging.debug("Entering get_content_source_settings()")
    
    try:
        config = load_config()
        content_sources = config.get('Content Sources', {})
        
        # Create a dictionary of content source types and their settings
        content_source_settings = {}
        for source_id, source_config in content_sources.items():
            source_type = source_config.get('type')
            if source_type:
                schema = SETTINGS_SCHEMA.get('Content Sources', {}).get('schema', {}).get(source_type, {})
                if source_type not in content_source_settings:
                    content_source_settings[source_type] = {
                        str(k): v for k, v in schema.items() 
                        if k is not None and v is not None
                    }
        
        #logging.debug(f"Content source settings: {content_source_settings}")
        
        return content_source_settings
    except Exception as e:
        logging.error(f"Error in get_content_source_settings: {str(e)}", exc_info=True)
        raise

def update_scraper(scraper_id, scraper_config):
    config = load_config()
    if 'Scrapers' in config and scraper_id in config['Scrapers']:
        # Validate and update only the fields present in the schema
        scraper_type = scraper_id.split('_')[0]
        schema = SETTINGS_SCHEMA['Scrapers']['schema'][scraper_type]
        for key, value in scraper_config.items():
            if key in schema:
                config['Scrapers'][scraper_id][key] = value
        save_config(config)
        return True
    return False

def clean_notifications(config):
    if 'Notifications' in config:
        config['Notifications'] = {k: v for k, v in config['Notifications'].items() if v is not None}
    return config

def trim_trailing_slashes(config):
    """Trim trailing slashes from file paths in the Debug section."""
    if 'Debug' in config:
        for key in ['original_files_path', 'symlinked_files_path']:
            if key in config['Debug'] and isinstance(config['Debug'][key], str):
                config['Debug'][key] = config['Debug'][key].rstrip('/')
    return config