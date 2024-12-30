from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Union, Tuple

class DebridProviderError(Exception):
    """Base exception class for all debrid provider errors"""
    pass

class TooManyDownloadsError(DebridProviderError):
    """Exception raised when the debrid service has too many active downloads"""
    pass

class ProviderUnavailableError(DebridProviderError):
    """Exception raised when the debrid service is unavailable"""
    pass

class DebridProvider(ABC):
    """Abstract base class that defines the interface for debrid providers"""
    
    @property
    def supports_direct_cache_check(self) -> bool:
        """Whether this provider supports checking cache status without adding the torrent"""
        return True  # Default to True for backward compatibility
    
    @abstractmethod
    def add_torrent(self, magnet_link: str, temp_file_path: Optional[str] = None) -> Dict:
        """Add a torrent/magnet link to the debrid service"""
        pass
    
    @abstractmethod
    def is_cached(self, hashes: Union[str, List[str]]) -> Dict:
        """Check if hash(es) are cached on the service"""
        pass
    
    @abstractmethod
    def get_cached_files(self, hash_: str) -> List[Dict]:
        """Get available cached files for a hash"""
        pass
    
    @abstractmethod
    def get_active_downloads(self, check: bool = False) -> List[Dict]:
        """Get list of active downloads"""
        pass
    
    @abstractmethod
    def get_user_traffic(self) -> Dict:
        """Get user traffic/usage information"""
        pass

    @abstractmethod
    def get_torrent_info(self, hash_value: str) -> Optional[Dict]:
        """Get information about a specific torrent by its hash"""
        pass

    @abstractmethod
    def get_torrent_files(self, hash_value: str) -> List[Dict]:
        """Get list of files in a torrent"""
        pass

    @abstractmethod
    def remove_torrent(self, torrent_id: str) -> bool:
        """Remove a torrent from the service"""
        pass

    @abstractmethod
    def download_and_extract_hash(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Download a torrent file and extract its hash. Returns (hash, temp_file_path)"""
        pass
