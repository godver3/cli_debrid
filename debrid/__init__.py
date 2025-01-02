from typing import Optional
from settings import get_setting, ensure_settings_file
from .base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
from .real_debrid import RealDebridProvider
from .torbox import TorBoxProvider
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
    
    provider_name = get_setting("Debrid Provider", "provider", "").lower()
    
    if provider_name == 'realdebrid':
        _provider_instance = RealDebridProvider()
    elif provider_name == 'torbox':
        _provider_instance = TorBoxProvider()
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
    'TorBoxProvider',
    'extract_hash_from_magnet',
    'download_and_extract_hash',
    'timed_lru_cache',
    'torrent_to_magnet',
    'is_video_file',
    'is_unwanted_file'
]
