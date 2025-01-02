import os
import logging
from typing import Optional, Dict, Any, Union
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ..base import ProviderUnavailableError
from .exceptions import RealDebridAPIError, RealDebridAuthError
from settings import get_setting
from api_tracker import api

def get_api_key() -> str:
    """Get Real-Debrid API key from settings"""
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise RealDebridAuthError("No API key found in settings. Please configure in settings.")
    return api_key

def should_retry_error(exception: Exception) -> bool:
    """Determine if we should retry the request based on the error"""
    if isinstance(exception, api.exceptions.HTTPError):
        return exception.response.status_code in [503, 504]  # Service Unavailable, Gateway Timeout
    return isinstance(exception, (api.exceptions.Timeout, api.exceptions.ConnectionError))

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((api.exceptions.RequestException, RealDebridAPIError)),
    retry_error_callback=lambda retry_state: None  # Return None on final failure
)
def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    **kwargs
) -> Any:
    """
    Make a request to the Real-Debrid API
    
    Args:
        method: HTTP method (GET, POST, etc)
        endpoint: API endpoint (e.g. /torrents/info)
        api_key: Real-Debrid API key
        data: Optional data for POST requests
        files: Optional files for upload
        **kwargs: Additional arguments for requests
        
    Returns:
        Response data from the API
        
    Raises:
        RealDebridAPIError: If the API returns an error
        RealDebridAuthError: If authentication fails
        ProviderUnavailableError: If the service is unavailable
    """
    url = f"https://api.real-debrid.com/rest/1.0{endpoint}"
    headers = {'Authorization': f'Bearer {api_key}'}
    kwargs['headers'] = headers
    
    try:
        if method.upper() == 'GET':
            response = api.get(url, **kwargs)
        elif method.upper() == 'POST':
            response = api.post(url, data=data, files=files, **kwargs)
        elif method.upper() == 'DELETE':
            response = api.delete(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
            
        # Handle HTTP errors
        if response.status_code >= 400:
            if response.status_code == 401:
                raise RealDebridAuthError("Invalid API key")
            elif response.status_code == 403:
                raise RealDebridAuthError("Access denied")
            elif response.status_code == 429:
                raise RealDebridAPIError("Rate limit exceeded")
            elif response.status_code == 404:
                # Check if this is a duplicate torrent add attempt
                if method == 'POST' and endpoint == '/torrents/addMagnet':
                    logging.warning("Torrent may already be added - 404 on addMagnet")
                    return None
                response.raise_for_status()
            elif response.status_code in [503, 504]:
                raise RealDebridAPIError(f"Service temporarily unavailable (HTTP {response.status_code})")
            else:
                response.raise_for_status()
        
        # Some endpoints return no content
        if response.status_code == 204:
            return None
            
        # Parse JSON response
        try:
            return response.json()
        except ValueError:
            return response.content
            
    except api.exceptions.Timeout:
        raise ProviderUnavailableError("Request timed out")
        
    except api.exceptions.RequestException as e:
        if should_retry_error(e):
            raise RealDebridAPIError(f"Temporary service error: {str(e)}")
        raise ProviderUnavailableError(f"Request failed: {str(e)}")
