from flask import jsonify, Blueprint
from settings import get_setting
from trakt.core import get_device_code, get_device_token
import time
import json
import os
import sys
from flask import current_app
import traceback

trakt_bp = Blueprint('trakt', __name__)

TRAKT_CONFIG_PATH = '/user/config/.pytrakt.json'

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
        current_app.logger.error(f"Error in trakt_auth: {str(e)}")
        current_app.logger.error(traceback.format_exc())
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
                current_app.logger.warning(f"Failed to push Trakt auth to battery: {push_result.json().get('message')}")

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
    if os.path.exists(TRAKT_CONFIG_PATH):
        with open(TRAKT_CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_trakt_config(config):
    with open(TRAKT_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def update_trakt_config(key, value):
    config = get_trakt_config()
    config[key] = value
    save_trakt_config(config)

@trakt_bp.route('/push_trakt_auth_to_battery', methods=['POST'])
def push_trakt_auth_to_battery():
    try:
        trakt_config = get_trakt_config()
        battery_url = get_setting('Metadata Battery', 'url')

        if not battery_url:
            return jsonify({'error': 'Battery URL not set in settings'}), 400

        auth_data = {
            'CLIENT_ID': trakt_config.get('CLIENT_ID'),
            'CLIENT_SECRET': trakt_config.get('CLIENT_SECRET'),
            'OAUTH_TOKEN': trakt_config.get('OAUTH_TOKEN'),
            'OAUTH_REFRESH': trakt_config.get('OAUTH_REFRESH'),
            'OAUTH_EXPIRES_AT': trakt_config.get('OAUTH_EXPIRES_AT')
        }

        response = requests.post(f"{battery_url}/receive_trakt_auth", json=auth_data)
        
        if response.status_code == 200:
            return jsonify({'status': 'success', 'message': 'Trakt auth pushed to battery successfully'})
        else:
            return jsonify({'status': 'error', 'message': f'Failed to push Trakt auth to battery: {response.text}'}), 500

    except Exception as e:
        logger.error(f"Error pushing Trakt auth to battery: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
