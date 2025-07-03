from flask import Blueprint, jsonify, request, render_template, session, current_app
from flask_login import login_required # Assuming you have role checks or just login requirement
import requests
import json
import os
import uuid
import time
from utilities.settings import get_config_dir, load_config, save_config # Import main config functions
from utilities.file_lock import FileLock # For safe JSON writing
from datetime import datetime # To timestamp the fetch
from .models import admin_required # Added import for admin_required

user_token_bp = Blueprint('user_token', __name__)

USER_TOKEN_FILE_NAME = 'user_plex_tokens.json'
USER_TOKEN_LOCK_FILE_NAME = '.user_plex_tokens.lock'

# --- Helper Functions for Token Storage ---

def get_user_token_file_path():
    """Gets the full path to the user tokens JSON file."""
    return os.path.join(get_config_dir(), USER_TOKEN_FILE_NAME)

def get_user_token_lock_file_path():
    """Gets the full path to the lock file for user tokens."""
    return os.path.join(get_config_dir(), USER_TOKEN_LOCK_FILE_NAME)

def load_user_tokens():
    """Loads user tokens from the JSON file."""
    token_file = get_user_token_file_path()
    lock_file_path = get_user_token_lock_file_path()
    # Ensure lock file directory exists
    os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)

    fd = None
    try:
        # Open the lock file first
        fd = open(lock_file_path, 'a+') # Use 'a+' to create if not exists, allow read/write
        fd.seek(0) # Go to the beginning for reading if needed by lock

        with FileLock(fd): # Pass the file handle to FileLock
            if os.path.exists(token_file):
                # Open the actual token file *inside* the lock
                with open(token_file, 'r') as f:
                    try:
                        # Move reading inside the lock too
                        content = f.read()
                        if not content: # Handle empty file case
                            return {}
                        return json.loads(content)
                    except json.JSONDecodeError:
                        current_app.logger.error(f"Error decoding JSON from {token_file}. File might be corrupt.")
                        return {} # Return empty if file is corrupt
            else:
                return {} # File doesn't exist
    except (IOError, Exception) as e:
        current_app.logger.error(f"Error loading user tokens: {e}", exc_info=True)
        return {} # Return empty on error
    finally:
        # Ensure the lock file handle is closed
        if fd:
            try:
                fd.close()
            except Exception as e:
                current_app.logger.error(f"Error closing lock file handle during load: {e}")

def save_user_tokens(tokens):
    """Saves user tokens to the JSON file."""
    token_file = get_user_token_file_path()
    lock_file_path = get_user_token_lock_file_path()
    # Ensure lock file directory exists
    os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)

    fd = None
    try:
        # Open the lock file first
        fd = open(lock_file_path, 'r+') # Open for read/write, must exist or error (use 'a+' if create needed)
        # If using 'a+', uncomment: fd.seek(0); fd.truncate() # Clear content if needed before write lock

        with FileLock(fd): # Pass the file handle
            # Create directory for token file if it doesn't exist
            os.makedirs(os.path.dirname(token_file), exist_ok=True)
            # Open and write the token file *inside* the lock
            with open(token_file, 'w') as f:
                json.dump(tokens, f, indent=2)
    except (IOError, Exception) as e:
        current_app.logger.error(f"Error saving user tokens: {e}", exc_info=True)
        # Optionally re-raise or handle more gracefully
    finally:
         # Ensure the lock file handle is closed
        if fd:
            try:
                fd.close()
            except Exception as e:
                current_app.logger.error(f"Error closing lock file handle during save: {e}")


# --- Routes ---

@user_token_bp.route('/collect_tokens')
@admin_required
def collect_tokens_page():
    """Renders the page for the admin to manage user token collection."""
    stored_tokens = load_user_tokens()
    # Only pass usernames to the template, not the tokens!
    stored_usernames = list(stored_tokens.keys())
    return render_template('user_token_collection.html', stored_usernames=stored_usernames)

@user_token_bp.route('/collect_tokens/initiate', methods=['POST'])
@admin_required
def initiate_user_plex_auth():
    """Generates a Plex PIN for a user to authenticate."""
    try:
        # Use a unique client ID for this flow, different from admin's main one
        # Store it temporarily in session or pass back to client if needed for polling check
        client_id = f"cli-debrid-user-auth-{uuid.uuid4()}"
        session['user_auth_client_id'] = client_id # Store for checking pin

        headers = {
            'Accept': 'application/json',
            'X-Plex-Product': 'cli_debrid (User Auth)', # Identify the purpose
            'X-Plex-Version': '0.6.07', # Example version
            'X-Plex-Client-Identifier': client_id,
            # Add other necessary X-Plex headers similar to onboarding
            'X-Plex-Platform': 'Web',
            'X-Plex-Device': 'Browser (User Auth)',
        }

        response = requests.post('https://plex.tv/api/v2/pins', headers=headers, json={'strong': True})
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

        pin_data = response.json()

        # Construct the user-facing auth URL
        auth_url = (
            'https://app.plex.tv/auth#?' +
            f'clientID={client_id}&' +
            f'code={pin_data["code"]}&' +
            'context%5Bdevice%5D%5Bproduct%5D=cli_debrid%20(User%20Auth)' # URL encoded product name
            # Add other context fields if desired
        )

        # Store pin_id for polling check, maybe associate with client_id if needed
        session['current_user_auth_pin_id'] = pin_data['id']

        return jsonify({
            'success': True,
            'pin_id': pin_data['id'], # Needed for polling
            'code': pin_data['code'], # For display to admin
            'auth_url': auth_url,     # For display to admin
            'client_id': client_id    # Needed for checking pin status
        })

    except requests.exceptions.RequestException as e:
        current_app.logger.error(f"Plex API request failed: {e}")
        return jsonify({'success': False, 'error': f"Plex API request failed: {e}"}), 500
    except Exception as e:
        current_app.logger.error(f"Error initiating user Plex auth: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@user_token_bp.route('/collect_tokens/check_pin', methods=['POST'])
@admin_required
def check_user_plex_pin():
    """Checks the status of the PIN the user is authorizing."""
    try:
        pin_id = request.json.get('pin_id')
        client_id = request.json.get('client_id') # Get client_id used for this specific auth attempt

        if not pin_id or not client_id:
            return jsonify({'success': False, 'error': 'Pin ID and Client ID are required'}), 400

        headers = {
            'Accept': 'application/json',
            'X-Plex-Client-Identifier': client_id
        }

        response = requests.get(f'https://plex.tv/api/v2/pins/{pin_id}', headers=headers)

        if response.status_code == 200:
            pin_data = response.json()
            auth_token = pin_data.get('authToken')
            if auth_token:
                # Pin authorized! Now get the username associated with this token
                user_info_response = requests.get(
                    'https://plex.tv/api/v2/user',
                    headers={**headers, 'X-Plex-Token': auth_token}
                )
                if user_info_response.status_code == 200:
                    user_data = user_info_response.json()
                    username = user_data.get('username', 'Unknown Plex User')

                    # Immediately store the token upon successful verification
                    tokens = load_user_tokens()
                    tokens[username] = auth_token # Store token associated with username
                    save_user_tokens(tokens)

                    return jsonify({
                        'success': True,
                        'status': 'authorized',
                        'username': username,
                        # DO NOT return the token to the admin's browser
                    })
                else:
                     # Couldn't verify the token right away, treat as error
                     current_app.logger.error(f"Failed to verify token {auth_token[:4]}... for user after PIN auth. Status: {user_info_response.status_code}")
                     return jsonify({'success': False, 'status': 'error', 'error': 'Could not verify user identity after authorization.'})
            else:
                # Still waiting
                return jsonify({'success': True, 'status': 'waiting'})
        elif response.status_code == 404:
            return jsonify({'success': False, 'status': 'error', 'error': 'Pin expired or invalid.'})
        else:
            response.raise_for_status() # Handle other potential errors

    except requests.exceptions.RequestException as e:
        current_app.logger.error(f"Plex API request failed during pin check: {e}")
        return jsonify({'success': False, 'status': 'error', 'error': f"Plex API request failed: {e}"}), 500
    except Exception as e:
        current_app.logger.error(f"Error checking user Plex pin: {e}", exc_info=True)
        return jsonify({'success': False, 'status': 'error', 'error': str(e)}), 500


@user_token_bp.route('/collect_tokens/delete', methods=['POST'])
@admin_required
def delete_user_token():
    """Deletes a stored token for a given username."""
    try:
        username_to_delete = request.json.get('username')
        if not username_to_delete:
            return jsonify({'success': False, 'error': 'Username is required'}), 400

        tokens = load_user_tokens()
        if username_to_delete in tokens:
            del tokens[username_to_delete]
            save_user_tokens(tokens)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'User token not found'}), 404

    except Exception as e:
        current_app.logger.error(f"Error deleting user token: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# --- START: New Routes ---

@user_token_bp.route('/collect_tokens/get_usernames', methods=['GET'])
@admin_required
def get_stored_usernames():
    """Returns a list of usernames for which tokens are stored."""
    try:
        tokens = load_user_tokens()
        usernames = list(tokens.keys())
        return jsonify({'success': True, 'usernames': usernames})
    except Exception as e:
        current_app.logger.error(f"Error getting stored usernames: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@user_token_bp.route('/collect_tokens/assign_to_source', methods=['POST'])
@admin_required
def assign_user_to_source():
    """Assigns a user (and their token) to a content source."""
    config = load_config() # Load main config
    try:
        data = request.json
        source_id = data.get('source_id')
        username = data.get('username')

        if not source_id or not username:
            return jsonify({'success': False, 'error': 'Source ID and Username are required'}), 400

        # 1. Get the user's token
        stored_tokens = load_user_tokens()
        user_token = stored_tokens.get(username)

        if not user_token:
            return jsonify({'success': False, 'error': f'No stored token found for user: {username}'}), 404

        # 2. Update the main application config
        save_needed = False

        if 'Content Sources' in config and source_id in config['Content Sources']:
            source_config = config['Content Sources'][source_id]

            # Ensure the source is the correct type
            if source_config.get('type') == 'Other Plex Watchlist':
                # Update username and token fields
                source_config['username'] = username
                source_config['token'] = user_token
                # Remove old fetch fields if they exist (optional cleanup)
                source_config.pop('associated_username', None)
                source_config.pop('last_fetched_timestamp', None)
                source_config.pop('last_fetched_count', None)

                save_needed = True
                current_app.logger.info(f"Updated Content Source '{source_id}' with username '{username}' and associated token.")
            else:
                 current_app.logger.warning(f"Content Source '{source_id}' is not of type 'Other Plex Watchlist'. Assignment skipped.")
                 return jsonify({'success': False, 'error': f'Source {source_id} is not of type "Other Plex Watchlist".'}), 400 # Return error if wrong type
        else:
            current_app.logger.error(f"Content Source ID '{source_id}' not found in configuration.")
            return jsonify({'success': False, 'error': f'Content Source ID {source_id} not found.'}), 404

        # 3. Save config if changes were made
        if save_needed:
            save_config(config) # Save the entire updated config

        # 4. Return username and token for frontend update
        # WARNING: Sending token back to frontend is necessary to update the field,
        # but be aware of the security implication if browser tools are used to inspect network traffic.
        current_app.logger.warning(f"Sending token for user {username} back to frontend for field update.")
        return jsonify({
            'success': True,
            'username': username,
            'token': user_token # Send token back
        })

    except Exception as e:
        current_app.logger.error(f"Error assigning user to source: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# --- END: New Routes ---
