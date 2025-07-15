#!/usr/bin/env python3
import os
import sys
import json

# Add the parent directory to the Python path so we can import settings
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from utilities.settings import get_setting
    
    # Get all credentials from settings
    config_data = {
        'tmdb_api_key': get_setting('TMDB', 'api_key', ''),
        'opensubtitles_username': get_setting('Subtitle Settings', 'opensubtitles_username', ''),
        'opensubtitles_password': get_setting('Subtitle Settings', 'opensubtitles_password', ''),
        'omdb_api_key': get_setting('OMDB', 'api_key', '')
    }
    
    # Output as JSON for easy parsing in shell script
    print(json.dumps(config_data))
        
except ImportError as e:
    # If we can't import settings, use empty fallback values
    fallback_config = {
        'tmdb_api_key': '',
        'opensubtitles_username': '',
        'opensubtitles_password': '',
        'omdb_api_key': ''
    }
    print(json.dumps(fallback_config))
    sys.exit(0)
except Exception as e:
    # If any other error occurs, use empty fallback values
    fallback_config = {
        'tmdb_api_key': '',
        'opensubtitles_username': '',
        'opensubtitles_password': '',
        'omdb_api_key': ''
    }
    print(json.dumps(fallback_config))
    sys.exit(0) 