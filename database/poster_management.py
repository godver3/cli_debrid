import logging
from settings import get_setting
import asyncio
from aiohttp import ClientConnectorError, ServerTimeoutError, ClientResponseError

async def get_poster_url(session, tmdb_id, media_type):
    from poster_cache import cache_unavailable_poster

    if not tmdb_id:
        cache_unavailable_poster(tmdb_id, media_type)
        return None

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    
    if not tmdb_api_key:
        logging.warning("TMDB API key is missing")
        cache_unavailable_poster(tmdb_id, media_type)
        return None
    
    # Use the correct endpoints for TV shows and movies
    if media_type == 'tv':
        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/images?api_key={tmdb_api_key}"
    else:
        url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/images?api_key={tmdb_api_key}"
    
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                posters = data.get('posters', [])
                
                # First, try to find an English poster
                english_posters = [p for p in posters if p.get('iso_639_1') == 'en']
                
                if english_posters:
                    poster_path = english_posters[0]['file_path']
                elif posters:
                    # If no English poster, use the first available poster
                    poster_path = posters[0]['file_path']
                else:
                    poster_path = None
                
                if poster_path:
                    return f"https://image.tmdb.org/t/p/w300{poster_path}"
                else:
                    logging.warning(f"No poster path found for {media_type} with TMDB ID {tmdb_id}")
            else:
                logging.error(f"TMDB API returned status {response.status} for {media_type} with TMDB ID {tmdb_id}")
            
    except (ClientConnectorError, ServerTimeoutError, ClientResponseError, asyncio.TimeoutError) as e:
        logging.error(f"Error fetching poster URL for {media_type} with TMDB ID {tmdb_id}: {e}")
    except Exception as e:
        logging.error(f"Unexpected error fetching poster URL for {media_type} with TMDB ID {tmdb_id}: {e}")
    
    cache_unavailable_poster(tmdb_id, media_type)
    return None