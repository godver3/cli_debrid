import logging
from routes.api_tracker import api
from typing import Dict, Any, Optional
from utilities.settings import get_setting

def get_details(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from metadata.metadata import get_metadata
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID not set. Please configure in settings.")
        return None

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }

    media_type = item['mediaType']
    tmdb_id = item['id']

    # Convert TMDB ID to Trakt ID
    tmdb_to_trakt_url = f"https://api.trakt.tv/search/tmdb/{tmdb_id}?type={'movie' if media_type == 'movie' else 'show'}"
    
    try:
        response = api.get(tmdb_to_trakt_url, headers=headers)
        response.raise_for_status()
        search_data = response.json()
        
        if not search_data:
            logging.error(f"No Trakt {media_type} found for TMDB ID: {tmdb_id}")
            return None
        
        trakt_id = search_data[0][media_type]['ids']['trakt']
    except api.exceptions.RequestException as e:
        logging.error(f"Error converting TMDB ID to Trakt ID: {e}")
        return None

    # Fetch details from Trakt
    details_url = f"https://api.trakt.tv/{'movies' if media_type == 'movie' else 'shows'}/{trakt_id}?extended=full"

    try:
        response = api.get(details_url, headers=headers)
        response.raise_for_status()
        trakt_data = response.json()

        # Fetch additional metadata using the metadata module
        metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)

        # Combine Trakt data with additional metadata and ensure media type is preserved
        combined_data = {
            **trakt_data,
            **metadata,
            'mediaType': media_type  # Explicitly preserve the media type
        }

        return combined_data
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching details from Trakt: {e}")
        return None
