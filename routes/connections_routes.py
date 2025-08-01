from flask import Blueprint, render_template, flash, redirect, url_for
import requests
import os
from datetime import datetime
from utilities.settings import get_setting, get_all_settings
from typing import Dict, List, Any
from content_checkers.trakt import ensure_trakt_auth, get_trakt_headers, make_trakt_request, parse_trakt_list_url
from content_checkers.plex_watchlist import MyPlexAccount
import logging
import feedparser # Keep import for RSS
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

# Attempt to import DirectAPI - adjust path if necessary based on your project structure
try:
    # Assuming cli_battery is a sibling directory or installed package
    from cli_battery.app.direct_api import DirectAPI 
except ImportError:
    # Fallback if direct import doesn't work (e.g., running not from root)
    import sys
    # Add the parent directory to sys.path if cli_battery is adjacent
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) 
    if os.path.exists(os.path.join(parent_dir, 'cli_battery')):
        sys.path.insert(0, parent_dir)
        from cli_battery.app.direct_api import DirectAPI
    else:
        # If it still fails, create a dummy class to prevent crashes, but log error
        logging.error("Could not import DirectAPI from cli_battery. Metadata fallback for Overseer samples will not work.")
        class DirectAPI: # Dummy class
            def tmdb_to_imdb(*args, **kwargs): return None, None
            def get_movie_metadata(*args, **kwargs): return None, None
            def get_show_metadata(*args, **kwargs): return None, None

# --- Instantiate DirectAPI ---
# It's generally better to instantiate once if the class handles sessions internally
# Or instantiate within the function if session management requires it per-request
try:
    direct_api_instance = DirectAPI()
    logging.info("DirectAPI instance created successfully for connections_routes.")
except Exception as e:
     logging.error(f"Failed to instantiate DirectAPI in connections_routes: {e}", exc_info=True)
     direct_api_instance = None # Set to None if instantiation fails

connections_bp = Blueprint('connections', __name__)

# Add logging configuration if not already present
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

from .models import user_required # Added import

def check_cli_battery_connection():
    """Check connection to cli_battery service using environment variables."""
    try:
        battery_url_from_settings = get_setting('Metadata Battery', 'url')
        if not battery_url_from_settings:
            log.error("CLI Battery connection check failed: Battery URL not configured in settings.")
            return {
                'name': 'cli_battery',
                'connected': False,
                'error': 'Battery URL not configured in settings.',
                'details': {}
            }

        # Extract port for details, default if not in URL (though it should be)
        parsed_url = urlparse(battery_url_from_settings)
        battery_port_from_url = parsed_url.port if parsed_url.port else int(os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001'))


        response = requests.get(battery_url_from_settings, timeout=5) # Use the full URL from settings
        return {
            'name': 'cli_battery',
            'connected': response.status_code == 200,
            'error': None if response.status_code == 200 else f'Status code: {response.status_code}',
            'details': {
                'url': battery_url_from_settings,
                'port': battery_port_from_url
            }
        }
    except requests.Timeout:
        battery_url_display = get_setting('Metadata Battery', 'url', f'http://{os.environ.get("CLI_DEBRID_BATTERY_HOST", "localhost")}:{os.environ.get("CLI_DEBRID_BATTERY_PORT", "5001")}/')
        log.warning(f"CLI Battery connection check failed: Timeout while trying to connect to {battery_url_display}")
        parsed_url_display = urlparse(battery_url_display)
        battery_port_display = parsed_url_display.port if parsed_url_display.port else int(os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001'))
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': 'Connection timed out',
            'details': {
                'url': battery_url_display,
                'port': battery_port_display
            }
        }
    except requests.ConnectionError:
        battery_url_display = get_setting('Metadata Battery', 'url', f'http://{os.environ.get("CLI_DEBRID_BATTERY_HOST", "localhost")}:{os.environ.get("CLI_DEBRID_BATTERY_PORT", "5001")}/')
        log.warning(f"CLI Battery connection check failed: Connection refused by {battery_url_display}")
        parsed_url_display = urlparse(battery_url_display)
        battery_port_display = parsed_url_display.port if parsed_url_display.port else int(os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001'))
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': 'Connection refused',
            'details': {
                'url': battery_url_display,
                'port': battery_port_display
            }
        }
    except Exception as e:
        battery_url_display = get_setting('Metadata Battery', 'url', f'http://{os.environ.get("CLI_DEBRID_BATTERY_HOST", "localhost")}:{os.environ.get("CLI_DEBRID_BATTERY_PORT", "5001")}/')
        log.error(f"CLI Battery connection check failed: An unexpected error occurred while trying to connect to {battery_url_display}. Error: {str(e)}", exc_info=True)
        parsed_url_display = urlparse(battery_url_display)
        battery_port_display = parsed_url_display.port if parsed_url_display.port else int(os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001'))
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': str(e),
            'details': {
                'url': battery_url_display,
                'port': battery_port_display
            }
        }

def check_plex_connection():
    """Check connection to Plex if configured and verify libraries."""
    plex_url = get_setting('Plex', 'url')
    plex_token = get_setting('Plex', 'token')
    movie_libraries = get_setting('Plex', 'movie_libraries')
    shows_libraries = get_setting('Plex', 'shows_libraries')
    
    if not plex_url or not plex_token:
        return None  # Plex not configured
        
    try:
        # Ensure URL ends with /identity
        if not plex_url.endswith('/'):
            plex_url += '/'
        identity_url = f"{plex_url}identity"
        
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'  # Request JSON response
        }
        response = requests.get(identity_url, headers=headers, timeout=5)
        
        if response.status_code != 200:
            return {
                'name': 'Plex',
                'connected': False,
                'error': f'Status code: {response.status_code}',
                'details': {
                    'url': plex_url,
                    'identity_url': identity_url
                }
            }
            
        # If we have libraries configured, check them
        libraries_to_check = []
        if movie_libraries:
            libraries_to_check.extend(movie_libraries.split(','))
        if shows_libraries:
            libraries_to_check.extend(shows_libraries.split(','))
            
        if not libraries_to_check:
            return {
                'name': 'Plex',
                'connected': True,
                'error': None,
                'details': {
                    'url': plex_url,
                    'identity_url': identity_url
                }
            }
            
        # Get list of libraries
        library_url = f"{plex_url}library/sections"
        library_response = requests.get(library_url, headers=headers, timeout=5)
        
        if library_response.status_code != 200:
            return {
                'name': 'Plex',
                'connected': False,
                'error': f'Failed to get libraries list. Status code: {library_response.status_code}',
                'details': {
                    'url': plex_url,
                    'library_url': library_url
                }
            }
            
        try:
            libraries = library_response.json()
            available_libraries = {lib['title']: lib['key'] for lib in libraries['MediaContainer']['Directory']}
            # Create a lowercase version of available library titles for case-insensitive check
            available_libraries_lower = {title.lower(): key for title, key in available_libraries.items()}
        except (ValueError, KeyError) as e:
            # If JSON parsing fails or expected structure isn't found
            return {
                'name': 'Plex',
                'connected': False,
                'error': 'Failed to parse library response',
                'details': {
                    'url': plex_url,
                    'library_url': library_url,
                    'error_details': str(e)
                }
            }
            
        # Check each configured library case-insensitively
        missing_libraries = []
        found_keys = set() # Track keys found to ensure config points to valid libraries

        for lib_name_or_id in libraries_to_check: # Renamed variable for clarity
            lib_name_or_id = lib_name_or_id.strip()
            lib_lower = lib_name_or_id.lower()

            # Check if lowercase name exists or if it's a valid key
            if lib_lower in available_libraries_lower:
                 found_keys.add(available_libraries_lower[lib_lower])
            elif lib_name_or_id in available_libraries.values(): # Check if it's a key
                 found_keys.add(lib_name_or_id)
            else:
                missing_libraries.append(lib_name_or_id)

        # Report missing libraries based on original input names/IDs
        if missing_libraries:
            return {
                'name': 'Plex',
                'connected': False, # Connection is fine, but config is wrong
                'error': f'Configured libraries not found: {", ".join(missing_libraries)}',
                'details': {
                    'url': plex_url,
                    'available_libraries': list(available_libraries.keys()), # Show actual names
                    'configured_libraries': libraries_to_check, # Show what user configured
                    'missing_libraries': missing_libraries
                }
            }
            
        # If no libraries were missing, the connection and configuration are valid
        return {
            'name': 'Plex',
            'connected': True,
            'error': None,
            'details': {
                'url': plex_url,
                'available_libraries': list(available_libraries.keys()),
                'configured_libraries': libraries_to_check,
                 # Optionally add 'found_library_keys': list(found_keys) for debugging
            }
        }
        
    except requests.Timeout:
        return {
            'name': 'Plex',
            'connected': False,
            'error': 'Connection timed out',
            'details': {
                'url': plex_url,
                'identity_url': identity_url
            }
        }
    except requests.ConnectionError:
        return {
            'name': 'Plex',
            'connected': False,
            'error': 'Connection refused',
            'details': {
                'url': plex_url,
                'identity_url': identity_url
            }
        }
    except Exception as e:
        return {
            'name': 'Plex',
            'connected': False,
            'error': str(e),
            'details': {
                'url': plex_url,
                'identity_url': identity_url
            }
        }

def check_jellyfin_connection():
    """Check connection to Jellyfin/Emby if configured."""
    jellyfin_url = get_setting('Debug', 'emby_jellyfin_url')
    jellyfin_token = get_setting('Debug', 'emby_jellyfin_token')

    if not jellyfin_url or not jellyfin_token:
        return None  # Not configured

    try:
        # Ensure URL ends with a slash
        if not jellyfin_url.endswith('/'):
            jellyfin_url += '/'
        
        system_info_url = f"{jellyfin_url}System/Info"
        
        headers = {
            'X-Emby-Token': jellyfin_token,
            'Accept': 'application/json'
        }
        
        response = requests.get(system_info_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            try:
                server_info = response.json()
                return {
                    'name': 'Jellyfin/Emby',
                    'connected': True,
                    'error': None,
                    'details': {
                        'url': jellyfin_url,
                        'server_name': server_info.get('ServerName'),
                        'version': server_info.get('Version')
                    }
                }
            except ValueError:
                 return {
                    'name': 'Jellyfin/Emby',
                    'connected': False,
                    'error': 'Failed to parse JSON response',
                    'details': {
                        'url': jellyfin_url
                    }
                }
        else:
            return {
                'name': 'Jellyfin/Emby',
                'connected': False,
                'error': f'Status code: {response.status_code}',
                'details': { 'url': jellyfin_url }
            }

    except requests.Timeout:
        return {
            'name': 'Jellyfin/Emby',
            'connected': False,
            'error': 'Connection timed out',
            'details': { 'url': jellyfin_url }
        }
    except requests.ConnectionError:
        return {
            'name': 'Jellyfin/Emby',
            'connected': False,
            'error': 'Connection refused',
            'details': { 'url': jellyfin_url }
        }
    except Exception as e:
        return {
            'name': 'Jellyfin/Emby',
            'connected': False,
            'error': str(e),
            'details': { 'url': jellyfin_url }
        }

def check_mounted_files_connection():
    """Check if mounted files location is accessible."""
    # Try original_files_path first, then fall back to Plex mounted_file_location
    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
        mount_path = get_setting('File Management', 'original_files_path')
        source = 'File Management'
    else:
        mount_path = get_setting('Plex', 'mounted_file_location')
        source = 'Plex'
    
    if not mount_path:
        return None  # No mount path configured
        
    try:
        # Check if path exists and is accessible
        if os.path.exists(mount_path) and os.access(mount_path, os.R_OK):
            # Try to list directory contents to verify mount is responsive
            os.listdir(mount_path)
            return {
                'name': 'Mounted Files',
                'connected': True,
                'error': None,
                'details': {
                    'path': mount_path,
                    'source': source
                }
            }
        else:
            return {
                'name': 'Mounted Files',
                'connected': False,
                'error': 'Mount path not accessible',
                'details': {
                    'path': mount_path,
                    'source': source
                }
            }
    except PermissionError:
        return {
            'name': 'Mounted Files',
            'connected': False,
            'error': 'Permission denied',
            'details': {
                'path': mount_path,
                'source': source
            }
        }
    except Exception as e:
        return {
            'name': 'Mounted Files',
            'connected': False,
            'error': str(e),
            'details': {
                'path': mount_path,
                'source': source
            }
        }

def check_phalanx_db_connection():
    """Check connection to phalanx_db service."""
    # Check if phalanx db is enabled
    if not get_setting('UI Settings', 'enable_phalanx_db', default=False):
        return None # Return None if the service is disabled

    # --- Use the EXACT same env logic as PhalanxDBClassManager ---
    try:
        phalanx_port = int(os.environ.get('CLI_DEBRID_PHALANX_PORT', 8888))
    except ValueError:
        phalanx_port = 8888

    # Check for the new host environment variable first (for Docker containers)
    phalanx_host = os.environ.get('CLI_DEBRID_PHALANX_HOST')
    if phalanx_host:
        phalanx_base_url = f'http://{phalanx_host}'
    else:
        # Fall back to the old URL environment variable
        phalanx_base_url = os.environ.get('CLI_DEBRID_PHALANX_URL', 'http://localhost')

    phalanx_base_url = phalanx_base_url.rstrip('/')
    url = f'{phalanx_base_url}:{phalanx_port}'
    # --- End env logic ---

    try:
        response = requests.get(url, timeout=5) # Increased timeout to 5s

        # A 404 with "Cannot GET /" is actually a success case here
        # as it means we can reach the service
        if response.status_code == 404 and "Cannot GET /" in response.text:
            return {
                'name': 'Phalanx DB',
                'connected': True,
                'error': None,
                'details': {
                    'url': url,
                    'host': phalanx_base_url,
                    'port': phalanx_port
                }
            }
        else:
            # Handle cases where the service responds but not with the expected 404
            return {
                'name': 'Phalanx DB',
                'connected': False,
                'error': f'Unexpected response: Status {response.status_code}',
                'details': {
                    'url': url,
                    'host': phalanx_base_url,
                    'port': phalanx_port,
                    'response_text': response.text[:200] # Include beginning of response text
                }
            }
            
    except requests.Timeout:
        return {
            'name': 'Phalanx DB',
            'connected': False,
            'error': 'Connection timed out (5s)',
            'details': {
                'url': url,
                'host': phalanx_base_url,
                'port': phalanx_port
            }
        }
    except requests.ConnectionError:
        return {
            'name': 'Phalanx DB',
            'connected': False,
            'error': f'Connection refused on {phalanx_base_url}',
            'details': {
                'url': url,
                'host': phalanx_base_url,
                'port': phalanx_port
            }
        }
    except Exception as e:
         return {
            'name': 'Phalanx DB',
            'connected': False,
            'error': f'Error connecting to {phalanx_base_url}: {str(e)}',
            'details': {
                'url': url,
                'host': phalanx_base_url,
                'port': phalanx_port
            }
        }

def check_scraper_connection(scraper_id, scraper_config):
    """Check connection to a specific scraper."""
    scraper_type = scraper_config.get('type')
    
    if not scraper_type:
        return None
        
    base_response = {
        'name': f'{scraper_type} ({scraper_id})',
        'connected': False,
        'error': None,
        'details': {}
    }
    
    # Skip check if scraper is not enabled
    if not scraper_config.get('enabled', False):
        return None
        
    try:
        if scraper_type == 'Zilean':
            url = scraper_config.get('url', '').strip()
            if not url:
                base_response['error'] = 'URL not configured'
                return base_response
                
            # Append healthcheck endpoint if not present
            if not url.endswith('/healthchecks/ping'):
                url = url.rstrip('/') + '/healthchecks/ping'
                
            response = requests.get(url, timeout=5)
            base_response['connected'] = response.status_code == 200 and 'Pong' in response.text
            if not base_response['connected']:
                if response.status_code != 200:
                    base_response['error'] = f'Status code: {response.status_code}'
                else:
                    base_response['error'] = 'Invalid response format (expected "Pong")'
            base_response['details'].update({
                'url': url,
                'response': response.text.strip() if response.status_code == 200 else None
            })
            
        elif scraper_type == 'Jackett':
            url = scraper_config.get('url', '').strip()
            api_key = scraper_config.get('api', '').strip()
            
            if not url or not api_key:
                base_response['error'] = 'URL or API key not configured'
                return base_response
                
            # Test Jackett connection by getting caps
            test_url = f"{url.rstrip('/')}/api/v2.0/indexers/all/results/torznab/api?apikey={api_key}&t=caps"
            response = requests.get(test_url, timeout=5)
            base_response['connected'] = response.status_code == 200
            if not base_response['connected']:
                base_response['error'] = f'Status code: {response.status_code}'
            base_response['details'].update({
                'url': url,
                'enabled_indexers': scraper_config.get('enabled_indexers', '')
            })
            
        elif scraper_type == 'MediaFusion':
            url = scraper_config.get('url', '').strip()
            if not url:
                base_response['error'] = 'URL not configured'
                return base_response
                
            response = requests.get(url, timeout=5)
            base_response['connected'] = response.status_code == 200
            if not base_response['connected']:
                base_response['error'] = f'Status code: {response.status_code}'
            base_response['details']['url'] = url
            
        elif scraper_type == 'Torrentio':
            from scraper.torrentio import scrape_torrentio_instance
            
            # Test with a known movie (The Dark Knight)
            try:
                results = scrape_torrentio_instance(
                    instance='Torrentio',
                    settings=scraper_config,
                    imdb_id='tt0468569',
                    title='The Dark Knight',
                    year=2008,
                    content_type='movie'
                )
                base_response['connected'] = len(results) > 0
                if not base_response['connected']:
                    base_response['error'] = 'No results found from test search'
                base_response['details'].update({
                    'test_movie': 'The Dark Knight (tt0468569)',
                    'results_found': len(results)
                })
            except Exception as e:
                base_response['connected'] = False
                base_response['error'] = str(e)
                
        elif scraper_type == 'Nyaa':
            from scraper.nyaa import test_nyaa_scraper
            
            # Test with a well-known anime movie
            try:
                results = test_nyaa_scraper(
                    title='Akira',
                    year=1988,
                    content_type='movie',
                    categories=scraper_config.get('categories', '1_2'),
                    filter=scraper_config.get('filter', '0')
                )
                base_response['connected'] = len(results) > 0
                if not base_response['connected']:
                    base_response['error'] = 'No results found from test search'
                base_response['details'].update({
                    'test_movie': 'Akira (1988)',
                    'results_found': len(results)
                })
            except Exception as e:
                base_response['connected'] = False
                base_response['error'] = str(e)
                
        elif scraper_type == 'Prowlarr':
            url = scraper_config.get('url', '').strip()
            api_key = scraper_config.get('api_key', '').strip()
            
            if not url or not api_key:
                base_response['error'] = 'URL or API key not configured'
                return base_response
                
            # Test Prowlarr connection by getting system status
            test_url = f"{url.rstrip('/')}/api/v1/system/status"
            headers = {'X-Api-Key': api_key}
            response = requests.get(test_url, headers=headers, timeout=5)
            base_response['connected'] = response.status_code == 200
            if not base_response['connected']:
                base_response['error'] = f'Status code: {response.status_code}'
            base_response['details'].update({
                'url': url,
                'tags': scraper_config.get('tags', '')
            })
                
        else:
            base_response['error'] = f'Unknown scraper type: {scraper_type}'
            
    except requests.Timeout:
        base_response['error'] = 'Connection timed out'
    except requests.ConnectionError:
        base_response['error'] = 'Connection refused'
    except Exception as e:
        base_response['error'] = str(e)
        
    return base_response

def check_nyaa_scrapers_only():
    """Check only Nyaa scrapers to avoid proxy conflicts with other connection checks."""
    from queues.config_manager import load_config
    
    config = load_config()
    scrapers = config.get('Scrapers', {})
    scraper_statuses = []

    enabled_scrapers = {
        scraper_id: scraper_config 
        for scraper_id, scraper_config in scrapers.items() 
        if scraper_config.get('enabled', False) and scraper_config.get('type') == 'Nyaa'
    }

    if not enabled_scrapers:
        return []

    # Run Nyaa scrapers sequentially to avoid proxy conflicts
    for scraper_id, scraper_config in enabled_scrapers.items():
        try:
            status = check_scraper_connection(scraper_id, scraper_config)
            if status:
                scraper_statuses.append(status)
        except Exception as exc:
            log.error(f'Nyaa scraper {scraper_id} check generated an exception: {exc}', exc_info=True)
            scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))
                
    return scraper_statuses

def check_non_nyaa_scrapers():
    """Check all scrapers except Nyaa in parallel."""
    from queues.config_manager import load_config
    
    config = load_config()
    scrapers = config.get('Scrapers', {})
    scraper_statuses = []

    enabled_scrapers = {
        scraper_id: scraper_config 
        for scraper_id, scraper_config in scrapers.items() 
        if scraper_config.get('enabled', False) and scraper_config.get('type') != 'Nyaa'
    }

    if not enabled_scrapers:
        return []

    with ThreadPoolExecutor(max_workers=len(enabled_scrapers)) as executor:
        future_to_scraper = {
            executor.submit(check_scraper_connection, scraper_id, scraper_config): (scraper_id, scraper_config)
            for scraper_id, scraper_config in enabled_scrapers.items()
        }
        
        for future in as_completed(future_to_scraper):
            scraper_id, scraper_config = future_to_scraper[future]
            try:
                status = future.result(timeout=10) # 10-second timeout per scraper
                if status:
                    scraper_statuses.append(status)
            except TimeoutError:
                log.warning(f'Scraper check for {scraper_id} timed out.')
                scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))
            except Exception as exc:
                log.error(f'Scraper {scraper_id} check generated an exception: {exc}', exc_info=True)
                scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))
                
    return scraper_statuses

def check_scrapers_connections():
    """Check connections to all enabled scrapers, running Nyaa first to avoid proxy conflicts."""
    from queues.config_manager import load_config
    
    config = load_config()
    scrapers = config.get('Scrapers', {})
    scraper_statuses = []

    enabled_scrapers = {
        scraper_id: scraper_config 
        for scraper_id, scraper_config in scrapers.items() 
        if scraper_config.get('enabled', False)
    }

    if not enabled_scrapers:
        return []

    # Separate Nyaa scrapers from others to avoid proxy conflicts
    nyaa_scrapers = {}
    other_scrapers = {}
    
    for scraper_id, scraper_config in enabled_scrapers.items():
        if scraper_config.get('type') == 'Nyaa':
            nyaa_scrapers[scraper_id] = scraper_config
        else:
            other_scrapers[scraper_id] = scraper_config

    # Run Nyaa scrapers first (sequentially) to avoid proxy conflicts
    for scraper_id, scraper_config in nyaa_scrapers.items():
        try:
            status = check_scraper_connection(scraper_id, scraper_config)
            if status:
                scraper_statuses.append(status)
        except Exception as exc:
            log.error(f'Nyaa scraper {scraper_id} check generated an exception: {exc}', exc_info=True)
            scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))

    # Run all other scrapers in parallel
    if other_scrapers:
        with ThreadPoolExecutor(max_workers=len(other_scrapers)) as executor:
            future_to_scraper = {
                executor.submit(check_scraper_connection, scraper_id, scraper_config): (scraper_id, scraper_config)
                for scraper_id, scraper_config in other_scrapers.items()
            }
            
            # We don't use a timeout on as_completed to avoid raising an exception
            # that would stop us from processing already completed results.
            # Instead, future.result(timeout=...) is used inside the loop.
            for future in as_completed(future_to_scraper):
                scraper_id, scraper_config = future_to_scraper[future]
                try:
                    # Use a timeout for getting the result of each future
                    status = future.result(timeout=10) # 10-second timeout per scraper
                    if status:
                        scraper_statuses.append(status)
                except TimeoutError:
                    log.warning(f'Scraper check for {scraper_id} timed out.')
                    scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))
                except Exception as exc:
                    log.error(f'Scraper {scraper_id} check generated an exception: {exc}', exc_info=True)
                    scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))
                
    return scraper_statuses

def create_timeout_status(scraper_type: str, scraper_id: str) -> Dict[str, Any]:
    """Generates a standardized timeout status for scraper checks."""
    return {
        'name': f'{scraper_type} ({scraper_id})',
        'connected': False,
        'error': 'Check timed out or failed with an exception.',
        'details': {}
    }

def check_content_source_connection(source_id: str, source_config: Dict[str, Any]) -> Dict[str, Any]:
    """Check connection to a specific content source and fetch a sample."""
    source_type = source_id.split('_')[0]
    
    if not source_config.get('enabled', False):
        return None
        
    display_name = source_config.get('display_name')
    name = f"{display_name} ({source_id})" if display_name else source_id
        
    base_response = {
        'name': name,
        'connected': False,
        'error': None,
        'details': {
            'type': source_type,
            'identifier': source_id,
            'media_type': source_config.get('media_type', 'All'),
            'sample_data': None,
            'sample_error': None
        }
    }
    
    try:
        # --- MDBList ---
        if source_type == 'MDBList':
            urls = source_config.get('urls', '').strip()
            if not urls:
                base_response['error'] = 'URLs not configured'
                base_response['connected'] = False
                return base_response
                
            # Test each URL
            url_list = [url.strip() for url in urls.split(',') if url.strip()]
            if not url_list:
                base_response['error'] = 'No valid URLs configured'
                base_response['connected'] = False
                return base_response
                
            failed_urls = []
            successful_urls = []
            for url in url_list:
                try:
                    # Add headers to mimic a browser request
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    # Ensure URL has proper format
                    if not url.startswith('http://') and not url.startswith('https://'):
                        url = 'https://' + url
                    if not url.endswith('/json'):
                        url = url.rstrip('/') + '/json'
                        
                    response = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
                    
                    # MDBList URLs should return JSON data
                    try:
                        response.json()  # Verify JSON response
                        if response.status_code == 200:
                            successful_urls.append(url)
                        else:
                            failed_urls.append(f"{url} (Status: {response.status_code})")
                    except ValueError:
                        failed_urls.append(f"{url} (Invalid JSON response)")
                except requests.exceptions.RequestException as e:
                    failed_urls.append(f"{url} (Error: {str(e)})")
                    
            base_response['connected'] = len(successful_urls) > 0
            if not base_response['connected']:
                base_response['error'] = 'Failed to connect to any URLs'
            elif len(failed_urls) > 0:
                base_response['error'] = f'Connected to {len(successful_urls)} URLs, but {len(failed_urls)} failed'
            else:
                base_response['error'] = f'Successfully connected to all {len(successful_urls)} URLs'
                
            base_response['details'].update({
                'successful_urls': successful_urls,
                'failed_urls': failed_urls,
                'total_urls': len(url_list),
                'versions': source_config.get('versions', {'Default': True})
            })
            
            # --- Fetch Sample Data for MDBList (if connected) ---
            if base_response['connected'] and successful_urls:
                sample_items = []
                try:
                    sample_url = successful_urls[0]
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    sample_response = requests.get(sample_url, headers=headers, timeout=5, allow_redirects=True)
                    sample_response.raise_for_status()
                    data = sample_response.json()
                    
                    if isinstance(data, list):
                        for item in data[:3]: 
                            title = item.get('title', 'Unknown Title')
                            year = item.get('year')
                            item_type = item.get('mediatype', 'unknown').lower()
                            display_type = 'TV' if item_type in ['show', 'tv'] else item_type.capitalize()
                            
                            display_text = f"{title} ({year})" if year else title
                            sample_items.append(f"[{display_type}] {display_text}")
                    else:
                         base_response['details']['sample_error'] = "Unexpected JSON format (not a list)"
                    base_response['details']['sample_data'] = sample_items if sample_items else ["No items found or could not parse."]

                except Exception as e:
                    log.warning(f"Failed to fetch sample for MDBList {source_id}: {e}", exc_info=True)
                    base_response['details']['sample_error'] = f"Failed to fetch sample: {str(e)}"
            # --- End MDBList Sample Fetch ---

        # --- Trakt Sources (Watchlist, Lists, Friends, Collection, Special Lists) ---
        elif source_type in ['Trakt Watchlist', 'Trakt Lists', 'Friends Trakt Watchlist', 'Trakt Collection', 'Special Trakt Lists']:
            access_token = ensure_trakt_auth()
            if not access_token:
                base_response['error'] = 'Failed to authenticate with Trakt'
                base_response['connected'] = False
                return base_response
            
            # Get headers with the valid token
            headers = get_trakt_headers()
            if not headers:
                base_response['error'] = 'Failed to get valid Trakt headers'
                base_response['connected'] = False
                return base_response
            
            # Test the connection with a simple API call
            try:
                response = make_trakt_request('get', '/sync/last_activities')
                if response and response.status_code == 200:
                    base_response['connected'] = True
                else:
                    base_response['error'] = f'Failed to connect to Trakt API: Status {response.status_code if response else "unknown"}'
                    base_response['connected'] = False
            except Exception as e:
                base_response['error'] = f'Trakt API error: {str(e)}'
                base_response['connected'] = False
            
            # --- Fetch Sample Data for Trakt (if connected) ---
            if base_response['connected']:
                sample_items = []
                try:
                    # --- Trakt Watchlist Sample ---
                    if source_type == 'Trakt Watchlist':
                        # Fetch both movies and shows for the sample, limit each
                        endpoints = {'movies': '/sync/watchlist/movies?limit=2', 'shows': '/sync/watchlist/shows?limit=2'}
                        for media_type, endpoint in endpoints.items():
                            items_response = make_trakt_request('get', endpoint)
                            if items_response and items_response.status_code == 200:
                                data = items_response.json()
                                for item in data:
                                    # Extract nested object ('movie' or 'show')
                                    item_data = item.get(media_type[:-1]) 
                                    if item_data:
                                        title = item_data.get('title', 'Unknown Title')
                                        year = item_data.get('year')
                                        display_type = "Movie" if media_type == 'movies' else "Show"
                                        display_text = f"{title} ({year})" if year else title
                                        sample_items.append(f"[{display_type}] {display_text}")
                            else:
                                log.warning(f"Failed to fetch {media_type} for Trakt Watchlist sample. Status: {items_response.status_code if items_response else 'N/A'}")
                    
                    # --- Trakt Lists Sample ---
                    elif source_type == 'Trakt Lists':
                        # Corrected: Use 'trakt_lists' key instead of 'urls'
                        list_urls_str = source_config.get('trakt_lists', '').strip()
                        if list_urls_str:
                             # Use the first URL for the sample
                            first_url = list_urls_str.split(',')[0].strip()
                            list_info = parse_trakt_list_url(first_url)
                            if list_info:
                                username = list_info.get('username')
                                list_id = list_info.get('list_id')
                                if username and list_id:
                                    endpoint = f"/users/{username}/lists/{list_id}/items?limit=3"
                                    items_response = make_trakt_request('get', endpoint)
                                    if items_response and items_response.status_code == 200:
                                        data = items_response.json()
                                        for item in data:
                                            item_type_key = None
                                            if 'movie' in item: item_type_key = 'movie'
                                            elif 'show' in item: item_type_key = 'show'
                                            elif 'episode' in item: item_type_key = 'episode' 

                                            if item_type_key:
                                                item_data = item.get(item_type_key)
                                                if item_type_key == 'episode':
                                                    item_data = item.get('show') 
                                                    display_type = "Show" 
                                                elif item_type_key == 'show':
                                                    display_type = "Show"
                                                else:
                                                    display_type = "Movie"

                                                if item_data:
                                                    title = item_data.get('title', 'Unknown Title')
                                                    year = item_data.get('year')
                                                    display_text = f"{title} ({year})" if year else title
                                                    sample_items.append(f"[{display_type}] {display_text}")
                                    else:
                                        log.warning(f"Failed to fetch items for Trakt List {list_id}. Status: {items_response.status_code if items_response else 'N/A'}")
                                        # Add error to sample details if fetch failed
                                        base_response['details']['sample_error'] = f"Failed to fetch sample from list (Status: {items_response.status_code if items_response else 'N/A'})"
                                else:
                                    base_response['details']['sample_error'] = "Could not parse username/list ID from URL."
                            else:
                                base_response['details']['sample_error'] = "Could not parse Trakt list URL."
                        else:
                             # This error message should now be correct if 'trakt_lists' is missing/empty
                             base_response['details']['sample_error'] = "No URLs configured for Trakt List source."

                    # --- Trakt Collection Sample (Existing - No change needed) ---
                    elif source_type == 'Trakt Collection':
                         movies_resp = make_trakt_request('get', '/sync/collection/movies?limit=2')
                         shows_resp = make_trakt_request('get', '/sync/collection/shows?limit=1')
                         if movies_resp and movies_resp.status_code == 200:
                             for item in movies_resp.json():
                                 movie = item.get('movie')
                                 if movie:
                                     title = movie.get('title', 'Unknown Title')
                                     year = movie.get('year')
                                     sample_items.append(f"[Movie] {title} ({year})" if year else f"[Movie] {title}")
                         if shows_resp and shows_resp.status_code == 200:
                              for item in shows_resp.json():
                                  show = item.get('show')
                                  if show:
                                      title = show.get('title', 'Unknown Title')
                                      year = show.get('year')
                                      sample_items.append(f"[Show] {title} ({year})" if year else f"[Show] {title}")

                    # --- Special Trakt Lists Sample ---
                    elif source_type == 'Special Trakt Lists':
                        selected_list_types = source_config.get('special_list_type', [])
                        media_type_filter = source_config.get('media_type', 'All').lower()
                        
                        if not selected_list_types:
                            base_response['details']['sample_error'] = "No special list types configured"
                        else:
                            # Sample from the first configured list type
                            first_list_type = selected_list_types[0]
                            sample_items = []
                            
                            # Define API endpoints for special lists
                            special_list_api_details = {
                                "Trending": {"movies": "/movies/trending", "shows": "/shows/trending"},
                                "Popular": {"movies": "/movies/popular", "shows": "/shows/popular"},
                                "Anticipated": {"movies": "/movies/anticipated", "shows": "/shows/anticipated"},
                                "Box Office": {"movies": "/movies/boxoffice", "shows": None},
                                "Played": {"movies": "/movies/played/weekly", "shows": "/shows/played/weekly"},
                                "Watched": {"movies": "/movies/watched/weekly", "shows": "/shows/watched/weekly"},
                                "Collected": {"movies": "/movies/collected/weekly", "shows": "/shows/collected/weekly"},
                                "Favorited": {"movies": "/movies/favorited/weekly", "shows": "/shows/favorited/weekly"}
                            }
                            
                            if first_list_type in special_list_api_details:
                                api_paths = special_list_api_details[first_list_type]
                                endpoints_to_call = []
                                
                                if media_type_filter in ['movies', 'all'] and api_paths.get("movies"):
                                    endpoints_to_call.append(api_paths["movies"])
                                if media_type_filter in ['shows', 'all'] and api_paths.get("shows"):
                                    endpoints_to_call.append(api_paths["shows"])
                                
                                for endpoint in endpoints_to_call[:2]:  # Limit to 2 endpoints for sample
                                    if endpoint:
                                        sample_response = make_trakt_request('get', f"{endpoint}?limit=2")
                                        if sample_response and sample_response.status_code == 200:
                                            try:
                                                data = sample_response.json()
                                                for item in data:
                                                    # Handle different response structures
                                                    item_data = None
                                                    display_type = "Unknown"
                                                    
                                                    if 'movie' in item:
                                                        item_data = item.get('movie')
                                                        display_type = "Movie"
                                                    elif 'show' in item:
                                                        item_data = item.get('show')
                                                        display_type = "Show"
                                                    
                                                    if item_data:
                                                        title = item_data.get('title', 'Unknown Title')
                                                        year = item_data.get('year')
                                                        display_text = f"{title} ({year})" if year else title
                                                        sample_items.append(f"[{display_type}] {display_text}")
                                            except Exception as e:
                                                log.warning(f"Failed to parse sample data from {endpoint}: {e}")
                            else:
                                base_response['details']['sample_error'] = f"Unknown special list type: {first_list_type}"
                            
                            if sample_items:
                                base_response['details']['sample_data'] = sample_items
                            else:
                                base_response['details']['sample_data'] = [f"No items found in {first_list_type} list"]

                    # Add Friends Trakt Watchlist sample fetch if needed later

                    base_response['details']['sample_data'] = sample_items if sample_items else ["No items found in sample."]
                    # Only overwrite sample_error if it wasn't already set by a specific list fetch failure
                    if not base_response['details']['sample_error'] and not sample_items:
                        base_response['details']['sample_data'] = ["No items found in sample."]

                except Exception as e:
                    log.warning(f"Failed to fetch sample for Trakt source {source_id}: {e}", exc_info=True) 
                    # Avoid overwriting specific errors (like URL parsing) with the general exception message
                    if not base_response['details']['sample_error']:
                        base_response['details']['sample_error'] = f"Failed to fetch sample: {str(e)}"
            # --- End Trakt Sample Fetch ---

        # --- Overseerr ---
        elif source_type == 'Overseerr':
            url = source_config.get('url', '').strip()
            api_key = source_config.get('api_key', '').strip()
            
            if not url or not api_key:
                base_response['error'] = 'URL or API key not configured'
                base_response['connected'] = False
                return base_response
                
            # Test Overseerr API connection
            headers = {
                'X-Api-Key': api_key
            }
            
            response = requests.get(f"{url.rstrip('/')}/api/v1/status", headers=headers, timeout=5)
            base_response['connected'] = response.status_code == 200
            if not base_response['connected']:
                base_response['error'] = f'Status code: {response.status_code}'
            base_response['details'].update({
                'url': url,
                'api_status': response.status_code
            })
            
            # --- Fetch Sample Data for Overseerr (if connected) ---
            if base_response['connected']:
                sample_items = []
                try:
                    sample_url = f"{url.rstrip('/')}/api/v1/request?take=3&skip=0&sort=added&filter=available"
                    headers = {'X-Api-Key': api_key}
                    sample_response = requests.get(sample_url, headers=headers, timeout=5)
                    sample_response.raise_for_status()
                    data = sample_response.json()
                    results = data.get('results', [])
                    
                    for request in results:
                         media_info = request.get('media', {})
                         media_type = media_info.get('mediaType') # 'movie' or 'tv'
                         
                         title = None
                         year = None
                         display_type = 'Unknown'
                         tmdb_id_for_log = media_info.get('tmdbId', 'N/A') 

                         if media_type == 'movie':
                             title = media_info.get('title') or media_info.get('originalTitle')
                             year = media_info.get('releaseDate', '')[:4] if media_info.get('releaseDate') else ''
                             display_type = "Movie"
                         elif media_type == 'tv':
                             title = media_info.get('name') or media_info.get('originalName')
                             year = media_info.get('firstAirDate', '')[:4] if media_info.get('firstAirDate') else ''
                             display_type = "TV"
                         else:
                             title = media_info.get('title') or media_info.get('name') or media_info.get('originalTitle') or media_info.get('originalName')
                             year = (media_info.get('releaseDate') or media_info.get('firstAirDate') or '')[:4]

                         # --- Fallback using DirectAPI if title is missing ---
                         if (not title or title == "Unknown Title") and tmdb_id_for_log != 'N/A' and direct_api_instance:
                             log.warning(f"Overseerr sample: Title missing for {media_type} TMDB ID {tmdb_id_for_log}. Attempting fallback lookup.")
                             try:
                                 # Convert TMDB to IMDb
                                 conversion_media_type = 'show' if media_type == 'tv' else media_type
                                 imdb_id, _ = direct_api_instance.tmdb_to_imdb(str(tmdb_id_for_log), media_type=conversion_media_type)
                                 
                                 if imdb_id:
                                     # Fetch metadata using IMDb ID
                                     metadata = None
                                     if media_type == 'movie':
                                         metadata, _ = direct_api_instance.get_movie_metadata(imdb_id)
                                     elif media_type == 'tv':
                                         metadata, _ = direct_api_instance.get_show_metadata(imdb_id)
                                     
                                     if metadata and isinstance(metadata, dict) and metadata.get('title'):
                                         fetched_title = metadata.get('title')
                                         log.info(f"Fallback successful: Found title '{fetched_title}' for TMDB ID {tmdb_id_for_log} (IMDb: {imdb_id})")
                                         title = fetched_title # Update the title
                                     else:
                                         log.warning(f"Fallback failed: Could not fetch metadata or title for IMDb ID {imdb_id}")
                                 else:
                                     log.warning(f"Fallback failed: Could not convert TMDB ID {tmdb_id_for_log} to IMDb ID.")
                             except Exception as fallback_e:
                                 log.error(f"Error during Overseerr sample title fallback lookup: {fallback_e}", exc_info=True)
                         # --- End Fallback ---

                         # Ensure title is not None or empty before formatting
                         title = title if title else "Unknown Title"

                         display_text = f"{title} ({year})" if year else title
                         sample_items.append(f"[{display_type}] {display_text}")

                    base_response['details']['sample_data'] = sample_items if sample_items else ["No available requests found in sample."]

                except Exception as e:
                    log.warning(f"Failed to fetch sample for Overseerr {source_id}: {e}", exc_info=True) 
                    base_response['details']['sample_error'] = f"Failed to fetch sample: {str(e)}"
            # --- End Overseerr Sample Fetch ---

        # --- Plex Watchlist (My/Other) ---
        elif source_type in ['My Plex Watchlist', 'Other Plex Watchlist']:
            token = None # Initialize token
            username = None # Initialize username for 'Other'
            account = None # Initialize account object

            # Get token and potentially username
            if source_type == 'Other Plex Watchlist':
                token = source_config.get('token', '').strip()
                username = source_config.get('username', '').strip()
                if not token or not username:
                    base_response['error'] = 'Token or username not configured'
                    base_response['connected'] = False
                    return base_response
            else: # My Plex Watchlist
                token = get_setting('Plex', 'token') or get_setting('File Management', 'plex_token_for_symlink')
                if not token:
                    base_response['error'] = 'Plex token not configured'
                    base_response['connected'] = False
                    return base_response
            
            # --- Connection Check ---
            try:
                # Try to connect with the token
                account = MyPlexAccount(token=token) # Instantiate account

                # For Other Plex Watchlist, verify username matches
                if source_type == 'Other Plex Watchlist' and account.username != username:
                    base_response['error'] = f'Token does not match username. Expected: {username}, Got: {account.username}'
                    base_response['connected'] = False
                    # No need to return here, let it fall through to sample fetch attempt if desired,
                    # but mark as disconnected
                else:
                    # If we get here, connection was successful
                    base_response['connected'] = True
                    base_response['details'].update({
                        'username': account.username,
                        'email': account.email
                    })

            except Exception as e:
                log.error(f"Failed to authenticate Plex Watchlist {source_id}: {e}", exc_info=True) # Log full traceback
                base_response['error'] = f'Failed to authenticate: {str(e)}'
                base_response['connected'] = False
                # Don't proceed to sample fetch if authentication failed
                return base_response
            # --- End Connection Check ---

            # --- Fetch Sample Data for Plex Watchlist (if connection seems okay) ---
            # We attempt sample fetch even if username mismatch occurred, but connection itself worked
            if account: # Check if account object was created
                sample_items = []
                try:
                    # Call watchlist() directly on the MyPlexAccount instance
                    # Pass maxresults=3 to limit the sample size
                    watchlist_items = account.watchlist(sort='addedAt:desc', maxresults=3) # Corrected call

                    for item in watchlist_items:
                         title = getattr(item, 'title', 'Unknown Title')
                         year = getattr(item, 'year', None)
                         item_type = getattr(item, 'type', 'unknown').capitalize()
                         display_text = f"{title} ({year})" if year else title
                         sample_items.append(f"[{item_type}] {display_text}")

                    base_response['details']['sample_data'] = sample_items if sample_items else ["Watchlist is empty or could not fetch sample."]

                except AttributeError as ae:
                     # Handle case where MyPlexAccount doesn't have watchlist method
                     log.error(f"MyPlexAccount object for {source_id} is missing the 'watchlist' method: {ae}", exc_info=True)
                     base_response['details']['sample_error'] = "Internal Error: Watchlist method not found."
                except Exception as e:
                    # Catch specific plexapi exceptions if known, otherwise general Exception
                    log.warning(f"Failed to fetch sample for Plex Watchlist {source_id}: {e}", exc_info=True) # Log traceback
                    base_response['details']['sample_error'] = f"Failed to fetch sample: {str(e)}"
            elif not base_response['connected']:
                 base_response['details']['sample_error'] = "Cannot fetch sample, connection failed."
            # --- End Plex Watchlist Sample Fetch ---

        # --- Plex RSS Watchlist ---
        elif source_type in ['My Plex RSS Watchlist', 'My Friends Plex RSS Watchlist']:
            url = source_config.get('url', '').strip()
            if not url:
                base_response['error'] = 'RSS URL not configured'
                base_response['connected'] = False
                return base_response
                
            response = requests.get(url, timeout=5)
            base_response['connected'] = response.status_code == 200
            if not base_response['connected']:
                base_response['error'] = f'Status code: {response.status_code}'
            base_response['details'].update({
                'url': url,
                'rss_status': response.status_code
            })
            
            # --- Fetch Sample Data for Plex RSS (if connected) ---
            if base_response['connected']:
                sample_items = []
                try:
                    import feedparser # Import here to avoid making it a hard dependency if RSS isn't used
                    url = source_config.get('url', '').strip()
                    feed = feedparser.parse(url)

                    for entry in feed.entries[:3]: # Get first 3 entries
                        title = entry.get('title', 'Unknown Title')
                        sample_items.append(title)

                    base_response['details']['sample_data'] = sample_items if sample_items else ["RSS feed empty or could not parse."]

                except ImportError:
                     base_response['details']['sample_error'] = "feedparser library not installed."
                except Exception as e:
                    log.warning(f"Failed to fetch sample for Plex RSS {source_id}: {e}")
                    base_response['details']['sample_error'] = f"Failed to fetch sample: {e}"
            # --- End Plex RSS Sample Fetch ---

    except requests.Timeout:
        base_response['error'] = 'Connection timed out'
        base_response['connected'] = False
    except requests.ConnectionError:
        base_response['error'] = 'Connection refused'
        base_response['connected'] = False
    except Exception as e:
        log.exception(f"Unhandled error during connection check for {source_id}: {e}") # Log unexpected errors
        base_response['error'] = f"Unexpected error: {str(e)}"
        base_response['connected'] = False
        
    return base_response

def check_content_sources_connections():
    """Check connections to all enabled content sources in parallel."""
    from utilities.settings import get_setting
    
    content_sources = get_setting('Content Sources')
    if not content_sources:
        return []
        
    source_statuses = []
    enabled_sources = {
        source_id: source_config 
        for source_id, source_config in content_sources.items() 
        if 'Collected' not in source_id and source_config.get('enabled', False)
    }

    if not enabled_sources:
        return []

    with ThreadPoolExecutor(max_workers=len(enabled_sources)) as executor:
        future_to_source = {
            executor.submit(check_content_source_connection, source_id, source_config): (source_id, source_config)
            for source_id, source_config in enabled_sources.items()
        }
        
        for future in as_completed(future_to_source):
            source_id, source_config = future_to_source[future]
            try:
                # Individual timeout per source check
                status = future.result(timeout=10) 
                if status:
                    source_statuses.append(status)
            except TimeoutError:
                log.warning(f'Content source check for {source_id} timed out.')
                # Create a generic timeout error status
                source_statuses.append({
                    'name': source_config.get('display_name', source_id),
                    'connected': False,
                    'error': 'Connection check timed out after 10 seconds.',
                    'details': {'type': source_id.split('_')[0]}
                })
            except Exception as exc:
                log.error(f'Content source {source_id} check generated an exception: {exc}', exc_info=True)
                source_statuses.append({
                    'name': source_config.get('display_name', source_id),
                    'connected': False,
                    'error': f'An unexpected error occurred: {str(exc)}',
                    'details': {'type': source_id.split('_')[0]}
                })
                
    return source_statuses

def get_trakt_sources() -> Dict[str, List[Dict[str, Any]]]:
    # Use get_all_settings instead of direct config loading if possible
    # Assuming get_all_settings provides the full config dictionary
    all_settings = get_all_settings()
    content_sources = all_settings.get('Content Sources', {})
    watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Watchlist')]
    list_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Lists')]
    friend_watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Friends Trakt Watchlist')]

    return {
        'watchlist': watchlist_sources,
        'lists': list_sources,
        'friend_watchlist': friend_watchlist_sources
    }

@connections_bp.route('/')
@user_required
def index():
    """Render the connections status page with a timeout."""
    start_time = datetime.now()
    
    # Initialize all statuses to None
    results = {
        'cli_battery_status': None,
        'plex_status': None,
        'jellyfin_status': None,
        'mounted_files_status': None,
        'phalanx_db_status': None,
        'scraper_statuses': [],
        'content_source_statuses': [],
    }

    # Run Nyaa scraper checks first to avoid proxy conflicts
    nyaa_scraper_statuses = check_nyaa_scrapers_only()
    results['scraper_statuses'].extend(nyaa_scraper_statuses)

    # Determine which media server check to run
    jellyfin_url = get_setting('Debug', 'emby_jellyfin_url')
    jellyfin_token = get_setting('Debug', 'emby_jellyfin_token')
    
    # Define tasks for all other connection checks (excluding Nyaa scrapers)
    tasks = {
        'cli_battery_status': check_cli_battery_connection,
        'mounted_files_status': check_mounted_files_connection,
        'phalanx_db_status': check_phalanx_db_connection,
        'non_nyaa_scraper_statuses': check_non_nyaa_scrapers,
        'content_source_statuses': check_content_sources_connections,
    }
    
    if jellyfin_url and jellyfin_token:
        tasks['jellyfin_status'] = check_jellyfin_connection
    else:
        tasks['plex_status'] = check_plex_connection

    # Run all other connection checks in parallel
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_task = {executor.submit(func): name for name, func in tasks.items()}
        
        try:
            # Wait for all futures to complete, with a total timeout of 5 seconds
            for future in as_completed(future_to_task, timeout=5):
                task_name = future_to_task[future]
                try:
                    task_result = future.result()
                    if task_name == 'non_nyaa_scraper_statuses':
                        # Add non-Nyaa scraper results to the scraper_statuses list
                        results['scraper_statuses'].extend(task_result)
                    else:
                        results[task_name] = task_result
                except Exception as exc:
                    log.error(f"Task {task_name} generated an exception: {exc}", exc_info=True)
                    # Optionally create an error status for the failed task
                    if task_name not in ['non_nyaa_scraper_statuses', 'content_source_statuses']:
                        results[task_name] = {'name': task_name, 'connected': False, 'error': str(exc), 'details': {}}

        except TimeoutError:
            log.warning("Connections page render timed out after 5 seconds. Rendering with available data.")
            # The loop is broken, results will contain only completed tasks.

    # Collect failing connections from the results we have
    failing_connections = []
    for key, status in results.items():
        if not status: # Skip if status is None or empty list
            continue
            
        if key in ['scraper_statuses', 'content_source_statuses']:
            failing_connections.extend([s for s in status if not s.get('connected')])
        elif isinstance(status, dict) and not status.get('connected'):
            failing_connections.append(status)

    # Add a flash message if the page timed out
    if (datetime.now() - start_time).total_seconds() >= 5:
        flash("Some connection checks timed out and may not be displayed. The page was loaded with available data.", "warning")

    return render_template('connections.html', 
                         cli_battery_status=results['cli_battery_status'],
                         plex_status=results['plex_status'],
                         jellyfin_status=results['jellyfin_status'],
                         mounted_files_status=results['mounted_files_status'],
                         phalanx_db_status=results['phalanx_db_status'],
                         scraper_statuses=results['scraper_statuses'],
                         content_source_statuses=results['content_source_statuses'],
                         failing_connections=failing_connections)