import os
import logging
from urllib.parse import urlparse
import json
import ast
from settings_schema import SETTINGS_SCHEMA
import fcntl
import time

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')

# Update the path to use the environment variable
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LOCK_FILE = os.path.join(CONFIG_DIR, '.config.lock')

class FileLock:
    def __init__(self, lock_file):
        self.lock_file = lock_file
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.lock_file, os.O_WRONLY | os.O_CREAT)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except IOError as e:
                # Failed to acquire lock, wait a bit and retry
                time.sleep(0.1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)

def load_config():
    with FileLock(LOCK_FILE):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as config_file:
                    config = json.load(config_file)
                    
                    # Parse string representations in Content Sources
                    if 'Content Sources' in config:
                        for key, value in config['Content Sources'].items():
                            if isinstance(value, str):
                                try:
                                    parsed_value = json.loads(value)
                                    config['Content Sources'][key] = parsed_value
                                except json.JSONDecodeError:
                                    logging.debug(f"Keeping original string value for {key}: {value}")
                    
                    return config
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON from {CONFIG_FILE}: {str(e)}. Using empty config.")
                # Try to load from backup
                backup_file = CONFIG_FILE + '.backup'
                if os.path.exists(backup_file):
                    try:
                        with open(backup_file, 'r') as backup:
                            return json.load(backup)
                    except Exception as e:
                        logging.error(f"Failed to load backup: {str(e)}")
        return {}

def save_config(config):
    # Create lock file directory if it doesn't exist
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    
    with FileLock(LOCK_FILE):
        # Ensure Content Sources are saved as proper JSON
        if 'Content Sources' in config:
            for key, value in config['Content Sources'].items():
                if isinstance(value, str):
                    try:
                        # Try to parse it as JSON
                        json.loads(value)
                    except json.JSONDecodeError:
                        # If it's not valid JSON, convert it to a JSON string
                        config['Content Sources'][key] = json.dumps(value)
        
        # Create a backup before saving
        if os.path.exists(CONFIG_FILE):
            backup_file = CONFIG_FILE + '.backup'
            try:
                with open(CONFIG_FILE, 'r') as src, open(backup_file, 'w') as dst:
                    dst.write(src.read())
            except Exception as e:
                logging.error(f"Failed to create backup: {str(e)}")
        
        # Save the new config
        try:
            with open(CONFIG_FILE, 'w') as config_file:
                json.dump(config, config_file, indent=2)
        except Exception as e:
            logging.error(f"Failed to save config: {str(e)}")
            # If save failed and we have a backup, restore it
            if os.path.exists(backup_file):
                try:
                    with open(backup_file, 'r') as src, open(CONFIG_FILE, 'w') as dst:
                        dst.write(src.read())
                except Exception as e:
                    logging.error(f"Failed to restore backup: {str(e)}")

# Helper function to safely parse boolean values
def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', 'yes', '1', 'on')
    return bool(value)

def get_setting(section, key=None, default=None):
    config = load_config()
    
    if section == 'Content Sources':
        content_sources = config.get(section, {})
        if not isinstance(content_sources, dict):
            logging.warning(f"'Content Sources' setting is not a dictionary. Resetting to empty dict.")
            content_sources = {}
        return content_sources

    if key is None:
        return config.get(section, {})
    
    value = config.get(section, {}).get(key, default)
    
    # Handle boolean values
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        return parse_bool(value)
    
    # Validate URL if the key ends with 'url'
    if key.lower().endswith('url'):
        return validate_url(value)
    
    return value

# Update the set_setting function to handle boolean values correctly
def set_setting(section, key, value):
    config = load_config()
    
    # Ensure we preserve existing settings
    if section not in config:
        config[section] = {}
        # If this is a new section, initialize it with defaults from schema if available
        if section in SETTINGS_SCHEMA:
            for schema_key, schema_value in SETTINGS_SCHEMA[section].items():
                if schema_key != 'tab' and schema_key not in config[section]:
                    config[section][schema_key] = schema_value.get('default', '')
    
    if key.lower().endswith('url'):
        value = validate_url(value)
    # Convert boolean strings to actual booleans
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        value = parse_bool(value)
    
    # Update just the specific setting
    config[section][key] = value
    
    # Ensure we don't lose any existing settings
    ensure_settings_file()
    
    # Now save the updated config
    save_config(config)

def parse_string_dicts(obj):
    if isinstance(obj, dict):
        return {k: parse_string_dicts(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [parse_string_dicts(item) for item in obj]
    elif isinstance(obj, str):
        try:
            return parse_string_dicts(ast.literal_eval(obj))
        except (ValueError, SyntaxError):
            return obj
    else:
        return obj

def deserialize_config(config):
    if isinstance(config, dict):
        return {k: deserialize_config(v) for k, v in config.items() if not k.isdigit()}
    elif isinstance(config, list):
        if config and isinstance(config[0], list) and len(config[0]) == 2:
            # This is likely a preferred filter list
            return [tuple(item) for item in config]
        return [deserialize_config(item) for item in config]
    else:
        return config

def validate_url(url):
    if not url:
        logging.debug(f"Empty URL provided")
        return ''
    if not url.startswith(('http://', 'https://')):
        url = f'http://{url}'
    try:
        result = urlparse(url)
        if all([result.scheme, result.netloc]):
            return url
        else:
            logging.warning(f"Invalid URL structure: {url}")
            return ''
    except Exception as e:
        logging.error(f"Error parsing URL {url}: {str(e)}")
        return ''

def get_all_settings():
    config = load_config()
    
    # Ensure 'Content Sources' is a dictionary
    if 'Content Sources' in config:
        if not isinstance(config['Content Sources'], dict):
            logging.warning("'Content Sources' setting is not a dictionary. Resetting to empty dict.")
            config['Content Sources'] = {}
    else:
        config['Content Sources'] = {}
    
    return config

def get_scraping_settings():
    config = load_config()
    scraping_settings = {}

    versions = config.get('Scraping', {}).get('versions', {})
    for version, settings in versions.items():
        for key, value in settings.items():
            label = f"{version.capitalize()} - {key.replace('_', ' ').title()}"
            scraping_settings[f"{version}_{key}"] = (label, value)

    return scraping_settings

def get_jackett_settings():
    config = load_config()
    jackett_settings = {}
    instances = config.get('Jackett', {})
    for instance, settings in instances.items():
        jackett_settings[f"{instance}"] = (settings)
        
    return jackett_settings

def ensure_settings_file():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        config = {}
        is_new_file = True
    else:
        config = load_config()
        is_new_file = not config  # Check if the config is empty (existing but empty file)
    
    for section, section_data in SETTINGS_SCHEMA.items():
        if section not in config:
            config[section] = {}
        
        # Skip adding defaults for Scrapers, Content Sources, and Notifications
        if section in ['Scrapers', 'Content Sources', 'Notifications']:
            continue
        
        if isinstance(section_data, dict) and 'schema' in section_data:
            # Handle nested schemas
            for key, value in section_data['schema'].items():
                if key not in config[section]:
                    config[section][key] = value.get('default', {})
        else:
            for key, value in section_data.items():
                if key != 'tab' and key not in config[section]:
                    config[section][key] = value.get('default', '')

    # Ensure default scraping version only if there are no versions or it's a new file
    if 'Scraping' not in config:
        config['Scraping'] = {}
    if 'versions' not in config['Scraping'] or not config['Scraping']['versions'] or is_new_file:
        config['Scraping']['versions'] = {
            'Default': {
                'enable_hdr': False,
                'max_resolution': '1080p',
                'resolution_wanted': '<=',
                'resolution_weight': '3',
                'hdr_weight': '3',
                'similarity_weight': '3',
                'size_weight': '3',
                'bitrate_weight': '3',
                'preferred_filter_in': '',
                'preferred_filter_out': '',
                'filter_in': '',
                'filter_out': '',
                'min_size_gb': '0.01',
                'max_size_gb': ''
            }
        }

    # Ensure Debrid Provider is set to Torbox if not already set
    if 'Debrid Provider' not in config:
        config['Debrid Provider'] = {}
    if 'provider' not in config['Debrid Provider'] or not config['Debrid Provider']['provider']:
        config['Debrid Provider']['provider'] = 'RealDebrid'
    if 'api_key' not in config['Debrid Provider']:
        config['Debrid Provider']['api_key'] = 'demo_key'  # Initialize with a demo key for testing
    
    # Migrate RealDebrid API key if it exists
    if 'RealDebrid' in config and 'api_key' in config['RealDebrid']:
        if 'api_key' not in config['Debrid Provider'] or not config['Debrid Provider']['api_key']:
            config['Debrid Provider']['api_key'] = config['RealDebrid']['api_key']
            # Optionally set provider to RealDebrid since we found a key
            config['Debrid Provider']['provider'] = 'RealDebrid'

    save_config(config)