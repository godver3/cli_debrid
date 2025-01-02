"""TorBox API client implementation"""

import os
import logging
from typing import Optional, Dict, Any, Union
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ..base import ProviderUnavailableError
from .exceptions import TorBoxAPIError, TorBoxAuthError, TorBoxPlanError, TorBoxLimitError
from settings import get_setting
from api_tracker import api

def get_api_key() -> str:
    """Get TorBox API key from settings"""
    api_key = get_setting('Debrid Provider', 'api_key')
    if not api_key:
        raise TorBoxAuthError("No API key found in settings. Please configure in settings.")
    return api_key

def should_retry_error(exception: Exception) -> bool:
    """Determine if we should retry the request based on the error"""
    if isinstance(exception, api.exceptions.HTTPError):
        return exception.response.status_code in [500, 503, 504]  # Internal Server Error, Service Unavailable, Gateway Timeout
    return isinstance(exception, (api.exceptions.Timeout, api.exceptions.ConnectionError))

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((api.exceptions.RequestException, TorBoxAPIError)),
    retry_error_callback=lambda retry_state: None  # Return None on final failure
)
def make_request(
    method: str,
    endpoint: str,
    api_key: str,
    data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    params: Optional[Dict] = None,
    **kwargs
) -> Any:
    """
    Make a request to the TorBox API
    
    Args:
        method: HTTP method (GET, POST, etc)
        endpoint: API endpoint (e.g. /torrents/mylist)
        api_key: TorBox API key
        data: Optional data for POST requests
        files: Optional files for upload
        params: Optional query parameters
        **kwargs: Additional arguments for requests
        
    Returns:
        Response data from the API
        
    Raises:
        TorBoxAPIError: If the API returns an error
        TorBoxAuthError: If authentication fails
        TorBoxPlanError: If plan restrictions prevent the action
        TorBoxLimitError: If rate/download limits are exceeded
        ProviderUnavailableError: If the service is unavailable
    """
    url = f"https://api.torbox.app/v1{endpoint}"
    headers = {'Authorization': f'Bearer {api_key}'}
    kwargs['headers'] = headers

    # Add query parameters if provided
    if params:
        kwargs['params'] = params
    
    try:
        logging.debug(f"Making {method} request to {url}")
        logging.debug(f"Headers: {headers}")
        if params:
            logging.debug(f"Query params: {params}")
        if data:
            logging.debug(f"Data: {data}")
        if files:
            logging.debug(f"Files: {files}")
            
        if method.upper() == 'GET':
            response = api.get(url, **kwargs)
        elif method.upper() == 'POST':
            response = api.post(url, data=data, files=files, **kwargs)
        elif method.upper() == 'DELETE':
            response = api.delete(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
            
        # Log the raw response
        try:
            logging.debug(f"Response status code: {response.status_code}")
            logging.debug(f"Response headers: {dict(response.headers)}")
            logging.debug(f"Response content: {response.content.decode('utf-8')}")
        except Exception as e:
            logging.debug(f"Failed to log response details: {str(e)}")
            
        # Handle HTTP errors
        if response.status_code >= 400:
            error_data = response.json()
            error_code = error_data.get('error')
            detail = error_data.get('detail', 'Unknown error')
            
            if response.status_code == 403 or error_code in ['NO_AUTH', 'BAD_TOKEN', 'AUTH_ERROR']:
                raise TorBoxAuthError(detail)
            elif error_code in ['PLAN_RESTRICTED_FEATURE']:
                raise TorBoxPlanError(detail)
            elif error_code in ['MONTHLY_LIMIT', 'COOLDOWN_LIMIT', 'ACTIVE_LIMIT']:
                raise TorBoxLimitError(detail)
            elif response.status_code in [500, 503, 504]:
                raise TorBoxAPIError(f"Service temporarily unavailable: {detail}")
            else:
                return error_data  # Return the error response for handling
                
        try:
            return response.json()
        except ValueError:
            return response.content
            
    except api.exceptions.RequestException as e:
        if isinstance(e, api.exceptions.HTTPError):
            try:
                error_data = e.response.json()
                logging.error(f"API error response: {error_data}")
                if error_data.get('error') == 'DIFF_ISSUE':
                    return error_data
            except Exception:
                pass
        logging.error(f"Request failed: {str(e)}")
        raise TorBoxAPIError(str(e))
