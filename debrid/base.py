from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Union, Tuple, Any
from .common import RateLimiter, timed_lru_cache
from .status import TorrentStatus
import hashlib
import platform
import base64
import logging
from cryptography.fernet import Fernet
from .common.c7d9e45f import _v, _p1, _p2, _p3
import functools

# Import from the new location: debrid.status
from debrid.status import TorrentInfoStatus, TorrentFetchStatus

class EncryptedCapabilityDescriptor:
    """A descriptor that protects capability values from being overridden"""
    
    def __init__(self, capability_name: str):
        self.capability_name = capability_name
        # Use a random name for the cached value to make it harder to find
        self.cached_name = f"_{hashlib.sha256(capability_name.encode()).hexdigest()[:8]}"
    
    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        # Cache the decrypted value to prevent tampering with _get_encrypted_value
        if not hasattr(instance, self.cached_name):
            # Get the value through a chain of protected calls
            value = instance._get_capability_value(self.capability_name)
            # Store in instance dict directly to bypass any __setattr__ overrides
            instance.__dict__[self.cached_name] = value
        return instance.__dict__[self.cached_name]
    
    def __set__(self, instance, value):
        raise AttributeError("Can't modify capability values")

class DebridProviderError(Exception):
    """Base exception class for all debrid provider errors"""
    pass

class TooManyDownloadsError(DebridProviderError):
    """Exception raised when the debrid service has too many active downloads"""
    pass

class ProviderUnavailableError(DebridProviderError):
    """Exception raised when the debrid service is unavailable"""
    pass

class TorrentAdditionError(DebridProviderError):
    """Exception raised when there is an error adding a torrent to the debrid service"""
    pass

class RateLimitError(DebridProviderError):
    """Exception raised when the debrid service rate limit is exceeded"""
    pass

class DebridProvider(ABC):
    """Abstract base class that defines the interface for debrid providers"""
    
    def __init__(self, api_key: str = None, rate_limit: float = 0.5):
        """Initialize the provider"""
        self.rate_limiter = RateLimiter(calls_per_second=rate_limit)
        self._api_key = api_key
        self._status = {}
        self._cached_torrent_ids = {}  # Store torrent IDs for cached content
        self._setup_encryption()
        
        # Log provider capabilities
        #logging.debug(f"[{self.__class__.__name__}] Capability check:")
        #logging.debug(f"[{self.__class__.__name__}] - supports_direct_cache_check: {self.supports_direct_cache_check}")
        #logging.debug(f"[{self.__class__.__name__}] - supports_bulk_cache_checking: {self.supports_bulk_cache_checking}")
        #logging.debug(f"[{self.__class__.__name__}] - supports_uncached: {self.supports_uncached}")
    
    def _setup_encryption(self) -> None:
        """Setup encryption for provider capabilities"""
        key_base = self.__class__.__name__.encode() + b'debrid_capabilities_key'
        key = base64.urlsafe_b64encode(hashlib.sha256(key_base).digest()[:32])
        self._cipher = Fernet(key)
    
    def _get_encrypted_value(self, capability: str) -> bool:
        """Get decrypted boolean value for a capability"""
        try:
            encrypted = _v[self.__class__.__name__][capability]
            decrypted = self._cipher.decrypt(encrypted)
            return decrypted.decode() == 'True'
        except Exception:
            return False
            
    def _get_capability_value(self, capability: str) -> bool:
        """Protected method to get capability value through encryption"""
        return self._get_encrypted_value(capability)

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
    
    # Define properties using imported descriptors
    supports_direct_cache_check = _p1
    supports_bulk_cache_checking = _p2
    supports_uncached = _p3
    
    @abstractmethod
    def add_torrent(self, magnet_link: str, temp_file_path: Optional[str] = None) -> str:
        """Add a torrent/magnet link to the debrid service"""
        pass
    
    @abstractmethod
    def is_cached(self, magnet_or_url: Union[str, List[str]], temp_file_path: Optional[str] = None, result_title: Optional[str] = None, result_index: Optional[str] = None, remove_uncached: bool = True, skip_phalanx_db: bool = False) -> Union[bool, Dict[str, Optional[bool]]]:
        """
        Check if a magnet link or torrent file is cached on the service.
        
        Args:
            magnet_or_url: Magnet link, hash, or URL to check
            temp_file_path: Optional path to torrent file
            result_title: Optional result title for cached content
            result_index: Optional result index for cached content
            remove_uncached: Whether to remove uncached content
            skip_phalanx_db: Whether to skip phalanx database check
            
        Returns:
            - For single input: bool or None (error)
            - For list input: dict mapping hashes to bool or None (error)
        """
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
        """Get basic information about a torrent."""
        pass

    @abstractmethod
    def get_torrent_info_with_status(self, torrent_id: str) -> TorrentInfoStatus:
        """
        Get detailed information about a torrent, including fetch status.
        """
        pass

    @abstractmethod
    def remove_torrent(self, torrent_id: str, removal_reason: Optional[str] = None) -> bool:
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

    @abstractmethod
    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        """Get stored torrent ID for a cached hash"""
        pass

    @abstractmethod
    def list_active_torrents(self) -> List[Dict]:
        """
        List all active torrents.
        
        Returns:
            List of dictionaries containing torrent information with at least:
            - filename: str
            - progress: float
            - status: str
        """
        pass

    def get_torrent_status(self) -> Tuple[List[Dict], Tuple[int, int]]:
        """
        Get a comprehensive view of active torrents and download limits.
        
        Returns:
            Tuple containing:
            - List of dictionaries with torrent details
            - Tuple of (active_count, max_downloads)
        """
        active_torrents = self.list_active_torrents()
        active_count, max_downloads = self.get_active_downloads()
        return active_torrents, (active_count, max_downloads)

    def is_cached_sync(self, magnet_link: str, temp_file_path: Optional[str] = None, result_title: Optional[str] = None, result_index: Optional[str] = None, remove_uncached: bool = True, skip_phalanx_db: bool = False) -> Union[bool, Dict[str, bool], None]:
        """Synchronous version of is_cached"""
        import asyncio
        try:
            # Create a new event loop for this thread if one doesn't exist
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # Run the async method in the event loop
            return loop.run_until_complete(
                self.is_cached(magnet_link, temp_file_path, result_title, result_index, remove_uncached, skip_phalanx_db=skip_phalanx_db)
            )
        except Exception as e:
            logging.error(f"Error in is_cached_sync: {str(e)}")
            return None
