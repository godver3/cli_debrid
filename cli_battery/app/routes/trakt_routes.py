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
import iso8601
from datetime import datetime, timezone, timedelta

settings = Settings()

trakt_bp = Blueprint('trakt', __name__)

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_CONFIG_PATH = os.path.join(CONFIG_DIR, '.pytrakt.json')

@trakt_bp.route('/check_trakt_auth', methods=['GET'])
def check_trakt_auth():
    try:
        # Always create a fresh TraktAuth instance to get current data
        trakt_auth = TraktAuth()
        
        if trakt_auth.is_authenticated():
            return jsonify({'status': 'authorized'})
        else:
            return jsonify({'status': 'unauthorized'})
                
    except Exception as e:
        logger.error(f"Error checking Trakt auth status: {e}")
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

@trakt_bp.route('/refresh_trakt_auth', methods=['POST'])
def refresh_trakt_auth():
    """Manually refresh Trakt authentication token"""
    try:
        # Create fresh instance to get current data
        trakt_auth = TraktAuth()
        
        if trakt_auth.refresh_access_token():
            return jsonify({'status': 'success', 'message': 'Trakt auth refreshed successfully'})
        else:
            return jsonify({'status': 'error', 'message': 'Failed to refresh Trakt auth'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

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
        
        # Save the new data to .pytrakt.json
        trakt_auth.save_trakt_credentials()
        
        # Update battery's settings.json with the new Trakt data
        settings = Settings()
        trakt_settings = {
            'client_id': trakt_auth.client_id,
            'client_secret': trakt_auth.client_secret,
            'access_token': trakt_auth.access_token,
            'refresh_token': trakt_auth.refresh_token,
            'expires_at': trakt_auth.expires_at,
            'last_refresh': datetime.now(timezone.utc).isoformat(),
            'redirect_uri': 'urn:ietf:wg:oauth:2.0:oob'
        }
        settings.update_trakt_settings(trakt_settings)
        
        logger.info("Trakt auth received and saved successfully to both .pytrakt.json and settings.json")
        
        return jsonify({'status': 'success', 'message': 'Trakt auth received and saved successfully'})
    except Exception as e:
        logger.error(f"Error receiving Trakt auth: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
