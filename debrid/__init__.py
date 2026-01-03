import logging
from typing import Optional
from utilities.settings import get_setting, ensure_settings_file
from .base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
from .real_debrid import RealDebridProvider
from .alldebrid import AllDebridProvider
from .common import (
    extract_hash_from_magnet,
    download_and_extract_hash,
    timed_lru_cache,
    torrent_to_magnet,
    is_video_file,
    is_unwanted_file
)

_provider_instance: Optional[DebridProvider] = None

def get_debrid_provider() -> DebridProvider:
    """
    Factory function that returns the configured debrid provider instance.
    Uses singleton pattern to maintain one instance per provider.
    """
    global _provider_instance
    
    if _provider_instance is not None:
        return _provider_instance
    
    # Ensure settings file exists and is properly initialized
    ensure_settings_file()

    provider_name_raw = get_setting("Debrid Provider", "provider", "")
    provider_name = provider_name_raw.lower()
    logging.info(f"[DEBRID FACTORY] Loading debrid provider: raw='{provider_name_raw}', lower='{provider_name}'")

    if provider_name == 'realdebrid':
        logging.info("[DEBRID FACTORY] Instantiating RealDebridProvider")
        _provider_instance = RealDebridProvider()
    elif provider_name == 'alldebrid':
        logging.info("[DEBRID FACTORY] Instantiating AllDebridProvider")
        _provider_instance = AllDebridProvider()
    else:
        raise ValueError(f"Unknown debrid provider: {provider_name}")
        
    return _provider_instance

def reset_provider() -> None:
    """Reset the debrid provider instance, forcing it to be reinitialized on next use."""
    global _provider_instance
    _provider_instance = None

# Export public interface
__all__ = [
    'get_debrid_provider',
    'reset_provider',
    'DebridProvider',
    'TooManyDownloadsError',
    'ProviderUnavailableError',
    'RealDebridProvider',
    'AllDebridProvider',
    'extract_hash_from_magnet',
    'download_and_extract_hash',
    'timed_lru_cache',
    'torrent_to_magnet',
    'is_video_file',
    'is_unwanted_file'
]
