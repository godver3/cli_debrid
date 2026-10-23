from flask import Blueprint, jsonify, request
import os
from plexapi.server import PlexServer
from plexapi.exceptions import Unauthorized, NotFound
import requests
from urllib.parse import urlparse
from trakt.core import get_device_code
from utilities.jellyfin_auth import jellyfin_auth_headers
from routes.models import admin_required

settings_validation_bp = Blueprint('settings_validation', __name__)

def validate_plex_settings(plex_url, plex_token):
    """Validate Plex connection settings."""
    try:
        # First validate URL format
        parsed_url = urlparse(plex_url)
        if not all([parsed_url.scheme, parsed_url.netloc]):
            return False, "Invalid Plex URL format. Must include protocol (http/https) and host"
        
        # Remove trailing slashes and validate connection
        plex_url = plex_url.rstrip('/')
        PlexServer(plex_url, plex_token)
        return True, "Successfully connected to Plex server"
    except Unauthorized:
        return False, "Invalid Plex token"
    except NotFound:
        return False, "Could not find Plex server at the specified URL"
    except Exception as e:
        return False, f"Error connecting to Plex: {str(e)}"

def validate_path_exists(path, require_writable=True):
    """Validate if a path exists and is accessible.

    Debrid/usenet mounts are normally read-only, so pass require_writable=False
    for the original files path.
    """
    try:
        if not path:
            return False, "Path cannot be empty"
            
        exists = os.path.exists(path)
        is_dir = os.path.isdir(path) if exists else False
        is_readable = os.access(path, os.R_OK) if exists else False
        is_writable = os.access(path, os.W_OK) if exists else False
        
        if not exists:
            return False, "Path does not exist"
        if not is_dir:
            return False, "Path exists but is not a directory"
        if not is_readable:
            return False, "Path exists but is not readable"
        if require_writable and not is_writable:
            return False, "Path exists but is not writable"
        
        return True, "Path exists and is accessible"
    except Exception as e:
        return False, f"Error checking path: {str(e)}"

def validate_symlink_setup(original_path, symlink_path):
    """Validate symlink setup configuration."""
    if not original_path or not symlink_path:
        return False, "Both original and symlink paths must be provided"
        
    original_valid, original_msg = validate_path_exists(original_path, require_writable=False)
    if not original_valid:
        return False, f"Original files path invalid: {original_msg}"
    
    # For symlink path, we just need to ensure the parent directory exists
    symlink_parent = os.path.dirname(symlink_path)
    parent_valid, parent_msg = validate_path_exists(symlink_parent)
    if not parent_valid:
        return False, f"Symlink parent directory invalid: {parent_msg}"
    
    # Check if paths are different
    if os.path.abspath(original_path) == os.path.abspath(symlink_path):
        return False, "Original path and symlink path cannot be the same"
    
    return True, "Symlink configuration is valid"

def validate_plex_libraries(libraries_str):
    """Validate Plex library string format."""
    if not libraries_str:
        return False, "No libraries specified"
    
    libraries = [lib.strip() for lib in libraries_str.split(',')]
    if not libraries:
        return False, "Invalid library format. Use comma-separated values"
    
    return True, f"Found {len(libraries)} libraries"

def validate_debrid_api_key(api_key, provider="RealDebrid"):
    """Validate Debrid API key format and basic authentication."""
    if not api_key:
        return False, "API key is required"
    
    if provider == "RealDebrid":
        try:
            # Make a test request to the user endpoint
            response = requests.get(
                "https://api.real-debrid.com/rest/1.0/user",
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=10
            )

            if response.status_code == 200:
                return True, "RealDebrid API key is valid"
            elif response.status_code == 401:
                return False, "Invalid RealDebrid API key"
            elif response.status_code == 403:
                return False, "Access denied - please check your RealDebrid API key"
            else:
                return False, f"RealDebrid API error (HTTP {response.status_code})"
        except requests.exceptions.Timeout:
            return False, "Timeout while validating RealDebrid API key"
        except requests.exceptions.RequestException as e:
            return False, f"Error validating RealDebrid API key: {str(e)}"

    elif provider == "AllDebrid":
        try:
            # Make a test request to the user endpoint
            response = requests.get(
                "https://api.alldebrid.com/v4.1/user",
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=10
            )

            if response.status_code == 200:
                # AllDebrid returns JSON with status field
                try:
                    data = response.json()
                    if data.get('status') == 'success':
                        return True, "AllDebrid API key is valid"
                    else:
                        return False, "Invalid AllDebrid API response"
                except ValueError:
                    return False, "Invalid AllDebrid API response format"
            elif response.status_code == 401:
                return False, "Invalid AllDebrid API key"
            elif response.status_code == 403:
                return False, "Access denied - please check your AllDebrid API key"
            else:
                return False, f"AllDebrid API error (HTTP {response.status_code})"
        except requests.exceptions.Timeout:
            return False, "Timeout while validating AllDebrid API key"
        except requests.exceptions.RequestException as e:
            return False, f"Error validating AllDebrid API key: {str(e)}"

    elif provider == "Torbox":
        try:
            response = requests.get(
                "https://api.torbox.app/v1/api/user/me",
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=10
            )
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data.get('success') is True:
                        return True, "Torbox API key is valid"
                    return False, "Invalid Torbox API response"
                except ValueError:
                    return False, "Invalid Torbox API response format"
            elif response.status_code == 401:
                return False, "Invalid Torbox API key"
            elif response.status_code == 403:
                return False, "Access denied - please check your Torbox API key"
            else:
                return False, f"Torbox API error (HTTP {response.status_code})"
        except requests.exceptions.Timeout:
            return False, "Timeout while validating Torbox API key"
        except requests.exceptions.RequestException as e:
            return False, f"Error validating Torbox API key: {str(e)}"

    elif provider == "DebridLink":
        try:
            response = requests.get(
                "https://debrid-link.com/api/v2/account/infos",
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=10
            )
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data.get('success') is True:
                        return True, "Debrid-Link API key is valid"
                    return False, "Invalid Debrid-Link API response"
                except ValueError:
                    return False, "Invalid Debrid-Link API response format"
            elif response.status_code == 401:
                return False, "Invalid Debrid-Link API key"
            elif response.status_code == 403:
                return False, "Access denied - please check your Debrid-Link API key"
            else:
                return False, f"Debrid-Link API error (HTTP {response.status_code})"
        except requests.exceptions.Timeout:
            return False, "Timeout while validating Debrid-Link API key"
        except requests.exceptions.RequestException as e:
            return False, f"Error validating Debrid-Link API key: {str(e)}"

    return False, f"Unsupported debrid provider: {provider}"

def validate_trakt_credentials(client_id, client_secret):
    """Validate Trakt credentials format and basic authentication."""
    if not client_id or not client_secret:
        return False, "Both Client ID and Client Secret are required"
    
    # Validate basic sanity (Trakt has changed key lengths over time, so we
    # avoid hardcoding an exact character count and only guard against
    # obviously-wrong input, e.g. empty/placeholder text pasted by mistake).
    if len(client_id.strip()) < 10:
        return False, "Invalid Trakt Client ID format (too short)"
    if len(client_secret.strip()) < 10:
        return False, "Invalid Trakt Client Secret format (too short)"
    
    try:
        # Try to get a device code - this validates both client ID and secret
        device_code_response = get_device_code(client_id, client_secret)
        
        # Add debug logging
        print(f"Trakt API Response: {device_code_response}")
        
        if not device_code_response:
            return False, "Empty response from Trakt API"
            
        # If we got a response with the expected fields, the credentials are valid
        if all(key in device_code_response for key in ['device_code', 'user_code', 'verification_url']):
            return True, "Trakt credentials are valid"
        else:
            missing_keys = [key for key in ['device_code', 'user_code', 'verification_url'] if key not in device_code_response]
            return False, f"Invalid response from Trakt API - missing fields: {', '.join(missing_keys)}"
            
    except ValueError as e:
        return False, f"Invalid JSON response from Trakt API: {str(e)}"
    except Exception as e:
        error_msg = str(e)
        if "Invalid API key" in error_msg:
            return False, "Invalid Trakt Client ID"
        elif "Invalid client credentials" in error_msg:
            return False, "Invalid Trakt Client Secret"
        elif "Not Found" in error_msg:
            return False, "Trakt API endpoint not found - please check your credentials"
        else:
            return False, f"Error validating Trakt credentials: {error_msg} - Please ensure your Client ID and Secret are correct"

USENET_BACKEND_NAMES = {'climount': 'cli_mount', 'nzbdav': 'NzbDAV', 'zurg': 'Zurg'}


def validate_usenet_provider(backend, url, token=''):
    """Check that the chosen usenet backend answers at url, and that the token works."""
    backend = (backend or 'climount').strip().lower()
    name = USENET_BACKEND_NAMES.get(backend, 'Usenet provider')
    url = (url or '').strip().rstrip('/')
    if not url:
        return False, f"{name} URL is required"
    parsed = urlparse(url)
    if not (parsed.scheme and parsed.netloc):
        return False, f"Invalid {name} URL. Include http:// and the port, e.g. http://192.168.1.50:8282"

    if backend == 'climount':
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        try:
            r = requests.get(f"{url}/version", headers=headers, timeout=10)
        except requests.RequestException as e:
            return False, f"Could not connect to cli_mount: {e}"
        if r.status_code in (401, 403):
            return False, "cli_mount rejected the API token"
        if r.status_code != 200:
            return False, f"cli_mount returned HTTP {r.status_code}"
        return True, "Connected to cli_mount"

    # NzbDAV and Zurg speak the SABnzbd API.
    if url.endswith('/api'):
        url = url[:-4].rstrip('/')
    try:
        r = requests.get(f"{url}/api", params={'mode': 'version'}, timeout=10)
        version = r.json().get('version') if r.status_code == 200 else None
    except (requests.RequestException, ValueError) as e:
        return False, f"Could not connect to {name}: {e}"
    if not version:
        return False, f"{name} did not answer the SABnzbd version call (HTTP {r.status_code}). Check the URL and port."
    try:
        r = requests.get(f"{url}/api", params={'mode': 'get_cats', 'apikey': token}, timeout=10)
        key_ok = r.status_code == 200 and isinstance((r.json() or {}).get('categories'), list)
    except (requests.RequestException, ValueError):
        key_ok = False
    if not key_ok:
        return False, f"Connected to {name} {version}, but it rejected the API key"
    return True, f"Connected to {name} {version}"


def validate_tvdb_api_key(api_key):
    """Log in to TVDB v4 with the key."""
    api_key = (api_key or '').strip()
    if not api_key:
        return False, "TVDB API key is required"
    try:
        r = requests.post("https://api4.thetvdb.com/v4/login", json={'apikey': api_key}, timeout=10)
    except requests.RequestException as e:
        return False, f"Could not reach TVDB: {e}"
    if r.status_code == 200:
        return True, "TVDB API key is valid"
    if r.status_code == 401:
        return False, "TVDB rejected the API key"
    return False, f"TVDB returned HTTP {r.status_code}"


def validate_tmdb_api_key(api_key):
    """Check a TMDB v3 API key against the configuration endpoint."""
    api_key = (api_key or '').strip()
    if not api_key:
        return False, "TMDB API key is required"
    if api_key.startswith('eyJ'):
        return False, "That looks like the TMDB 'API Read Access Token'. Use the shorter 'API Key' instead"
    try:
        r = requests.get("https://api.themoviedb.org/3/configuration", params={'api_key': api_key}, timeout=10)
    except requests.RequestException as e:
        return False, f"Could not reach TMDB: {e}"
    if r.status_code == 200:
        return True, "TMDB API key is valid"
    if r.status_code == 401:
        return False, "TMDB rejected the API key"
    return False, f"TMDB returned HTTP {r.status_code}"


def check_mount_path(path):
    """Look at the folder cli_debrid reads media from.

    Returns (ok, message, warning). warning is True when the path works but
    something looks off, such as an empty folder or a parent of __all__.
    """
    path = (path or '').strip()
    if not path:
        return False, "Path cannot be empty", False
    if not os.path.exists(path):
        return False, ("Path does not exist inside cli_debrid. Check that the mount is running and "
                       "that your docker volumes map it to this path"), False
    if not os.path.isdir(path):
        return False, "Path exists but is not a folder", False
    try:
        entries = os.listdir(path)
    except OSError as e:
        return False, f"Path exists but cannot be read: {e}", False
    if '__all__' in entries and os.path.basename(path.rstrip('/')) != '__all__':
        return True, (f"This folder has an __all__ subfolder. Point cli_debrid at "
                      f"{os.path.join(path, '__all__')} instead"), True
    if not entries:
        return True, ("Path exists but is empty. Is the mount running? "
                      "(A new usenet-only setup stays empty until the first download.)"), True
    return True, f"Mount is readable ({len(entries)} items)", False


def _is_on(value):
    return value in (True, 'on', 'true', 'True', '1', 1)


@settings_validation_bp.route('/onboarding-settings', methods=['POST'])
@admin_required
def validate_onboarding_settings():
    """Validate the onboarding step 2 form before the user moves on.

    Every check has a name, valid and message. Checks marked warning=True are
    shown but don't stop the user from continuing.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({
            'valid': False,
            'checks': [{
                'name': 'Request Error',
                'valid': False,
                'message': 'Invalid request data'
            }]
        }), 400

    management_type = data.get('management_type', '')
    settings_data = data.get('settings', {})
    checks = []

    def add(name, valid, message, blocking=True, warning=False):
        checks.append({'name': name, 'valid': valid, 'message': message,
                       'warning': warning or (not valid and not blocking)})

    if management_type == 'skip':
        add('Skip Validation', True, 'Library management setup skipped')
        return jsonify({'valid': True, 'checks': checks})

    # --- Debrid / usenet provider -------------------------------------------------
    use_debrid = _is_on(settings_data.get('use_debrid'))
    use_usenet = _is_on(settings_data.get('use_usenet'))
    if 'use_debrid' not in settings_data and 'use_usenet' not in settings_data:
        # Older form: one provider_type tab.
        use_usenet = settings_data.get('provider_type') == 'usenet'
        use_debrid = not use_usenet
    if not use_debrid and not use_usenet:
        add('Provider', False, 'Choose a debrid provider, a usenet provider, or both')
    if use_debrid:
        ok, msg = validate_debrid_api_key(settings_data.get('debrid_api_key', ''),
                                          settings_data.get('debrid_provider', 'RealDebrid'))
        add('Debrid Provider', ok, msg)
    if use_usenet:
        backend = settings_data.get('usenet_backend', 'climount')
        ok, msg = validate_usenet_provider(backend, settings_data.get('usenet_url', ''),
                                           settings_data.get('usenet_api_token', ''))
        add(f"Usenet Provider ({USENET_BACKEND_NAMES.get(backend, backend)})", ok, msg)

    # --- Mount ----------------------------------------------------------------------
    original_path = (settings_data.get('original_files_path') or '').strip()
    update_plex = _is_on(settings_data.get('Plex.update_plex_on_file_discovery'))
    is_symlink = management_type in ('plex_symlink', 'Local', 'Symlinked/Local')
    if is_symlink or update_plex or original_path:
        if original_path:
            ok, msg, warn = check_mount_path(original_path)
        else:
            ok, msg, warn = False, 'Enter the folder where your mount shows your media', False
        # In Plex mode the path is only needed for "Update Plex on file discovery".
        add('Mount Path', ok, msg, blocking=is_symlink or update_plex, warning=warn)

    # --- Library / media server -----------------------------------------------------
    if management_type in ('plex_direct', 'plex_symlink', 'Plex'):
        plex_valid, plex_message = validate_plex_settings(settings_data.get('plex_url', ''),
                                                          settings_data.get('plex_token', ''))
        add('Plex Connection', plex_valid, plex_message)
        if plex_valid:
            movie_libraries = settings_data.get('movie_libraries', '')
            shows_libraries = settings_data.get('shows_libraries', '')
            if movie_libraries:
                add('Movie Libraries', *validate_plex_libraries(movie_libraries))
            if shows_libraries:
                add('TV Show Libraries', *validate_plex_libraries(shows_libraries))
            if not movie_libraries and not shows_libraries:
                add('Libraries Configuration', False, 'At least one movie or TV show library must be specified')

    if is_symlink:
        symlink_path = (settings_data.get('symlinked_files_path') or '').strip()
        if not symlink_path:
            add('Symlink Folder', False, 'Symlinked files path is required')
        elif original_path and os.path.abspath(original_path) == os.path.abspath(symlink_path):
            add('Symlink Folder', False, 'The symlink folder must be different from the mount path')
        else:
            parent_ok, parent_msg = validate_path_exists(os.path.dirname(symlink_path.rstrip('/')) or '/')
            add('Symlink Folder', parent_ok,
                'Symlink folder can be created here' if parent_ok else f'Parent folder: {parent_msg}')

        # The media server is optional in symlink mode, so it never blocks.
        media_server = settings_data.get('symlink_media_server', 'none')
        if media_server == 'plex':
            plex_url = settings_data.get('plex_url_for_symlink', '')
            plex_token = settings_data.get('plex_token_for_symlink', '')
            if plex_url and plex_token:
                add('Plex Connection', *validate_plex_settings(plex_url, plex_token), blocking=False)
        elif media_server == 'jellyfin':
            jellyfin_url = settings_data.get('emby_jellyfin_url', '')
            jellyfin_token = settings_data.get('emby_jellyfin_token', '')
            if jellyfin_url and jellyfin_token:
                try:
                    resp = requests.get(f"{jellyfin_url.rstrip('/')}/System/Info",
                                        headers=jellyfin_auth_headers(jellyfin_token), timeout=5)
                    jf_valid = resp.status_code == 200
                    jf_message = 'Jellyfin connection successful' if jf_valid else f'Jellyfin returned status {resp.status_code}'
                except Exception as e:
                    jf_valid = False
                    jf_message = f'Error connecting to Jellyfin: {str(e)}'
                add('Jellyfin Connection', jf_valid, jf_message, blocking=False)

    # --- Metadata -------------------------------------------------------------------
    # Shows and movies are looked up on TVDB when a TVDB key is set, otherwise on
    # Trakt, so one of the two is required.
    tvdb_key = (settings_data.get('tvdb_api_key') or '').strip()
    tmdb_key = (settings_data.get('tmdb_api_key') or '').strip()
    trakt_client_id = (settings_data.get('trakt_client_id') or '').strip()
    trakt_client_secret = (settings_data.get('trakt_client_secret') or '').strip()
    if tvdb_key:
        add('TVDB', *validate_tvdb_api_key(tvdb_key))
    if trakt_client_id or trakt_client_secret:
        add('Trakt Configuration', *validate_trakt_credentials(trakt_client_id, trakt_client_secret),
            blocking=not tvdb_key)
    if not tvdb_key and not (trakt_client_id and trakt_client_secret):
        add('Metadata', False, 'Add a TVDB API key (recommended) or Trakt credentials. '
            'cli_debrid needs one of them to look up shows and movies')
    if tmdb_key:
        add('TMDB', *validate_tmdb_api_key(tmdb_key))
    else:
        add('TMDB', False, 'No TMDB API key. Posters, digital/physical release dates and Discover '
            'need one, so adding it is recommended', blocking=False)

    all_valid = all(c['valid'] or c['warning'] for c in checks)
    return jsonify({'valid': all_valid, 'checks': checks})
