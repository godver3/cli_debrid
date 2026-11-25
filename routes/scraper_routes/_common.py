"""Scraper blueprint, Phalanx cache, search analytics and small helpers shared by the scraper route modules."""

from flask import jsonify, request, Blueprint
import logging
from utilities.settings import get_setting
from queues.media_matcher import MediaMatcher
from typing import Dict, Any, Tuple
import requests
import bencodepy
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
import re
import time

scraper_bp = Blueprint('scraper', __name__)

# Initialize cache manager only if enabled
_phalanx_cache_manager = PhalanxDBClassManager() if get_setting('UI Settings', 'enable_phalanx_db', default=False) else None

# PHASE 2 OPTIMIZATION: Search analytics for data-driven prefetching
_search_analytics = {}

@scraper_bp.route('/convert_tmdb_to_imdb/<int:tmdb_id>')
def convert_tmdb_to_imdb(tmdb_id):
    from metadata.metadata import get_imdb_id_if_missing
    max_retries = 1
    base_delay = 0.1  # Base delay in seconds
    
    for attempt in range(max_retries):
        try:
            imdb_id = get_imdb_id_if_missing({'tmdb_id': tmdb_id})
            if imdb_id:
                return jsonify({'imdb_id': imdb_id})
            
            # If we get None but no exception, try again with backoff
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.warning(f"TMDB to IMDB conversion attempt {attempt + 1} failed, retrying in {delay} seconds...")
                time.sleep(delay)
                continue
                
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.error(f"Error in TMDB to IMDB conversion attempt {attempt + 1}: {str(e)}, retrying in {delay} seconds...")
                time.sleep(delay)
                continue
            else:
                logging.error(f"All TMDB to IMDB conversion attempts failed: {str(e)}")
                break
    
    return jsonify({'imdb_id': 'N/A'})

def obfuscate_magnet_link(magnet_link: str) -> str:
    """
    Obfuscate the magnet link by hiding the domain and API key if present.
    """
    # Check if the magnet link contains 'jackett_apikey'
    if 'jackett_apikey' in magnet_link:
        # Use regex to find and replace the domain and API key
        # Replace the domain (e.g., http://192.168.1.51:9117) with '***'
        magnet_link = re.sub(r'^http:\/\/[^\/]+', '***', magnet_link)
        # Replace the jackett_apikey value with '***'
        magnet_link = re.sub(r'jackett_apikey=[^&]+', 'jackett_apikey=***', magnet_link)
    return magnet_link

class ContentProcessor:
    """Handles the processing of media content after it's been added to the debrid service"""
    
    def __init__(self):
        self.media_matcher = MediaMatcher()

    def process_content(self, torrent_info: Dict[str, Any], item: Dict[str, Any] = None) -> Tuple[bool, str]:
        """
        Process content after it's been added to the debrid service
        
        Args:
            torrent_info: Information about the added torrent
            item: Optional media item to match against
            
        Returns:
            Tuple of (success, message)
        """
        try:
            files = torrent_info.get('files', [])
            if not files:
                return False, "No files found in torrent"

            # If we have a specific item to match against
            if item:
                matches = self.media_matcher.match_content(files, item)
                if not matches:
                    return False, "No matching files found"
                if len(matches) > 1 and item.get('type') == 'movie':
                    return False, "Multiple matches found for movie"
            # Otherwise just check for any valid video files
            else:
                video_files = [f for f in files if self._is_video_file(f.get('path', ''))]
                if not video_files:
                    return False, "No suitable video files found"

            return True, "Content processed successfully"
            
        except Exception as e:
            logging.error(f"Error processing content: {str(e)}")
            return False, f"Error processing content: {str(e)}"

    def _is_video_file(self, file_path: str) -> bool:
        """Check if a file is a video file based on extension"""
        video_extensions = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv'}
        return any(file_path.lower().endswith(ext) for ext in video_extensions)

def _download_and_get_hash(url: str) -> str:
    """
    Download a torrent file from URL and extract its hash
    
    Args:
        url: URL to download torrent from
        
    Returns:
        Torrent hash string
    
    Raises:
        Exception if download fails or hash cannot be extracted
    """
    try:
        # Download the file
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        try:
            # Parse the torrent data directly from response content
            torrent = bencodepy.decode(response.content)
            info = torrent[b'info']
            encoded_info = bencodepy.encode(info)
            import hashlib
            torrent_hash = hashlib.sha1(encoded_info).hexdigest()
            return torrent_hash
            
        except Exception as e:
            raise Exception(f"Failed to decode torrent data: {str(e)}")
                
    except Exception as e:
        raise Exception(f"Failed to process torrent URL: {str(e)}")

def _record_manual_grab(item_id, kind, release_title, info_hash=None):
    """Scraper stats: log a grab made from the web UI. The UI posts the chosen
    result's source fields alongside the add request; never raises."""
    try:
        from flask import has_request_context
        from database.scraper_grabs import record_grab
        from database.database_reading import get_media_item_by_id
        result = {}
        if has_request_context():
            result = {k: request.form.get(k) for k in ('source', 'scraper_type', 'scraper_instance', 'indexer')
                      if request.form.get(k)}
        item = get_media_item_by_id(item_id) or {'id': item_id}
        record_grab(item, result, trigger='manual', kind=kind,
                    release_title=release_title, info_hash=info_hash)
    except Exception as e:
        logging.debug(f"[ScraperStats] manual grab not recorded for item {item_id}: {e}")
