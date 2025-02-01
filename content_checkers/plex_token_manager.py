import os
import json
import logging
from datetime import datetime
from config_manager import CONFIG_DIR

TOKEN_STATUS_FILE = os.path.join(CONFIG_DIR, 'plex_token_status.json')

def load_token_status():
    """Load the token status from the JSON file."""
    try:
        if os.path.exists(TOKEN_STATUS_FILE):
            with open(TOKEN_STATUS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error loading token status: {e}")
    return {}

def save_token_status(status):
    """Save the token status to the JSON file."""
    try:
        with open(TOKEN_STATUS_FILE, 'w') as f:
            json.dump(status, f, indent=4, default=str)
    except Exception as e:
        logging.error(f"Error saving token status: {e}")

def update_token_status(username, valid, expires_at=None, plex_username=None):
    """Update the status for a specific token."""
    status = load_token_status()
    status[username] = {
        'valid': valid,
        'last_checked': datetime.now().isoformat(),
        'expires_at': expires_at.isoformat() if expires_at else None,
        'username': plex_username
    }
    save_token_status(status)

def get_token_status():
    """Get the current status of all tokens."""
    return load_token_status()
