import asyncio
import aiohttp
import logging
from settings import get_setting
import time
from typing import Dict, List, Any
import ast

logging.basicConfig(level=logging.DEBUG)
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

async def get_detailed_movie_metadata(session: aiohttp.ClientSession, plex_url: str, movie_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{movie_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else {}

async def get_show_seasons(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{show_key}/children?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def get_season_episodes(session: aiohttp.ClientSession, plex_url: str, season_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{season_key}/children?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def get_detailed_show_metadata(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    url = f"{plex_url}/library/metadata/{show_key}?includeGuids=1"
    data = await fetch_data(session, url, headers, semaphore)
    return data['MediaContainer']['Metadata'][0] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else {}

async def process_show(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, show: Dict[str, Any]) -> List[Dict[str, Any]]:
    show_key = show['ratingKey']
    
    # Get detailed show metadata
    detailed_show = await get_detailed_show_metadata(session, plex_url, show_key, headers, semaphore)
    
    show_title = detailed_show['title']
    show_genres = []
    if 'Genre' in detailed_show and isinstance(detailed_show['Genre'], list):
        show_genres = [genre['tag'] for genre in detailed_show['Genre'] if 'tag' in genre]
    
    # Filter genres
    filtered_show_genres = filter_genres(show_genres)
    
    # Extract show IMDB ID and TMDB ID
    show_imdb_id = None
    show_tmdb_id = None
    if 'Guid' in detailed_show:
        for guid in detailed_show['Guid']:
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
        
        for episode in episodes:
            episode_entries = await process_episode(episode, show_title, season_number, show_imdb_id, show_tmdb_id, filtered_show_genres)
            all_episodes.extend(episode_entries)
    
    return all_episodes

async def process_episode(episode: Dict[str, Any], show_title: str, season_number: int, show_imdb_id: str, show_tmdb_id: str, show_genres: List[str]) -> List[Dict[str, Any]]:
    base_episode_data = {
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
        'type': 'episode',
        'genres': show_genres
    }

    logger.info(f"Processing episode: {base_episode_data['title']}, genres: {base_episode_data['genres']}")
           
    if 'Guid' in episode:
        for guid in episode['Guid']:
            if guid['id'].startswith('imdb://'):
                base_episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                base_episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]
    
    episode_entries = []
    if 'Media' in episode and episode['Media']:
        for media in episode['Media']:
            if 'Part' in media and media['Part']:
                for part in media['Part']:
                    if 'file' in part:
                        episode_entry = base_episode_data.copy()
                        episode_entry['location'] = part['file']
                        episode_entries.append(episode_entry)
    
    if not episode_entries:
        logger.error(f"No file path found for episode: {show_title} - S{season_number:02d}E{episode.get('index', 'Unknown'):02d} - {episode['title']}")
    
    return episode_entries

async def process_shows_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks = [process_show(session, plex_url, headers, semaphore, show) for show in shows]
    results = await asyncio.gather(*tasks)
    flattened_results = [episode for show_episodes in results for episode in show_episodes]
    return flattened_results

async def process_movies_chunk(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore, movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for movie in movies:
        detailed_movie = await get_detailed_movie_metadata(session, plex_url, movie['ratingKey'], headers, semaphore)
        movie_entries = await process_movie(detailed_movie)
        results.extend(movie_entries)
    return results

async def process_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
    genres = [genre['tag'] for genre in movie.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(genres)
    logging.info(f"Movie: {movie['title']}, All genres: {genres}, Filtered genres: {filtered_genres}")

    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie['addedAt'],
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'release_date': movie.get('originallyAvailableAt'),
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie',
        'genres': filter_genres(genres) 
    }

    logger.debug(f"Movie: {movie_data['title']}, All genres: {genres}")
    
    if 'Guid' in movie:
        for guid in movie['Guid']:
            if guid['id'].startswith('imdb://'):
                movie_data['imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                movie_data['tmdb_id'] = guid['id'].split('://')[1]
    
    movie_entries = []
    if 'Media' in movie and movie['Media']:
        for media in movie['Media']:
            if 'Part' in media and media['Part']:
                for part in media['Part']:
                    if 'file' in part:
                        movie_entry = movie_data.copy()
                        movie_entry['location'] = part['file']
                        movie_entries.append(movie_entry)
    
    if not movie_entries:
        logger.error(f"No file path found for movie: {movie['title']}")
    
    logger.debug(f"Processed {len(movie_entries)} entries for movie: {movie['title']}")
    return movie_entries

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
            
            all_libraries = {library['title']: library['key'] for library in libraries_data['MediaContainer']['Directory']}
            
            movie_library_names = get_setting('Plex', 'movie_libraries', '').split(',')
            show_library_names = get_setting('Plex', 'shows_libraries', '').split(',')
            
            movie_libraries = [all_libraries[name.strip()] for name in movie_library_names if name.strip() in all_libraries]
            show_libraries = [all_libraries[name.strip()] for name in show_library_names if name.strip() in all_libraries]
            
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
                logger.debug(f"Processing chunk of {len(chunk)} shows")
                chunk_episodes = await process_shows_chunk(session, plex_url, headers, semaphore, chunk)
                all_episodes.extend(chunk_episodes)
                logger.info(f"Processed {i+len(chunk)}/{len(all_shows)} shows")
                logger.debug(f"Total episodes: {len(all_episodes)}")

            
            all_movies_processed = []
            for i in range(0, len(all_movies), CHUNK_SIZE):
                chunk = all_movies[i:i+CHUNK_SIZE]
                logger.debug(f"Processing chunk of {len(chunk)} movies")
                chunk_movies = await process_movies_chunk(session, plex_url, headers, semaphore, chunk)
                all_movies_processed.extend(chunk_movies)
                logger.info(f"Processed {i+len(chunk)}/{len(all_movies)} movies")
                logger.debug(f"Total movies: {len(all_movies_processed)}")
            

        end_time = time.time()
        total_time = end_time - start_time
        logger.info(f"Collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Collected: {len(all_episodes)} episodes and {len(all_movies_processed)} movies")
        
        logger.debug(f"Final episodes list length: {len(all_episodes)}")
        logger.debug(f"Final movies list length: {len(all_movies_processed)}")

        
        return {
            'movies': all_movies_processed,
            'episodes': all_episodes
        }
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
            libraries_url = f"{plex_url}/library/sections"
            libraries_data = await fetch_data(session, libraries_url, headers, semaphore)
            
            all_libraries = {library['title']: library['key'] for library in libraries_data['MediaContainer']['Directory']}
            
            movie_library_names = get_setting('Plex', 'movie_libraries', '').split(',')
            show_library_names = get_setting('Plex', 'shows_libraries', '').split(',')
            
            movie_libraries = [all_libraries[name.strip()] for name in movie_library_names if name.strip() in all_libraries]
            show_libraries = [all_libraries[name.strip()] for name in show_library_names if name.strip() in all_libraries]
            
            logger.info(f"TV Show libraries to process: {show_libraries}")
            logger.info(f"Movie libraries to process: {movie_libraries}")

            processed_movies = []
            processed_episodes = []
            
            for library_key in movie_libraries + show_libraries:
                recent_url = f"{plex_url}/library/sections/{library_key}/recentlyAdded"
                recent_data = await fetch_data(session, recent_url, headers, semaphore)
                
                if 'MediaContainer' in recent_data and 'Metadata' in recent_data['MediaContainer']:
                    recent_items = recent_data['MediaContainer']['Metadata']
                    logger.info(f"Retrieved {len(recent_items)} recent items from library {library_key}")
                    
                    for item in recent_items:
                        if item['type'] == 'movie':
                            metadata_url = f"{plex_url}{item['key']}?includeGuids=1"
                            metadata = await fetch_data(session, metadata_url, headers, semaphore)
                            
                            if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer']:
                                full_metadata = metadata['MediaContainer']['Metadata'][0]
                                processed_items = await process_recent_movie(full_metadata)
                                processed_movies.extend(processed_items)
                        elif item['type'] == 'season':
                            show_metadata_url = f"{plex_url}/library/metadata/{item['parentRatingKey']}?includeGuids=1"
                            show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                            
                            if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                season_episodes = await process_recent_season(item, show_full_metadata, session, plex_url, headers, semaphore)
                                processed_episodes.extend([episode for sublist in season_episodes for episode in sublist])
                        elif item['type'] == 'episode':
                            show_metadata_url = f"{plex_url}/library/metadata/{item['grandparentRatingKey']}?includeGuids=1"
                            show_metadata = await fetch_data(session, show_metadata_url, headers, semaphore)
                            
                            if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                show_full_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                show_imdb_id, show_tmdb_id = extract_show_ids(show_full_metadata)
                                episode_data = await process_recent_episode(item, show_full_metadata['title'], item['parentIndex'], show_imdb_id, show_tmdb_id, show_full_metadata)
                                processed_episodes.extend(episode_data)
                            else:
                                logger.error(f"Failed to fetch show metadata for episode: {item.get('title', 'Unknown')}")
                        else:
                            logger.warning(f"Skipping item: {item.get('title', 'Unknown')} (Type: {item.get('type', 'Unknown')})")

        end_time = time.time()
        total_time = end_time - start_time

        logger.info(f"Collection complete. Total time: {total_time:.2f} seconds")
        logger.info(f"Collected: {len(processed_episodes)} episodes and {len(processed_movies)} movies")
        
        logger.debug(f"Final episodes list length: {len(processed_episodes)}")
        logger.debug(f"Final movies list length: {len(processed_movies)}")

        for episode in processed_episodes:
            genres = episode['genres']
            logger.debug(f"Processed episode: {genres}")
        for movie in processed_movies:
            genres = movie['genres']
            logger.debug(f"Processed movie: {genres}")

        return {
            'movies': processed_movies,
            'episodes': processed_episodes
        }
    except Exception as e:
        logger.error(f"Error collecting content from Plex: {str(e)}", exc_info=True)
        return None

def is_anime(item):
    if 'Genre' in item:
        return any(genre.get('tag') == 'Anime' for genre in item['Genre'])
    return False

def filter_genres(genres):
    # If genres is a string representation of a list, convert it to a list
    if isinstance(genres, str):
        try:
            genres = ast.literal_eval(genres)
        except (ValueError, SyntaxError):
            # If it's not a valid list representation, treat it as a single genre
            genres = [genres]
    
    # Ensure genres is a list
    if not isinstance(genres, list):
        genres = [genres]
    
    logger.debug(f"Filtering genres: {genres}")
    filtered = ['anime'] if any(str(genre).strip().lower() == 'anime' for genre in genres) else []
    logger.debug(f"Filtered genres: {filtered}")
    return filtered

async def process_recent_movie(movie: Dict[str, Any]) -> List[Dict[str, Any]]:
    genres = [genre['tag'] for genre in movie.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(genres)
    logging.info(f"Movie: {movie['title']}, All genres: {genres}, Filtered genres: {filtered_genres}")

    movie_data = {
        'title': movie['title'],
        'year': movie.get('year'),
        'addedAt': movie['addedAt'],
        'guid': movie.get('guid'),
        'ratingKey': movie['ratingKey'],
        'release_date': movie.get('originallyAvailableAt'),
        'imdb_id': None,
        'tmdb_id': None,
        'type': 'movie',
        'genres': filter_genres(genres)
    }
    

    if 'Guid' in movie:
        for guid in movie['Guid']:
            if guid['id'].startswith('imdb://'):
                movie_data['imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                movie_data['tmdb_id'] = guid['id'].split('://')[1]

    movie_entries = []
    if 'Media' in movie:
        for media in movie['Media']:
            if 'Part' in media:
                for part in media['Part']:
                    file_path = part.get('file')
                    if file_path:
                        movie_entry = movie_data.copy()
                        movie_entry['location'] = file_path
                        movie_entries.append(movie_entry)
                        logger.info(f"Movie location: {file_path}")
    
    if not movie_entries:
        logger.error(f"No filename found for movie: {movie['title']}")
    
    return movie_entries

async def process_recent_season(season: Dict[str, Any], show: Dict[str, Any], session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], semaphore: asyncio.Semaphore) -> List[Dict[str, Any]]:
    show_imdb_id, show_tmdb_id = extract_show_ids(show)
    show_genres = filter_genres([genre['tag'] for genre in show.get('Genre', []) if 'tag' in genre])

    season_episodes_url = f"{plex_url}/library/metadata/{season['ratingKey']}/children?includeGuids=1"
    season_episodes_data = await fetch_data(session, season_episodes_url, headers, semaphore)

    processed_episodes = []
    if 'MediaContainer' in season_episodes_data and 'Metadata' in season_episodes_data['MediaContainer']:
        episodes = season_episodes_data['MediaContainer']['Metadata']
        for episode in episodes:
            processed_episode = await process_recent_episode(episode, show['title'], season['index'], show_imdb_id, show_tmdb_id, show)
            processed_episodes.append(processed_episode)

    return processed_episodes

async def process_recent_episode(episode: Dict[str, Any], show_title: str, season_number: int, show_imdb_id: str, show_tmdb_id: str, show: Dict[str, Any]) -> List[Dict[str, Any]]:
    show_genres = [genre['tag'] for genre in show.get('Genre', []) if 'tag' in genre]
    filtered_genres = filter_genres(show_genres)
    logging.info(f"Episode: {show_title} - {episode['title']}, genres: {filtered_genres}")

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
        'type': 'episode',
        'genres': filter_genres(show_genres)
    }
    

    if 'Guid' in episode:
        for guid in episode['Guid']:
            if guid['id'].startswith('imdb://'):
                episode_data['episode_imdb_id'] = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                episode_data['episode_tmdb_id'] = guid['id'].split('://')[1]

    episode_entries = []
    if 'Media' in episode:
        for media in episode['Media']:
            if 'Part' in media:
                for part in media['Part']:
                    file_path = part.get('file')
                    if file_path:
                        episode_entry = episode_data.copy()
                        episode_entry['location'] = file_path
                        episode_entries.append(episode_entry)
                        logger.info(f"Episode location: {file_path}")
    
    if not episode_entries:
        logger.error(f"No filename found for episode: {show_title} - S{season_number:02d}E{episode.get('index', 'Unknown'):02d} - {episode['title']}")
    
    return episode_entries

def extract_show_ids(show_metadata):
    show_imdb_id = None
    show_tmdb_id = None
    if 'Guid' in show_metadata:
        for guid in show_metadata['Guid']:
            if guid['id'].startswith('imdb://'):
                show_imdb_id = guid['id'].split('://')[1]
            elif guid['id'].startswith('tmdb://'):
                show_tmdb_id = guid['id'].split('://')[1]
    return show_imdb_id, show_tmdb_id

async def run_get_recent_from_plex():
    logger.info("Starting run_get_recent_from_plex")
    result = await get_recent_from_plex()
    logger.info("Completed run_get_recent_from_plex")
    return result

def sync_run_get_recent_from_plex():
    return asyncio.run(run_get_recent_from_plex())