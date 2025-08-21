import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
import bencodepy
import hashlib
import tempfile
import os
# Remove Enum and dataclass if they were only for the moved types and not used elsewhere locally
# from enum import Enum # REMOVE if not used by other local things
# from dataclasses import dataclass # REMOVE if not used by other local things
import requests
from ..common.utils import extract_hash_from_magnet, is_valid_hash, process_hashes
from .api import make_request
# Update this import to get the moved types from debrid.status
from ..status import TorrentStatus, get_status_flags, TorrentFetchStatus, TorrentInfoStatus
from ..base import ProviderUnavailableError

def process_hashes(hashes: Union[str, List[str]], batch_size: int = 100) -> List[str]:
    """Process and validate a list of hashes"""
    if isinstance(hashes, str):
        hashes = [hashes]
    
    # Remove duplicates and invalid hashes
    return list(set(h.lower() for h in hashes if is_valid_hash(h)))

def get_torrent_info(api_key: str, torrent_id: str) -> Dict:
    """Get detailed information about a torrent"""
    return make_request('GET', f'/torrents/info/{torrent_id}', api_key)

def get_torrent_info_status(api_key: str, torrent_id: str) -> TorrentInfoStatus:
    """
    Gets detailed information about a torrent and returns a status object.
    Tries to distinguish between success, various error types (404, 429),
    and cases where the underlying make_request handles errors and returns None.
    """
    # Add retry mechanism for rate limit errors with more aggressive backoff
    max_retries = 2  # Reduced from 3 to be less aggressive
    retry_delay = 10  # Increased from 5 to 10 seconds delay
    
    try:
        for retry_attempt in range(max_retries):
            try:
                # Assumption: make_request might return None if it handles an HTTP error internally (e.g., logs it and suppresses exception).
                # Or, it might raise an exception (e.g., requests.exceptions.HTTPError) if it doesn't handle it.
                response_data = make_request('GET', f'/torrents/info/{torrent_id}', api_key)

                if response_data is not None:
                    # Successful retrieval of torrent info
                    return TorrentInfoStatus(status=TorrentFetchStatus.OK, data=response_data)
                else:
                    # This case is likely hit if make_request encountered an HTTP error (e.g., 404, 429),
                    # logged it internally (as suggested by user logs), and then returned None.
                    # Without changes to make_request to propagate specific error types or codes,
                    # we cannot reliably distinguish the exact HTTP error here.
                    logging.warning(
                        f"make_request for torrent {torrent_id} returned None. This often means an HTTP error "
                        f"(like 404 or 429) occurred and was logged by the underlying API call. "
                        f"The specific error type cannot be determined here without changes to make_request."
                    )
                    return TorrentInfoStatus(
                        status=TorrentFetchStatus.PROVIDER_HANDLED_ERROR,
                        message="Provider API call returned no data. Specific error (e.g., 404, 429) was likely logged by the provider. Check provider logs."
                    )
            except ProviderUnavailableError as e:
                if "429" in str(e) and retry_attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                    logging.warning(f"Rate limit (429) hit when getting torrent info status. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                    time.sleep(wait_time)
                else:
                    # Not a 429 error or we've exhausted retries
                    return TorrentInfoStatus(
                        status=TorrentFetchStatus.RATE_LIMITED if "429" in str(e) else TorrentFetchStatus.UNKNOWN_ERROR,
                        message=f"Provider unavailable: {str(e)}"
                    )

    # The following except blocks handle cases where make_request *does* raise exceptions
    # that were not caught and converted to a None return by make_request itself.
    # This acts as a secondary error-handling mechanism.
    # These specific exceptions (requests.exceptions.*) assume 'requests' library is used by make_request.
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        error_message = str(e)
        logging.warning(f"HTTPError caught directly in get_torrent_info_status for torrent {torrent_id}: {status_code} - {error_message}")
        if status_code == 404:
            return TorrentInfoStatus(status=TorrentFetchStatus.NOT_FOUND, http_status_code=status_code, message=error_message)
        elif status_code == 429:
            return TorrentInfoStatus(status=TorrentFetchStatus.RATE_LIMITED, http_status_code=status_code, message=error_message)
        elif status_code and 400 <= status_code < 500:
            return TorrentInfoStatus(status=TorrentFetchStatus.CLIENT_ERROR, http_status_code=status_code, message=error_message)
        elif status_code and 500 <= status_code < 600:
            return TorrentInfoStatus(status=TorrentFetchStatus.SERVER_ERROR, http_status_code=status_code, message=error_message)
        else: # Unknown HTTP error or no status_code
            return TorrentInfoStatus(status=TorrentFetchStatus.UNKNOWN_ERROR, http_status_code=status_code, message=f"Unhandled HTTPError: {error_message}")
    except requests.exceptions.RequestException as e: # Handles other request issues like network errors, DNS failure, connection refused
        logging.warning(f"RequestException caught in get_torrent_info_status for torrent {torrent_id}: {str(e)}")
        return TorrentInfoStatus(status=TorrentFetchStatus.REQUEST_ERROR, message=str(e))
    except Exception as e:
        # General catch-all for other unexpected errors from make_request or during its call
        logging.error(f"Unexpected general error in get_torrent_info_status for torrent {torrent_id}: {str(e)}", exc_info=True)
        return TorrentInfoStatus(status=TorrentFetchStatus.UNKNOWN_ERROR, message=f"An unexpected error occurred: {str(e)}")

def add_torrent(api_key: str, magnet_link: str, temp_file_path: Optional[str] = None) -> Dict:
    """Add a torrent to Real-Debrid and return the full response"""
    # Add retry mechanism for rate limit errors
    max_retries = 3
    retry_delay = 5  # Start with 5 seconds delay
    
    for retry_attempt in range(max_retries):
        try:
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
        except ProviderUnavailableError as e:
            if "429" in str(e) and retry_attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                logging.warning(f"Rate limit (429) hit when adding torrent. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                time.sleep(wait_time)
            else:
                # Not a 429 error or we've exhausted retries
                raise
        except Exception as e:
            logging.error(f"Error adding torrent: {str(e)}")
            raise

def select_files(api_key: str, torrent_id: str, file_ids: List[int]) -> None:
    """Select specific files from a torrent"""
    # Add retry mechanism for rate limit errors
    max_retries = 3
    retry_delay = 5  # Start with 5 seconds delay
    
    for retry_attempt in range(max_retries):
        try:
            data = {'files': ','.join(map(str, file_ids))}
            make_request('POST', f'/torrents/selectFiles/{torrent_id}', api_key, data=data)
            return  # Success, exit function
        except ProviderUnavailableError as e:
            if "429" in str(e) and retry_attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                logging.warning(f"Rate limit (429) hit when selecting files. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                time.sleep(wait_time)
            else:
                # Not a 429 error or we've exhausted retries
                raise
        except Exception as e:
            logging.error(f"Error selecting files: {str(e)}")
            raise

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
    logging.error(f"Removing torrent {torrent_id} - THIS FUNCTION IS DEPRECATED AND SHOULD NOT BE CALLED")
    logging.error("Use the remove_torrent method from RealDebridProvider class instead")
    
    # Add retry mechanism for rate limit errors
    max_retries = 3
    retry_delay = 5  # Start with 5 seconds delay
    
    for retry_attempt in range(max_retries):
        try:
            result = make_request('DELETE', f'/torrents/delete/{torrent_id}', api_key)
            # Check if the request was successful (either None for backward compatibility or success dict)
            if result is None or (isinstance(result, dict) and result.get('success')):
                return  # Success, exit function
            else:
                # Unexpected response format
                logging.error(f"Unexpected response format from DELETE request: {result}")
                raise ProviderUnavailableError(f"Unexpected response format: {result}")
        except ProviderUnavailableError as e:
            if "429" in str(e) and retry_attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                logging.warning(f"Rate limit (429) hit when removing torrent. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                time.sleep(wait_time)
            else:
                # Continue with the function as if removal succeeded if it's a 429 error
                if "429" in str(e):
                    logging.warning(f"Rate limit hit when removing torrent {torrent_id} after all retries. Will mark as removed anyway.")
                    return
                # Re-raise other provider errors
                raise
        except Exception as e:
            logging.error(f"Error removing torrent: {str(e)}")
            raise

def list_active_torrents(api_key: str) -> List[Dict]:
    """List all active torrents"""
    # Add retry mechanism for rate limit errors
    max_retries = 3
    retry_delay = 5  # Start with 5 seconds delay
    
    for retry_attempt in range(max_retries):
        try:
            availability = make_request('GET', '/torrents', api_key)
            if not availability:
                return []
                
            return availability
        except ProviderUnavailableError as e:
            if "429" in str(e) and retry_attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                logging.warning(f"Rate limit (429) hit when listing active torrents. Waiting {wait_time}s before retry {retry_attempt + 1}/{max_retries}.")
                time.sleep(wait_time)
            else:
                # Not a 429 error or we've exhausted retries
                logging.error(f"Error listing active torrents: {str(e)}")
                return []
        except Exception as e:
            logging.error(f"Error listing active torrents: {str(e)}")
            return []

def cleanup_stale_torrents(api_key: str) -> None:
    """Remove stale torrents that are older than 24 hours"""
    try:
        torrents = list_active_torrents(api_key)
        for torrent in torrents:
            added_date = datetime.fromtimestamp(torrent['added'])
            if datetime.now() - added_date > timedelta(hours=24):
                try:
                    # Use make_request directly instead of the deprecated remove_torrent function
                    result = make_request('DELETE', f'/torrents/delete/{torrent["id"]}', api_key)
                    # Check if the request was successful (either None for backward compatibility or success dict)
                    if result is None or (isinstance(result, dict) and result.get('success')):
                        logging.info(f"Removed stale torrent {torrent['id']}")
                    else:
                        logging.error(f"Failed to remove stale torrent {torrent['id']}: unexpected response format {result}")
                except ProviderUnavailableError as e:
                    if "429" in str(e):
                        logging.warning(f"Rate limit hit when removing stale torrent {torrent['id']}. Will continue with next torrent.")
                        # Add a small delay before continuing to the next torrent
                        time.sleep(5)
                    else:
                        logging.error(f"Provider error removing stale torrent {torrent['id']}: {str(e)}")
                except Exception as e:
                    logging.error(f"Error removing stale torrent {torrent['id']}: {str(e)}")
    except Exception as e:
        logging.error(f"Error during stale torrent cleanup: {str(e)}")
