import hashlib
import tempfile
import os
import logging
import requests
import bencodepy
from typing import Optional, Tuple
from urllib.parse import urlencode
from .utils import extract_hash_from_magnet
from .cache import timed_lru_cache

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
        info_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest().lower()
        
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
        
        # Build the magnet URI manually to avoid double encoding
        magnet_parts = [f'magnet:?xt=urn:btih:{info_hash}']
        
        # Add display name
        magnet_parts.append(f'dn={urlencode({"": display_name})[1:]}')
        
        # Add trackers
        for tracker in trackers:
            magnet_parts.append(f'tr={urlencode({"": tracker})[1:]}')
            
        # Join all parts with &
        magnet = '&'.join(magnet_parts)
        
        logging.debug(f"Generated magnet link: {magnet}")
        return magnet
        
    except Exception as e:
        logging.error(f"Error converting torrent to magnet: {str(e)}")
        return None

@timed_lru_cache(seconds=60)
def download_and_extract_hash(url: str, max_redirects: int = 5) -> Optional[str]:
    """
    Download a torrent file from a URL or handle a URL that 
    redirects to/contains a magnet link, and extract its infohash.
    """
    if max_redirects <= 0:
        logging.error(f"Max redirects reached for URL processing: {url}")
        return None

    current_url = url.strip()
    logging.debug(f"Attempting to extract hash from URL: {current_url} (redirects left: {max_redirects})")

    try:
        if current_url.startswith('magnet:'):
            logging.debug(f"URL is a magnet link. Extracting hash directly: {current_url}")
            return extract_hash_from_magnet(current_url)

        response = requests.get(current_url, timeout=30, allow_redirects=False)

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            if location:
                location = location.strip()
                logging.info(f"URL {current_url} redirected to: {location}")
                if location.startswith('magnet:'):
                    return extract_hash_from_magnet(location)
                elif location.startswith('http://') or location.startswith('https://'):
                    # Recursive call for the new HTTP(S) URL
                    return download_and_extract_hash(location, max_redirects=max_redirects - 1)
                else:
                    logging.warning(f"URL {current_url} redirected to unhandled scheme: {location}")
                    # Fall through to raise_for_status on the original response, which might be an error code
            else:
                logging.warning(f"Redirect from {current_url} with no Location header.")
        
        response.raise_for_status() # Check for HTTP errors if not a handled redirect or if redirect had no location

        # Check if response body *is* a magnet link
        # Read a small part of the content to check for 'magnet:'
        # Be careful with response.content if it's huge and not a magnet
        content_preview = response.content[:256].decode('utf-8', errors='ignore').strip()
        if content_preview.startswith('magnet:'):
            # If preview suggests magnet, try to get full text if it's reasonable
            # This assumes magnet links are relatively small.
            full_text_content = response.text.strip()
            if full_text_content.startswith('magnet:'):
                 logging.info(f"URL {current_url} content is a magnet link.")
                 return extract_hash_from_magnet(full_text_content)
            else:
                logging.debug(f"Content of {current_url} started like a magnet but full text was not. Proceeding as torrent file.")


        # If none of the above, assume it's torrent file content
        logging.debug(f"Processing content of {current_url} as a torrent file.")
        with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as temp_file:
            temp_file_path = temp_file.name
            try:
                temp_file.write(response.content)
                temp_file.flush()
                
                # Extract hash from the downloaded torrent file
                with open(temp_file_path, 'rb') as f_torrent:
                    torrent_data = bencodepy.decode(f_torrent.read())
                info = torrent_data.get(b'info')
                if info is None:
                    logging.error(f"No 'info' dictionary found in torrent data from {current_url}")
                    return None
                return hashlib.sha1(bencodepy.encode(info)).hexdigest().lower()
            finally:
                try:
                    os.unlink(temp_file_path)
                except Exception as e_unlink:
                    logging.warning(f"Error cleaning up temporary file {temp_file_path}: {str(e_unlink)}")
                    
    except requests.exceptions.RequestException as e_req:
        # This catches network errors, timeout, too many (HTTP) redirects if allow_redirects were true elsewhere
        logging.error(f"RequestException for URL {current_url}: {str(e_req)}")
        return None
    except bencodepy.BencodeDecodeError as e_bencode:
        logging.error(f"Bencode decoding error for content from {current_url}: {str(e_bencode)}")
        return None
    except Exception as e:
        # Catch-all for other unexpected errors
        logging.error(f"Error extracting hash from URL {current_url}: {str(e)}", exc_info=True)
        return None

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

def extract_hash_from_file(file_path: str) -> Optional[str]:
    """
    Extract hash from a local torrent file
    
    Args:
        file_path: Path to the torrent file
        
    Returns:
        Torrent hash string or None if extraction fails
    """
    try:
        with open(file_path, 'rb') as f:
            torrent_data = bencodepy.decode(f.read())
        info = torrent_data[b'info']
        return hashlib.sha1(bencodepy.encode(info)).hexdigest()
    except Exception as e:
        logging.error(f"Error extracting hash from torrent file: {str(e)}")
        return None
