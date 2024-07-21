import logging
import requests
from settings import get_setting
from logging_config import get_logger
from database import get_all_media_items
from utilities.release_date_checker import get_trakt_movie_release_date, get_trakt_episode_release_date

logger = get_logger()

DEFAULT_TAKE = 50
DEFAULT_TOTAL_RESULTS = float('inf')
REQUEST_TIMEOUT = 10  # seconds

def get_overseerr_headers(api_key):
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url, endpoint):
    return f"{base_url}{endpoint}"

def fetch_data(url, headers, cookies=None):
    response = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

def fetch_overseerr_wanted_content(overseerr_url, overseerr_api_key, take=DEFAULT_TAKE):
    headers = get_overseerr_headers(overseerr_api_key)
    wanted_content = []
    skip = 0
    total_results = DEFAULT_TOTAL_RESULTS

    while skip < total_results:
        try:
            response = requests.get(
                get_url(overseerr_url, f"/api/v1/request?filter=approved&take={take}&skip={skip}"),
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            logger.debug(f"Overseerr response data: {data}")

            total_results = data.get('pageInfo', {}).get('totalResults', data.get('totalResults', 0))
            wanted_content.extend(data.get('results', []))
            skip += take

        except requests.RequestException as e:
            logger.error(f"Error fetching wanted content from Overseerr: {e}")
            break
        except KeyError as e:
            logger.error(f"Unexpected response structure from Overseerr: {e}")
            logger.debug(f"Full response: {data}")
            break
        except Exception as e:
            logger.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    return wanted_content

def get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies):
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    return fetch_data(url, headers, cookies)

def get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number, cookies):
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}/season/{season_number}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    return fetch_data(url, headers, cookies)

def get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies):
    url = get_url(overseerr_url, f"/api/v1/movie/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    return fetch_data(url, headers, cookies)

def get_overseerr_cookies(overseerr_url):
    session = requests.Session()
    session.get(overseerr_url)
    return session.cookies

def get_wanted_from_overseerr():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logger.error("Overseerr URL or API key not set. Please configure in settings.")
        return {'movies': [], 'episodes': []}
    try:
        cookies = get_overseerr_cookies(overseerr_url)
        wanted_content_raw = fetch_overseerr_wanted_content(overseerr_url, overseerr_api_key)
        wanted_movies = []
        wanted_episodes = []

        # Get collected movies and episodes from the database
        collected_movies = get_all_media_items(state='Collected', media_type='movie')
        collected_episodes = get_all_media_items(state='Collected', media_type='episode')

        collected_movie_titles_years = {(movie['title'], movie['year']) for movie in collected_movies if movie['title'] and movie['year']}
        collected_episode_titles = {(episode['title'], episode['season_number'], episode['episode_number']) for episode in collected_episodes}

        for item in wanted_content_raw:
            media = item.get('media', {})
            if media.get('mediaType') == 'tv':
                tmdb_id = media.get('tmdbId')
                logger.debug(f"Processing TV media: {media}")
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                imdb_id = show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID')
                show_title = show_details.get('name', 'Unknown Show Title')
                logger.debug(f"Show details: {show_details}")
                for season in range(1, show_details.get('numberOfSeasons', 0) + 1):
                    season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season, cookies)
                    for episode in season_details.get('episodes', []):
                        if (show_title, season, episode.get('episodeNumber')) not in collected_episode_titles:
                            episode_item = {
                                'imdb_id': imdb_id,
                                'tmdb_id': tmdb_id,
                                'title': show_title,
                                'episode_title': episode.get('name', 'Unknown Episode Title'),
                                'year': episode.get('airDate', 'Unknown Year')[:4] if episode.get('airDate') else 'Unknown Year',
                                'season_number': season,
                                'episode_number': episode.get('episodeNumber', 'Unknown Episode Number'),
                                'release_date': get_trakt_episode_release_date(imdb_id, season, episode.get('episodeNumber'))
                            }
                            wanted_episodes.append(episode_item)

            elif media.get('mediaType') == 'movie':
                tmdb_id = media.get('tmdbId')
                logger.debug(f"Processing movie media: {media}")
                movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                title = movie_details.get('title', 'Unknown Title')
                year = movie_details.get('releaseDate', 'Unknown Year')[:4] if movie_details.get('releaseDate') else None
                if title and year and (title, year) not in collected_movie_titles_years:
                    movie_item = {
                        'imdb_id': movie_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                        'tmdb_id': tmdb_id,
                        'title': title,
                        'year': year,
                        'release_date': get_trakt_movie_release_date(movie_details.get('externalIds', {}).get('imdbId', ''))
                    }
                    wanted_movies.append(movie_item)

        logger.info(f"Retrieved {len(wanted_movies)} wanted movies from Overseerr")
        logger.info(f"Retrieved {len(wanted_episodes)} wanted episodes from Overseerr")
        return {'movies': wanted_movies, 'episodes': wanted_episodes}
    except Exception as e:
        logger.error(f"Unexpected error while processing Overseerr response: {e}")
        return {'movies': [], 'episodes': []}
