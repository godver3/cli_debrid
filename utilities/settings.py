import os
import logging
from urllib.parse import urlparse
import json
import ast
from utilities.settings_schema import SETTINGS_SCHEMA
from utilities.file_lock import FileLock
import time
import shutil # Added for copy2

# --- Start Dynamic Path Functions ---
def get_config_dir():
    """Dynamically gets the configuration directory from environment variable."""
    return os.environ.get('USER_CONFIG', '/user/config') # Default fallback for non-Windows or if env var not set

def get_config_file_path():
    """Dynamically gets the full path to the config.json file."""
    return os.path.join(get_config_dir(), 'config.json')

def get_lock_file_path():
    """Dynamically gets the full path to the .config.lock file."""
    return os.path.join(get_config_dir(), '.config.lock')
# --- End Dynamic Path Functions ---

# # Get config directory from environment variable with fallback
# CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config') # REMOVED

# # Update the path to use the environment variable
# CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json') # REMOVED
# LOCK_FILE = os.path.join(CONFIG_DIR, '.config.lock') # REMOVED

class Settings:
    def __init__(self, lock_file_path): # Modified to accept path dynamically
        self.lock_file_path = lock_file_path
        self.fd = None

        # Create lock file directory if it doesn't exist
        lock_dir = os.path.dirname(self.lock_file_path)
        os.makedirs(lock_dir, exist_ok=True)
        # Create empty lock file if it doesn't exist
        if not os.path.exists(self.lock_file_path):
            try:
                # Attempt to create the lock file
                with open(self.lock_file_path, 'w') as f:
                    f.write('') # Write something small to ensure creation
                logging.debug(f"Created missing lock file at {self.lock_file_path}")
            except Exception as e:
                logging.error(f"Failed to create lock file at {self.lock_file_path}: {e}")
                # If creation fails, we might not be able to proceed with locking
                # Depending on the desired behavior, could raise an error here

    def __enter__(self):
        try:
            # Open the lock file for locking mechanism
            self.fd = open(self.lock_file_path, 'r+')
            self.lock = FileLock(self.fd)
            self.lock.acquire()
            return self
        except Exception as e:
            logging.error(f"Failed to open or acquire lock on {self.lock_file_path}: {e}")
            # Ensure fd is closed if opening failed but fd was assigned
            if self.fd:
                try:
                    self.fd.close()
                except Exception:
                    pass # Ignore errors during cleanup
            self.fd = None # Ensure fd is None if lock failed
            raise # Re-raise the exception so the caller knows locking failed


    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            try:
                self.lock.release()
            except Exception as e:
                logging.error(f"Failed to release lock on {self.lock_file_path}: {e}")
            finally:
                try:
                    self.fd.close()
                    self.fd = None # Clear fd after closing
                except Exception as e:
                    logging.error(f"Failed to close lock file handle for {self.lock_file_path}: {e}")

def load_config():
    config_file_path = get_config_file_path()
    lock_file_path = get_lock_file_path()

    try:
        with Settings(lock_file_path): # Pass dynamic lock path
            if os.path.exists(config_file_path):
                try:
                    with open(config_file_path, 'r') as config_file:
                        config = json.load(config_file)

                    # Parse string representations in Content Sources (Keep this logic)
                    if 'Content Sources' in config:
                        for key, value in config['Content Sources'].items():
                            if isinstance(value, str):
                                try:
                                    parsed_value = json.loads(value)
                                    config['Content Sources'][key] = parsed_value
                                except json.JSONDecodeError:
                                    logging.warning(f"Keeping original string value for {key}: {value}")

                    return config
                except json.JSONDecodeError as e:
                    logging.error(f"Error decoding JSON from {config_file_path}: {str(e)}. Checking backup.")
                    # Try to load from backup
                    backup_file = config_file_path + '.backup'
                    if os.path.exists(backup_file):
                        try:
                            with open(backup_file, 'r') as backup:
                                config = json.load(backup)
                                logging.info(f"Successfully loaded config from backup: {backup_file}")
                                return config
                        except Exception as e_backup:
                            logging.error(f"Failed to load backup {backup_file}: {str(e_backup)}")
                    logging.warning(f"load_config: Backup failed or non-existent. Returning empty config.")
                    return {} # Return empty dict if primary and backup fail
                except Exception as e_read:
                     logging.error(f"Error reading config file {config_file_path}: {str(e_read)}")
                     return {} # Return empty dict on other read errors
            else:
                logging.warning(f"load_config: Config file not found at {config_file_path}. Returning empty config.")
                return {} # Return empty dict if file doesn't exist
    except Exception as e_lock:
        # This catches errors during lock acquisition (__enter__)
        logging.error(f"load_config: Failed to acquire lock or other error in Settings context for {lock_file_path}: {e_lock}. Returning empty config.")
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
    # Get the project root directory (assuming settings.py is in utilities)
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_file = os.path.join(root_dir, '.env')
    
    # If not in root, try config directory (use dynamic path)
    if not os.path.exists(env_file):
        env_file = os.path.join(get_config_dir(), '.env')
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
    config_file_path = get_config_file_path()
    lock_file_path = get_lock_file_path()

    try:
        with Settings(lock_file_path): # Pass dynamic lock path
            # Ensure Content Sources are saved as proper JSON (Keep this logic)
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
            backup_file = config_file_path + '.backup'
            if os.path.exists(config_file_path):
                try:
                    # Use shutil.copy2 to preserve metadata
                    shutil.copy2(config_file_path, backup_file)
                except Exception as e_backup:
                    logging.error(f"Failed to create backup {backup_file}: {str(e_backup)}")
            else:
                logging.debug(f"save_config: Original config file {config_file_path} not found, skipping backup.")


            # Save the new config
            try:
                # Create directory if it doesn't exist
                os.makedirs(os.path.dirname(config_file_path), exist_ok=True)
                with open(config_file_path, 'w') as config_file:
                    json.dump(config, config_file, indent=2)
                logging.debug(f"save_config: Successfully saved config to {config_file_path}")
            except Exception as e_save:
                logging.error(f"Failed to save config to {config_file_path}: {str(e_save)}")
                # If save failed and we have a backup, try to restore it
                if os.path.exists(backup_file):
                    logging.warning(f"Attempting to restore backup {backup_file} due to save failure.")
                    try:
                         # Use shutil.copy2 for restore as well
                        shutil.copy2(backup_file, config_file_path)
                    except Exception as e_restore:
                        logging.error(f"Failed to restore backup {backup_file}: {str(e_restore)}")
                # Propagate the original save error? Or just log? Currently just logs.

    except Exception as e_lock:
        # This catches errors during lock acquisition (__enter__)
        logging.error(f"save_config: Failed to acquire lock or other error in Settings context for {lock_file_path}: {e_lock}")
        # Depending on severity, might want to raise this


# Helper function to safely parse boolean values
def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', 'yes', '1', 'on')
    return bool(value)

def get_setting(section, key=None, default=None):
    # This function relies on load_config, which is now dynamic
    config = load_config()
    if section == 'Content Sources':
        content_sources = config.get(section, {})
        if not isinstance(content_sources, dict):
            logging.warning(f"get_setting: 'Content Sources' is not a dictionary (type: {type(content_sources)}). Resetting to empty dict.")
            content_sources = {}
        return content_sources

    if key is None:
        # Return the whole section, default to empty dict if section missing
        section_data = config.get(section, {})
        return section_data

    # Get specific key from section, default to provided default if section or key missing
    section_data = config.get(section, {})
    value = section_data.get(key, default)


    # Handle boolean values (Keep this logic)
    if isinstance(value, str) and value.lower() in ('true', 'false'):
         parsed = parse_bool(value)
         logging.debug(f"get_setting: Parsed boolean string '{value}' to {parsed}")
         return parsed

    # Validate URL if the key ends with 'url' (Keep this logic)
    if isinstance(key, str) and key.lower().endswith('url'):
        validated_url = validate_url(value)
        if validated_url != value:
             logging.debug(f"get_setting: Validated URL '{value}' to '{validated_url}'")
        return validated_url

    return value

# Update the set_setting function to handle boolean values correctly
def set_setting(section, key, value):
    # Load config dynamically
    config = load_config()

    # Ensure section exists
    if section not in config:
        config[section] = {}
        # Initialize with defaults from schema if new section and schema exists
        if section in SETTINGS_SCHEMA:
            schema_section = SETTINGS_SCHEMA[section]
            # Handle nested schema structure
            schema_items = schema_section.get('schema', schema_section)
            for schema_key, schema_value in schema_items.items():
                 # Ensure schema_value is a dict and has 'default'
                 if isinstance(schema_value, dict) and 'default' in schema_value:
                    if schema_key != 'tab' and schema_key not in config[section]:
                         config[section][schema_key] = schema_value.get('default', '') # Use empty string default if schema default missing
                 # Handle cases where schema value itself might be the default (less common)
                 elif not isinstance(schema_value, dict) and schema_key != 'tab' and schema_key not in config[section]:
                     # This case might need refinement based on schema structure
                     pass # Avoid setting if not a dict with 'default'

    # Validate URL
    if isinstance(key, str) and key.lower().endswith('url'):
        value = validate_url(value)
    # Convert boolean strings
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        value = parse_bool(value)

    # Update the specific setting
    config[section][key] = value

    # Ensure settings file structure exists (uses dynamic paths internally now)
    # This call might be redundant if load_config ensures structure, but keep for safety
    ensure_settings_file()

    # Save the updated config (uses dynamic paths internally now)
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
    if not url or not isinstance(url, str): # Check type
        # logging.debug(f"Empty or non-string URL provided: {url}")
        return ''
    if not url.startswith(('http://', 'https://')):
        url = f'http://{url}'
    try:
        result = urlparse(url)
        # Check scheme and netloc are present
        if all([result.scheme, result.netloc]):
            return url
        else:
            logging.warning(f"Invalid URL structure (scheme or netloc missing): {url}")
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
    config_file_path = get_config_file_path()
    logging.debug(f"ensure_settings_file: Checking existence of {config_file_path}")
    if not os.path.exists(config_file_path):
        logging.info(f"ensure_settings_file: Config file not found at {config_file_path}. Creating default config.")
        os.makedirs(os.path.dirname(config_file_path), exist_ok=True)
        config = {}
        is_new_file = True # Flag indicating we are creating defaults

        # Create default config structure from schema
        for section, section_data in SETTINGS_SCHEMA.items():
            if section not in config:
                config[section] = {}

            # Skip adding defaults for sections that should start empty or are handled specially
            if section in ['Scrapers', 'Content Sources', 'Notifications', 'Jackett']:
                 logging.debug(f"ensure_settings_file: Skipping default population for section '{section}'")
                 continue

            # Determine where the actual schema items are (could be nested under 'schema')
            schema_items = section_data.get('schema', section_data)

            if isinstance(schema_items, dict):
                for key, value_schema in schema_items.items():
                    # Ensure value_schema is a dict and has 'default'
                    if isinstance(value_schema, dict) and 'default' in value_schema:
                        if key != 'tab' and key not in config[section]:
                             config[section][key] = value_schema.get('default') # Use schema default
                             logging.debug(f"ensure_settings_file: Setting default for ['{section}']['{key}'].")
                    # Handle case where key points to another nested schema (e.g., 'versions' in 'Scraping')
                    elif isinstance(value_schema, dict) and 'schema' in value_schema:
                         if key not in config[section]:
                             config[section][key] = {} # Initialize nested dict
                         # Recursively apply defaults? Or handle specific cases like 'versions'?
                         # Handle 'versions' specifically for now
                         if section == 'Scraping' and key == 'versions':
                             # Add default 'Default' version only if creating a new file
                             if is_new_file:
                                version_defaults = value_schema['schema']
                                default_version_config = {}
                                for v_key, v_value_schema in version_defaults.items():
                                    if isinstance(v_value_schema, dict) and 'default' in v_value_schema:
                                        default_version_config[v_key] = v_value_schema.get('default')
                                config[section][key]['Default'] = default_version_config
                                logging.debug(f"ensure_settings_file: Added default 'Default' version settings.")
                         else:
                             # Generic handling for other nested schemas might be needed
                             pass
            else:
                logging.warning(f"ensure_settings_file: Schema items for section '{section}' is not a dictionary: {type(schema_items)}")


        # Specific default settings overrides/additions
        # Ensure default scraping version (handled above)
        # Ensure Debrid Provider defaults
        if 'Debrid Provider' not in config: config['Debrid Provider'] = {}
        if 'provider' not in config['Debrid Provider'] or not config['Debrid Provider']['provider']:
            config['Debrid Provider']['provider'] = 'RealDebrid'
        if 'api_key' not in config['Debrid Provider']:
            config['Debrid Provider']['api_key'] = 'demo_key' # Initialize with placeholder

        # Reverse Parser defaults
        if 'Reverse Parser' not in config:
             config['Reverse Parser'] = {
                 'version_terms': {'Default': []},
                 'default_version': 'Default',
                 'version_order': ['Default']
             }

        # Now try to load and merge .env config (Keep this logic, ensure load_env_config works)
        logging.debug("ensure_settings_file: Attempting to load and merge .env config.")
        env_config = load_env_config()
        if env_config:
            logging.info("ensure_settings_file: Merging config from .env file/environment.")
            config = merge_configs(config, env_config)
        else:
            logging.debug("ensure_settings_file: No .env config found to merge.")

        # Save the newly created default/merged config
        save_config(config)
        logging.info(f"ensure_settings_file: Saved default configuration to {config_file_path}")

    else:
        # If file exists, load it to ensure it's valid JSON and potentially merge missing defaults?
        # Current logic just checks existence. Let's add a load and save to ensure structure.
        logging.debug(f"ensure_settings_file: Config file {config_file_path} already exists.")
        config = load_config()
        if not config:
            # If load_config returned empty dict (e.g., due to corruption), trigger default creation
            logging.warning(f"ensure_settings_file: Existing config file {config_file_path} loaded empty or failed. Re-initializing defaults.")
            try:
                os.remove(config_file_path) # Remove potentially corrupt file
                # Potentially remove backup too? os.remove(config_file_path + '.backup')
                ensure_settings_file() # Recurse to create defaults
            except OSError as e:
                 logging.error(f"ensure_settings_file: Failed to remove corrupt config file {config_file_path}: {e}")
        else:
            # Optionally, merge defaults into existing config here if needed, then save
            # This could ensure older configs get new default keys.
            # Example: config = merge_defaults_into_existing(config, SETTINGS_SCHEMA)
            # save_config(config)
            pass # For now, just assume existing config is okay if loaded non-empty

    