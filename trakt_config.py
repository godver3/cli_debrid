import os
import pickle
import logging
import requests
from datetime import datetime, timedelta, date
from settings import get_setting

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CACHE_FILE = 'release_dates_cache.pkl'
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

def get_cached_release_date(imdb_id, season=None, episode=None):
    cache = load_cache()
    key = (imdb_id, season, episode)
    if key in cache:
        entry = cache[key]
        if datetime.now() - entry['timestamp'] < timedelta(hours=CACHE_EXPIRY_HOURS):
            return entry['release_date']
    return None

def update_cache_release_date(imdb_id, release_date, season=None, episode=None):
    cache = load_cache()
    key = (imdb_id, season, episode)
    cache[key] = {'release_date': release_date, 'timestamp': datetime.now()}
    save_cache(cache)

import os
import pickle
import logging
import requests
from datetime import datetime, date

def get_trakt_movie_release_date(imdb_id):
    cached_date = get_cached_release_date(imdb_id)
    if cached_date:
        logging.debug(f"Returning cached release date for IMDb ID {imdb_id}: {cached_date}")
        return cached_date

    if not TRAKT_CLIENT_ID:
        logging.error("Trakt Client ID is not set. Please configure in settings.")
        return "Unknown"

    url = f"{TRAKT_API_URL}/movies/{imdb_id}/releases/us"
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID
    }

    logging.debug(f"Sending GET request to Trakt API: {url}")

    try:
        response = requests.get(url, headers=headers)
        logging.debug(f"Trakt API response status code: {response.status_code}")
        logging.debug(f"Trakt API response content: {response.text[:500]}...")  # Log first 500 characters of response

        response.raise_for_status()
        releases = response.json()

        logging.debug(f"Parsed releases: {releases}")

        digital_release = None
        physical_release = None

        for release in releases:
            release_type = release.get('release_type')
            release_date = release.get('release_date')
            logging.debug(f"Processing release: type={release_type}, date={release_date}")

            if release_type == 'digital':
                digital_release = release_date
            elif release_type == 'physical':
                physical_release = release_date

            if digital_release and physical_release:
                break

        release_date = digital_release or physical_release or 'Unknown'
        logging.debug(f"Selected release date: {release_date}")

        if release_date != 'Unknown':
            if isinstance(release_date, str):
                try:
                    release_date = datetime.strptime(release_date, "%Y-%m-%d").date()
                except ValueError:
                    logging.error(f"Invalid date format for release date: {release_date}")
                    release_date = 'Unknown'
            elif isinstance(release_date, (datetime, date)):
                release_date = release_date.date() if isinstance(release_date, datetime) else release_date
            else:
                logging.error(f"Unexpected type for release date: {type(release_date)}")
                release_date = 'Unknown'

        logging.debug(f"Final release date: {release_date}")
        update_cache_release_date(imdb_id, release_date)
        return release_date

    except requests.RequestException as e:
        logging.error(f"Error fetching release date from Trakt for IMDb ID {imdb_id}: {e}")
        return "Unknown"

def get_trakt_episode_release_date(imdb_id, season, episode):
    cached_date = get_cached_release_date(imdb_id, season, episode)
    if cached_date:
        return cached_date

    if not TRAKT_CLIENT_ID:
        logging.error("Trakt Client ID is not set. Please configure in settings.")
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

        if 'first_aired' not in data:
            logging.info(f"No Trakt data found for IMDb ID: {imdb_id}, Season: {season}, Episode: {episode}")
            return "Unknown"

        first_aired = data.get('first_aired', 'Unknown')
        if first_aired != "Unknown":
            first_aired = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S.%fZ").date()

        #logging.info(f"Found release date for IMDb ID {imdb_id}, Season: {season}, Episode: {episode}: {first_aired}")
        update_cache_release_date(imdb_id, first_aired, season, episode)
        return first_aired

    except requests.RequestException as e:
        logging.error(f"Error fetching release date from Trakt: {e}")
        return "Unknown"

def debug_print_cache():
    cache = load_cache()
    for key, value in cache.items():
        imdb_id, season, episode = key
        print(f"IMDb ID: {imdb_id}, Season: {season}, Episode: {episode}, Release Date: {value['release_date']}, Cached At: {value['timestamp']}")
