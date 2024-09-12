import asyncio
import aiohttp
import logging
from settings import get_setting
from typing import Dict, List, Any

async def get_simplified_plex_data():
    try:
        plex_url = get_setting('Plex', 'url').rstrip('/')
        plex_token = get_setting('Plex', 'token')
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }

        async with aiohttp.ClientSession() as session:
            libraries_url = f"{plex_url}/library/sections"
            libraries_data = await fetch_data(session, libraries_url, headers)
            
            all_libraries = {library['title']: library['key'] for library in libraries_data['MediaContainer']['Directory']}
            
            movie_library_names = get_setting('Plex', 'movie_libraries', '').split(',')
            show_library_names = get_setting('Plex', 'shows_libraries', '').split(',')
            
            movie_libraries = [all_libraries[name.strip()] for name in movie_library_names if name.strip() in all_libraries]
            show_libraries = [all_libraries[name.strip()] for name in show_library_names if name.strip() in all_libraries]
            
            all_movies = []
            all_episodes = []

            print(f"Processing {len(movie_libraries)} movie libraries and {len(show_libraries)} TV show libraries")

            for i, library_key in enumerate(movie_libraries, 1):
                print(f"Processing movie library {i}/{len(movie_libraries)}")
                movies = await get_library_contents(session, plex_url, library_key, headers)
                print(f"Found {len(movies)} movies in library {i}")
                processed_movies = await process_movies(session, plex_url, headers, movies)
                all_movies.extend(processed_movies)
                print(f"Processed {len(processed_movies)} movies from library {i}")

            for i, library_key in enumerate(show_libraries, 1):
                print(f"Processing TV show library {i}/{len(show_libraries)}")
                shows = await get_library_contents(session, plex_url, library_key, headers)
                print(f"Found {len(shows)} TV shows in library {i}")
                episodes = await process_shows(session, plex_url, headers, shows)
                all_episodes.extend(episodes)
                print(f"Processed {len(episodes)} episodes from library {i}")

        print(f"Total processed: {len(all_movies)} movies and {len(all_episodes)} episodes")
        return {
            'movies': all_movies,
            'episodes': all_episodes
        }
    except Exception as e:
        logger.error(f"Error collecting content from Plex: {str(e)}", exc_info=True)
        return None

async def fetch_data(session: aiohttp.ClientSession, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    async with session.get(url, headers=headers) as response:
        return await response.json()

async def get_library_contents(session: aiohttp.ClientSession, plex_url: str, library_key: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/sections/{library_key}/all?includeGuids=1"
    data = await fetch_data(session, url, headers)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def process_movies(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed_movies = []
    for i, movie in enumerate(movies, 1):
        if i % 100 == 0:
            print(f"Processing movie {i}/{len(movies)}")
        imdb_id = next((guid['id'].split('://')[1] for guid in movie.get('Guid', []) if guid['id'].startswith('imdb://')), None)
        if imdb_id:
            for media in movie.get('Media', []):
                for part in media.get('Part', []):
                    processed_movies.append({
                        'imdb_id': imdb_id,
                        'media_type': 'movie',
                        'location': part.get('file')
                    })
    return processed_movies

async def process_shows(session: aiohttp.ClientSession, plex_url: str, headers: Dict[str, str], shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed_episodes = []
    for i, show in enumerate(shows, 1):
        if i % 10 == 0:
            print(f"Processing TV show {i}/{len(shows)}")
        show_imdb_id = next((guid['id'].split('://')[1] for guid in show.get('Guid', []) if guid['id'].startswith('imdb://')), None)
        if show_imdb_id:
            seasons = await get_show_seasons(session, plex_url, show['ratingKey'], headers)
            for season in seasons:
                episodes = await get_season_episodes(session, plex_url, season['ratingKey'], headers)
                for episode in episodes:
                    for media in episode.get('Media', []):
                        for part in media.get('Part', []):
                            processed_episodes.append({
                                'imdb_id': show_imdb_id,
                                'media_type': 'episode',
                                'location': part.get('file')
                            })
    return processed_episodes


async def get_show_seasons(session: aiohttp.ClientSession, plex_url: str, show_key: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{show_key}/children"
    data = await fetch_data(session, url, headers)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

async def get_season_episodes(session: aiohttp.ClientSession, plex_url: str, season_key: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    url = f"{plex_url}/library/metadata/{season_key}/children"
    data = await fetch_data(session, url, headers)
    return data['MediaContainer']['Metadata'] if 'MediaContainer' in data and 'Metadata' in data['MediaContainer'] else []

def sync_get_simplified_plex_data():
    return asyncio.run(get_simplified_plex_data())

# Test function
def test_simplified_plex_data():
    data = sync_get_simplified_plex_data()
    if data:
        print(f"Total movies: {len(data['movies'])}")
        print(f"Total episodes: {len(data['episodes'])}")
        print("\nSample movie data:")
        print(data['movies'][0] if data['movies'] else "No movies found")
        print("\nSample episode data:")
        print(data['episodes'][0] if data['episodes'] else "No episodes found")
    else:
        print("Failed to retrieve data from Plex")

if __name__ == "__main__":
    test_simplified_plex_data()