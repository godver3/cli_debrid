import os
import logging

class TorrentProcessor:
    def __init__(self, debrid_provider):
        self.debrid_provider = debrid_provider
        self.temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp')
        os.makedirs(self.temp_dir, exist_ok=True)

    def check_cache_for_hash(self, torrent_hash):
        """
        Check if a torrent is cached using its hash directly.
        
        Args:
            torrent_hash (str): The hash of the torrent to check
            
        Returns:
            bool: True if cached, False if not cached, None if check failed
        """
        try:
            logging.info(f"Checking cache status for hash: {torrent_hash}")
            if not torrent_hash:
                logging.warning("No hash provided for cache check")
                return None
                
            # Clean the hash
            torrent_hash = torrent_hash.lower().strip()
            
            # Check cache status
            is_cached = self.debrid_provider.is_cached(torrent_hash)
            logging.info(f"Cache check result for hash {torrent_hash}: {is_cached}")
            return is_cached
            
        except Exception as e:
            logging.error(f"Error checking cache status for hash: {e}")
            return None 