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
        
        self.load_auth()
        
    def load_auth(self):
        self.access_token = self.settings.Trakt['access_token']
        self.refresh_token = self.settings.Trakt['refresh_token']
        self.expires_at = self.settings.Trakt['expires_at']
        self.last_refresh = self.settings.Trakt.get('last_refresh')
        
        if not self.access_token:
            self.load_from_pytrakt()

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
            
            # Update settings with the loaded data
            self.settings.Trakt['access_token'] = self.access_token
            self.settings.Trakt['refresh_token'] = self.refresh_token
            self.settings.Trakt['expires_at'] = self.expires_at
            self.settings.Trakt['last_refresh'] = self.last_refresh
            self.settings.save_settings()
            
        else:
            logger.warning(f".pytrakt.json file not found at {self.pytrakt_file}")

    def save_token_data(self, token_data):
        now = datetime.now(timezone.utc)
        self.settings.Trakt['access_token'] = token_data['access_token']
        self.settings.Trakt['refresh_token'] = token_data['refresh_token']
        self.settings.Trakt['expires_at'] = int((now + timedelta(seconds=token_data['expires_in'])).timestamp())
        self.settings.Trakt['last_refresh'] = now.isoformat()
        logger.debug(f"Saving token data - Last Refresh: {now.isoformat()}")
        self.settings.save_settings()
        self.load_auth()  # Reload the auth data after saving
        self.save_trakt_credentials()  # Also update the .pytrakt.json file

    def is_authenticated(self):

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
        """Get the current token data"""
        return {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at,
            'last_refresh': self.last_refresh
        }

    def get_last_refresh_time(self):
        """Get the last refresh time"""
        last_refresh = self.settings.Trakt.get('last_refresh')
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
        """Get the token expiration time"""
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
