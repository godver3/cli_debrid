from flask import Blueprint, jsonify, request
import os
from plexapi.server import PlexServer
from plexapi.exceptions import Unauthorized, NotFound
import requests
from urllib.parse import urlparse
from trakt.core import get_device_code

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

def validate_path_exists(path):
    """Validate if a path exists and is accessible."""
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
        if not is_writable:
            return False, "Path exists but is not writable"
        
        return True, "Path exists and is accessible"
    except Exception as e:
        return False, f"Error checking path: {str(e)}"

def validate_symlink_setup(original_path, symlink_path):
    """Validate symlink setup configuration."""
    if not original_path or not symlink_path:
        return False, "Both original and symlink paths must be provided"
        
    original_valid, original_msg = validate_path_exists(original_path)
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

@settings_validation_bp.route('/onboarding-settings', methods=['POST'])
def validate_onboarding_settings():
    """Validate settings based on the selected management type."""
    data = request.get_json()
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
    validation_checks = []
    all_valid = True

    if management_type == 'skip':
        return jsonify({
            'valid': True,
            'checks': [{
                'name': 'Skip Validation',
                'valid': True,
                'message': 'Library management setup skipped'
            }]
        })

    # Common checks for all non-skip options
    # Check if update_plex_on_file_discovery is enabled
    update_plex = settings_data.get('Plex.update_plex_on_file_discovery') == 'on'
    original_path = settings_data.get('original_files_path', '')

    # If update_plex_on_file_discovery is enabled, original_files_path is required
    if update_plex:
        if not original_path:
            validation_checks.append({
                'name': 'Original Files Path',
                'valid': False,
                'message': 'Original files path is required when "Update Plex on file discovery" is enabled'
            })
            all_valid = False
        else:
            original_valid, original_message = validate_path_exists(original_path)
            validation_checks.append({
                'name': 'Original Files Path',
                'valid': original_valid,
                'message': original_message
            })
            all_valid = all_valid and original_valid
    # If update_plex_on_file_discovery is not enabled, original_files_path is optional
    # during onboarding — skip validation even if a default value is pre-filled

    if management_type in ['plex_direct', 'plex_symlink', 'Plex']:
        # Validate Plex settings
        plex_url = settings_data.get('plex_url', '')
        plex_token = settings_data.get('plex_token', '')
        
        plex_valid, plex_message = validate_plex_settings(plex_url, plex_token)
        validation_checks.append({
            'name': 'Plex Connection',
            'valid': plex_valid,
            'message': plex_message
        })
        all_valid = all_valid and plex_valid

        # Validate libraries if Plex connection is valid
        if plex_valid:
            movie_libraries = settings_data.get('movie_libraries', '')
            shows_libraries = settings_data.get('shows_libraries', '')
            
            if movie_libraries:
                movie_valid, movie_message = validate_plex_libraries(movie_libraries)
                validation_checks.append({
                    'name': 'Movie Libraries',
                    'valid': movie_valid,
                    'message': movie_message
                })
                all_valid = all_valid and movie_valid
            
            if shows_libraries:
                shows_valid, shows_message = validate_plex_libraries(shows_libraries)
                validation_checks.append({
                    'name': 'TV Show Libraries',
                    'valid': shows_valid,
                    'message': shows_message
                })
                all_valid = all_valid and shows_valid
            
            if not movie_libraries and not shows_libraries:
                validation_checks.append({
                    'name': 'Libraries Configuration',
                    'valid': False,
                    'message': 'At least one movie or TV show library must be specified'
                })
                all_valid = False

    if management_type in ['plex_symlink', 'Local', 'Symlinked/Local']:
        # Validate symlink settings
        symlink_path = settings_data.get('symlinked_files_path', '')

        if symlink_path and original_path:
            symlink_valid, symlink_message = validate_symlink_setup(original_path, symlink_path)
            validation_checks.append({
                'name': 'Symlink Configuration',
                'valid': symlink_valid,
                'message': symlink_message
            })
            all_valid = all_valid and symlink_valid
        elif symlink_path:
            validation_checks.append({
                'name': 'Symlink Configuration',
                'valid': True,
                'message': 'Symlink path is configured'
            })

        # Check which media server is configured (optional)
        symlink_media_server = settings_data.get('symlink_media_server', 'none')

        if symlink_media_server == 'plex':
            plex_url = settings_data.get('plex_url_for_symlink', '')
            plex_token = settings_data.get('plex_token_for_symlink', '')
            if plex_url and plex_token:
                plex_valid, plex_message = validate_plex_settings(plex_url, plex_token)
                validation_checks.append({
                    'name': 'Plex Connection',
                    'valid': plex_valid,
                    'message': plex_message
                })
                # Don't affect all_valid — media server is optional
        elif symlink_media_server == 'jellyfin':
            jellyfin_url = settings_data.get('emby_jellyfin_url', '')
            jellyfin_token = settings_data.get('emby_jellyfin_token', '')
            if jellyfin_url and jellyfin_token:
                try:
                    import requests as _req
                    resp = _req.get(f"{jellyfin_url.rstrip('/')}/System/Info",
                                    headers={'X-Emby-Token': jellyfin_token}, timeout=5)
                    jf_valid = resp.status_code == 200
                    jf_message = 'Jellyfin connection successful' if jf_valid else f'Jellyfin returned status {resp.status_code}'
                except Exception as e:
                    jf_valid = False
                    jf_message = f'Error connecting to Jellyfin: {str(e)}'
                validation_checks.append({
                    'name': 'Jellyfin Connection',
                    'valid': jf_valid,
                    'message': jf_message
                })
                # Don't affect all_valid — media server is optional
        else:
            # Fallback: check old-style plex_url_for_symlink fields (backwards compat)
            plex_url = settings_data.get('plex_url_for_symlink', '')
            plex_token = settings_data.get('plex_token_for_symlink', '')
            if plex_url and plex_token:
                plex_valid, plex_message = validate_plex_settings(plex_url, plex_token)
                validation_checks.append({
                    'name': 'Plex Integration (Optional)',
                    'valid': plex_valid,
                    'message': plex_message
                })

    # Validate Debrid or Usenet provider
    provider_type = settings_data.get('provider_type', 'debrid')
    if provider_type == 'usenet':
        usenet_url = settings_data.get('usenet_url', '').strip()
        if usenet_url:
            try:
                usenet_token = settings_data.get('usenet_api_token', '')
                headers = {'Authorization': f'Bearer {usenet_token}'} if usenet_token else {}
                r = requests.get(f"{usenet_url.rstrip('/')}/version", headers=headers, timeout=10)
                if r.status_code == 200:
                    provider_valid, provider_message = True, "Successfully connected to cli_mount"
                else:
                    provider_valid, provider_message = False, f"cli_mount returned HTTP {r.status_code}"
            except Exception as e:
                provider_valid, provider_message = False, f"Could not connect to cli_mount: {str(e)}"
        else:
            provider_valid, provider_message = False, "cli_mount URL is required"
        validation_checks.append({
            'name': 'Usenet Provider (cli_mount)',
            'valid': provider_valid,
            'message': provider_message
        })
    else:
        debrid_provider = settings_data.get('debrid_provider', 'RealDebrid')
        debrid_api_key = settings_data.get('debrid_api_key', '')
        provider_valid, provider_message = validate_debrid_api_key(debrid_api_key, debrid_provider)
        validation_checks.append({
            'name': 'Debrid Provider',
            'valid': provider_valid,
            'message': provider_message
        })
    all_valid = all_valid and provider_valid

    # Validate Trakt settings — Trakt is optional, so only validate (and gate
    # all_valid on) it if the user actually entered credentials.
    trakt_client_id = settings_data.get('trakt_client_id', '')
    trakt_client_secret = settings_data.get('trakt_client_secret', '')
    if trakt_client_id or trakt_client_secret:
        trakt_valid, trakt_message = validate_trakt_credentials(trakt_client_id, trakt_client_secret)
        validation_checks.append({
            'name': 'Trakt Configuration',
            'valid': trakt_valid,
            'message': trakt_message
        })
        all_valid = all_valid and trakt_valid

    return jsonify({
        'valid': all_valid,
        'checks': validation_checks
    }) 
