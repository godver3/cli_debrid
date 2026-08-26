from flask import Blueprint, render_template, flash, redirect, url_for
import requests
import os
import threading
from datetime import datetime, timedelta
from utilities.settings import get_setting, get_all_settings
from typing import Dict, List, Any
from content_checkers.trakt import ensure_trakt_auth, get_trakt_headers, make_trakt_request, parse_trakt_list_url
from content_checkers.plex_watchlist import MyPlexAccount
import logging
import feedparser # Keep import for RSS
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

# --- Connection status cache ---
_connections_cache = {
    'results': None,
    'timestamp': None,
    'refreshing': False,
}
_connections_cache_lock = threading.Lock()
_CACHE_TTL = timedelta(seconds=60)  # Serve cached results for up to 60s

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

# Settings cache to avoid repeated config loading
_settings_cache = {}
_settings_cache_time = None

def get_cached_setting(section, key=None, default=None):
    """Get setting with caching to reduce repeated config loads."""
    global _settings_cache, _settings_cache_time
    from datetime import datetime, timedelta

    # Cache for 5 seconds
    if _settings_cache_time is None or (datetime.now() - _settings_cache_time) > timedelta(seconds=5):
        _settings_cache = {}
        _settings_cache_time = datetime.now()

    cache_key = f"{section}:{key}" if key else section
    if cache_key not in _settings_cache:
        _settings_cache[cache_key] = get_setting(section, key, default)

    return _settings_cache[cache_key]

def check_cli_battery_connection():
    """Check cli_battery module status via DirectAPI (in-process, no HTTP)."""
    try:
        from cli_battery.app.direct_api import DirectAPI
        result = DirectAPI.check_trakt_auth()
        return {
            'name': 'cli_battery',
            'connected': True,  # Module is reachable if we get here
            'error': None,
            'details': {
                'trakt_status': result.get('status', 'unknown'),
                'mode': 'in-process',
            }
        }
    except Exception as e:
        log.error(f"CLI Battery connection check failed: {e}", exc_info=True)
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': str(e),
            'details': {}
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

def _check_path_with_timeout(path, timeout_sec=5):
    """Check if a path exists with a timeout to prevent hanging on unresponsive mounts.

    Uses a separate thread to perform the check, with a timeout.
    Returns dict with exists, accessible, error, listed keys.
    """
    result = {'exists': False, 'accessible': False, 'error': None, 'listed': False}

    def check_path():
        try:
            result['exists'] = os.path.exists(path)
            if result['exists']:
                result['accessible'] = os.access(path, os.R_OK)
                if result['accessible']:
                    # Quick listdir to verify mount is responsive
                    os.listdir(path)
                    result['listed'] = True
        except Exception as e:
            result['error'] = str(e)

    thread = threading.Thread(target=check_path, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)

    if thread.is_alive():
        # Thread is still running - mount is unresponsive
        result['error'] = f'Mount check timed out after {timeout_sec}s (mount may be unresponsive)'
        return result

    return result


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
        # Use timeout-protected path check to prevent hanging on unresponsive mounts
        check_result = _check_path_with_timeout(mount_path, timeout_sec=5)

        if check_result['error']:
            return {
                'name': 'Mounted Files',
                'connected': False,
                'error': check_result['error'],
                'details': {
                    'path': mount_path,
                    'source': source
                }
            }

        if check_result['exists'] and check_result['accessible'] and check_result['listed']:
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

def check_tvdb_connection():
    """Check TVDB API key if configured."""
    api_key = get_setting('TVDB', 'api_key', '').strip()
    if not api_key:
        return None
    try:
        import requests as _req
        r = _req.post('https://api4.thetvdb.com/v4/login',
                      json={'apikey': api_key}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            token = data.get('data', {}).get('token') or data.get('token')
            return {'name': 'TVDB', 'connected': bool(token), 'error': None,
                    'details': {'status': 'API key valid'}}
        return {'name': 'TVDB', 'connected': False,
                'error': f'HTTP {r.status_code}', 'details': {}}
    except Exception as e:
        return {'name': 'TVDB', 'connected': False, 'error': str(e), 'details': {}}


def check_tmdb_connection():
    """Check TMDB API key if configured."""
    api_key = get_setting('TMDB', 'api_key', '').strip()
    if not api_key:
        return None
    try:
        import requests as _req
        r = _req.get(f'https://api.themoviedb.org/3/configuration?api_key={api_key}', timeout=10)
        if r.status_code == 200:
            return {'name': 'TMDB', 'connected': True, 'error': None,
                    'details': {'status': 'API key valid'}}
        return {'name': 'TMDB', 'connected': False,
                'error': f'HTTP {r.status_code}', 'details': {}}
    except Exception as e:
        return {'name': 'TMDB', 'connected': False, 'error': str(e), 'details': {}}


def check_trakt_connection():
    """Check Trakt auth status. Only shown when Trakt has been authorized."""
    try:
        from cli_battery.app.direct_api import DirectAPI
        result = DirectAPI.check_trakt_auth()
        status = result.get('status')
        if status != 'authorized':
            return None  # Not connected/authorized - don't show the card
        return {'name': 'Trakt', 'connected': True, 'error': None, 'details': {}}
    except Exception as e:
        log.error(f"Trakt connection check failed: {e}", exc_info=True)
        return None


def check_scrob_connection():
    """Check Scrob connection if URL and API key are configured."""
    from content_checkers.scrob import get_scrob_config, _scrob_get
    config = get_scrob_config()
    if not config:
        return None
    try:
        data = _scrob_get('/lists')
        if data is not None:
            return {'name': 'Scrob', 'connected': True, 'error': None,
                    'details': {'url': config['base_url']}}
        return {'name': 'Scrob', 'connected': False,
                'error': 'Failed to connect to Scrob API', 'details': {}}
    except Exception as e:
        return {'name': 'Scrob', 'connected': False, 'error': str(e), 'details': {}}


def check_climount_connection():
    """Check usenet provider connection if usenet is enabled and URL is set.

    Uses the provider factory so NzbDAV is checked (and its ensure_categories()
    side-effect runs) when NzbDAV is the active provider.
    """
    enabled = get_setting('Usenet Provider', 'enabled', default=False)
    url = get_setting('Usenet Provider', 'url', '').strip().rstrip('/')
    if not enabled or not url:
        return None
    try:
        from usenet import get_usenet_client, get_usenet_provider_display_name
        client = get_usenet_client()
        ok, err = client.check_connectivity()
        display_name = get_usenet_provider_display_name()
        return {'name': display_name, 'connected': ok,
                'error': err, 'details': {'url': url}}
    except Exception as e:
        return {'name': 'Usenet Provider', 'connected': False, 'error': str(e), 'details': {}}


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

        elif scraper_type == 'Newznab':
            url = scraper_config.get('url', '').strip().rstrip('/')
            api_key = scraper_config.get('api_key', '').strip()

            if not url:
                base_response['error'] = 'URL not configured'
                return base_response

            try:
                params = {'t': 'caps'}
                if api_key:
                    params['apikey'] = api_key
                response = requests.get(f"{url}/api", params=params, timeout=8)
                if response.status_code == 200 and '<caps' in response.text:
                    base_response['connected'] = True
                elif response.status_code == 200:
                    base_response['connected'] = True
                else:
                    base_response['connected'] = False
                    base_response['error'] = f'Status code: {response.status_code}'
            except requests.exceptions.Timeout:
                base_response['error'] = 'Connection timed out'
            except requests.exceptions.ConnectionError:
                base_response['error'] = 'Connection refused'
            except Exception as e:
                base_response['error'] = str(e)

            base_response['details'].update({
                'url': url,
                'subscription_expiry_date': scraper_config.get('subscription_expiry_date', ''),
                'auto_renew': scraper_config.get('auto_renew', False)
            })

        elif scraper_type == 'AIOStreams':
            # Test AIOStreams Stremio addon by checking manifest.json endpoint
            # This is more reliable than scraping since it just checks if the addon is reachable
            url = scraper_config.get('url', '').strip()
            if not url:
                base_response['error'] = 'URL not configured'
                return base_response

            try:
                # Remove /manifest.json if already present
                base_url = url.rstrip('/')
                if base_url.endswith('/manifest.json'):
                    base_url = base_url[:-14]

                # Test the manifest endpoint (standard for all Stremio addons)
                manifest_url = f"{base_url}/manifest.json"
                response = requests.get(manifest_url, timeout=5)

                # Check if we get a valid manifest response
                if response.status_code == 200:
                    try:
                        manifest = response.json()
                        # Verify it's a valid Stremio manifest
                        if 'id' in manifest and 'version' in manifest:
                            base_response['connected'] = True
                            base_response['details'].update({
                                'url': base_url,
                                'manifest_url': manifest_url,
                                'addon_name': manifest.get('name', 'Unknown'),
                                'addon_version': manifest.get('version', 'Unknown')
                            })
                        else:
                            base_response['error'] = 'Invalid Stremio manifest format'
                            base_response['details']['url'] = base_url
                    except ValueError:
                        base_response['error'] = 'Invalid JSON response from manifest endpoint'
                        base_response['details']['url'] = base_url
                else:
                    base_response['error'] = f'Manifest endpoint returned status code: {response.status_code}'
                    base_response['details']['url'] = base_url

            except Exception as e:
                base_response['connected'] = False
                base_response['error'] = str(e)
                base_response['details']['url'] = scraper_config.get('url', '')

        elif scraper_type == 'AIOStreams-API':
            from scraper.aiostreams import scrape_aiostreams_api

            # Test with a known movie (The Dark Knight)
            try:
                results = scrape_aiostreams_api(
                    instance='AIOStreams-API',
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
                    'results_found': len(results),
                    'url': scraper_config.get('url', ''),
                    'api_key': scraper_config.get('api_key', '')[:10] + '...' if scraper_config.get('api_key') else 'Not set'
                })
            except Exception as e:
                base_response['connected'] = False
                base_response['error'] = str(e)

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
    # Use ThreadPoolExecutor with single worker to enable timeout
    with ThreadPoolExecutor(max_workers=1) as executor:
        for scraper_id, scraper_config in enabled_scrapers.items():
            future = executor.submit(check_scraper_connection, scraper_id, scraper_config)
            try:
                status = future.result(timeout=3)  # 5-second timeout per Nyaa scraper
                if status:
                    scraper_statuses.append(status)
            except TimeoutError:
                log.warning(f'Nyaa scraper check for {scraper_id} timed out.')
                scraper_statuses.append(create_timeout_status(scraper_config.get('type'), scraper_id))
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

    # Use fixed worker limit instead of O(n) workers to prevent resource exhaustion
    with ThreadPoolExecutor(max_workers=min(5, len(enabled_scrapers))) as executor:
        future_to_scraper = {
            executor.submit(check_scraper_connection, scraper_id, scraper_config): (scraper_id, scraper_config)
            for scraper_id, scraper_config in enabled_scrapers.items()
        }
        
        for future in as_completed(future_to_scraper):
            scraper_id, scraper_config = future_to_scraper[future]
            try:
                status = future.result(timeout=3) # 3-second timeout per scraper
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

    # Run all other scrapers in parallel with fixed worker limit
    if other_scrapers:
        with ThreadPoolExecutor(max_workers=min(5, len(other_scrapers))) as executor:
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
                    status = future.result(timeout=3) # 5-second timeout per scraper
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
            source_mode = (source_config.get('source_mode') or 'json_url').strip() or 'json_url'

            # API modes talk to api.mdblist.com directly and need the configured API key
            if source_mode != 'json_url':
                from content_checkers.mdb_list import build_mdblist_api_url

                api_key = (get_setting('MDBList', 'api_key', '') or '').strip()
                if not api_key:
                    base_response['error'] = 'MDBList API key not configured (Additional Settings -> MDBList)'
                    base_response['connected'] = False
                    return base_response

                try:
                    # Only the first list ID needs testing to prove the endpoint works
                    endpoint = build_mdblist_api_url(
                        source_mode,
                        username=source_config.get('username'),
                        listname=source_config.get('listname'),
                        list_id=str(source_config.get('list_id') or '').split(',')[0]
                    )
                except ValueError as e:
                    base_response['error'] = str(e)
                    base_response['connected'] = False
                    return base_response

                try:
                    response = requests.get(
                        endpoint,
                        params={'apikey': api_key, 'limit': 1},
                        headers={'Accept': 'application/json'},
                        timeout=10
                    )
                    if response.status_code == 200:
                        base_response['connected'] = True
                        base_response['error'] = f'Successfully connected to {endpoint}'
                    elif response.status_code in (401, 403):
                        base_response['error'] = 'MDBList authentication failed - check your API key'
                    elif response.status_code == 404:
                        base_response['error'] = f'MDBList list not found: {endpoint}'
                    else:
                        base_response['error'] = f'MDBList API returned status {response.status_code}'
                except requests.exceptions.RequestException as e:
                    base_response['error'] = f'MDBList API error: {str(e)}'

                base_response['details'].update({
                    'source_mode': source_mode,
                    'endpoint': endpoint,
                    'versions': source_config.get('versions', {'Default': True})
                })
                return base_response

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
                'source_mode': source_mode,
                'successful_urls': successful_urls,
                'failed_urls': failed_urls,
                'total_urls': len(url_list),
                'versions': source_config.get('versions', {'Default': True})
            })

            # Sample data fetching removed to improve performance
            # Connection check should only verify connectivity, not fetch data

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

            # Sample data fetching removed to improve performance
            # Connection check should only verify connectivity, not fetch data

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

            # Sample data fetching removed to improve performance
            # Connection check should only verify connectivity, not fetch data

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

            # Sample data fetching removed to improve performance
            # Connection check should only verify connectivity, not fetch data

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

            # Sample data fetching removed to improve performance
            # Connection check should only verify connectivity, not fetch data

        # --- Adaptive List ---
        elif source_type == 'Adaptive List':
            # Adaptive lists are internal TMDB-based dynamic lists
            # Test by verifying TMDB API key is configured and working
            tmdb_api_key = get_setting('TMDB', 'api_key', '').strip()

            if not tmdb_api_key:
                base_response['error'] = 'TMDB API key not configured in Additional Settings'
                base_response['connected'] = False
                return base_response

            try:
                # Test TMDB API with a simple configuration endpoint
                test_url = f"https://api.themoviedb.org/3/configuration?api_key={tmdb_api_key}"
                response = requests.get(test_url, timeout=5)

                if response.status_code == 200:
                    base_response['connected'] = True
                    base_response['details'].update({
                        'list_name': source_config.get('list_name', 'Unknown'),
                        'filter_config': source_config.get('filter_name', 'Unknown'),
                        'tmdb_status': 'API key valid'
                    })
                else:
                    base_response['error'] = f'TMDB API returned status code: {response.status_code}'
                    base_response['connected'] = False

            except Exception as e:
                base_response['error'] = f'TMDB API error: {str(e)}'
                base_response['connected'] = False

        # --- Scrob Sources (Lists, Collection, Special Lists) ---
        elif source_type in ['Scrob Lists', 'Scrob Collection', 'Special Scrob Lists']:
            from content_checkers.scrob import get_scrob_config, _scrob_get

            scrob_config = get_scrob_config()
            if not scrob_config:
                base_response['error'] = 'Scrob URL or API key not configured in Additional Settings'
                base_response['connected'] = False
                return base_response

            try:
                data = _scrob_get('/lists')
                if data is not None:
                    base_response['connected'] = True
                    base_response['details'].update({
                        'url': scrob_config['base_url'],
                        'list_count': len(data.get('lists', []))
                    })
                else:
                    base_response['error'] = 'Failed to connect to Scrob API'
                    base_response['connected'] = False
            except Exception as e:
                base_response['error'] = f'Scrob API error: {str(e)}'
                base_response['connected'] = False

        # --- Agregarr ---
        elif source_type == 'Agregarr':
            # Agregarr is a one-way webhook integration (Agregarr → CLI Debrid)
            # CLI Debrid doesn't initiate connections to Agregarr, it only receives webhooks
            # Just verify it's configured (enabled) and mark as connected

            # Agregarr doesn't have an API key - it's configured if it's enabled
            base_response['connected'] = True
            base_response['details'].update({
                'webhook_mode': 'receive_only',
                'note': 'Agregarr sends webhooks to CLI Debrid. No outbound connection required.',
                'media_type': source_config.get('media_type', 'All')
            })

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
    # Ignore per-source enabled flags; check connections for all configured (non-Collected) sources
    selected_sources = {
        source_id: source_config 
        for source_id, source_config in content_sources.items() 
        if 'Collected' not in source_id
    }

    if not selected_sources:
        return []

    # Use fixed worker limit instead of O(n) workers to prevent resource exhaustion
    with ThreadPoolExecutor(max_workers=min(10, len(selected_sources))) as executor:
        future_to_source = {
            executor.submit(check_content_source_connection, source_id, source_config): (source_id, source_config)
            for source_id, source_config in selected_sources.items()
        }

        for future in as_completed(future_to_source):
            source_id, source_config = future_to_source[future]
            try:
                # Individual timeout per source check (increased to 20s for slow Trakt API)
                status = future.result(timeout=20)
                if status:
                    source_statuses.append(status)
            except TimeoutError:
                log.warning(f'Content source check for {source_id} timed out.')
                # Create a generic timeout error status
                source_statuses.append({
                    'name': source_config.get('display_name', source_id),
                    'connected': False,
                    'error': 'Connection check timed out after 20 seconds.',
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

@connections_bp.route('/api/check/system')
@user_required
def api_check_system():
    """Check cli_battery, plex/jellyfin, mounted files, phalanx db."""
    from flask import jsonify
    jellyfin_url = get_cached_setting('Debug', 'emby_jellyfin_url')
    jellyfin_token = get_cached_setting('Debug', 'emby_jellyfin_token')
    tasks = {
        'cli_battery_status': check_cli_battery_connection,
        'mounted_files_status': check_mounted_files_connection,
        'phalanx_db_status': check_phalanx_db_connection,
    }
    if jellyfin_url and jellyfin_token:
        tasks['jellyfin_status'] = check_jellyfin_connection
    else:
        tasks['plex_status'] = check_plex_connection
    tasks['tvdb_status'] = check_tvdb_connection
    tasks['tmdb_status'] = check_tmdb_connection
    tasks['climount_status'] = check_climount_connection
    tasks['scrob_status'] = check_scrob_connection
    tasks['trakt_status'] = check_trakt_connection
    results = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_task = {executor.submit(func): name for name, func in tasks.items()}
        try:
            for future in as_completed(future_to_task, timeout=10):
                name = future_to_task[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = {'name': name, 'connected': False, 'error': str(exc), 'details': {}}
        except TimeoutError:
            pass
    results.setdefault('plex_status', None)
    results.setdefault('jellyfin_status', None)
    results.setdefault('mounted_files_status', None)
    results.setdefault('phalanx_db_status', None)
    results.setdefault('cli_battery_status', None)
    results.setdefault('tvdb_status', None)
    results.setdefault('tmdb_status', None)
    results.setdefault('climount_status', None)
    results.setdefault('scrob_status', None)
    results.setdefault('trakt_status', None)
    return jsonify(results)


@connections_bp.route('/api/check/scrapers')
@user_required
def api_check_scrapers():
    """Check all scraper connections."""
    from flask import jsonify
    results = {'scraper_statuses': []}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(check_nyaa_scrapers_only): 'nyaa',
                   executor.submit(check_non_nyaa_scrapers): 'non_nyaa'}
        try:
            for future in as_completed(futures, timeout=15):
                try:
                    results['scraper_statuses'].extend(future.result())
                except Exception as exc:
                    log.error(f"Scraper check error: {exc}")
        except TimeoutError:
            pass
    return jsonify(results)


@connections_bp.route('/api/check/content-sources')
@user_required
def api_check_content_sources():
    """Check all content source connections."""
    from flask import jsonify
    try:
        statuses = check_content_sources_connections()
    except Exception as exc:
        log.error(f"Content sources check error: {exc}")
        statuses = []
    return jsonify({'content_source_statuses': statuses})


@connections_bp.route('/api/stream/scrapers')
@user_required
def api_stream_scrapers():
    """SSE stream: emits each scraper result as it completes."""
    import json
    from flask import Response

    def generate():
        try:
            from queues.config_manager import load_config
            config = load_config()
            scrapers = config.get('Scrapers', {})
            all_scrapers = [
                (sid, cfg) for sid, cfg in scrapers.items()
                if cfg.get('enabled', False)
            ]
        except Exception:
            all_scrapers = []

        if not all_scrapers:
            yield "event: done\ndata: {}\n\n"
            return

        with ThreadPoolExecutor(max_workers=min(5, len(all_scrapers))) as executor:
            future_to_scraper = {
                executor.submit(check_scraper_connection, sid, cfg): (sid, cfg)
                for sid, cfg in all_scrapers
            }
            try:
                for future in as_completed(future_to_scraper, timeout=30):
                    try:
                        result = future.result()
                        if result:
                            yield f"event: scraper\ndata: {json.dumps(result)}\n\n"
                    except Exception as exc:
                        sid, cfg = future_to_scraper[future]
                        scraper_type = cfg.get('type', sid)
                        name = f"{scraper_type} ({sid})"
                        yield f"event: scraper\ndata: {json.dumps({'name': name, 'connected': False, 'error': str(exc)})}\n\n"
            except TimeoutError:
                pass
        yield "event: done\ndata: {}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@connections_bp.route('/api/stream/content-sources')
@user_required
def api_stream_content_sources():
    """SSE stream: emits each content source result as it completes."""
    import json
    from flask import Response

    def generate():
        all_sources = []
        try:
            all_settings = get_all_settings()
            content_sources = all_settings.get('Content Sources', {})
            all_sources = [
                (sid, cfg) for sid, cfg in content_sources.items()
                if cfg.get('enabled', False) and cfg.get('type') != 'Collected'
            ]
        except Exception:
            pass

        if not all_sources:
            yield "event: done\ndata: {}\n\n"
            return

        with ThreadPoolExecutor(max_workers=min(10, len(all_sources))) as executor:
            future_to_source = {
                executor.submit(check_content_source_connection, sid, cfg): (sid, cfg)
                for sid, cfg in all_sources
            }
            try:
                for future in as_completed(future_to_source, timeout=60):
                    try:
                        result = future.result()
                        if result:
                            yield f"event: source\ndata: {json.dumps(result)}\n\n"
                    except Exception as exc:
                        sid, cfg = future_to_source[future]
                        display_name = cfg.get('display_name')
                        name = f"{display_name} ({sid})" if display_name else sid
                        yield f"event: source\ndata: {json.dumps({'name': name, 'connected': False, 'error': str(exc), 'details': {}})}\n\n"
            except TimeoutError:
                pass
        yield "event: done\ndata: {}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _run_all_connection_checks():
    """Run all connection checks and return results dict. Used by cache refresh."""
    results = {
        'cli_battery_status': None,
        'plex_status': None,
        'jellyfin_status': None,
        'mounted_files_status': None,
        'phalanx_db_status': None,
        'trakt_status': None,
        'scraper_statuses': [],
        'content_source_statuses': [],
    }

    jellyfin_url = get_cached_setting('Debug', 'emby_jellyfin_url')
    jellyfin_token = get_cached_setting('Debug', 'emby_jellyfin_token')

    tasks = {
        'cli_battery_status': check_cli_battery_connection,
        'mounted_files_status': check_mounted_files_connection,
        'phalanx_db_status': check_phalanx_db_connection,
        'trakt_status': check_trakt_connection,
        'nyaa_scraper_statuses': check_nyaa_scrapers_only,
        'non_nyaa_scraper_statuses': check_non_nyaa_scrapers,
        'content_source_statuses': check_content_sources_connections,
    }

    if jellyfin_url and jellyfin_token:
        tasks['jellyfin_status'] = check_jellyfin_connection
    else:
        tasks['plex_status'] = check_plex_connection

    with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as executor:
        future_to_task = {executor.submit(func): name for name, func in tasks.items()}
        try:
            for future in as_completed(future_to_task, timeout=25):
                task_name = future_to_task[future]
                try:
                    task_result = future.result()
                    if task_name in ['nyaa_scraper_statuses', 'non_nyaa_scraper_statuses']:
                        results['scraper_statuses'].extend(task_result)
                    else:
                        results[task_name] = task_result
                except Exception as exc:
                    log.error(f"Task {task_name} generated an exception: {exc}", exc_info=True)
                    if task_name not in ['nyaa_scraper_statuses', 'non_nyaa_scraper_statuses', 'content_source_statuses']:
                        results[task_name] = {'name': task_name, 'connected': False, 'error': str(exc), 'details': {}}
        except TimeoutError:
            log.warning("Connections check timed out after 25 seconds.")

    return results


def _refresh_connections_cache():
    """Run checks in background and update cache. Ensures only one refresh runs at a time."""
    with _connections_cache_lock:
        if _connections_cache['refreshing']:
            return
        _connections_cache['refreshing'] = True

    try:
        results = _run_all_connection_checks()
        with _connections_cache_lock:
            _connections_cache['results'] = results
            _connections_cache['timestamp'] = datetime.now()
    except Exception as exc:
        log.error(f"Background connection cache refresh failed: {exc}", exc_info=True)
    finally:
        with _connections_cache_lock:
            _connections_cache['refreshing'] = False


@connections_bp.route('/')
@user_required
def index():
    """Render the connections status page instantly with skeleton cards."""
    # Build skeleton cards from config using exact SSE name format
    skeleton_scrapers = []
    skeleton_sources = []
    try:
        from queues.config_manager import load_config
        _cfg = load_config()
        for sid, scfg in _cfg.get('Scrapers', {}).items():
            if scfg.get('enabled', False):
                scraper_type = scfg.get('type', sid)
                name = f"{scraper_type} ({sid})"
                details = {}
                if scraper_type == 'Newznab':
                    details['subscription_expiry_date'] = scfg.get('subscription_expiry_date', '')
                    details['auto_renew'] = scfg.get('auto_renew', False)
                skeleton_scrapers.append({'name': name, 'connected': None, 'details': details})
        for sid, scfg in _cfg.get('Content Sources', {}).items():
            if scfg.get('enabled', False) and scfg.get('type') != 'Collected':
                display_name = scfg.get('display_name')
                name = f"{display_name} ({sid})" if display_name else sid
                skeleton_sources.append({'name': name, 'connected': None, 'details': {}})
    except Exception:
        pass

    results = {
        'cli_battery_status': None, 'plex_status': None,
        'jellyfin_status': None, 'mounted_files_status': None,
        'phalanx_db_status': None,
        'tvdb_status': None, 'tmdb_status': None, 'climount_status': None,
        'scrob_status': None, 'trakt_status': None,
        'scraper_statuses': skeleton_scrapers,
        'content_source_statuses': skeleton_sources,
    }
    cache_info = None

    # Collect failing connections
    failing_connections = []
    for key, status in results.items():
        if not status:
            continue
        if key in ['scraper_statuses', 'content_source_statuses']:
            failing_connections.extend([s for s in status if not s.get('connected')])
        elif isinstance(status, dict) and not status.get('connected'):
            failing_connections.append(status)

    return render_template('connections.html',
                         cli_battery_status=results['cli_battery_status'],
                         plex_status=results['plex_status'],
                         jellyfin_status=results['jellyfin_status'],
                         mounted_files_status=results['mounted_files_status'],
                         phalanx_db_status=results['phalanx_db_status'],
                         tvdb_status=results['tvdb_status'],
                         tmdb_status=results['tmdb_status'],
                         climount_status=results['climount_status'],
                         scrob_status=results['scrob_status'],
                         trakt_status=results['trakt_status'],
                         scraper_statuses=results['scraper_statuses'],
                         content_source_statuses=results['content_source_statuses'],
                         failing_connections=failing_connections,
                         cache_info=cache_info)
