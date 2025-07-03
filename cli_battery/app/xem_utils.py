import requests
import json
from typing import Optional, List, Dict, Any
from .logger_config import logger

XEM_API_URL = "https://thexem.info/map/all"

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
    if not tvdb_id:
        logger.warning("fetch_xem_mapping called with no TVDB ID.")
        return None

    params = {'id': tvdb_id, 'origin': 'tvdb'}
    url = f"{XEM_API_URL}"

    try:
        logger.info(f"Querying TheXEM for TVDB ID {tvdb_id}...")
        response = requests.get(url, params=params, timeout=15) # Increased timeout slightly
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

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