import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import requests
import json
import os

class PhalanxDBClassManager:
    """Manager class for handling cache status checks using Phalanx-DB service"""
    
    def __init__(self, phalanx_base_url: str = None, phalanx_port: int = None):
        """Initialize the Phalanx DB cache manager
        
        Args:
            phalanx_base_url: Base URL of the Phalanx DB service (e.g., http://localhost). Overrides env var.
            phalanx_port: Port number for the Phalanx DB service. Overrides env var.
        """
        # Determine the port
        if phalanx_port is None:
            try:
                phalanx_port = int(os.environ.get('CLI_DEBRID_PHALANX_PORT', 8888))
            except ValueError:
                logging.warning("Invalid CLI_DEBRID_PHALANX_PORT value, using default 8888.")
                phalanx_port = 8888
        self._port = phalanx_port
        
        # Determine the base URL
        if phalanx_base_url is None:
            phalanx_base_url = os.environ.get('CLI_DEBRID_PHALANX_URL', 'http://localhost')
        self._base_url = phalanx_base_url.rstrip('/')
        
        # Construct the single full URL
        self._url = f"{self._base_url}:{self._port}"
        logging.info(f"Using Phalanx DB URL: {self._url}")

        self._primary_url = None
        self._backup_url = None
        self._last_sync_time = None
        self.auth_token = "phalanx_db_v1_32byte_key_20240312_01"
        self.headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "X-Encryption-Key": self.auth_token  # Adding encryption key header
        }
        
        # Create a persistent session for connection pooling
        self._session = requests.Session()

    def _try_urls(self, endpoint: str, method: str = 'GET', data: Optional[Dict] = None, 
                 params: Optional[Dict] = None) -> Tuple[Optional[Any], Optional[str], List[Tuple[str, str]]]:
        """Try the single URL until it works.
        
        Args:
            endpoint: API endpoint to call
            method: HTTP method to use
            data: Optional data to send with request
            params: Optional query parameters to include
            
        Returns:
            Tuple of (response data, working URL, errors) where errors is a list of (url, error_message) tuples
        """
        errors = []
        expected_responses = []  # Track expected responses like 404 "Entry not found"
        
        # Use longer timeouts for /api/debug and /api/entries endpoints since they return larger datasets
        # Determine timeout based on endpoint and connection type
        if endpoint in ['/api/debug', '/api/entries']:
            timeout = 5.0 if 'localhost' in self._url else 5.0  # Increased timeouts for status endpoints
        else:
            timeout = 1.0 if 'localhost' in self._url else 1.0  # Default timeouts for other endpoints
            
        try:
            full_url = f"{self._url}{endpoint}"
            
            if method == 'GET':
                response = self._session.get(full_url, headers=self.headers, params=params, timeout=timeout)
                response_text = response.text
                if response.status_code == 200:
                    try:
                        result = response.json()
                        if result:  # Found data
                            return result, self._url, errors
                    except json.JSONDecodeError as je:
                        logging.debug(f"Failed to parse JSON from URL {self._url}: {str(je)}")
                        errors.append((self._url, f"JSON parse error: {str(je)}"))
                elif response.status_code == 404 and response_text == '{"error":"Entry not found"}':
                    expected_responses.append((self._url, f"HTTP {response.status_code}: {response_text}"))
                    # Service responded correctly, but entry not found
                    return None, None, expected_responses
                else:
                    errors.append((self._url, f"HTTP {response.status_code}: {response_text[:200]}"))
            elif method == 'POST':
                response = self._session.post(full_url, headers=self.headers, json=data, timeout=timeout)
                response_text = response.text
                if response.status_code in [200, 201]:  # Accept both 200 OK and 201 Created
                    try:
                        result = response.json()
                        return result, self._url, errors
                    except json.JSONDecodeError as je:
                        # Still consider it successful even if we cannot parse JSON
                        if response_text:
                            return {"success": True}, self._url, errors
                        errors.append((self._url, f"JSON parse error: {str(je)}"))
                else:
                    errors.append((self._url, f"HTTP {response.status_code}: {response_text[:200]}"))
        except Exception as e:
            errors.append((self._url, str(e)))
                
        # Log error if the single URL failed unexpectedly
        if errors and not expected_responses:
            error_details = f"{self._url}: {errors[0][1]}" # Get the first error
            logging.error(f"PhalanxDB URL failed - {error_details}")
        elif expected_responses:
            # If we got expected responses (like 404 Entry not found), include them in the return
            return None, None, expected_responses
            
        return None, None, errors

    def get_cache_status(self, hash_value: str) -> Optional[Dict[str, Any]]:
        """Get the cache status for a given hash
        
        Args:
            hash_value: The torrent hash to look up
            
        Returns:
            Optional[Dict]: Cache status info if found, None if not found or unchecked or expired
            The dict contains:
                - is_cached: bool  # true or false
                - timestamp: datetime
                - expiry: datetime
                - service: str
        """
        try:
            # Format the URL with infohash+service as required by the API
            result, url, errors = self._try_urls(f"/api/entries/{hash_value}+real_debrid")
            if not result:
                # Check if this was a 404 "Entry not found" response
                if any(err[1].startswith('HTTP 404: {"error":"Entry not found"}') for err in errors):
                    # This is a valid case - the data just doesn't exist yet
                    return None
                return None

            # Check if we got a valid response with data
            if not isinstance(result, dict) or 'data' not in result or not result['data']:
                return None

            # Get the first (and should be only) entry from data array
            entry = result['data'][0]
            
            # Handle schema v2.0 with nested services
            if 'services' in entry and 'real_debrid' in entry['services']:
                service_data = entry['services']['real_debrid']
                
                # Parse timestamps
                last_modified = None
                expiry = None
                
                if 'last_modified' in service_data:
                    timestamp = service_data['last_modified']
                    if timestamp is not None:
                        last_modified = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                
                if 'expiry' in service_data:
                    timestamp = service_data['expiry']
                    if timestamp is not None:
                        expiry = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    
                    # Check if entry is expired - ensure both datetimes are timezone aware
                    if expiry:
                        now = datetime.now(expiry.tzinfo)
                        if expiry < now:
                            logging.info(f"Cache entry for {hash_value} is expired (expiry: {expiry}), triggering new cache check")
                            return None

                return {
                    'is_cached': service_data.get('cached', False),
                    'timestamp': last_modified,
                    'expiry': expiry,
                    'service': 'real_debrid'
                }
            else:
                logging.warning(f"Entry for {hash_value} does not contain expected services structure")
                return None
                    
        except Exception as e:
            logging.error(f"Error getting cache status: {str(e)}")
            return None

    def get_multi_cache_status(self, hash_values: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """Get the cache status for multiple hashes at once
        
        Args:
            hash_values: List of torrent hashes to look up
            
        Returns:
            Dict[str, Optional[Dict]]: Dictionary mapping each hash to its cache status info.
            Each cache status contains:
                - is_cached: bool  # true or false
                - timestamp: datetime
                - expiry: datetime
                - service: str
            If a hash is not found, expired, or unchecked, its value will be None.
        """
        try:
            # Process hashes in batches, using the correct URL format with +real_debrid
            batch_size = 20  # Reasonable batch size to prevent URL length issues
            results = {hash_value: None for hash_value in hash_values}
            
            for i in range(0, len(hash_values), batch_size):
                batch_hashes = hash_values[i:i+batch_size]
                # Format each hash with the service suffix
                formatted_hashes = [f"{hash_val}+real_debrid" for hash_val in batch_hashes]
                hash_list = ','.join(formatted_hashes)
                result, url, errors = self._try_urls(f"/api/entries/{hash_list}")
                
                if not result:
                    # Check if this was a 404 "Entry not found" response
                    if any(err[1].startswith('HTTP 404: {"error":"Entry not found"}') for err in errors):
                        # This is a valid case - the data just doesn't exist yet
                        continue
                    continue

                # Check if we got a valid response with data
                if not isinstance(result, dict) or 'data' not in result:
                    continue

                # Process each hash result from the data array
                for entry in result.get('data', []):
                    if not isinstance(entry, dict) or 'infohash' not in entry or 'services' not in entry:
                        continue
                        
                    hash_value = entry['infohash']
                    if 'real_debrid' not in entry['services']:
                        continue
                        
                    service_data = entry['services']['real_debrid']
                    
                    # Parse timestamps
                    last_modified = None
                    expiry = None
                    
                    if 'last_modified' in service_data:
                        try:
                            timestamp = service_data['last_modified']
                            if timestamp is not None:
                                last_modified = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        except (ValueError, AttributeError):
                            continue
                    
                    if 'expiry' in service_data:
                        try:
                            timestamp = service_data['expiry']
                            if timestamp is not None:
                                expiry = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                # Check if entry is expired - ensure both datetimes are timezone aware
                                if expiry:
                                    now = datetime.now(expiry.tzinfo)
                                    if expiry < now:
                                        logging.info(f"Cache entry for {hash_value} is expired (expiry: {expiry}), triggering new cache check")
                                        continue
                        except (ValueError, AttributeError):
                            continue

                    results[hash_value] = {
                        'is_cached': service_data.get('cached', False),
                        'timestamp': last_modified,
                        'expiry': expiry,
                        'service': 'real_debrid'
                    }

            return results
                    
        except Exception as e:
            logging.error(f"Error getting multi cache status: {str(e)}")
            # Return dictionary with None for all hashes on error
            return {hash_value: None for hash_value in hash_values}

    def update_cache_status(self, hash_value: str, cached: bool | str, service: str = "real_debrid") -> bool:
        """Update the cache status for a hash
        
        Args:
            hash_value: The torrent hash to update
            cached: The cache state - can be True, False, or 'unchecked'
            service: The service provider name (default: 'real_debrid')
            
        Returns:
            bool: True if update successful, False otherwise
        """
        try:
            # Format the data in the expected structure with data array and nested services
            entry_data = {
                "data": [
                    {
                        "infohash": hash_value,
                        "services": {
                            service: {
                                "cached": cached
                            }
                        }
                    }
                ]
            }
            
            # First try to get existing entry using the correct URL format with +service
            existing, url, errors = self._try_urls(f"/api/entries/{hash_value}+{service}")
            # Don't consider 404 "Entry not found" as an error
            if not existing and any(err[1].startswith('HTTP 404: {"error":"Entry not found"}') for err in errors):
                logging.debug(f"No existing entry found for {hash_value}, will create new one")
            
            # Then update with new data
            logging.debug(f"Sending update data: {entry_data}")
            result, url, errors = self._try_urls("/api/entries", method='POST', data=entry_data)
            
            if not result:
                # Don't consider empty response as error if we got a 200 or 201 status
                if any(err[1].startswith('HTTP 200:') for err in errors) or any(err[1].startswith('HTTP 201:') for err in errors):
                    logging.debug("Got empty 200/201 response, considering update successful")
                    return True
                logging.error(f"No response received when updating cache status for {hash_value}")
                return False
                
            logging.debug(f"Received update response: {result}")
            
            # More flexible response checking - consider it successful if we get a response
            # and it doesn't contain an explicit error
            if isinstance(result, dict):
                # Check for a results array with success status
                if 'results' in result and isinstance(result['results'], list):
                    for item in result['results']:
                        if item.get('key') == f"{hash_value}+{service}" and item.get('success') is True:
                            logging.debug(f"Found success status for {hash_value} in results array")
                            return True
                
                if result.get('error'):
                    logging.error(f"Update failed with error: {result['error']}")
                    return False
                # Consider it successful if we got a dict response without error
                return True
            elif result:
                # If we got any non-empty response, consider it successful
                return True
                
            return False
                    
        except Exception as e:
            logging.error(f"Error updating cache status: {str(e)}")
            return False
            
    def get_mesh_status(self) -> Dict[str, Any]:
        """Get the mesh network status
        
        Returns:
            Dict containing the debug endpoint format:
                - syncsSent: int
                - syncsReceived: int
                - lastSyncAt: str (ISO format)
                - connectionsActive: int
                - databaseEntries: int
                - nodeId: str
                - memory: Dict with memory metrics
        """
        try:
            result, url, errors = self._try_urls("/api/debug")
            if not result:
                return {
                    'syncsSent': 0,
                    'syncsReceived': 0,
                    'lastSyncAt': datetime.now().isoformat(),
                    'connectionsActive': 0,
                    'databaseEntries': 0,
                    'nodeId': 'unavailable',
                    'memory': {
                        'heapTotal': '0 MB',
                        'heapUsed': '0 MB',
                        'rss': '0 MB',
                        'external': '0 MB'
                    }
                }
            
            # Return the specified fields from the debug endpoint response, with defaults for missing fields
            return {
                'syncsSent': result.get('syncsSent', 0),
                'syncsReceived': result.get('syncsReceived', 0),
                'lastSyncAt': result.get('lastSyncAt', datetime.now().isoformat()),
                'connectionsActive': result.get('connectionsActive', 0),
                'databaseEntries': result.get('databaseEntries', 0),
                'nodeId': result.get('nodeId', 'unavailable'),
                'memory': result.get('memory', {
                    'heapTotal': '0 MB',
                    'heapUsed': '0 MB',
                    'rss': '0 MB',
                    'external': '0 MB'
                })
            }
                    
        except Exception as e:
            logging.error(f"Error getting mesh status: {str(e)}")
            return {
                'syncsSent': 0,
                'syncsReceived': 0,
                'lastSyncAt': datetime.now().isoformat(),
                'connectionsActive': 0,
                'databaseEntries': 0,
                'nodeId': 'unavailable',
                'memory': {
                    'heapTotal': '0 MB',
                    'heapUsed': '0 MB',
                    'rss': '0 MB',
                    'external': '0 MB'
                }
            }

    def get_all_entries(self) -> List[Dict[str, Any]]:
        """Get all cache entries
        
        Returns:
            List of entries, each containing:
                - hash: str
                - is_cached: bool  # true or false (unchecked items are filtered out)
                - timestamp: datetime
                - expiry: datetime
                - service: str  # The service name (e.g. 'real_debrid')
            Entries are sorted by timestamp in descending order (newest first)
        """
        try:
            result, url, errors = self._try_urls("/api/entries")
            if not result or not isinstance(result, dict):
                logging.error(f"Invalid response format: {result}")
                return []
            
            # Log the response structure for debugging
            logging.debug(f"API Response: {result}")
            
            entries = []
            data_list = result.get('data', [])
            
            if not isinstance(data_list, list):
                logging.error(f"Expected 'data' to be a list, got: {type(data_list)}")
                return []
            
            for entry in data_list:
                try:
                    # Skip entries without required fields
                    if not isinstance(entry, dict) or 'infohash' not in entry or 'services' not in entry:
                        logging.warning(f"Skipping invalid entry: {entry}")
                        continue
                    
                    # Process each service in the entry
                    for service_name, service_data in entry['services'].items():
                        try:
                            # Parse timestamps
                            last_modified = None
                            expiry = None
                            
                            if 'last_modified' in service_data:
                                try:
                                    timestamp = service_data['last_modified']
                                    if timestamp is not None:
                                        last_modified = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                except (ValueError, AttributeError) as e:
                                    logging.warning(f"Invalid timestamp format in entry {entry['infohash']}: {service_data.get('last_modified')}")
                                    continue
                            
                            if 'expiry' in service_data:
                                try:
                                    timestamp = service_data['expiry']
                                    if timestamp is not None:
                                        expiry = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                        # Check if entry is expired
                                        if expiry:
                                            now = datetime.now(expiry.tzinfo)
                                            if expiry < now:
                                                continue
                                except (ValueError, AttributeError) as e:
                                    logging.warning(f"Invalid expiry format in entry {entry['infohash']}: {service_data.get('expiry')}")
                                    continue
                            
                            entries.append({
                                'hash': entry['infohash'],
                                'is_cached': service_data.get('cached', False),
                                'timestamp': last_modified,
                                'expiry': expiry,
                                'service': service_name
                            })
                        except Exception as e:
                            logging.warning(f"Error processing service {service_name} for hash {entry['infohash']}: {str(e)}")
                            continue
                            
                except Exception as e:
                    logging.warning(f"Error processing entry: {entry}, Error: {str(e)}")
                    continue
            
            # Sort entries by timestamp in descending order (newest first)
            entries.sort(key=lambda x: x.get('timestamp', datetime.min), reverse=True)
            
            return entries
                    
        except Exception as e:
            logging.error(f"Error getting all entries: {str(e)}")
            return []

    def test_connection(self) -> bool:
        """Test the connection to the Gun.js service
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            result, url, errors = self._try_urls("/api/debug")
            return result is not None
        except Exception as e:
            logging.error(f"Error connecting to Gun.js service: {str(e)}")
            return False 