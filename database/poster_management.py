import logging
from settings import get_setting
import asyncio
from aiohttp import ClientConnectorError, ServerTimeoutError, ClientResponseError

async def get_poster_url(session, tmdb_id, media_type):
    from content_checkers.overseerr import get_overseerr_headers
    from poster_cache import cache_unavailable_poster

    if not tmdb_id:
        cache_unavailable_poster(tmdb_id, media_type)
        return None

    overseerr_url = get_setting('Overseerr', 'url', '').rstrip('/')
    overseerr_api_key = get_setting('Overseerr', 'api_key', '')
    
    if not overseerr_url or not overseerr_api_key:
        logging.warning("Overseerr URL or API key is missing")
        cache_unavailable_poster(tmdb_id, media_type)
        return None
    
    headers = get_overseerr_headers(overseerr_api_key)
    
    url = f"{overseerr_url}/api/v1/{media_type}/{tmdb_id}"
    
    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                poster_path = data.get('posterPath')
                if poster_path:
                    return f"https://image.tmdb.org/t/p/w300{poster_path}"
                else:
                    logging.warning(f"No poster path found for {media_type} with TMDB ID {tmdb_id}")
            else:
                logging.error(f"Overseerr API returned status {response.status} for {media_type} with TMDB ID {tmdb_id}")
            
    except (ClientConnectorError, ServerTimeoutError, ClientResponseError, asyncio.TimeoutError) as e:
        logging.error(f"Error fetching poster URL for {media_type} with TMDB ID {tmdb_id}: {e}")
    except Exception as e:
        logging.error(f"Unexpected error fetching poster URL for {media_type} with TMDB ID {tmdb_id}: {e}")
    
    cache_unavailable_poster(tmdb_id, media_type)
    return None