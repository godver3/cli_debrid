import json
from .logger_config import logger
import os
import sys
from datetime import timedelta
from functools import cached_property
import tempfile
import shutil # Added for backup

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities.settings import get_setting

class Settings:
    def __init__(self):
        # Get config directory from environment variable with fallback
        config_dir = os.environ.get('USER_CONFIG', '/user/config')
        self.config_file = os.path.join(config_dir, 'settings.json')
        self.backup_file = self.config_file + ".bak" # Added backup file path
        self.active_provider = 'none'
        self.providers = [
            {'name': 'trakt', 'enabled': False},
            # Add more providers here as they become available
        ]
        self._staleness_threshold = None  # Initialize as None
        self.max_entries = 1000  # default value, adjust as needed
        self.log_level = 'INFO'
        self.load()

    @property
    def staleness_threshold(self):
        # Always get fresh value from settings
        # Note: This reads from the MAIN config via utilities.settings, not this settings.json
        return get_setting('Staleness Threshold', 'staleness_threshold', 7)

    @staleness_threshold.setter
    def staleness_threshold(self, value):
        # This setter seems to store the value locally (_staleness_threshold)
        # but the property getter always reads from the main config.
        # The save() method then writes this local value back to *this* settings.json
        # This interaction seems potentially confusing. Consider if staleness_threshold
        # should live *only* in the main config or *only* here.
        # For now, leaving the logic as is, but highlighting the potential confusion.
        self._staleness_threshold = value
        self.save() # This will save the local value to settings.json

    @cached_property
    def Trakt(self):
        # Always read fresh data from main config, don't cache
        battery_port = int(os.environ.get('CLI_DEBRID_BATTERY_PORT', 5001))
        battery_host = os.environ.get('CLI_DEBRID_BATTERY_HOST', 'localhost') # Get host from env
        # These read from the MAIN config via utilities.settings - always fresh
        return {
            'client_id': get_setting('Trakt', 'client_id', ''),
            'client_secret': get_setting('Trakt', 'client_secret', ''),
            'access_token': get_setting('Trakt', 'access_token', ''),
            'refresh_token': get_setting('Trakt', 'refresh_token', ''),
            'expires_at': get_setting('Trakt', 'expires_at', None),
            'redirect_uri': get_setting('Trakt', 'redirect_uri', f'http://{battery_host}:{battery_port}/trakt_callback')
        }

    def invalidate_trakt_cache(self):
        # No longer needed since we always read fresh data
        # This method is kept for backward compatibility
        pass

    def save(self):
        # Note: This saves the state of *this* Settings object, including
        # _staleness_threshold (which might differ from the main config's value
        # read by the property getter) and Trakt details (read from main config).
        config = {
            'active_provider': self.active_provider,
            'providers': self.providers,
            # Saving the internal _staleness_threshold value set by the setter
            'staleness_threshold': self._staleness_threshold if self._staleness_threshold is not None else self.staleness_threshold,
            'max_entries': self.max_entries,
            'log_level': self.log_level,
            'Trakt': self.Trakt # Saves the cached Trakt details
        }
        try:
            # Ensure the directory exists
            config_dir = os.path.dirname(self.config_file)
            os.makedirs(config_dir, exist_ok=True)

            # Backup the current file before writing
            if os.path.exists(self.config_file):
                try:
                    shutil.copy2(self.config_file, self.backup_file) # Use copy2 to preserve metadata
                    logger.debug(f"Created backup: {self.backup_file}")
                except Exception as backup_err:
                    logger.warning(f"Failed to create backup for {self.config_file}: {backup_err}")
                    # Decide if we should proceed without backup? For now, we continue.

            # Use atomic write
            temp_path = None # Initialize temp_path
            with tempfile.NamedTemporaryFile('w', dir=config_dir, delete=False) as temp_f:
                json.dump(config, temp_f, indent=4)
                temp_path = temp_f.name # Store the temporary file path
            # Atomically replace the final config file path (overwrites if exists)
            os.replace(temp_path, self.config_file)
            logger.debug(f"Settings saved successfully to {self.config_file}")

        except IOError as e:
            logger.error(f"IOError saving settings to {self.config_file}: {e}")
            # Clean up the temporary file if replace failed
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception as e:
            logger.error(f"Unexpected error saving settings: {e}")
            # Clean up the temporary file if replace failed
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    def load(self):
        config = {}  # Default to empty config
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    # Check if file is not empty
                    if os.fstat(f.fileno()).st_size > 0:
                        f.seek(0)  # Rewind to start
                        config = json.load(f)
                    else:
                        logger.warning(f"Config file is empty: {self.config_file}. Using default settings.")
            except json.JSONDecodeError:
                logger.error(f"Error decoding JSON from {self.config_file}. Using default settings.")
            except Exception as e:
                logger.error(f"Unexpected error loading settings from {self.config_file}: {e}. Using defaults.")
        else:
            logger.warning(f"Config file not found: {self.config_file}. Using default settings.")

        # Apply loaded config or defaults
        self.active_provider = config.get('active_provider', 'none')
        self.providers = config.get('providers', [ {'name': 'trakt', 'enabled': False} ])
        self._staleness_threshold = config.get('staleness_threshold', None)
        self.max_entries = config.get('max_entries', 1000)
        self.log_level = config.get('log_level', 'INFO')

    def get_all(self):
        return {
            # Use the property getter for staleness, which reads from main config
            "staleness_threshold": self.staleness_threshold,
            "max_entries": self.max_entries,
            "providers": self.providers,
            "log_level": self.log_level,
            "Trakt": self.Trakt # Use the property getter
        }

    def update(self, new_settings):
        # Note: This updates the internal _staleness_threshold, which is saved,
        # but the getter always reads from the main config.
        self._staleness_threshold = int(new_settings.get('staleness_threshold', self._staleness_threshold if self._staleness_threshold is not None else self.staleness_threshold))
        self.max_entries = int(new_settings.get('max_entries', self.max_entries))
        self.log_level = new_settings.get('log_level', self.log_level)

        enabled_providers = new_settings.get('providers', [])
        for provider in self.providers:
            provider['enabled'] = provider['name'] in enabled_providers
            api_key = new_settings.get(f"provider_{provider['name']}_api_key")
            if api_key is not None:
                provider['api_key'] = api_key

        # Update Trakt settings - These should probably update the main config via a utility function
        # Currently, this updates the local cached dict, which is then saved to settings.json
        # but not necessarily persisted back to the main config where get_setting reads from.
        if 'Trakt[client_id]' in new_settings or 'Trakt[client_secret]' in new_settings:
            self.invalidate_trakt_cache() # Clears the cache
            # Accessing self.Trakt re-caches using get_setting initially
            # Then we update the cached dictionary
            self.Trakt['client_id'] = new_settings.get('Trakt[client_id]', self.Trakt['client_id'])
            self.Trakt['client_secret'] = new_settings.get('Trakt[client_secret]', self.Trakt['client_secret'])

        # Save settings to file
        self.save() # Saves the current state of this object to settings.json

    def save_settings(self):
        # This method gets *all* settings (including potentially stale Trakt/staleness values
        # if the main config changed since last load/cache) and saves them.
        settings = self.get_all()
        try:
            # Ensure the directory exists
            config_dir = os.path.dirname(self.config_file)
            os.makedirs(config_dir, exist_ok=True)

            # Backup the current file before writing
            if os.path.exists(self.config_file):
                try:
                    shutil.copy2(self.config_file, self.backup_file)
                    logger.debug(f"Created backup via save_settings: {self.backup_file}")
                except Exception as backup_err:
                     logger.warning(f"Failed to create backup for {self.config_file} via save_settings: {backup_err}")

            # Use atomic write
            temp_path = None # Initialize temp_path
            with tempfile.NamedTemporaryFile('w', dir=config_dir, delete=False) as temp_f:
                json.dump(settings, temp_f, indent=4)
                temp_path = temp_f.name # Store the temporary file path
            # Atomically replace the final config file path (overwrites if exists)
            os.replace(temp_path, self.config_file)
            logger.debug(f"Settings saved successfully via save_settings to {self.config_file}")

        except IOError as e:
            logger.error(f"Error saving settings to file via save_settings: {str(e)}")
            # Clean up the temporary file if replace failed
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception as e:
            logger.error(f"Unexpected error while saving settings via save_settings: {str(e)}")
            # Clean up the temporary file if replace failed
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

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
        # Use the property which reads from the main config
        return timedelta(days=self.staleness_threshold)