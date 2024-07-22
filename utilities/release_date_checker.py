import os
import pickle
import logging
import requests
from datetime import datetime, timedelta
from settings import get_setting
from logging_config import get_logger

logger = get_logger()

CACHE_FILE = 'db_content/trakt_cache.pkl'
CACHE_EXPIRY_HOURS = 24
TRAKT_API_URL = 'https://api.trakt.tv'
TRAKT_CLIENT_ID = get_setting('Trakt', 'client_id')

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache, f)

def is_cache_valid(entry):
    return datetime.now() - entry['timestamp'] < timedelta(hours=CACHE_EXPIRY_HOURS)

def get_cached_data(key):
    cache = load_cache()
    if key in cache and is_cache_valid(cache[key]):
        return cache[key]['data']
    return None

def update_cache(key, data):
    cache = load_cache()
    cache[key] = {'data': data, 'timestamp': datetime.now()}
    save_cache(cache)

def get_trakt_movie_release_date(imdb_id):
    key = f"movie_release_{imdb_id}"
    cached_data = get_cached_data(key)
    if cached_data:
        logger.debug(f"Returning cached release date for IMDb ID {imdb_id}: {cached_data}")
        return cached_data

    if not TRAKT_CLIENT_ID:
        logger.error("Trakt Client ID is not set. Please configure in settings.")
        return "Unknown"

    url = f"{TRAKT_API_URL}/movies/{imdb_id}/releases/us"
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID
    }

    logger.debug(f"Sending GET request to Trakt API: {url}")

    try:
        response = requests.get(url, headers=headers)
        logger.debug(f"Trakt API response status code: {response.status_code}")
        #logger.debug(f"Trakt API response content: {response.text[:500]}...")  # Log first 500 characters of response

        response.raise_for_status()
        releases = response.json()

        logger.debug(f"Parsed releases: {releases}")

        digital_release = None
        physical_release = None

        for release in releases:
            release_type = release.get('release_type')
            release_date = release.get('release_date')
            logger.debug(f"Processing release: type={release_type}, date={release_date}")

            if release_type == 'digital':
                digital_release = release_date
            elif release_type == 'physical':
                physical_release = release_date

            if digital_release and physical_release:
                break

        release_date = digital_release or physical_release or 'Unknown'
        logger.debug(f"Selected release date: {release_date}")

        if release_date != 'Unknown':
            if isinstance(release_date, str):
                try:
                    release_date = datetime.strptime(release_date, "%Y-%m-%d").date()
                except ValueError:
                    logger.error(f"Invalid date format for release date: {release_date}")
                    release_date = 'Unknown'
            elif isinstance(release_date, (datetime, date)):
                release_date = release_date.date() if isinstance(release_date, datetime) else release_date
            else:
                logger.error(f"Unexpected type for release date: {type(release_date)}")
                release_date = 'Unknown'

        logger.debug(f"Final release date: {release_date}")
        update_cache(key, release_date)
        return release_date

    except requests.RequestException as e:
        logger.error(f"Error fetching release date from Trakt for IMDb ID {imdb_id}: {e}")
        return "Unknown"

def get_trakt_episode_release_date(imdb_id, season, episode):
    key = f"episode_release_{imdb_id}_S{season}_E{episode}"
    cached_data = get_cached_data(key)
    if cached_data:
        return cached_data

    if not TRAKT_CLIENT_ID:
        logger.error("Trakt Client ID is not set. Please configure in settings.")
        return "Unknown"

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-key': TRAKT_CLIENT_ID,
        'trakt-api-version': '2'
    }

    try:
        url = f"{TRAKT_API_URL}/shows/{imdb_id}/seasons/{season}/episodes/{episode}?extended=full"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        first_aired = data.get('first_aired', 'Unknown')
        if first_aired != "Unknown" and first_aired is not None:
            try:
                first_aired = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S.%fZ").date()
            except ValueError:
                logger.error(f"Invalid date format for first_aired: {first_aired}")
                first_aired = "Unknown"
        else:
            first_aired = "Unknown"

        update_cache(key, first_aired)
        return first_aired

    except requests.RequestException as e:
        logger.error(f"Error fetching release date from Trakt: {e}")
        return "Unknown"

def get_trakt_movie_information(imdb_id):
    key = f"movie_info_{imdb_id}"
    cached_data = get_cached_data(key)
    if cached_data:
        logger.debug(f"Returning cached movie information for IMDb ID {imdb_id}: {cached_data}")
        return cached_data

    if not TRAKT_CLIENT_ID:
        logger.error("Trakt Client ID is not set. Please configure in settings.")
        return None

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID
    }

    try:
        url = f"{TRAKT_API_URL}/movies/{imdb_id}?extended=full"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        movie_info = response.json()
        #logger.debug(f"Trakt API movie response for IMDb ID {imdb_id}: {movie_info}")
        update_cache(key, movie_info)
        return movie_info
    except requests.RequestException as e:
        logger.error(f"Error fetching movie information from Trakt for IMDb ID {imdb_id}: {e}")
        return None

def get_trakt_show_information(imdb_id):
    key = f"show_info_{imdb_id}"
    cached_data = get_cached_data(key)
    if cached_data:
        logger.debug(f"Returning cached show information for IMDb ID {imdb_id}: {cached_data}")
        return cached_data

    if not TRAKT_CLIENT_ID:
        logger.error("Trakt Client ID is not set. Please configure in settings.")
        return None

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID
    }

    try:
        url = f"{TRAKT_API_URL}/shows/{imdb_id}/seasons?extended=full"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        seasons = response.json()
        #logger.debug(f"Trakt API seasons response for IMDb ID {imdb_id}: {seasons}")

        if not seasons:
            logger.warning(f"No seasons found for IMDb ID {imdb_id}")
            return None

        show_info = {
            'seasons': []
        }

        for season in seasons:
            season_number = season.get('number')
            season_info = {
                'number': season_number,
                'episodes': []
            }

            episode_url = f"{TRAKT_API_URL}/shows/{imdb_id}/seasons/{season_number}?extended=full"
            episode_response = requests.get(episode_url, headers=headers)
            episode_response.raise_for_status()
            episodes = episode_response.json()
            logger.debug(f"Trakt API episodes response for IMDb ID {imdb_id}, Season {season_number}: {episodes}")

            for episode in episodes:
                episode_info = {
                    'number': episode['number'],
                    'title': episode['title'],
                    'first_aired': episode.get('first_aired', "Unknown")
                }
                season_info['episodes'].append(episode_info)

            show_info['seasons'].append(season_info)

        update_cache(key, show_info)
        return show_info

    except requests.RequestException as e:
        logger.error(f"Error fetching show information from Trakt for IMDb ID {imdb_id}: {e}")
        return None

    except ValueError as e:
        logger.error(f"Error parsing date for IMDb ID {imdb_id}: {e}")
        return None
