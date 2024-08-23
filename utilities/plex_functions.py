import asyncio
import aiohttp
import logging
from settings import get_setting
import time
from typing import Dict, List, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 400
CHUNK_SIZE = 10  # Adjust this value to find the optimal balance

async def fetch_data(session: aiohttp.ClientSession, url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        async with session.get(url, headers=headers) as response:
            return await response.json()

async def get_library_contents(session: aiohttp.ClientSession, plex_url: str, library_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/sections/{library_key}/all?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    metadata = data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []
    logger.info(f"Retrieved {len(metadata)} items from library {library_key}")
    return metadata

async def get_show_seasons(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{show_key}/children?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def get_season_episodes(session: aiohttp.ClientSession, plex_url: str, season_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{season_key}/children?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def process_episode(episode: Dict[str, Any], show_title: str, season_number: int, show_imdb_id: str, show_tmdb_id: str) -> Dict[str, Any]:
    episode_data = {
        'title': show_title,
        'episode_title': episode['title'],
        'season_number': season_number,
        'episode_number': episode.get('index'),
        'year': episode.get('year'),
        'addedAt': episode['addedAt'],
        'guid': episode.get('guid'),
        'ratingKey': episode['ratingKey'],
        'release_date': episode.get('originallyAvailableAt'),
        'imdb_id': show_imdb_id,
        'tmdb_id': show_tmdb_id,
        'episode_imdb_id': None,
        'episode_tmdb_id': None,
        'type': 'episode'
    }
    
    if 'Guid' in episode:
        for guid in episode['Guid']:
            if guid['id'].startswith('imdb://'):
                episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]
    
    return episode_data

async def process_show(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, show: Dict[str, Any]) -> List[Dict[str, Any]]:
    show_title = show['title']
    show_key = show['ratingKey']
    
    # Extract show IMDB ID
    show_imdb_id = None
    show_tmdb_id = None
    if 'Guid' in show:
        for guid in show['Guid']:
            if guid['id'].startswith('imdb://'):
                show_imdb_id = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                show_tmdb_id = guid['id'].split('://')[1]
    
    seasons = await get_show_seasons(session, plex_url, show_key, headers, semaphore)
    
    all_episodes = []
    for season in seasons:
        season_number = season.get('index')
        season_key = season['ratingKey']
        
        episodes = await get_season_episodes(session, plex_url, season_key, headers, semaphore)
        
        season_episodes = [await process_episode(episode, show_title, season_number, show_imdb_id, show_tmdb_id) for episode in episodes]
        all_episodes.extend(season_episodes)
    
    return all_episodes

async def process_shows_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks = [process_show(session, plex_url, headers, semaphore, show) for show in shows]
    results = await asyncio.gather(*tasks)
    return [episode for show_episodes in results for episode in show_episodes]

async def process_movie(movie: Dict[str, Any]) -> Dict[str, Any]:
    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie['addedAt'],
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'release_date': movie.get('originallyAvailableAt'),
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie'
    }
    
    if 'Guid' in movie:
        for guid in movie['Guid']:
            if guid['id'].startswith('imdb://'):
                movie_data['imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                movie_data['tmdb_id'] = guid['id'].split('://')[1]
    
    return movie_data

async def process_movies_chunk(movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [await process_movie(movie) for movie in movies]

async def get_collected_from_plex(request='all'):
    try:
        start_time = time.time()
        logger.info(f"Starting Plex content collection. Request type: {request}")

        plex_url = get_setting('Plex', 'url').rstrip('/')
        plex_token = get_setting('Plex', 'token')
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async with aiohttp.ClientSession() as session:
            libraries_url = f"{plex_url}/library/sections"
            libraries_data = await fetch_data(session, libraries_url, headers, semaphore)
            
            show_libraries = [library['key'] for library in libraries_data['MediaContainer']['Directory'] if library['type'] == 'show']
            movie_libraries = [library['key'] for library in libraries_data['MediaContainer']['Directory'] if library['type'] == 'movie']
            
            logger.info(f"TV Show libraries to process: {show_libraries}")
            logger.info(f"Movie libraries to process: {movie_libraries}")

            all_shows = []
            for library_key in show_libraries:
                shows = await get_library_contents(session, plex_url, library_key, headers, semaphore)
                all_shows.extend(shows)

            all_movies = []
            for library_key in movie_libraries:
                movies = await get_library_contents(session, plex_url, library_key, headers, semaphore)
                all_movies.extend(movies)

            logger.info(f"Total shows found: {len(all_shows)}")
            logger.info(f"Total movies found: {len(all_movies)}")

            all_episodes = []
            for i in range(0, len(all_shows), CHUNK_SIZE):
                chunk = all_shows[i:i+CHUNK_SIZE]
                chunk_episodes = await process_shows_chunk(session, plex_url, headers, semaphore, chunk)
                all_episodes.extend(chunk_episodes)
                logger.info(f"Processed {i+len(chunk)}/{len(all_shows)} shows")

            all_movies_processed = []
            for i in range(0, len(all_movies), CHUNK_SIZE):
                chunk = all_movies[i:i+CHUNK_SIZE]
                chunk_movies = await process_movies_chunk(chunk)
                all_movies_processed.extend(chunk_movies)
                logger.info(f"Processed {i+len(chunk)}/{len(all_movies)} movies")

        end_time = time.time()
        total_time = end_time - start_time
        logger.info(f"Collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Collected: {len(all_episodes)} episodes and {len(all_movies_processed)} movies")
        
        # Log a sample of processed data for verification
        if all_episodes:
            logger.debug(f"Sample episode: {all_episodes[0]}")
        if all_movies_processed:
            logger.debug(f"Sample movie: {all_movies_processed[0]}")

        return {'movies': all_movies_processed, 'episodes': all_episodes}
    except Exception as e:
        logger.error(f"Error collecting content from Plex: {str(e)}", exc_info=True)
        return None

async def run_get_collected_from_plex(request='all'):
    logger.info("Starting run_get_collected_from_plex")
    result = await get_collected_from_plex(request)
    logger.info("Completed run_get_collected_from_plex")
    return result

def sync_run_get_collected_from_plex(request='all'):
    return asyncio.run(run_get_collected_from_plex(request))

async def get_recent_from_plex():
    try:
        start_time = time.time()
        logger.info("Starting Plex recent content collection")

        plex_url = get_setting('Plex', 'url').rstrip('/')
        plex_token = get_setting('Plex', 'token')
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async with aiohttp.ClientSession() as session:
            recent_url = f"{plex_url}/library/recentlyAdded"
            recent_data = await fetch_data(session, recent_url, headers, semaphore)
            
            processed_movies = []
            processed_episodes = []
            
            if 'MediaContainer' in recent_data and 'Metadata' in recent_data['MediaContainer']:
                recent_items = recent_data['MediaContainer']['Metadata']
                logger.info(f"Retrieved {len(recent_items)} recent items")
                
                for item in recent_items:
                    if item['type'] == 'movie':
                        metadata_url = f"{plex_url}{item['key']}?includeGuids=1"
                        metadata = await fetch_data(session, metadata_url, headers, semaphore)
                        
                        if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer']:
                            full_metadata = metadata['MediaContainer']['Metadata'][0]
                            processed_item = await process_recent_movie(full_metadata)
                            processed_movies.append(processed_item)
                    elif item['type'] == 'season':
                        show_metadata_url = f"{plex_url}/library/metadata/{item['parentRatingKey']}?includeGuids=1"
                        show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                        
                        if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                            show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                            season_episodes = await process_recent_season(item, show_full_metadata, session, plex_url, headers, semaphore)
                            processed_episodes.extend(season_episodes)
                    else:
                        logger.info(f"Skipping item: {item['title']} (Type: {item['type']})")

        end_time = time.time()
        total_time = end_time - start_time
        logger.info(f"Recent items collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Processed {len(processed_movies)} recent movies and {len(processed_episodes)} recent episodes")

        return {'movies': processed_movies, 'episodes': processed_episodes}
    except Exception as e:
        logger.error(f"Error collecting recent content from Plex: {str(e)}", exc_info=True)
        return None

async def process_recent_movie(movie: Dict[str, Any]) -> Dict[str, Any]:
    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie['addedAt'],
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'release_date': movie.get('originallyAvailableAt'),
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie'
    }
    
    if 'Guid' in movie:
        for guid in movie['Guid']:
            if guid['id'].startswith('imdb://'):
                movie_data['imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                movie_data['tmdb_id'] = guid['id'].split('://')[1]
    
    return movie_data

async def process_recent_season(season: Dict[str, Any], show: Dict[str, Any], session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:

    show_imdb_id = None
    show_tmdb_id = None
    if 'Guid' in show:
        for guid in show['Guid']:
            if guid['id'].startswith('imdb://'):
                show_imdb_id = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                show_tmdb_id = guid['id'].split('://')[1]

    season_episodes_url = f"{plex_url}/library/metadata/{season['ratingKey']}/children?includeGuids=1"
    season_episodes_data = await fetch_data(session, season_episodes_url, headers, semaphore)

    processed_episodes = []
    if 'MediaContainer' in season_episodes_data and 'Metadata' in season_episodes_data['MediaContainer']:
        episodes = season_episodes_data['MediaContainer']['Metadata']
        for episode in episodes:
            processed_episode = await process_recent_episode(episode, show['title'], season['index'], show_imdb_id, show_tmdb_id)
            processed_episodes.append(processed_episode)

    return processed_episodes

async def process_recent_episode(episode: Dict[str, Any], show_title: str, season_number: int, show_imdb_id: str, show_tmdb_id: str) -> Dict[str, Any]:

    episode_data = {
        'title': show_title,
        'episode_title': episode['title'],
        'season_number': season_number,
        'episode_number': episode.get('index'),
        'year': episode.get('year'),
        'addedAt': episode['addedAt'],
        'guid': episode.get('guid'),
        'ratingKey': episode['ratingKey'],
        'release_date': episode.get('originallyAvailableAt'),
        'imdb_id': show_imdb_id,
        'tmdb_id': show_tmdb_id,
        'episode_imdb_id': None,
        'episode_tmdb_id': None,
        'type': 'episode'
    }
    
    if 'Guid' in episode:
        for guid in episode['Guid']:
            if guid['id'].startswith('imdb://'):
                episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]
    else:
        logger.error(f"No 'Guid' key found for episode: '{show_title}' S{season_number:02d}E{episode.get('index', 'Unknown'):02d} - '{episode['title']}'")
    
    return episode_data

async def run_get_recent_from_plex():
    logger.info("Starting run_get_recent_from_plex")
    result = await get_recent_from_plex()
    logger.info("Completed run_get_recent_from_plex")
    return result

def sync_run_get_recent_from_plex():
    return asyncio.run(run_get_recent_from_plex())