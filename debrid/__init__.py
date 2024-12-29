from typing import Optional
from settings import get_setting, ensure_settings_file
from .base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
from .real_debrid import RealDebridProvider
from .torbox import TorboxProvider

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
    
    provider_name = get_setting("Debrid Provider", "provider")
    
    if provider_name == 'RealDebrid':
        _provider_instance = RealDebridProvider()
    elif provider_name == 'Torbox' or provider_name is None:
        _provider_instance = TorboxProvider()
    else:
        raise ValueError(f"Unknown debrid provider: {provider_name}")
        
    return _provider_instance

# Convenience functions that delegate to the configured provider
def add_to_real_debrid(magnet_link, temp_file_path=None):
    return get_debrid_provider().add_torrent(magnet_link, temp_file_path)
    
def is_cached_on_rd(hashes):
    return get_debrid_provider().is_cached(hashes)
    
def get_cached_files(hash_):
    return get_debrid_provider().get_cached_files(hash_)
    
def get_active_downloads(check=False):
    return get_debrid_provider().get_active_downloads(check)
    
def get_user_traffic():
    return get_debrid_provider().get_user_traffic()

def check_daily_usage():
    """Check the daily usage for the configured debrid provider"""
    traffic = get_debrid_provider().get_user_traffic()
    if not traffic:
        return None
    return {
        'downloaded': traffic.get('downloaded', 0),
        'limit': traffic.get('limit', 0)
    }

def extract_hash_from_magnet(magnet_link):
    """Extract hash from a magnet link"""
    import re
    xt = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", magnet_link)
    if xt:
        return xt.group(1).lower()
    return None

def get_magnet_files(magnet_link):
    """Get files from a magnet link"""
    hash_ = extract_hash_from_magnet(magnet_link)
    if hash_:
        return get_cached_files(hash_)
    return None

# Export error classes
__all__ = [
    'get_debrid_provider',
    'add_to_real_debrid',
    'is_cached_on_rd',
    'get_cached_files',
    'get_active_downloads',
    'get_user_traffic',
    'check_daily_usage',
    'extract_hash_from_magnet',
    'get_magnet_files',
    'TooManyDownloadsError',
    'ProviderUnavailableError'
]
