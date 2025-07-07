from flask import jsonify, Blueprint, current_app
from utilities.settings import get_setting
from trakt.core import get_device_code, get_device_token
import time
import json
import os
import sys
import traceback
from routes.api_tracker import api
import logging
from pathlib import Path
import re
from .models import admin_required
from datetime import datetime, timedelta, timezone
from content_checkers.trakt import _should_refresh_token

trakt_bp = Blueprint('trakt', __name__)

# Use environment variable for config directory with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_CONFIG_PATH = Path(CONFIG_DIR) / '.pytrakt.json'

@trakt_bp.route('/trakt_auth', methods=['POST'])
@admin_required
def trakt_auth():
    try:
        client_id = get_setting('Trakt', 'client_id')
        client_secret = get_setting('Trakt', 'client_secret')
        
        if not client_id or not client_secret:
            return jsonify({'error': 'Trakt client ID or secret not set. Please configure in settings.'}), 400
              
        device_code_response = get_device_code(client_id, client_secret)
        
        # Store the device code response in the Trakt config file
        update_trakt_config('device_code_response', device_code_response)
        
        return jsonify({
            'user_code': device_code_response['user_code'],
            'verification_url': device_code_response['verification_url'],
            'device_code': device_code_response['device_code']
        })
    except Exception as e:
        logging.error(f"Error in trakt_auth: {str(e)}")
        logging.error(traceback.format_exc())
        return jsonify({'error': f'Unable to start authorization process: {str(e)}'}), 500

@trakt_bp.route('/trakt_auth_status', methods=['POST'])
@admin_required
def trakt_auth_status():
    try:
        trakt_config = get_trakt_config()
        device_code_response = trakt_config.get('device_code_response')
        
        if not device_code_response:
            return jsonify({'error': 'No pending Trakt authorization'}), 400
        
        client_id = get_setting('Trakt', 'client_id')
        client_secret = get_setting('Trakt', 'client_secret')
        device_code = device_code_response['device_code']
        
        response = get_device_token(device_code, client_id, client_secret)
        
        if response.status_code == 200:
            token_data = response.json()
            
            # Store the new tokens
            update_trakt_config('CLIENT_ID', client_id)
            update_trakt_config('CLIENT_SECRET', client_secret)
            update_trakt_config('OAUTH_TOKEN', token_data['access_token'])
            update_trakt_config('OAUTH_REFRESH', token_data['refresh_token'])
            
            # Save expiration as Unix timestamp for consistency
            expires_at_dt = datetime.now(timezone.utc) + timedelta(seconds=token_data['expires_in'])
            update_trakt_config('OAUTH_EXPIRES_AT', int(expires_at_dt.timestamp()))
            update_trakt_config('LAST_REFRESH', datetime.now(timezone.utc).isoformat())
            
            # Remove the device code response as it's no longer needed
            trakt_config = get_trakt_config()
            trakt_config.pop('device_code_response', None)
            save_trakt_config(trakt_config)
            
            # TODO: - Purge and reload Trakt auth

            # Push the new auth data to the battery
            push_result = push_trakt_auth_to_battery()
            if push_result.status_code != 200:
                logging.warning(f"Failed to push Trakt auth to battery: {push_result.json().get('message')}")

            return jsonify({
                'status': 'authorized', 
                'battery_push_status': 'success' if push_result.status_code == 200 else 'failed'
            })
        elif response.status_code == 400:
            return jsonify({'status': 'pending'})
        else:
            return jsonify({'status': 'error', 'message': response.text}), response.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Add a new route to check if Trakt is already authorized
@trakt_bp.route('/trakt_auth_status', methods=['GET'])
def check_trakt_auth_status():
    try:
        # Use the standardized ensure_trakt_auth function which always reads fresh data
        from content_checkers.trakt import ensure_trakt_auth
        access_token = ensure_trakt_auth()
        
        if access_token:
            return jsonify({'status': 'authorized'})
        else:
            return jsonify({'status': 'unauthorized'})
    except Exception as e:
        logging.error(f"Error checking Trakt auth status: {e}")
        return jsonify({'status': 'unauthorized'})

def _to_timestamp(value):
    """Converts an ISO 8601 string or a Unix timestamp to a float timestamp."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            # Handle ISO format, replacing 'Z' with timezone info
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            # Handle stringified timestamp
            try:
                return float(value)
            except ValueError:
                return None
    return None

def get_trakt_config():
    if TRAKT_CONFIG_PATH.exists():
        try:
            with TRAKT_CONFIG_PATH.open('r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning(f"Could not decode .pytrakt.json file at {TRAKT_CONFIG_PATH}. It might be empty or malformed.")
            return {}
    return {}

def save_trakt_config(config):
    TRAKT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRAKT_CONFIG_PATH.open('w') as f:
        json.dump(config, f, indent=2)

def update_trakt_config(key, value):
    config = get_trakt_config()
    config[key] = value
    save_trakt_config(config)

def push_trakt_auth_to_battery_core():
    try:
        trakt_config = get_trakt_config()
        battery_url = get_setting('Metadata Battery', 'url')

        logging.info(f"Battery URL from settings: {battery_url}")

        if not battery_url:
            logging.error("Battery URL not set in settings")
            return False, 'Battery URL not set in settings'

        auth_data = {
            'CLIENT_ID': trakt_config.get('CLIENT_ID'),
            'CLIENT_SECRET': trakt_config.get('CLIENT_SECRET'),
            'OAUTH_TOKEN': trakt_config.get('OAUTH_TOKEN'),
            'OAUTH_REFRESH': trakt_config.get('OAUTH_REFRESH'),
            'OAUTH_EXPIRES_AT': trakt_config.get('OAUTH_EXPIRES_AT')
        }

        logging.info(f"Attempting to push Trakt auth to battery at URL: {battery_url}")
        try:
            response = api.post(f"{battery_url}/receive_trakt_auth", json=auth_data)
            logging.info(f"Response status code: {response.status_code}")
            logging.info(f"Response content: {response.text}")
        except Exception as request_error:
            logging.error(f"Request to battery failed: {str(request_error)}")
            logging.error(f"Request exception type: {type(request_error).__name__}")
            logging.error(f"Request exception details: {traceback.format_exc()}")
            return False, f'Request to battery failed: {str(request_error)}'
        
        if response.status_code == 200:
            logging.info("Successfully pushed Trakt auth to battery")
            return True, 'Trakt auth pushed to battery successfully'
        else:
            logging.error(f"Failed to push Trakt auth to battery. Status code: {response.status_code}, Response: {response.text}")
            return False, f'Failed to push Trakt auth to battery: {response.text}'
    except Exception as e:
        logging.error(f"Error pushing Trakt auth to battery: {str(e)}")
        logging.error(f"Exception type: {type(e).__name__}")
        logging.error(f"Exception traceback: {traceback.format_exc()}")
        return False, str(e)

@trakt_bp.route('/push_trakt_auth_to_battery', methods=['POST'])
@admin_required
def push_trakt_auth_to_battery():
    success, message = push_trakt_auth_to_battery_core()
    if success:
        return jsonify({'status': 'success', 'message': message})
    else:
        return jsonify({'status': 'error', 'message': message}), 500
