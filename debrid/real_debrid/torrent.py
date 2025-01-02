import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
import bencodepy
import hashlib
import tempfile
import os
from .utils import extract_hash_from_magnet, is_valid_hash, file_matches_item
from .api import make_request
from ..status import TorrentStatus, get_status_flags

def process_hashes(hashes: Union[str, List[str]], batch_size: int = 100) -> List[str]:
    """Process and validate a list of hashes"""
    if isinstance(hashes, str):
        hashes = [hashes]
    
    # Remove duplicates and invalid hashes
    return list(set(h.lower() for h in hashes if is_valid_hash(h)))

def get_torrent_info(api_key: str, torrent_id: str) -> Dict:
    """Get detailed information about a torrent"""
    return make_request('GET', f'/torrents/info/{torrent_id}', api_key)

def add_torrent(api_key: str, magnet_link: str, temp_file_path: Optional[str] = None) -> Dict:
    """Add a torrent to Real-Debrid and return the full response"""
    if magnet_link.startswith('magnet:'):
        # Add magnet link
        data = {'magnet': magnet_link}
        result = make_request('POST', '/torrents/addMagnet', api_key, data=data)
    else:
        # Add torrent file
        if not temp_file_path:
            raise ValueError("Temp file path required for torrent file upload")
            
        with open(temp_file_path, 'rb') as f:
            files = {'file': f}
            result = make_request('PUT', '/torrents/addTorrent', api_key, files=files)
            
    return result

def select_files(api_key: str, torrent_id: str, file_ids: List[int]) -> None:
    """Select specific files from a torrent"""
    data = {'files': ','.join(map(str, file_ids))}
    make_request('POST', f'/torrents/selectFiles/{torrent_id}', api_key, data=data)

def get_torrent_files(api_key: str, hash_value: str) -> List[Dict]:
    """Get list of files in a torrent"""
    try:
        availability = make_request('GET', f'/torrents/instantAvailability/{hash_value}', api_key)
        if not availability or hash_value not in availability:
            return []
            
        rd_data = availability[hash_value].get('rd', [])
        if not rd_data:
            return []
            
        files = []
        for data in rd_data:
            if not data:
                continue
            for file_id, file_info in data.items():
                files.append({
                    'id': file_id,
                    'filename': file_info.get('filename', ''),
                    'size': file_info.get('filesize', 0)
                })
                
        return files
    except Exception as e:
        logging.error(f"Error getting torrent files for hash {hash_value}: {str(e)}")
        return []

def remove_torrent(api_key: str, torrent_id: str) -> None:
    """Remove a torrent from Real-Debrid"""
    make_request('DELETE', f'/torrents/delete/{torrent_id}', api_key)

def list_active_torrents(api_key: str) -> List[Dict]:
    """List all active torrents"""
    return make_request('GET', '/torrents', api_key)

def cleanup_stale_torrents(api_key: str) -> None:
    """Remove stale torrents that are older than 24 hours"""
    try:
        torrents = list_active_torrents(api_key)
        for torrent in torrents:
            added_date = datetime.fromtimestamp(torrent['added'])
            if datetime.now() - added_date > timedelta(hours=24):
                try:
                    remove_torrent(api_key, torrent['id'])
                    logging.info(f"Removed stale torrent {torrent['id']}")
                except Exception as e:
                    logging.error(f"Error removing stale torrent {torrent['id']}: {str(e)}")
    except Exception as e:
        logging.error(f"Error during stale torrent cleanup: {str(e)}")
