import json
from settings_schema import SETTINGS_SCHEMA
import logging
import uuid
import fcntl
import os

CONFIG_LOCK_FILE = './config/config.lock'
CONFIG_FILE = './config/config.json'

def log_config_state(message, config):
    logging.debug(f"[CONFIG_STATE] {message}: {json.dumps(config, indent=2)}")

def acquire_lock():
    lock_file = open(CONFIG_LOCK_FILE, 'w')
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    return lock_file

def release_lock(lock_file):
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()

def load_config():
    lock_file = acquire_lock()
    try:
        with open(CONFIG_FILE, 'r') as config_file:
            config = json.load(config_file)
            
        # Ensure all required top-level keys are present
        for key in SETTINGS_SCHEMA.keys():
            if key not in config:
                config[key] = {}
        
        # Fix any potential issues with Content Sources
        config = fix_content_sources(config)
        
        logging.debug(f"Config loaded: {json.dumps(config, indent=2)}")
        log_config_state("Config loaded", config)
        return config
    except Exception as e:
        logging.error(f"Error loading config: {str(e)}")
        return {}
    finally:
        release_lock(lock_file)
def save_config(config):
    lock_file = acquire_lock()
    try:
        # Ensure only valid top-level keys are present
        valid_keys = set(SETTINGS_SCHEMA.keys())
        cleaned_config = {}
        
        for key, value in config.items():
            if key in valid_keys:
                if isinstance(value, dict):
                    # For nested structures, keep all keys
                    cleaned_config[key] = value
                else:
                    cleaned_config[key] = value
        
        # Write the entire config to a temporary file first
        temp_file = CONFIG_FILE + '.tmp'
        with open(temp_file, 'w') as config_file:
            json.dump(cleaned_config, config_file, indent=2)
        
        # If the write was successful, rename the temp file to the actual config file
        os.replace(temp_file, CONFIG_FILE)
        
        log_config_state("Config saved", cleaned_config)
    except Exception as e:
        logging.error(f"Error saving config: {str(e)}", exc_info=True)
        if os.path.exists(temp_file):
            os.remove(temp_file)
    finally:
        release_lock(lock_file)

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

def add_content_source(source_type, source_config):
    process_id = str(uuid.uuid4())[:8]
    logging.debug(f"[{process_id}] Starting add_content_source process for source_type: {source_type}")
    
    config = load_config()
    log_config_state(f"[{process_id}] Config before modification", config)
    
    if 'Content Sources' not in config:
        config['Content Sources'] = {}

    # Generate a new content source ID
    base_name = source_type
    index = 1
    while f"{base_name}_{index}" in config['Content Sources']:
        index += 1
    new_source_id = f"{base_name}_{index}"
    logging.debug(f"[{process_id}] Generated new content source ID: {new_source_id}")
    
    # Validate and set values based on the schema
    validated_config = {}
    schema = SETTINGS_SCHEMA['Content Sources']['schema'][source_type]
    for key, value in schema.items():
        if key in source_config:
            validated_config[key] = source_config[key]
        elif 'default' in value:
            validated_config[key] = value['default']
    
    # Add type, enabled, versions, and display_name
    validated_config['type'] = source_type
    validated_config['enabled'] = source_config.get('enabled', False)
    validated_config['versions'] = source_config.get('versions', [])
    validated_config['display_name'] = source_config.get('display_name', '')
    
    logging.debug(f"[{process_id}] Validated config for {new_source_id}: {validated_config}")
    
    # Add the new content source to the 'Content Sources' section
    config['Content Sources'][new_source_id] = validated_config
    
    log_config_state(f"[{process_id}] Config after adding content source", config)
    save_config(config)
    logging.debug(f"[{process_id}] Successfully added content source: {new_source_id}")
    return new_source_id

def delete_content_source(source_id):
    logging.info(f"Attempting to delete content source: {source_id}")
    config = load_config()
    if 'Content Sources' in config and source_id in config['Content Sources']:
        del config['Content Sources'][source_id]
        save_config(config)
        logging.info(f"Content source {source_id} deleted successfully")
        return True
    else:
        logging.warning(f"Content source not found: {source_id}")
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
            if key in schema:
                config['Content Sources'][source_id][key] = value
        log_config_state(f"[{process_id}] Config after updating content source", config)
        save_config(config)
        logging.debug(f"[{process_id}] Successfully updated content source: {source_id}")
        return True
    else:
        logging.warning(f"[{process_id}] Content source not found for update: {source_id}")
        return False

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
