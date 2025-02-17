import os
import logging
from urllib.parse import urlparse
import json
import ast
from settings_schema import SETTINGS_SCHEMA
from utilities.file_lock import FileLock
import time

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')

# Update the path to use the environment variable
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LOCK_FILE = os.path.join(CONFIG_DIR, '.config.lock')

class Settings:
    def __init__(self, filename):
        self.filename = filename
        self.fd = None

        # Create lock file directory if it doesn't exist
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        # Create empty lock file if it doesn't exist
        if not os.path.exists(LOCK_FILE):
            open(LOCK_FILE, 'w').close()
        
    def __enter__(self):
        self.fd = open(self.filename, 'r+')
        self.lock = FileLock(self.fd)
        self.lock.acquire()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            self.lock.release()
            self.fd.close()

def load_config():
    with Settings(LOCK_FILE):
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

def load_env_config():
    """Load configuration from environment variable or .env file if it exists."""
    # First try to load from environment variable
    env_config = os.environ.get('CLI_DEBRID_CONFIG_JSON')
    if env_config:
        try:
            config = json.loads(env_config)
            logging.info("Configuration loaded from CLI_DEBRID_CONFIG_JSON environment variable")
            return config
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse CLI_DEBRID_CONFIG_JSON environment variable: {str(e)}")
            
    # Fallback to .env file if environment variable not found or invalid
    # Get the project root directory (where settings.py is located)
    root_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(root_dir, '.env')
    
    # If not in root, try config directory
    if not os.path.exists(env_file):
        env_file = os.path.join(CONFIG_DIR, '.env')
        if not os.path.exists(env_file):
            logging.info("No .env file found in root or config dir - using default configuration")
            return {}
    
    try:
        # First load traditional env vars
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and not line.startswith('CONFIG_JSON'):
                    try:
                        key, value = line.split('=', 1)
                        os.environ[key.strip()] = value.strip().strip("'").strip('"')
                    except ValueError:
                        continue
        
        logging.debug(f"Loaded traditional environment variables from {env_file}")
        
        # Now load JSON config
        with open(env_file, 'r') as f:
            lines = f.readlines()
        
        # Try multi-line format first
        in_json_block = False
        json_lines = []
        
        for line in lines:
            if 'CONFIG_JSON_START' in line:
                in_json_block = True
                continue
            elif 'CONFIG_JSON_END' in line:
                break
            elif in_json_block:
                json_lines.append(line)
        
        if json_lines:
            try:
                json_content = ''.join(json_lines)
                config = json.loads(json_content)
                logging.info(f"Configuration loaded from multi-line JSON block in {env_file}")
                return config
            except json.JSONDecodeError as e:
                logging.debug(f"Failed to parse multi-line JSON: {str(e)}")
        
        # If multi-line format fails, try single line format
        for line in lines:
            if line.startswith('CONFIG_JSON='):
                config_json = line[12:].strip()  # Remove CONFIG_JSON= prefix
                config = json.loads(config_json)
                logging.info(f"Configuration loaded from single-line CONFIG_JSON in {env_file}")
                return config
        
        logging.info(f"No valid JSON configuration found in {env_file} - using default configuration")
        return {}
            
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Failed to load or parse config from {env_file}: {str(e)}")
        return {}

def save_config(config):
    
    with Settings(LOCK_FILE):
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

def merge_configs(base, overlay):
    """Recursively merge two config dictionaries."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            merge_configs(base[key], value)
        else:
            base[key] = value
    return base

def ensure_settings_file():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        config = {}
        is_new_file = True

                # First create default config
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
            # Get the default version settings from schema
            version_defaults = SETTINGS_SCHEMA['Scraping']['versions']['schema']
            default_version_config = {}
            for key, value in version_defaults.items():
                default_version_config[key] = value.get('default')
            
            config['Scraping']['versions'] = {
                'Default': default_version_config
            }

        # Ensure Debrid Provider is set to Real-Debrid if not already set
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

        config['Reverse Parser'] = {
            'version_terms': {
                'Default': []
            },
            'default_version': 'Default',
            'version_order': ['Default']
        }

        # Now try to load and merge .env config
        env_config = load_env_config()
        if env_config:
            #logging.debug("Merging config from .env file")
            config = merge_configs(config, env_config)


        save_config(config)
    else:
        config = load_config()
        is_new_file = not config  # Check if the config is empty (existing but empty file)
    
