import hashlib
import tempfile
import os
import logging
import requests
import bencodepy
from typing import Optional, Tuple
from urllib.parse import urlencode

def torrent_to_magnet(file_path: str) -> Optional[str]:
    """
    Convert a torrent file to a magnet link.
    Returns None if conversion fails.
    """
    try:
        # Read and decode the torrent file
        with open(file_path, 'rb') as f:
            torrent_data = bencodepy.decode(f.read())
        info = torrent_data[b'info']
        
        # Calculate the info hash
        info_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest()
        
        # Get the display name
        if b'name.utf-8' in info:
            display_name = info[b'name.utf-8'].decode('utf-8')
        elif b'name' in info:
            display_name = info[b'name'].decode('utf-8', errors='replace')
        else:
            display_name = info_hash
            
        # Build tracker list
        trackers = []
        
        # Add announce URL if present
        if b'announce' in torrent_data:
            trackers.append(torrent_data[b'announce'].decode('utf-8'))
            
        # Add announce-list if present
        if b'announce-list' in torrent_data:
            for tier in torrent_data[b'announce-list']:
                for tracker in tier:
                    trackers.append(tracker.decode('utf-8'))
                    
        # Remove duplicates while preserving order
        trackers = list(dict.fromkeys(trackers))
        
        # Build magnet parameters
        magnet_params = {
            'xt': f'urn:btih:{info_hash}',
            'dn': display_name
        }
        
        # Add trackers
        if trackers:
            magnet_params['tr'] = trackers
            
        # Build the magnet URI
        magnet = 'magnet:?' + urlencode(magnet_params, True)
        
        return magnet
        
    except Exception as e:
        logging.error(f"Error converting torrent to magnet: {str(e)}")
        return None

def download_and_extract_hash(url: str) -> Optional[str]:
    """Download a torrent file and extract its hash"""
    with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as temp_file:
        try:
            # Download the torrent file
            response = requests.get(url)
            response.raise_for_status()
            
            temp_file.write(response.content)
            temp_file.flush()
            
            # Extract hash from the torrent file
            with open(temp_file.name, 'rb') as f:
                torrent_data = bencodepy.decode(f.read())
            info = torrent_data[b'info']
            return hashlib.sha1(bencodepy.encode(info)).hexdigest()
        except Exception as e:
            logging.error(f"Error extracting hash from torrent: {str(e)}")
            return None
        finally:
            # Clean up the temporary file
            try:
                os.unlink(temp_file.name)
            except Exception as e:
                logging.error(f"Error cleaning up temporary file: {str(e)}")

def download_and_convert_to_magnet(url: str) -> Optional[str]:
    """
    Download a torrent file from a URL and convert it to a magnet link.
    Returns None if the download or conversion fails.
    """
    with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as temp_file:
        try:
            # Download the torrent file
            response = requests.get(url)
            response.raise_for_status()
            
            temp_file.write(response.content)
            temp_file.flush()
            
            # Convert to magnet
            return torrent_to_magnet(temp_file.name)
            
        except Exception as e:
            logging.error(f"Error converting torrent to magnet: {str(e)}")
            return None
            
        finally:
            # Clean up the temporary file
            try:
                os.unlink(temp_file.name)
            except Exception as e:
                logging.error(f"Error cleaning up temporary file: {str(e)}")
