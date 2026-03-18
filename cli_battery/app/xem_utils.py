import requests
import json
import time
from typing import Optional, List, Dict, Any
from .logger_config import logger

XEM_API_URL = "https://thexem.info/map/all"

# Simple circuit breaker: if XEM returns 403 (IP/rate blocked), pause all
# requests for _XEM_COOLDOWN_SECONDS to avoid hammering the server.
_xem_blocked_until: float = 0.0
_XEM_COOLDOWN_SECONDS = 300  # 5-minute cooldown after a 403

def fetch_xem_mapping(tvdb_id: int) -> Optional[List[Dict[str, Any]]]:
    """
    Fetches the episode numbering mapping for a given TVDB ID from TheXEM.

    Args:
        tvdb_id: The TVDB ID of the show.

    Returns:
        A list of mapping dictionaries if successful, otherwise None.
        Each dictionary in the list typically contains keys like 'scene', 'tvdb', etc.,
        each mapping to another dictionary with 'season', 'episode', 'absolute'.
    """
    global _xem_blocked_until
    if not tvdb_id:
        logger.warning("fetch_xem_mapping called with no TVDB ID.")
        return None

    # Circuit breaker: skip if XEM recently returned 403
    if time.time() < _xem_blocked_until:
        remaining = int(_xem_blocked_until - time.time())
        logger.debug(f"XEM circuit breaker active, skipping request for TVDB ID {tvdb_id} ({remaining}s remaining).")
        return None

    params = {'id': tvdb_id, 'origin': 'tvdb'}
    url = f"{XEM_API_URL}"
    headers = {
        'User-Agent': 'cli_debrid/1.0 (https://github.com/godver3/cli_debrid)'
    }

    try:
        logger.info(f"Querying TheXEM for TVDB ID {tvdb_id}...")
        response = requests.get(url, params=params, headers=headers, timeout=15)
        if response.status_code == 403:
            _xem_blocked_until = time.time() + _XEM_COOLDOWN_SECONDS
            logger.warning(f"TheXEM returned 403 for TVDB ID {tvdb_id}. Pausing XEM requests for {_XEM_COOLDOWN_SECONDS}s.")
            return None
        response.raise_for_status()  # Raise an exception for other bad status codes

        data = response.json()

        if data.get("result") == "success":
            logger.info(f"Successfully retrieved XEM mapping for TVDB ID {tvdb_id}.")
            return data.get("data") # This should be the list of mappings
        else:
            message = data.get("message", "Unknown reason")
            # Don't log an error if the show simply isn't found, just info.
            if "no show with the" in message:
                 logger.info(f"No mapping found on TheXEM for TVDB ID {tvdb_id}: {message}")
            else:
                logger.error(f"Failed to retrieve XEM mapping for TVDB ID {tvdb_id}. Result: {data.get('result')}, Message: {message}")
            return None

    except requests.exceptions.Timeout:
        logger.error(f"Timeout while requesting XEM mapping for TVDB ID {tvdb_id}.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error requesting XEM mapping for TVDB ID {tvdb_id}: {e}")
        return None
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON response from TheXEM for TVDB ID {tvdb_id}.")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred in fetch_xem_mapping for TVDB ID {tvdb_id}: {e}", exc_info=True)
        return None