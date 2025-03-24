import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import requests
import json

class PhalanxDBClassManager:
    """Manager class for handling cache status checks using Phalanx-DB service"""
    
    def __init__(self, gun_service_url: str = "http://localhost:8888"):
        """Initialize the Gun.js cache manager
        
        Args:
            gun_service_url: URL of the Gun.js service. If not specified, will try both localhost and phalanx_db.
        """
        self._primary_url = None
        self._backup_url = None
        self._last_sync_time = None
        self.auth_token = "phalanx_db_v1_32byte_key_20240312_01"
        self.headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json"
        }
        
        # Create a persistent session for connection pooling
        self._session = requests.Session()
        
        # Initialize URLs - prioritizing localhost
        self._urls = [
            "http://localhost:8888",
            "http://phalanx_db:8888"
        ]
        if gun_service_url not in self._urls:
            self._urls.insert(0, gun_service_url.rstrip('/'))
            
        # Try to set primary URL to localhost immediately if that's the preferred URL
        if "localhost" in self._urls[0]:
            self._primary_url = self._urls[0]

    def _try_urls(self, endpoint: str, method: str = 'GET', data: Optional[Dict] = None, 
                 params: Optional[Dict] = None) -> Tuple[Optional[Any], Optional[str], List[Tuple[str, str]]]:
        """Try each URL in sequence until one works. Prioritizes cached primary URL.
        
        Args:
            endpoint: API endpoint to call
            method: HTTP method to use
            data: Optional data to send with request
            params: Optional query parameters to include
            
        Returns:
            Tuple of (response data, working URL, errors) where errors is a list of (url, error_message) tuples
        """
        errors = []
        expected_responses = []  # Track expected responses like 404 "Data not found"
        
        # Use longer timeouts for /debug and /data endpoints since they return larger datasets
        for url in self._urls:
            # Determine timeout based on endpoint and connection type
            if endpoint in ['/debug', '/data']:
                timeout = 15.0 if 'localhost' in url else 15.0  # Longer timeouts for status endpoints
            else:
                timeout = 5.0 if 'localhost' in url else 5.0  # Default timeouts for other endpoints
                
            try:
                full_url = f"{url}{endpoint}"
                logging.debug(f"Trying URL: {full_url}")
                
                if method == 'GET':
                    response = self._session.get(full_url, headers=self.headers, params=params, timeout=timeout)
                    response_text = response.text
                    logging.debug(f"URL {url} response status: {response.status_code}, content: {response_text[:200]}")
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            if result:  # Found a working URL
                                self._primary_url = url  # Cache this working URL
                                logging.debug(f"Found working URL: {url}")
                                return result, url, errors
                        except json.JSONDecodeError as je:
                            logging.debug(f"Failed to parse JSON from URL {url}: {str(je)}")
                            errors.append((url, f"JSON parse error: {str(je)}"))
                    elif response.status_code == 404 and response_text == '{"error":"Data not found"}':
                        expected_responses.append((url, f"HTTP {response.status_code}: {response_text}"))
                        # Still set this as primary URL since the service is responding correctly
                        self._primary_url = url
                        return None, None, expected_responses
                    else:
                        errors.append((url, f"HTTP {response.status_code}: {response_text[:200]}"))
                elif method == 'POST':
                    response = self._session.post(full_url, headers=self.headers, json=data, timeout=timeout)
                    response_text = response.text
                    logging.debug(f"URL {url} response status: {response.status_code}, content: {response_text[:200]}")
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            if result:  # Found a working URL
                                self._primary_url = url  # Cache this working URL
                                logging.debug(f"Found working URL: {url}")
                                return result, url, errors
                        except json.JSONDecodeError as je:
                            logging.debug(f"Failed to parse JSON from URL {url}: {str(je)}")
                            errors.append((url, f"JSON parse error: {str(je)}"))
                    else:
                        errors.append((url, f"HTTP {response.status_code}: {response_text[:200]}"))
            except Exception as e:
                logging.debug(f"URL {url} failed with exception: {str(e)}")
                errors.append((url, str(e)))
                continue
                
        # Only log error if all URLs failed with unexpected errors
        if errors and not expected_responses:
            error_details = '; '.join([f"{url}: {err}" for url, err in errors])
            logging.error(f"All PhalanxDB URLs failed - {error_details}")
        elif expected_responses:
            # If we got expected responses (like 404 Data not found), include them in the return
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
            # Use the service parameter to make the request more targeted and efficient
            result, url, errors = self._try_urls(f"/data/{hash_value}", params={"service": "real_debrid"})
            if not result:
                # Check if this was a 404 "Data not found" response
                if any(err[1].startswith('HTTP 404: {"error":"Data not found"}') for err in errors):
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
            # Join hashes with commas for the API call - limit batch size to prevent URL length issues
            batch_size = 20  # Reasonable batch size to prevent URL length issues
            results = {hash_value: None for hash_value in hash_values}
            
            for i in range(0, len(hash_values), batch_size):
                batch_hashes = hash_values[i:i+batch_size]
                hash_list = ','.join(batch_hashes)
                result, url, errors = self._try_urls(f"/data/{hash_list}", params={"service": "real_debrid"})
                
                if not result:
                    # Check if this was a 404 "Data not found" response
                    if any(err[1].startswith('HTTP 404: {"error":"Data not found"}') for err in errors):
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
            # Format the data as expected by the API
            entry_data = {
                'infohash': hash_value,
                'service': service,
                'cached': cached
            }
            
            # First try to get existing entry
            existing, url, errors = self._try_urls(f"/data/{hash_value}")
            # Don't consider 404 "Data not found" as an error
            if not existing and any(err[1].startswith('HTTP 404: {"error":"Data not found"}') for err in errors):
                logging.debug(f"No existing entry found for {hash_value}, will create new one")
            
            # Then update with new data
            logging.debug(f"Sending update data: {entry_data}")
            result, url, errors = self._try_urls("/data", method='POST', data=entry_data)
            
            if not result:
                # Don't consider empty response as error if we got a 200 status
                if any(err[1].startswith('HTTP 200:') for err in errors):
                    logging.debug("Got empty 200 response, considering update successful")
                    return True
                logging.error(f"No response received when updating cache status for {hash_value}")
                return False
                
            logging.debug(f"Received update response: {result}")
            
            # More flexible response checking - consider it successful if we get a response
            # and it doesn't contain an explicit error
            if isinstance(result, dict):
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
            Dict containing the new debug endpoint format:
                - timestamp: str (ISO format)
                - memoryUsage: Dict with memory metrics
                - totalEntriesSeen: int
                - totalEntryCount: int
                - serviceCounts: List[Tuple[str, int]]
                - uptime: str
                - startupTime: str
                - connectedRelays: List[str]
        """
        try:
            result, url, errors = self._try_urls("/debug")
            if not result:
                return {
                    'timestamp': datetime.now().isoformat(),
                    'memoryUsage': {
                        'rss': '0 MB',
                        'heapTotal': '0 MB',
                        'heapUsed': '0 MB',
                        'external': '0 MB',
                        'arrayBuffers': '0 MB'
                    },
                    'totalEntriesSeen': 0,
                    'totalEntryCount': 0,
                    'serviceCounts': [],
                    'uptime': '0 seconds',
                    'startupTime': datetime.now().isoformat(),
                    'connectedRelays': []
                }
            
            # Return the raw debug endpoint response, with defaults for missing fields
            return {
                'timestamp': result.get('timestamp', datetime.now().isoformat()),
                'memoryUsage': result.get('memoryUsage', {
                    'rss': '0 MB',
                    'heapTotal': '0 MB',
                    'heapUsed': '0 MB',
                    'external': '0 MB',
                    'arrayBuffers': '0 MB'
                }),
                'totalEntriesSeen': result.get('totalEntriesSeen', 0),
                'totalEntryCount': result.get('totalEntryCount', 0),
                'serviceCounts': result.get('serviceCounts', []),
                'uptime': result.get('uptime', '0 seconds'),
                'startupTime': result.get('startupTime', datetime.now().isoformat()),
                'connectedRelays': result.get('connectedRelays', [])
            }
                    
        except Exception as e:
            logging.error(f"Error getting mesh status: {str(e)}")
            return {
                'timestamp': datetime.now().isoformat(),
                'memoryUsage': {
                    'rss': '0 MB',
                    'heapTotal': '0 MB',
                    'heapUsed': '0 MB',
                    'external': '0 MB',
                    'arrayBuffers': '0 MB'
                },
                'totalEntriesSeen': 0,
                'totalEntryCount': 0,
                'serviceCounts': [],
                'uptime': '0 seconds', 
                'startupTime': datetime.now().isoformat(),
                'connectedRelays': []
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
            result, url, errors = self._try_urls("/data")
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
            result, url, errors = self._try_urls("/debug")
            return result is not None
        except Exception as e:
            logging.error(f"Error connecting to Gun.js service: {str(e)}")
            return False 