from flask import Blueprint, render_template, flash, redirect, url_for
import requests
import os
from datetime import datetime
from utilities.settings import get_setting
from typing import Dict, List, Any

connections_bp = Blueprint('connections', __name__)

def check_cli_battery_connection():
    """Check connection to cli_battery service using environment variables."""
    try:
        battery_port = int(os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001'))
        response = requests.get(f'http://localhost:{battery_port}/', timeout=5)
        return {
            'name': 'cli_battery',
            'connected': response.status_code == 200,
            'error': None if response.status_code == 200 else f'Status code: {response.status_code}',
            'details': {
                'url': f'http://localhost:{battery_port}/',
                'port': battery_port
            }
        }
    except requests.Timeout:
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': 'Connection timed out',
            'details': {
                'url': f'http://localhost:{battery_port}/',
                'port': battery_port
            }
        }
    except requests.ConnectionError:
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': 'Connection refused',
            'details': {
                'url': f'http://localhost:{battery_port}/',
                'port': battery_port
            }
        }
    except Exception as e:
        return {
            'name': 'cli_battery',
            'connected': False,
            'error': str(e),
            'details': {
                'url': f'http://localhost:{battery_port}/',
                'port': battery_port
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
            
        # Check each configured library
        missing_libraries = []
        for lib in libraries_to_check:
            lib = lib.strip()
            if lib not in available_libraries:
                missing_libraries.append(lib)
                
        if missing_libraries:
            return {
                'name': 'Plex',
                'connected': False,
                'error': f'Missing libraries: {", ".join(missing_libraries)}',
                'details': {
                    'url': plex_url,
                    'available_libraries': list(available_libraries.keys()),
                    'configured_libraries': libraries_to_check,
                    'missing_libraries': missing_libraries
                }
            }
            
        return {
            'name': 'Plex',
            'connected': True,
            'error': None,
            'details': {
                'url': plex_url,
                'available_libraries': list(available_libraries.keys()),
                'configured_libraries': libraries_to_check
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

def check_mounted_files_connection():
    """Check if mounted files location is accessible."""
    # Try original_files_path first, then fall back to Plex mounted_file_location
    mount_path = get_setting('File Management', 'original_files_path')
    source = 'File Management'
    
    if not mount_path:
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
    # Try both localhost and phalanx_db hostname
    hosts = ['localhost', 'phalanx_db']
    port = 8888
    
    for host in hosts:
        try:
            url = f'http://{host}:{port}'
            response = requests.get(url, timeout=2)
            
            # A 404 with "Cannot GET /" is actually a success case here
            # as it means we can reach the service
            if response.status_code == 404 and "Cannot GET /" in response.text:
                return {
                    'name': 'Phalanx DB',
                    'connected': True,
                    'error': None,
                    'details': {
                        'url': url,
                        'host': host,
                        'port': port
                    }
                }
        except requests.Timeout:
            continue
        except requests.ConnectionError:
            continue
        except Exception:
            continue
            
    # If we get here, none of the connection attempts worked
    return {
        'name': 'Phalanx DB',
        'connected': False,
        'error': 'Could not connect to service on any host',
        'details': {
            'tried_hosts': hosts,
            'port': port,
            'urls': [f'http://{host}:{port}' for host in hosts]
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
                
        else:
            base_response['error'] = f'Unknown scraper type: {scraper_type}'
            
    except requests.Timeout:
        base_response['error'] = 'Connection timed out'
    except requests.ConnectionError:
        base_response['error'] = 'Connection refused'
    except Exception as e:
        base_response['error'] = str(e)
        
    return base_response

def check_scrapers_connections():
    """Check connections to all enabled scrapers."""
    from queues.config_manager import load_config
    
    config = load_config()
    scrapers = config.get('Scrapers', {})
    
    scraper_statuses = []
    for scraper_id, scraper_config in scrapers.items():
        status = check_scraper_connection(scraper_id, scraper_config)
        if status:  # Only include if status was returned (scraper is enabled)
            scraper_statuses.append(status)
            
    return scraper_statuses

def check_content_source_connection(source_id: str, source_config: Dict[str, Any]) -> Dict[str, Any]:
    """Check connection to a specific content source."""
    source_type = source_id.split('_')[0]  # Get base type without number
    
    if not source_config.get('enabled', False):
        return None
        
    # Format name to include identifier in brackets if there's a display name
    display_name = source_config.get('display_name')
    name = f"{display_name} ({source_id})" if display_name else source_id
        
    base_response = {
        'name': name,
        'connected': False,
        'error': None,
        'details': {
            'type': source_type,
            'identifier': source_id,
            'media_type': source_config.get('media_type', 'All')
        }
    }
    
    try:
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
            
        elif source_type in ['Trakt Watchlist', 'Trakt Lists', 'Friends Trakt Watchlist']:
            from content_checkers.trakt import ensure_trakt_auth, get_trakt_headers, make_trakt_request
            
            # First ensure we have valid authentication
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
            
        elif source_type in ['My Plex Watchlist', 'Other Plex Watchlist']:
            from content_checkers.plex_watchlist import MyPlexAccount
            
            # Get the appropriate token based on source type
            if source_type == 'Other Plex Watchlist':
                token = source_config.get('token', '').strip()
                username = source_config.get('username', '').strip()
                if not token or not username:
                    base_response['error'] = 'Token or username not configured'
                    base_response['connected'] = False
                    return base_response
            else:
                from utilities.settings import get_setting
                token = get_setting('Plex', 'token')
                if not token:
                    token = get_setting('File Management', 'plex_token_for_symlink')
                if not token:
                    base_response['error'] = 'Plex token not configured'
                    base_response['connected'] = False
                    return base_response
            
            try:
                # Try to connect with the token
                account = MyPlexAccount(token=token)
                
                # For Other Plex Watchlist, verify username matches
                if source_type == 'Other Plex Watchlist' and account.username != username:
                    base_response['error'] = f'Token does not match username. Expected: {username}, Got: {account.username}'
                    base_response['connected'] = False
                    return base_response
                
                # If we get here, connection was successful
                base_response['connected'] = True
                base_response['details'].update({
                    'username': account.username,
                    'email': account.email
                })
            except Exception as e:
                base_response['error'] = f'Failed to authenticate: {str(e)}'
                base_response['connected'] = False
            
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
            
    except requests.Timeout:
        base_response['error'] = 'Connection timed out'
        base_response['connected'] = False
    except requests.ConnectionError:
        base_response['error'] = 'Connection refused'
        base_response['connected'] = False
    except Exception as e:
        base_response['error'] = str(e)
        base_response['connected'] = False
        
    return base_response

def check_content_sources_connections():
    """Check connections to all enabled content sources."""
    from utilities.settings import get_setting
    
    content_sources = get_setting('Content Sources')
    if not content_sources:
        return []
        
    source_statuses = []
    for source_id, source_config in content_sources.items():
        # Skip sources with "Collected" in their name
        if 'Collected' in source_id:
            continue
            
        status = check_content_source_connection(source_id, source_config)
        if status:  # Only include if status was returned (source is enabled)
            source_statuses.append(status)
            
    return source_statuses

def get_trakt_sources() -> Dict[str, List[Dict[str, Any]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Watchlist')]
    list_sources = [data for source, data in content_sources.items() if source.startswith('Trakt Lists')]
    friend_watchlist_sources = [data for source, data in content_sources.items() if source.startswith('Friends Trakt Watchlist')]
    
    return {
        'watchlist': watchlist_sources,
        'lists': list_sources,
        'friend_watchlist': friend_watchlist_sources
    }

@connections_bp.route('/')
def index():
    """Display the connections status page."""
    # Get connection statuses
    cli_battery_status = check_cli_battery_connection()
    plex_status = check_plex_connection()
    mounted_files_status = check_mounted_files_connection()
    phalanx_db_status = check_phalanx_db_connection()
    scraper_statuses = check_scrapers_connections()
    content_source_statuses = check_content_sources_connections()
    
    # Collect failing connections
    failing_connections = []
    if not cli_battery_status['connected']:
        failing_connections.append(cli_battery_status)
    if plex_status and not plex_status['connected']:
        failing_connections.append(plex_status)
    if mounted_files_status and not mounted_files_status['connected']:
        failing_connections.append(mounted_files_status)
    if not phalanx_db_status['connected']:
        failing_connections.append(phalanx_db_status)
    
    # Add any failing scraper connections
    failing_connections.extend([s for s in scraper_statuses if not s['connected']])
    
    # Add any failing content source connections (excluding Collected sources)
    failing_connections.extend([s for s in content_source_statuses if not s['connected'] and not s['name'].startswith('Collected')])
    
    return render_template('connections.html', 
                         cli_battery_status=cli_battery_status,
                         plex_status=plex_status,
                         mounted_files_status=mounted_files_status,
                         phalanx_db_status=phalanx_db_status,
                         scraper_statuses=scraper_statuses,
                         content_source_statuses=content_source_statuses,
                         failing_connections=failing_connections)