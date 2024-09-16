import os
import logging
from urllib.parse import urlparse
import json
import ast
from settings_schema import SETTINGS_SCHEMA

CONFIG_FILE = '/user/config/config.json'

def load_config():
    #logging.debug("Starting load_config()")
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as config_file:
            try:
                config = json.load(config_file)
                #logging.debug(f"Raw loaded config: {json.dumps(config, indent=2)}")
                
                # Parse string representations in Content Sources
                if 'Content Sources' in config:
                    #logging.debug("Content Sources before parsing: %s", json.dumps(config['Content Sources'], indent=2))
                    for key, value in config['Content Sources'].items():
                        if isinstance(value, str):
                            try:
                                parsed_value = json.loads(value)
                                config['Content Sources'][key] = parsed_value
                                #logging.debug(f"Parsed value for {key}: {parsed_value}")
                            except json.JSONDecodeError:
                                # If it's not valid JSON, keep it as is
                                logging.debug(f"Keeping original string value for {key}: {value}")
                    #logging.debug("Content Sources after parsing: %s", json.dumps(config['Content Sources'], indent=2))
                
                #logging.debug(f"Final loaded config: {json.dumps(config, indent=2)}")
                return config
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON from {CONFIG_FILE}: {str(e)}. Using empty config.")
    logging.debug("Config file not found or empty, returning empty dict")
    return {}

def save_config(config):
    logging.debug("Starting save_config()")
    #logging.debug(f"Config before saving: {json.dumps(config, indent=2)}")
    
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
    
    with open(CONFIG_FILE, 'w') as config_file:
        json.dump(config, config_file, indent=2)
    
    #logging.debug(f"Final saved config: {json.dumps(config, indent=2)}")

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
    if section not in config:
        config[section] = {}
    if key.lower().endswith('url'):
        value = validate_url(value)
    # Convert boolean strings to actual booleans
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        value = parse_bool(value)
    config[section][key] = value

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

    save_config(config)