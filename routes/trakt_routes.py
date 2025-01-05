from flask import jsonify, Blueprint, current_app
from settings import get_setting
from trakt.core import get_device_code, get_device_token
import time
import json
import os
import sys
import traceback
from api_tracker import api
import logging
from pathlib import Path
import re

trakt_bp = Blueprint('trakt', __name__)

# Use environment variable for config directory with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_CONFIG_PATH = Path(CONFIG_DIR) / '.pytrakt.json'

@trakt_bp.route('/trakt_auth', methods=['POST'])
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
            update_trakt_config('OAUTH_EXPIRES_AT', int(time.time()) + token_data['expires_in'])
            
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
    trakt_config = get_trakt_config()
    if 'OAUTH_TOKEN' in trakt_config and 'OAUTH_EXPIRES_AT' in trakt_config:
        if trakt_config['OAUTH_EXPIRES_AT'] > time.time():
            return jsonify({'status': 'authorized'})
    return jsonify({'status': 'unauthorized'})

def get_trakt_config():
    if TRAKT_CONFIG_PATH.exists():
        with TRAKT_CONFIG_PATH.open('r') as f:
            return json.load(f)
    return {}

def save_trakt_config(config):
    TRAKT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRAKT_CONFIG_PATH.open('w') as f:
        json.dump(config, f, indent=2)

def update_trakt_config(key, value):
    config = get_trakt_config()
    config[key] = value
    save_trakt_config(config)

@trakt_bp.route('/push_trakt_auth_to_battery', methods=['POST'])
def push_trakt_auth_to_battery():
    try:
        trakt_config = get_trakt_config()
        battery_url = get_setting('Metadata Battery', 'url', 'http://localhost:5001')
        battery_port = os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001')

        if not battery_url:
            logging.error("Battery URL not set in settings")
            return jsonify({'error': 'Battery URL not set in settings'}), 400

        # Remove any existing port numbers and add the correct one
        battery_url = re.sub(r':\d+/?$', '', battery_url)  # Remove any port number at the end
        battery_url = f"{battery_url}:{battery_port}"

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
            return jsonify({'status': 'error', 'message': f'Request to battery failed: {str(request_error)}'}), 500
        
        if response.status_code == 200:
            logging.info("Successfully pushed Trakt auth to battery")
            return jsonify({'status': 'success', 'message': 'Trakt auth pushed to battery successfully'})
        else:
            logging.error(f"Failed to push Trakt auth to battery. Status code: {response.status_code}, Response: {response.text}")
            return jsonify({'status': 'error', 'message': f'Failed to push Trakt auth to battery: {response.text}'}), 500

    except Exception as e:
        logging.error(f"Error pushing Trakt auth to battery: {str(e)}")
        logging.error(f"Exception type: {type(e).__name__}")
        logging.error(f"Exception traceback: {traceback.format_exc()}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
