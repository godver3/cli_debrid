from settings import get_setting
import logging
import requests
from database import add_or_update_wanted_movies_batch, add_or_update_wanted_episodes_batch, verify_database, load_collected_cache, save_collected_cache, update_collected_cache, is_in_collected_cache
from database import (add_or_update_wanted_movies_batch, add_or_update_wanted_episodes_batch, 
                      verify_database, load_collected_cache, save_collected_cache, 
                      update_collected_cache, is_in_collected_cache, 
                      remove_from_wanted_movies, remove_from_wanted_episodes)
from logging_config import get_logger

logger = get_logger()

def get_mdblists():
    mdblist_api_key = get_setting('MDBList', 'api_key')
    mdblist_urls = get_setting('MDBList', 'urls')

    if not mdblist_api_key or not mdblist_urls:
        logger.error("MDBList API key or URLs not set. Please configure in settings.")
        return []

    # Split the URLs into a list and append '/json' to each URL
    url_list = [url.strip() + '/json' for url in mdblist_urls.split(',')]

    headers = {
        'Authorization': f'Bearer {mdblist_api_key}',
        'Accept': 'application/json'
    }

    all_mdblist_content = []

    for url in url_list:
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            all_mdblist_content.extend(data)
        except requests.RequestException as e:
            logger.error(f"Error fetching content from MDBList: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while processing MDBList response: {e}")

    unavailable_movies = [
        {
            'imdb_id': item.get('imdb_id', 'Unknown IMDb ID'),
            'tmdb_id': item.get('id'),
            'title': item.get('title', 'Unknown Title'),
            'year': str(item.get('release_year', 'Unknown Year')),
        }
        for item in all_mdblist_content if item.get('mediatype') == 'movie'
    ]

    #for item in all_mdblist_content:
        #print(item)

    add_or_update_wanted_movies_batch(all_mdblist_content)


def check_overseer_requests():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    #logger.info(f"Checking Overseerr requests at {overseerr_url}")
    # Implement actual request fetching logic here
    return []  # Mock empty list of requests

DEFAULT_TAKE = 50
DEFAULT_TOTAL_RESULTS = float('inf')

def get_overseerr_headers(api_key):
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url, endpoint):
    return f"{base_url}{endpoint}"

def fetch_data(url, headers):
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def check_overseer_requests():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    #logger.info(f"Checking Overseerr requests at {overseerr_url}")
    # Implement actual request fetching logic here
    return []  # Mock empty list of requests

def get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id):
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    return fetch_data(url, headers)

def get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number):
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}/season/{season_number}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    return fetch_data(url, headers)

def get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id):
    url = get_url(overseerr_url, f"/api/v1/movie/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    return fetch_data(url, headers)

def bk_fetch_overseerr_unavailable_content(overseerr_url, overseerr_api_key, take=DEFAULT_TAKE):
    headers = get_overseerr_headers(overseerr_api_key)
    unavailable_content = []
    skip = 0
    total_results = DEFAULT_TOTAL_RESULTS

    while skip < total_results:
        try:
            response = requests.get(
                get_url(overseerr_url, f"/api/v1/request?filter=unavailable&take={take}&skip={skip}"),
                headers=headers
            )
            response.raise_for_status()
            data = response.json()

            #logger.info(f"Overseerr response data: {data}")

            total_results = data.get('pageInfo', {}).get('totalResults', data.get('totalResults', 0))
            unavailable_content.extend(data.get('results', []))
            skip += take

        except requests.RequestException as e:
            #logger.error(f"Error fetching unavailable content from Overseerr: {e}")
            break
        except KeyError as e:
            #logger.error(f"Unexpected response structure from Overseerr: {e}")
            #logger.info(f"Full response: {data}")
            break
        except Exception as e:
            #logger.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    return unavailable_content

def bk_get_unavailable_content():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        #logger.error("Overseerr URL or API key not set. Please configure in settings.")
        return []
    try:
        unavailable_content_raw = fetch_overseerr_unavailable_content(overseerr_url, overseerr_api_key)
        unavailable_movies = []
        unavailable_episodes = []
        collected_cache = load_collected_cache()

        for item in unavailable_content_raw:
            media = item.get('media', {})
            if media.get('mediaType') == 'tv':
                tmdb_id = media.get('tmdbId')
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id)
                imdb_id = show_details['externalIds'].get('imdbId', 'Unknown IMDb ID')
                show_title = show_details.get('name', 'Unknown Show Title')
                for season in range(1, show_details['numberOfSeasons'] + 1):
                    season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season)
                    for episode in season_details.get('episodes', []):
                        episode_item = {
                            'show_imdb_id': imdb_id,
                            'show_tmdb_id': tmdb_id,
                            'show_title': show_title,
                            'episode_title': episode.get('name', 'Unknown Episode Title'),
                            'year': episode.get('airDate', 'Unknown Year')[:4] if episode.get('airDate') else 'Unknown Year',
                            'season_number': season,
                            'episode_number': episode.get('episodeNumber', 'Unknown Episode Number'),
                        }
                        if (show_title, season, episode.get('episodeNumber')) not in collected_cache['episodes']:
                            unavailable_episodes.append(episode_item)
                        else:
                            remove_from_wanted_episodes(show_title, season, episode.get('episodeNumber'))
                            #logger.info(f"Removed collected episode from wanted database: {show_title} S{season}E{episode.get('episodeNumber')}")

            elif media.get('mediaType') == 'movie':
                tmdb_id = media.get('tmdbId')
                movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id)
                movie_item = {
                    'imdb_id': movie_details['externalIds'].get('imdbId', 'Unknown IMDb ID'),
                    'tmdb_id': tmdb_id,
                    'title': movie_details.get('title', 'Unknown Title'),
                    'year': movie_details.get('releaseDate', 'Unknown Year')[:4] if movie_details.get('releaseDate') else 'Unknown Year',
                }
                if (movie_item['title'], movie_item['year']) not in collected_cache['movies']:
                    unavailable_movies.append(movie_item)
                else:
                    remove_from_wanted_movies(movie_item['title'], movie_item['year'])
                    #logger.info(f"Removed collected movie from wanted database: {movie_item['title']} ({movie_item['year']})")

        logger.debug(f"Retrieved {len(unavailable_movies)} unavailable movies from Overseerr")
        logger.debug(f"Retrieved {len(unavailable_episodes)} unavailable episodes from Overseerr")
        add_or_update_wanted_movies_batch(unavailable_movies)
        add_or_update_wanted_episodes_batch(unavailable_episodes)
        return unavailable_movies, unavailable_episodes
    except Exception as e:
        #logger.error(f"Unexpected error while processing Overseerr response: {e}")
        return []

def fetch_overseerr_unavailable_content(overseerr_url, overseerr_api_key, take=DEFAULT_TAKE):
    headers = get_overseerr_headers(overseerr_api_key)
    unavailable_content = []
    skip = 0
    total_results = DEFAULT_TOTAL_RESULTS

    while skip < total_results:
        try:
            response = requests.get(
                get_url(overseerr_url, f"/api/v1/request?filter=unavailable&take={take}&skip={skip}"),
                headers=headers
            )
            response.raise_for_status()
            data = response.json()

            logger.debug(f"Overseerr response data: {data}")

            total_results = data.get('pageInfo', {}).get('totalResults', data.get('totalResults', 0))
            unavailable_content.extend(data.get('results', []))
            skip += take

        except requests.RequestException as e:
            logger.debug(f"Error fetching unavailable content from Overseerr: {e}")
            break
        except KeyError as e:
            logger.debug(f"Unexpected response structure from Overseerr: {e}")
            logger.debug(f"Full response: {data}")
            break
        except Exception as e:
            logger.debug(f"Unexpected error while processing Overseerr response: {e}")
            break

    return unavailable_content

def get_unavailable_content():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logger.info("Overseerr URL or API key not set. Please configure in settings.")
        return []
    try:
        unavailable_content_raw = fetch_overseerr_unavailable_content(overseerr_url, overseerr_api_key)
        unavailable_movies = []
        unavailable_episodes = []
        collected_cache = load_collected_cache()

        for item in unavailable_content_raw:
            media = item.get('media', {})
            if media.get('mediaType') == 'tv':
                tmdb_id = media.get('tmdbId')
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id)
                imdb_id = show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID')
                show_title = show_details.get('name', 'Unknown Show Title')
                for season in range(1, show_details.get('numberOfSeasons', 0) + 1):
                    season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season)
                    for episode in season_details.get('episodes', []):
                        episode_item = {
                            'show_imdb_id': imdb_id,
                            'show_tmdb_id': tmdb_id,
                            'show_title': show_title,
                            'episode_title': episode.get('name', 'Unknown Episode Title'),
                            'year': episode.get('airDate', 'Unknown Year')[:4] if episode.get('airDate') else 'Unknown Year',
                            'season_number': season,
                            'episode_number': episode.get('episodeNumber', 'Unknown Episode Number'),
                        }
                        if (show_title, season, episode.get('episodeNumber')) not in collected_cache['episodes']:
                            unavailable_episodes.append(episode_item)
                        else:
                            remove_from_wanted_episodes(show_title, season, episode.get('episodeNumber'))
                            logger.debug(f"Removed collected episode from wanted database: {show_title} S{season}E{episode.get('episodeNumber')}")

            elif media.get('mediaType') == 'movie':
                tmdb_id = media.get('tmdbId')
                movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id)
                movie_item = {
                    'imdb_id': movie_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                    'tmdb_id': tmdb_id,
                    'title': movie_details.get('title', 'Unknown Title'),
                    'year': movie_details.get('releaseDate', 'Unknown Year')[:4] if movie_details.get('releaseDate') else 'Unknown Year',
                }
                if (movie_item['title'], movie_item['year']) not in collected_cache['movies']:
                    unavailable_movies.append(movie_item)
                else:
                    remove_from_wanted_movies(movie_item['title'], movie_item['year'])
                    logger.debug(f"Removed collected movie from wanted database: {movie_item['title']} ({movie_item['year']})")

        logger.info(f"Retrieved {len(unavailable_movies)} unavailable movies from Overseerr")
        logger.info(f"Retrieved {len(unavailable_episodes)} unavailable episodes from Overseerr")
        add_or_update_wanted_movies_batch(unavailable_movies)
        add_or_update_wanted_episodes_batch(unavailable_episodes)
        return unavailable_movies, unavailable_episodes
    except Exception as e:
        logger.debug(f"Unexpected error while processing Overseerr response: {e}")
        return []

if __name__ == "__main__":
    mdblist_content = get_mdblists()
    for item in mdblist_content:
        print(item)
