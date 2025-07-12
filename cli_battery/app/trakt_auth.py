from .logger_config import logger
from datetime import datetime, timedelta
import iso8601
from datetime import timezone
import requests
from urllib.parse import urlencode
import json
from .settings import Settings
import os
import traceback
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities.settings import get_setting  # Add this import

TRAKT_API_URL = "https://api.trakt.tv"
REQUEST_TIMEOUT = 10  # seconds

class TraktAuth:
    def __init__(self):
        self.settings = Settings()
        self.base_url = TRAKT_API_URL
        self.client_id = self.settings.Trakt['client_id']
        self.client_secret = self.settings.Trakt['client_secret']
        self.redirect_uri = self.settings.Trakt['redirect_uri']
        
        # Get config directory from environment variable with fallback
        config_dir = os.environ.get('USER_CONFIG', '/user/config')
        self.pytrakt_file = os.path.join(config_dir, '.pytrakt.json')
        
        # Initialize instance variables
        self.access_token = None
        self.refresh_token = None
        self.expires_at = None
        self.last_refresh = None
        
        # Load fresh data from file
        self.load_auth()
        
        # Force sync settings if needed
        self.sync_settings_if_needed()

    def load_auth(self):
        # Always load fresh data from file first, then fall back to settings
        self.load_from_pytrakt()
        
        # If file loading failed, try settings as fallback
        if not self.access_token:
            # Try to get Trakt settings from the battery's own settings.json file
            trakt_settings = self.settings.get_trakt_settings_from_file()
            if trakt_settings:
                self.access_token = trakt_settings.get('access_token')
                self.refresh_token = trakt_settings.get('refresh_token')
                self.expires_at = trakt_settings.get('expires_at')
                self.last_refresh = trakt_settings.get('last_refresh')
                logger.debug("Loaded Trakt auth from battery settings.json")
            else:
                # Fall back to main config (for backward compatibility)
                self.access_token = self.settings.Trakt['access_token']
                self.refresh_token = self.settings.Trakt['refresh_token']
                self.expires_at = self.settings.Trakt['expires_at']
                self.last_refresh = self.settings.Trakt.get('last_refresh')
                logger.debug("Loaded Trakt auth from main config")

    def load_from_pytrakt(self):
        if os.path.exists(self.pytrakt_file):
            with open(self.pytrakt_file, 'r') as f:
                try:
                    pytrakt_data = json.load(f)
                except json.JSONDecodeError:
                    logger.warning(f"Could not decode .pytrakt.json file at {self.pytrakt_file}. It might be empty or malformed.")
                    pytrakt_data = {}

            self.access_token = pytrakt_data.get('OAUTH_TOKEN')
            self.refresh_token = pytrakt_data.get('OAUTH_REFRESH')
            self.expires_at = pytrakt_data.get('OAUTH_EXPIRES_AT')
            self.last_refresh = pytrakt_data.get('LAST_REFRESH')
            
            # Update battery's settings.json with the loaded data for consistency
            # Only update if we actually have valid data
            if self.access_token and self.refresh_token:
                trakt_settings = {
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'access_token': self.access_token,
                    'refresh_token': self.refresh_token,
                    'expires_at': self.expires_at,
                    'last_refresh': self.last_refresh,
                    'redirect_uri': self.redirect_uri
                }
                self.settings.update_trakt_settings(trakt_settings)
                logger.debug("Synced .pytrakt.json data to settings.json")
            
        else:
            logger.warning(f".pytrakt.json file not found at {self.pytrakt_file}")
            # Clear instance variables if file doesn't exist
            self.access_token = None
            self.refresh_token = None
            self.expires_at = None
            self.last_refresh = None

    def save_token_data(self, token_data):
        now = datetime.now(timezone.utc)
        
        # Update instance variables first
        self.access_token = token_data['access_token']
        self.refresh_token = token_data['refresh_token']
        self.expires_at = int((now + timedelta(seconds=token_data['expires_in'])).timestamp())
        self.last_refresh = now.isoformat()
        
        logger.debug(f"Saving token data - Last Refresh: {now.isoformat()}")
        
        # Save to .pytrakt.json file
        self.save_trakt_credentials()
        
        # Save to battery's settings.json using the new method
        trakt_settings = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at,
            'last_refresh': self.last_refresh,
            'redirect_uri': self.redirect_uri
        }
        self.settings.update_trakt_settings(trakt_settings)

    def is_authenticated(self):
        """Check if authenticated - always fresh from file"""
        self.load_auth()  # Ensure fresh data

        # If we have a refresh token but no access token, try to refresh
        if not self.access_token and self.refresh_token:
            logger.info("Access token missing but refresh token available. Attempting to refresh...")
            if self.refresh_access_token():
                logger.info("Token refreshed successfully")
                return True
            else:
                logger.error("Failed to refresh token")
                return False

        if not self.access_token or not self.expires_at:
            logger.warning(f"Missing authentication data: access_token={bool(self.access_token)}, expires_at={self.expires_at}")
            return False
        
        if isinstance(self.expires_at, str):
            expires_at = iso8601.parse_date(self.expires_at)
        elif isinstance(self.expires_at, (int, float)):
            expires_at = datetime.fromtimestamp(self.expires_at, tz=timezone.utc)
        else:
            logger.error(f"Unexpected type for expires_at: {type(self.expires_at)}")
            return False
        
        now = datetime.now(timezone.utc)
        is_valid = now < expires_at
        
        # Check if token is expired or nearing expiration (within 1 hour)
        refresh_threshold = expires_at - timedelta(hours=1)
        needs_refresh = now >= refresh_threshold
        
        if needs_refresh:
            if now >= expires_at:
                logger.info("Token expired, attempting automatic refresh")
            else:
                logger.info("Token nearing expiration (within 1 hour), attempting automatic refresh")
            
            if self.refresh_access_token():
                logger.info("Token refreshed successfully")
                return True
            else:
                logger.error("Failed to refresh token")
                return False
        
        return is_valid

    def refresh_access_token(self):
        if not self.refresh_token:
            logger.error("No refresh token available.")
            return False

        data = {
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token'
        }
        response = requests.post(f"{self.base_url}/oauth/token", json=data)
        if response.status_code == 200:
            token_data = response.json()
            self.save_token_data(token_data)
            return True
        else:
            # Check if this is a refresh token expiration error
            try:
                error_data = response.json()
                error_description = error_data.get('error_description', '').lower()
                # Only clear refresh token for specific refresh token errors, not general invalid_grant
                if 'refresh_token' in error_description and ('invalid' in error_description or 'expired' in error_description or 'revoked' in error_description):
                    logger.error(f"Refresh token has expired or is invalid. Manual re-authentication required: {response.text}")
                    # Clear the expired tokens to force re-authentication
                    self.access_token = None
                    self.refresh_token = None
                    self.expires_at = None
                    self.last_refresh = None
                    self.save_trakt_credentials()
                    logger.info("Cleared expired tokens from config file")
                elif 'invalid_grant' in error_description:
                    # This might be an access token issue, not necessarily refresh token
                    logger.warning(f"Invalid grant error - this might be an access token issue: {response.text}")
                    logger.info("Attempting to refresh access token using refresh token...")
                    # Don't clear the refresh token, just return False to indicate we need to retry
                else:
                    logger.error(f"Failed to refresh access token: {response.text}")
            except (json.JSONDecodeError, KeyError):
                logger.error(f"Failed to refresh access token: {response.text}")
            return False

    def get_device_code(self):
        url = f"{self.base_url}/oauth/device/code"
        data = {
            "client_id": self.client_id
        }
        response = requests.post(url, json=data, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def get_device_token(self, device_code: str):
        url = f"{self.base_url}/oauth/device/token"
        data = {
            "code": device_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        return requests.post(url, json=data, timeout=REQUEST_TIMEOUT)

    def get_authorization_url(self):
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }
        auth_url = f"{self.base_url}/oauth/authorize?{urlencode(params)}"
        logger.info(f"Generated Trakt authorization URL: {auth_url}")
        return auth_url

    def exchange_code_for_token(self, code):
        data = {
            'code': code,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
            'grant_type': 'authorization_code'
        }
        response = requests.post(f"{self.base_url}/oauth/token", json=data)
        if response.status_code == 200:
            token_data = response.json()
            self.save_token_data(token_data)
            return True
        else:
            logger.error(f"Failed to exchange code for token: {response.text}")
            return False

    def save_trakt_credentials(self):
        credentials = {
            'CLIENT_ID': self.client_id,
            'CLIENT_SECRET': self.client_secret,
            'OAUTH_TOKEN': self.access_token,
            'OAUTH_REFRESH': self.refresh_token,
            'OAUTH_EXPIRES_AT': self.expires_at,
            'LAST_REFRESH': self.last_refresh
        }
        os.makedirs(os.path.dirname(self.pytrakt_file), exist_ok=True)
        with open(self.pytrakt_file, 'w') as f:
            json.dump(credentials, f)

    def get_token_data(self):
        """Get the current token data - always fresh from file"""
        self.load_auth()  # Ensure fresh data
        return {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at,
            'last_refresh': self.last_refresh
        }

    def get_last_refresh_time(self):
        """Get the last refresh time - always fresh from file"""
        self.load_auth()  # Ensure fresh data
        last_refresh = self.last_refresh
        if not last_refresh:
            # If no last_refresh, use the token data to estimate it
            if self.expires_at:
                try:
                    if isinstance(self.expires_at, str):
                        expires_at = iso8601.parse_date(self.expires_at)
                    elif isinstance(self.expires_at, (int, float)):
                        expires_at = datetime.fromtimestamp(self.expires_at, tz=timezone.utc)
                    else:
                        return None
                    # Token is valid for 90 days, so last refresh was when the current token was issued
                    last_refresh = expires_at - timedelta(days=90)
                    return last_refresh.isoformat()
                except Exception as e:
                    logger.error(f"Error calculating last refresh time: {str(e)}")
                    return None
            return None
        return last_refresh

    def get_expiration_time(self):
        """Get the token expiration time - always fresh from file"""
        self.load_auth()  # Ensure fresh data
        if not self.expires_at:
            return None
        try:
            if isinstance(self.expires_at, str):
                return self.expires_at
            elif isinstance(self.expires_at, (int, float)):
                return datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat()
            return None
        except Exception as e:
            logger.error(f"Error formatting expiration time: {str(e)}")
            return None

    def reload_auth(self):
        """Force reload authentication data from file"""
        logger.debug("Forcing reload of Trakt authentication data from file")
        self.load_auth()

    def sync_settings_if_needed(self):
        """Force sync settings.json with .pytrakt.json if there's a mismatch"""
        if os.path.exists(self.pytrakt_file):
            try:
                with open(self.pytrakt_file, 'r') as f:
                    pytrakt_data = json.load(f)
                
                pytrakt_token = pytrakt_data.get('OAUTH_TOKEN')
                pytrakt_refresh = pytrakt_data.get('OAUTH_REFRESH')
                
                # Get current settings from battery's settings.json
                battery_trakt_settings = self.settings.get_trakt_settings_from_file()
                settings_token = battery_trakt_settings.get('access_token')
                settings_refresh = battery_trakt_settings.get('refresh_token')
                
                # If .pytrakt.json has tokens but battery settings.json doesn't, sync them
                if pytrakt_token and pytrakt_refresh and (not settings_token or not settings_refresh):
                    logger.info("Detected mismatch between .pytrakt.json and battery settings.json, syncing...")
                    
                    trakt_settings = {
                        'client_id': self.client_id,
                        'client_secret': self.client_secret,
                        'access_token': pytrakt_token,
                        'refresh_token': pytrakt_refresh,
                        'expires_at': pytrakt_data.get('OAUTH_EXPIRES_AT'),
                        'last_refresh': pytrakt_data.get('LAST_REFRESH'),
                        'redirect_uri': self.redirect_uri
                    }
                    self.settings.update_trakt_settings(trakt_settings)
                    logger.info("Successfully synced .pytrakt.json to battery settings.json")
                    
                    # Update instance variables
                    self.access_token = pytrakt_token
                    self.refresh_token = pytrakt_refresh
                    self.expires_at = pytrakt_data.get('OAUTH_EXPIRES_AT')
                    self.last_refresh = pytrakt_data.get('LAST_REFRESH')
                    
            except Exception as e:
                logger.error(f"Error syncing settings: {e}")
