from flask import Blueprint, jsonify, request, render_template, redirect, url_for, session
import logging
import json
import os
import uuid
import requests
from datetime import datetime, timedelta
from settings import load_config
from config_manager import add_content_source, save_config

trakt_friends_bp = Blueprint('trakt_friends', __name__)

# Trakt API constants
TRAKT_API_URL = "https://api.trakt.tv"
REQUEST_TIMEOUT = 10  # seconds

# Get config directory from environment variable with fallback
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
TRAKT_FRIENDS_DIR = os.path.join(CONFIG_DIR, 'trakt_friends')

# Create the TRAKT_FRIENDS_DIR if it doesn't exist
if not os.path.exists(TRAKT_FRIENDS_DIR):
    os.makedirs(TRAKT_FRIENDS_DIR, exist_ok=True)

def get_trakt_client_credentials():
    """Get Trakt client ID and secret from main settings"""
    config = load_config()
    trakt_settings = config.get('Trakt', {})
    return {
        'client_id': trakt_settings.get('client_id', ''),
        'client_secret': trakt_settings.get('client_secret', '')
    }

@trakt_friends_bp.route('/authorize', methods=['POST'])
def authorize_friend():
    """Start the authorization process for a friend's Trakt account"""
    # Generate a unique ID for this authorization
    auth_id = str(uuid.uuid4())
    
    # Get Trakt client credentials from form
    client_id = request.form.get('client_id', '')
    client_secret = request.form.get('client_secret', '')
    
    if not client_id or not client_secret:
        return jsonify({
            'success': False, 
            'error': 'Trakt client ID and secret are required'
        }), 400
    
    try:
        # Create the trakt_friends directory if it doesn't exist
        if not os.path.exists(TRAKT_FRIENDS_DIR):
            os.makedirs(TRAKT_FRIENDS_DIR)
        
        # Get device code from Trakt API
        response = requests.post(
            f"{TRAKT_API_URL}/oauth/device/code",
            json={
                'client_id': client_id
            },
            timeout=REQUEST_TIMEOUT
        )
        
        if response.status_code != 200:
            error_message = "Error getting device code from Trakt API"
            try:
                error_data = response.json()
                error_message = error_data.get('error_description', error_message)
            except json.JSONDecodeError:
                pass
            
            return jsonify({'success': False, 'error': error_message}), 400
        
        device_data = response.json()
        
        # Save the state
        state = {
            'status': 'pending',
            'device_code': device_data['device_code'],
            'user_code': device_data['user_code'],
            'verification_url': device_data['verification_url'],
            'friend_name': request.form.get('friend_name', 'Friend'),
            'client_id': client_id,
            'client_secret': client_secret
        }
        
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        with open(state_file, 'w') as f:
            json.dump(state, f)
        
        return jsonify({
            'success': True,
            'auth_id': auth_id,
            'user_code': device_data['user_code'],
            'verification_url': device_data['verification_url'],
            'expires_in': device_data['expires_in']
        })
    
    except Exception as e:
        logging.error(f"Error starting Trakt friend authorization: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@trakt_friends_bp.route('/check_auth/<auth_id>', methods=['GET'])
def check_auth_status(auth_id):
    """Check the status of a friend's Trakt authorization"""
    try:
        # Load the state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            return jsonify({'success': False, 'error': 'Authorization not found'}), 404
        
        with open(state_file, 'r') as f:
            state = json.load(f)
        
        # If already authorized, return success
        if state.get('status') == 'authorized':
            return jsonify({
                'success': True,
                'status': 'authorized',
                'friend_name': state.get('friend_name')
            })
        
        # Check with Trakt API
        response = requests.post(
            f"{TRAKT_API_URL}/oauth/device/token",
            json={
                'code': state['device_code'],
                'client_id': state['client_id'],
                'client_secret': state['client_secret']
            },
            timeout=REQUEST_TIMEOUT
        )
        
        # If successful, update state and return success
        if response.status_code == 200:
            token_data = response.json()
            
            # Update state with token information
            state.update({
                'status': 'authorized',
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'expires_at': int((datetime.now() + timedelta(seconds=token_data['expires_in'])).timestamp())
            })
            
            # Save the updated state
            with open(state_file, 'w') as f:
                json.dump(state, f)
            
            # Get the username from Trakt API
            headers = {
                'Content-Type': 'application/json',
                'trakt-api-version': '2',
                'trakt-api-key': state['client_id'],
                'Authorization': f'Bearer {token_data["access_token"]}'
            }
            
            user_response = requests.get(f"{TRAKT_API_URL}/users/me", headers=headers, timeout=REQUEST_TIMEOUT)
            if user_response.status_code == 200:
                user_data = user_response.json()
                state['username'] = user_data.get('username')
                state['friend_name'] = user_data.get('name') or state['friend_name']
                
                # Save the updated state again
                with open(state_file, 'w') as f:
                    json.dump(state, f)
            
            return jsonify({
                'success': True,
                'status': 'authorized',
                'friend_name': state.get('friend_name'),
                'username': state.get('username')
            })
        
        # If pending, return pending status
        elif response.status_code == 400 and 'pending' in response.text.lower():
            return jsonify({
                'success': True,
                'status': 'pending',
                'user_code': state.get('user_code'),
                'verification_url': state.get('verification_url')
            })
        
        # If expired or other error, return error
        else:
            try:
                error_data = response.json()
                error_message = error_data.get('error_description', 'Unknown error')
            except json.JSONDecodeError:
                error_message = f"Error response from Trakt: {response.text}"
            
            return jsonify({
                'success': False,
                'status': 'error',
                'error': error_message
            }), 400
    
    except Exception as e:
        logging.error(f"Error checking Trakt friend authorization: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@trakt_friends_bp.route('/add_source/<auth_id>', methods=['POST'])
def add_friend_source(auth_id):
    """Add a friend's Trakt account as a content source"""
    try:
        # Load the state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            return jsonify({'success': False, 'error': 'Authorization not found'}), 404
        
        with open(state_file, 'r') as f:
            state = json.load(f)
        
        # Check if authorized
        if state.get('status') != 'authorized':
            return jsonify({'success': False, 'error': 'Account not authorized yet'}), 400
        
        # Get form data
        display_name = request.form.get('display_name')
        media_type = request.form.get('media_type', 'All')
        versions = request.form.getlist('versions')
        
        # If no display name is provided, use the friend's name
        if not display_name:
            display_name = f"{state.get('friend_name', 'Friend')}'s Watchlist"
        
        # Create a source config
        source_config = {
            'display_name': display_name,
            'enabled': True,
            'auth_id': auth_id,
            'username': state.get('username', ''),
            'media_type': media_type,
            'versions': versions
        }
        
        # Add the content source
        source_id = add_content_source('Friends Trakt Watchlist', source_config)
        
        return jsonify({
            'success': True,
            'source_id': source_id
        })
    
    except Exception as e:
        logging.error(f"Error adding friend's Trakt source: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@trakt_friends_bp.route('/refresh_token/<auth_id>', methods=['POST'])
def refresh_token(auth_id):
    """Refresh the access token for a friend's Trakt account"""
    try:
        # Load the state
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            return jsonify({'success': False, 'error': 'Authorization not found'}), 404
        
        with open(state_file, 'r') as f:
            state = json.load(f)
        
        # Check if we have a refresh token
        if not state.get('refresh_token'):
            return jsonify({'success': False, 'error': 'No refresh token available'}), 400
        
        # Refresh the token
        response = requests.post(
            f"{TRAKT_API_URL}/oauth/token",
            json={
                'refresh_token': state['refresh_token'],
                'client_id': state['client_id'],
                'client_secret': state['client_secret'],
                'grant_type': 'refresh_token'
            },
            timeout=REQUEST_TIMEOUT
        )
        
        if response.status_code == 200:
            token_data = response.json()
            
            # Update state with new token information
            state.update({
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'expires_at': int((datetime.now() + timedelta(seconds=token_data['expires_in'])).timestamp())
            })
            
            # Save the updated state
            with open(state_file, 'w') as f:
                json.dump(state, f)
            
            return jsonify({'success': True})
        else:
            try:
                error_data = response.json()
                error_message = error_data.get('error_description', 'Unknown error')
            except json.JSONDecodeError:
                error_message = f"Error response from Trakt: {response.text}"
            
            return jsonify({'success': False, 'error': error_message}), 400
    
    except Exception as e:
        logging.error(f"Error refreshing Trakt friend token: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@trakt_friends_bp.route('/list', methods=['GET'])
def list_friends():
    """List all authorized friend's Trakt accounts"""
    try:
        friends = []
        
        # List all files in the TRAKT_FRIENDS_DIR
        if os.path.exists(TRAKT_FRIENDS_DIR):
            for filename in os.listdir(TRAKT_FRIENDS_DIR):
                if filename.endswith('.json'):
                    try:
                        # Extract auth_id from filename
                        auth_id = filename.replace('.json', '')
                        
                        with open(os.path.join(TRAKT_FRIENDS_DIR, filename), 'r') as f:
                            state = json.load(f)
                        
                        # Only include authorized accounts
                        if state.get('status') == 'authorized':
                            friends.append({
                                'auth_id': auth_id,
                                'friend_name': state.get('friend_name', 'Unknown Friend'),
                                'username': state.get('username', ''),
                                'expires_at': state.get('expires_at', '')
                            })
                    except Exception as e:
                        logging.error(f"Error reading friend state file {filename}: {str(e)}")
        
        return jsonify({
            'success': True,
            'friends': friends
        })
    
    except Exception as e:
        logging.error(f"Error listing Trakt friends: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@trakt_friends_bp.route('/delete/<auth_id>', methods=['POST'])
def delete_friend(auth_id):
    """Delete a friend's Trakt authorization"""
    try:
        # Check if the auth file exists
        state_file = os.path.join(TRAKT_FRIENDS_DIR, f'{auth_id}.json')
        if not os.path.exists(state_file):
            return jsonify({'success': False, 'error': 'Authorization not found'}), 404
        
        # Delete the file
        os.remove(state_file)
        
        # Also remove any content sources using this auth_id
        config = load_config()
        if 'Content Sources' in config:
            sources_to_delete = []
            for source_id, source_config in config['Content Sources'].items():
                if source_id.startswith('Friends Trakt Watchlist') and source_config.get('auth_id') == auth_id:
                    sources_to_delete.append(source_id)
            
            for source_id in sources_to_delete:
                del config['Content Sources'][source_id]
            
            save_config(config)
        
        return jsonify({'success': True})
    
    except Exception as e:
        logging.error(f"Error deleting Trakt friend: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@trakt_friends_bp.route('/manage', methods=['GET'])
def manage_friends():
    """Render the friend management page"""
    return render_template('trakt_friends.html')
