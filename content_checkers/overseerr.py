import logging
import requests
from settings import get_setting
from logging_config import get_logger
from database import get_all_media_items, get_media_item_status
from datetime import datetime

logger = get_logger()

DEFAULT_TAKE = 100
DEFAULT_TOTAL_RESULTS = float('inf')
REQUEST_TIMEOUT = 3  # seconds
REQUEST_LIMIT = 5  # Limit of items to handle
PAGINATION_TAKE = 20  # Number of items to fetch per pagination request
MAX_RETRIES = 1
RETRY_DELAY = 2  # seconds
JITTER = 2  # seconds

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
    page = 1

    while True:
        try:
            logger.debug(f"Fetching page {page} (skip={skip}, take={take})")
            response = requests.get(
                get_url(overseerr_url, f"/api/v1/request?take={take}&skip={skip}"),
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get('results', [])
            total_results = data.get('pageInfo', {}).get('total', data.get('total', 0))
            
            logger.debug(f"Page {page}: Received {len(results)} results. API reports total of {total_results}")
            
            if not results:
                logger.debug("No more results returned. Stopping pagination.")
                break

            wanted_content.extend(results)
            skip += take
            page += 1

            if len(results) < take:
                logger.debug("Received fewer results than requested. This is likely the last page.")
                break

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

    logger.info(f"Fetched a total of {len(wanted_content)} wanted content items from Overseerr")
    logger.debug(f"Pagination details: Pages fetched: {page - 1}, Last skip value: {skip - take}")
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

        for item in wanted_content_raw:
            media = item.get('media', {})
            if media.get('mediaType') == 'tv':
                tmdb_id = media.get('tmdbId')
                logger.debug(f"Processing TV media: {media}")
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                imdb_id = show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID')
                show_title = show_details.get('name', 'Unknown Show Title')
                for season in range(1, show_details.get('numberOfSeasons', 0) + 1):
                    season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season, cookies)
                    for episode in season_details.get('episodes', []):
                        # Check if this episode is already collected
                        status = get_media_item_status(imdb_id=imdb_id, season_number=season, episode_number=episode.get('episodeNumber'))
                        if status == "Missing":
                            logger.info(f"Episode missing: {show_title} S{season}E{episode.get('episodeNumber')}, fetching air date.")
                            air_date = episode.get('airDate', 'Unknown')
                            logger.info(f"Fetched air date for episode: {air_date}")
                            episode_item = {
                                'imdb_id': imdb_id,
                                'tmdb_id': tmdb_id,
                                'title': show_title,
                                'episode_title': episode.get('name', 'Unknown Episode Title'),
                                'year': episode.get('airDate', 'Unknown Year')[:4] if episode.get('airDate') else 'Unknown Year',
                                'season_number': season,
                                'episode_number': episode.get('episodeNumber', 'Unknown Episode Number'),
                                'release_date': air_date
                            }
                            wanted_episodes.append(episode_item)
                        else:
                            logger.debug(f"Episode already collected: {show_title} S{season}E{episode.get('episodeNumber')}")

            elif media.get('mediaType') == 'movie':
                tmdb_id = media.get('tmdbId')
                logger.debug(f"Processing movie media: {media}")
                movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                title = movie_details.get('title', 'Unknown Title')
                year = movie_details.get('releaseDate', 'Unknown Year')[:4] if movie_details.get('releaseDate') else 'Unknown Year'
                # Check if this movie is already collected
                status = get_media_item_status(imdb_id=movie_details.get('externalIds', {}).get('imdbId'), title=title, year=year)
                if status == "Missing":
                    logger.info(f"Movie missing: {title} ({year}), fetching release date.")
                    releases = movie_details.get('releases', {}).get('results', [])
                    release_date = 'Unknown'
                    for release in releases:
                        for date in release.get('release_dates', []):
                            if date.get('type') in [4, 5]:  # Type 4 and 5 are of interest
                                release_date = date.get('release_date')
                                break
                        if release_date != 'Unknown':
                            break
                    logger.info(f"Fetched release date for movie: {release_date}")
                    movie_item = {
                        'imdb_id': movie_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                        'tmdb_id': tmdb_id,
                        'title': title,
                        'year': year,
                        'release_date': release_date
                    }
                    wanted_movies.append(movie_item)
                else:
                    logger.debug(f"Movie already collected: {title} ({year})")

        logger.info(f"Retrieved {len(wanted_movies)} wanted movies from Overseerr")
        logger.info(f"Retrieved {len(wanted_episodes)} wanted episodes from Overseerr")
        return {'movies': wanted_movies, 'episodes': wanted_episodes}
    except Exception as e:
        logger.error(f"Unexpected error while processing Overseerr response: {e}")
        return {'movies': [], 'episodes': []}
