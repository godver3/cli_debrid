from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Union, Tuple
from .common import RateLimiter, timed_lru_cache
from .status import TorrentStatus

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
    
    def __init__(self, api_key: str = None, rate_limit: float = 0.5):
        """
        Initialize the provider
        
        Args:
            api_key: Optional API key. If not provided, will attempt to load from settings
            rate_limit: API rate limit in calls per second
        """
        self.rate_limiter = RateLimiter(calls_per_second=rate_limit)
        self._api_key = api_key
        self._status = {}  # Track status of operations
    
    @property
    def api_key(self) -> str:
        """Get the API key, loading from settings if not set"""
        if not self._api_key:
            self._api_key = self._load_api_key()
        return self._api_key
    
    @abstractmethod
    def _load_api_key(self) -> str:
        """Load API key from settings"""
        pass
    
    @property
    def supports_direct_cache_check(self) -> bool:
        """Whether this provider supports checking cache status without adding the torrent"""
        return True
    
    @property
    def supports_bulk_cache_checking(self) -> bool:
        """Whether this provider supports checking multiple hashes in a single API call"""
        return False
    
    @abstractmethod
    def add_torrent(self, magnet_link: str, temp_file_path: Optional[str] = None) -> str:
        """Add a torrent/magnet link to the debrid service"""
        pass
    
    @abstractmethod
    def is_cached(self, magnet_link: str) -> bool:
        """Check if a magnet link is cached on the service"""
        pass
    
    @abstractmethod
    def get_active_downloads(self) -> Tuple[int, int]:
        """Get number of active downloads and the concurrent download limit"""
        pass
    
    @abstractmethod
    def get_user_traffic(self) -> Dict:
        """Get user traffic/usage information"""
        pass

    @abstractmethod
    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """Get information about a specific torrent"""
        pass
        
    @abstractmethod
    def remove_torrent(self, torrent_id: str) -> None:
        """Remove a torrent from the service"""
        pass
        
    def get_status(self, torrent_id: str) -> TorrentStatus:
        """Get the current status of a torrent"""
        return self._status.get(torrent_id, TorrentStatus.UNKNOWN)
        
    def update_status(self, torrent_id: str, status: TorrentStatus) -> None:
        """Update the status of a torrent"""
        self._status[torrent_id] = status
        
    def cleanup(self) -> None:
        """Clean up any resources or stale torrents"""
        self._status.clear()
