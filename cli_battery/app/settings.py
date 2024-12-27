import json
from .logger_config import logger
import os
import sys
from datetime import timedelta
from functools import cached_property

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from settings import get_setting

class Settings:
    def __init__(self):
        # Get config directory from environment variable with fallback
        config_dir = os.environ.get('USER_CONFIG', '/user/config')
        self.config_file = os.path.join(config_dir, 'settings.json')
        self.active_provider = 'none'
        self.providers = [
            {'name': 'trakt', 'enabled': False},
            # Add more providers here as they become available
        ]
        self.staleness_threshold = get_setting('Staleness Threshold', 'staleness_threshold', 7)  # in days
        self.max_entries = 1000  # default value, adjust as needed
        self.log_level = 'INFO'
        self._trakt = None
        self.load()

    @cached_property
    def Trakt(self):
        if self._trakt is None:
            self._trakt = {
                'client_id': get_setting('Trakt', 'client_id', ''),
                'client_secret': get_setting('Trakt', 'client_secret', ''),
                'access_token': get_setting('Trakt', 'access_token', ''),
                'refresh_token': get_setting('Trakt', 'refresh_token', ''),
                'expires_at': get_setting('Trakt', 'expires_at', None),
                'redirect_uri': get_setting('Trakt', 'redirect_uri', 'http://localhost:5001/trakt_callback')
            }
        return self._trakt

    def invalidate_trakt_cache(self):
        if 'Trakt' in self.__dict__:
            del self.__dict__['Trakt']
        self._trakt = None

    def save(self):
        config = {
            'active_provider': self.active_provider,
            'providers': self.providers,
            'staleness_threshold': self.staleness_threshold,
            'max_entries': self.max_entries,
            'log_level': self.log_level,
            'Trakt': self.Trakt
        }
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=4)

    def load(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            self.active_provider = config.get('active_provider', 'none')
            self.providers = config.get('providers', self.providers)
            self.staleness_threshold = get_setting('Staleness Threshold', 'staleness_threshold', 7)
            self.max_entries = config.get('max_entries', 1000)
            self.log_level = config.get('log_level', 'INFO')
        else:
            logger.warning(f"Config file not found: {self.config_file}")

    def get_all(self):
        return {
            "staleness_threshold": self.staleness_threshold,
            "max_entries": self.max_entries,
            "providers": self.providers,
            "log_level": self.log_level,
            "Trakt": self.Trakt
        }

    def update(self, new_settings):
        self.staleness_threshold = int(new_settings.get('staleness_threshold', self.staleness_threshold))
        self.max_entries = int(new_settings.get('max_entries', self.max_entries))
        self.log_level = new_settings.get('log_level', self.log_level)

        enabled_providers = new_settings.get('providers', [])
        for provider in self.providers:
            provider['enabled'] = provider['name'] in enabled_providers
            api_key = new_settings.get(f"provider_{provider['name']}_api_key")
            if api_key is not None:
                provider['api_key'] = api_key

        # Update Trakt settings
        if 'Trakt[client_id]' in new_settings or 'Trakt[client_secret]' in new_settings:
            self.invalidate_trakt_cache()
            self.Trakt['client_id'] = new_settings.get('Trakt[client_id]', self.Trakt['client_id'])
            self.Trakt['client_secret'] = new_settings.get('Trakt[client_secret]', self.Trakt['client_secret'])

        # Save settings to file
        self.save()

    def save_settings(self):
        settings = self.get_all()
        try:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            
            with open(self.config_file, 'w') as f:
                json.dump(settings, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving settings to file: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error while saving settings: {str(e)}")

    def toggle_provider(self, provider_name, enable):
        for provider in self.providers:
            if provider['name'] == provider_name:
                provider['enabled'] = enable
                return True
        return False

    def get_default_settings(self):
        return {
            # ... (existing default settings)
            'Trakt': {
                'client_id': '',
                'client_secret': '',
            }
        }

    @property
    def staleness_threshold_timedelta(self):
        return timedelta(days=self.staleness_threshold)