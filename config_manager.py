import json
from settings_schema import SETTINGS_SCHEMA
import logging
import uuid
import fcntl
import os
import shutil
from datetime import datetime

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
    process_id = str(uuid.uuid4())[:8]
    lock_file = acquire_lock()
    try:
        logging.debug(f"[{process_id}] Attempting to load config")
        if not os.path.exists(CONFIG_FILE):
            logging.warning(f"[{process_id}] Config file not found. Creating a new one.")
            return {}
        
        with open(CONFIG_FILE, 'r') as config_file:
            config = json.load(config_file)
        
        logging.debug(f"[{process_id}] Config loaded successfully")
        return config
    except json.JSONDecodeError as e:
        logging.error(f"[{process_id}] Error decoding JSON in config file: {str(e)}")
        return {}
    except Exception as e:
        logging.error(f"[{process_id}] Error loading config: {str(e)}")
        return {}
    finally:
        release_lock(lock_file)
        logging.debug(f"[{process_id}] Lock released after loading config")

def save_config(config):
    process_id = str(uuid.uuid4())[:8]
    lock_file = acquire_lock()
    try:
        logging.debug(f"[{process_id}] Attempting to save config")
        
        temp_file = CONFIG_FILE + '.tmp'
        with open(temp_file, 'w') as config_file:
            json.dump(config, config_file, indent=2)
        
        os.replace(temp_file, CONFIG_FILE)
        
        logging.debug(f"[{process_id}] Config saved successfully")
    except Exception as e:
        logging.error(f"[{process_id}] Error saving config: {str(e)}", exc_info=True)
        if os.path.exists(temp_file):
            os.remove(temp_file)
    finally:
        release_lock(lock_file)
        logging.debug(f"[{process_id}] Lock released after saving config")

def add_content_source(source_type, source_config):
    process_id = str(uuid.uuid4())[:8]
    logging.debug(f"[{process_id}] Starting add_content_source process for source_type: {source_type}")
    
    config = load_config()
    logging.debug(f"[{process_id}] Config before modification: {json.dumps(config, indent=2)}")
    
    if 'Content Sources' not in config:
        config['Content Sources'] = {}

    # Generate a new content source ID
    base_name = source_type
    index = 1
    while f"{base_name}_{index}" in config['Content Sources']:
        index += 1
    new_source_id = f"{base_name}_{index}"
    
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
    validated_config['display_name'] = source_config.get('display_name', '')
    
    logging.debug(f"[{process_id}] Validated config for {new_source_id}: {validated_config}")
    
    # Add the new content source to the 'Content Sources' section
    config['Content Sources'][new_source_id] = validated_config
    
    logging.debug(f"[{process_id}] Config after adding content source: {json.dumps(config, indent=2)}")
    save_config(config)
    
    logging.debug(f"[{process_id}] Successfully added content source: {new_source_id}")
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
    
    try:
        logging.debug(f"[{process_id}] Loading config")
        config = load_config()
        logging.debug(f"[{process_id}] Config loaded successfully")
        
        if 'Content Sources' in config and source_id in config['Content Sources']:
            logging.debug(f"[{process_id}] Found content source {source_id} in config")
            del config['Content Sources'][source_id]
            logging.debug(f"[{process_id}] Deleted content source {source_id} from config")
            
            logging.debug(f"[{process_id}] Saving updated config")
            save_config(config)
            logging.debug(f"[{process_id}] Config saved successfully")
            
            logging.info(f"[{process_id}] Content source {source_id} deleted successfully")
        else:
            logging.warning(f"[{process_id}] Content source {source_id} not found in config")
        
        logging.debug(f"[{process_id}] Delete operation completed")
    except Exception as e:
        logging.error(f"[{process_id}] Error during delete operation: {str(e)}", exc_info=True)

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
