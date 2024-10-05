from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, Blueprint
from app.settings import Settings
from app.metadata_manager import MetadataManager
import io
from app.trakt_auth import TraktAuth
from app.logger_config import logger
from flask import flash
from sqlalchemy import inspect
from app.database import Session, Item, Metadata, Season, Poster  # Add this line
from app.trakt_metadata import TraktMetadata  # Add this import at the top of the file
import json
import time
import os

settings = Settings()

trakt_bp = Blueprint('trakt', __name__)

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_CONFIG_PATH = os.path.join(CONFIG_DIR, '.pytrakt.json')

@trakt_bp.route('/trakt_auth', methods=['GET', 'POST'])
def trakt_auth():
    try:
        trakt = TraktAuth()
        device_code_response = trakt.get_device_code()
        
        # Store the device code response in the Trakt config file
        update_trakt_config('device_code_response', device_code_response)
        
        return jsonify({
            'user_code': device_code_response['user_code'],
            'verification_url': device_code_response['verification_url'],
            'device_code': device_code_response['device_code']
        })
    except Exception as e:
        logger.error(f"Error in trakt_auth: {str(e)}")
        return jsonify({'error': f'Unable to start authorization process: {str(e)}'}), 500

@trakt_bp.route('/trakt_auth_status', methods=['POST'])
def trakt_auth_status():
    try:
        trakt_config = get_trakt_config()
        device_code_response = trakt_config.get('device_code_response')
        
        if not device_code_response:
            return jsonify({'error': 'No pending Trakt authorization'}), 400
        
        trakt = TraktAuth()
        device_code = device_code_response['device_code']
        
        response = trakt.get_device_token(device_code)
        
        if response.status_code == 200:
            token_data = response.json()
            
            # Store the new tokens
            update_trakt_config('CLIENT_ID', trakt.client_id)
            update_trakt_config('CLIENT_SECRET', trakt.client_secret)
            update_trakt_config('OAUTH_TOKEN', token_data['access_token'])
            update_trakt_config('OAUTH_REFRESH', token_data['refresh_token'])
            update_trakt_config('OAUTH_EXPIRES_AT', int(time.time()) + token_data['expires_in'])
            
            # Remove the device code response as it's no longer needed
            trakt_config = get_trakt_config()
            trakt_config.pop('device_code_response', None)
            save_trakt_config(trakt_config)
            
            # Reload Trakt auth
            trakt.load_auth()

            return jsonify({'status': 'authorized'})
        elif response.status_code == 400:
            return jsonify({'status': 'pending'})
        else:
            return jsonify({'status': 'error', 'message': response.text}), response.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@trakt_bp.route('/check_trakt_auth', methods=['GET'])
def check_trakt_auth():
    trakt_config = get_trakt_config()
    if 'OAUTH_TOKEN' in trakt_config and 'OAUTH_EXPIRES_AT' in trakt_config:
        if trakt_config['OAUTH_EXPIRES_AT'] > time.time():
            return jsonify({'status': 'authorized'})
    return jsonify({'status': 'unauthorized'})

# Add these helper functions
def get_trakt_config():
    if os.path.exists(TRAKT_CONFIG_PATH):
        with open(TRAKT_CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_trakt_config(config):
    os.makedirs(os.path.dirname(TRAKT_CONFIG_PATH), exist_ok=True)
    with open(TRAKT_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def update_trakt_config(key, value):
    config = get_trakt_config()
    config[key] = value
    save_trakt_config(config)


@trakt_bp.route('/receive_trakt_auth', methods=['POST'])
def receive_trakt_auth():
    try:
        auth_data = request.json
        trakt_auth = TraktAuth()
        
        # Update TraktAuth instance with new data
        trakt_auth.client_id = auth_data.get('CLIENT_ID')
        trakt_auth.client_secret = auth_data.get('CLIENT_SECRET')
        trakt_auth.access_token = auth_data.get('OAUTH_TOKEN')
        trakt_auth.refresh_token = auth_data.get('OAUTH_REFRESH')
        trakt_auth.expires_at = auth_data.get('OAUTH_EXPIRES_AT')
        
        # Save the new data
        trakt_auth.save_trakt_credentials()
        
        # Update settings
        trakt_auth.settings.Trakt['client_id'] = trakt_auth.client_id
        trakt_auth.settings.Trakt['client_secret'] = trakt_auth.client_secret
        trakt_auth.settings.Trakt['access_token'] = trakt_auth.access_token
        trakt_auth.settings.Trakt['refresh_token'] = trakt_auth.refresh_token
        trakt_auth.settings.Trakt['expires_at'] = trakt_auth.expires_at
        trakt_auth.settings.save_settings()
        
        return jsonify({'status': 'success', 'message': 'Trakt auth received and saved successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
