import logging
from settings import get_setting
import asyncio
from aiohttp import ClientConnectorError, ServerTimeoutError, ClientResponseError

async def get_poster_url(session, tmdb_id, media_type):
    from poster_cache import get_cached_poster_url, cache_poster_url, cache_unavailable_poster, UNAVAILABLE_POSTER

    # Log incoming parameters
    logging.info(f"get_poster_url called with tmdb_id: {tmdb_id}, original media_type: {media_type}")

    # Normalize media type early
    normalized_type = 'tv' if media_type.lower() in ['tv', 'show', 'series'] else 'movie'
    logging.info(f"Normalized media_type from '{media_type}' to '{normalized_type}'")

    # First check the cache using normalized type
    cached_url = get_cached_poster_url(tmdb_id, normalized_type)
    if cached_url:
        logging.info(f"Cache hit for {tmdb_id}_{normalized_type}: {cached_url}")
        return cached_url

    if not tmdb_id:
        logging.warning("No TMDB ID provided")
        cache_unavailable_poster(tmdb_id, normalized_type)
        return UNAVAILABLE_POSTER

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    
    if not tmdb_api_key:
        logging.warning("TMDB API key is missing")
        cache_unavailable_poster(tmdb_id, normalized_type)
        return UNAVAILABLE_POSTER
    
    url = f"https://api.themoviedb.org/3/{normalized_type}/{tmdb_id}/images?api_key={tmdb_api_key}"
    logging.info(f"Fetching poster from TMDB API for {tmdb_id} as type '{normalized_type}'")
    
    try:
        async with session.get(url, timeout=10) as response:
            logging.info(f"TMDB API response status: {response.status} for {tmdb_id}_{normalized_type}")
            if response.status == 200:
                data = await response.json()
                posters = data.get('posters', [])
                
                if posters:
                    # First try English posters
                    english_posters = [p for p in posters if p.get('iso_639_1') == 'en']
                    poster = english_posters[0] if english_posters else posters[0]
                    poster_url = f"https://image.tmdb.org/t/p/w300{poster['file_path']}"
                    logging.info(f"Found poster for {tmdb_id}_{normalized_type}: {poster_url}")
                    cache_poster_url(tmdb_id, normalized_type, poster_url)
                    return poster_url
                
                logging.warning(f"No posters found for {normalized_type} with TMDB ID {tmdb_id}")
                cache_unavailable_poster(tmdb_id, normalized_type)
                return UNAVAILABLE_POSTER
            
            logging.error(f"TMDB API returned status {response.status} for {normalized_type} with TMDB ID {tmdb_id}")
            
    except Exception as e:
        logging.error(f"Error fetching poster URL for {normalized_type} with TMDB ID {tmdb_id}: {e}")
    
    cache_unavailable_poster(tmdb_id, normalized_type)
    return UNAVAILABLE_POSTER