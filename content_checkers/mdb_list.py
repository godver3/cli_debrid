import logging
import requests
import os
import pickle
from settings import get_setting
from content_checkers.overseerr import (
    get_overseerr_cookies,
    get_overseerr_show_details,
    get_overseerr_show_episodes,
    get_release_date,
    get_overseerr_movie_details
)
from collections import Counter
from urllib.parse import quote

REQUEST_TIMEOUT = 10  # seconds
CACHE_FILE = "db_content/mdb_tmdbs.pkl"

def determine_list_type(items):
    type_counter = Counter(item.get('mediatype', '').lower() for item in items)
    movie_count = type_counter['movie']
    show_count = type_counter['show'] + type_counter['tv']

    logging.info(f"Type counter: {dict(type_counter)}")
    logging.info(f"Movie count: {movie_count}, Show count: {show_count}")

    if movie_count > show_count:
        return 'movie'
    elif show_count > movie_count:
        return 'show'
    else:
        # If counts are equal, let's default to 'movie'
        logging.warning("Equal number of movies and shows. Defaulting to 'movie'.")
        return 'movie'

def load_tmdb_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    return {}

def save_tmdb_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache, f)

def get_tmdb_info(imdb_id, mdblist_api_key, tmdb_cache):
    if imdb_id in tmdb_cache:
        return tmdb_cache[imdb_id]

    url = f"https://mdblist.com/api/?apikey={mdblist_api_key}&i={imdb_id}"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        tmdb_id = data.get('tmdbid')

        if tmdb_id:
            tmdb_info = {
                'tmdb_id': tmdb_id,
                'type': data.get('type', 'unknown').lower(),
                'title': data.get('title'),
                'year': data.get('year'),
                'released': data.get('released'),
                'releases': data.get('releases', {})  # Ensure releases are included
            }
            tmdb_cache[imdb_id] = tmdb_info
            save_tmdb_cache(tmdb_cache)
            return tmdb_info
        else:
            logging.error(f"Missing TMDB ID for IMDB ID {imdb_id}")
            return None

    except requests.RequestException as e:
        logging.error(f"Error fetching TMDB info for IMDB ID {imdb_id}: {e}")
        return None

def search_overseerr(title, media_type, overseerr_url, overseerr_api_key):
    encoded_title = quote(title)
    url = f"{overseerr_url}/api/v1/search?query={encoded_title}&page=1&language=en"
    headers = {
        'accept': 'application/json',
        'X-Api-Key': overseerr_api_key
    }

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        # Map media_type to Overseerr mediaType
        media_type_map = {
            'show': 'tv',
            'movie': 'movie'
        }
        overseerr_media_type = media_type_map.get(media_type)

        # Filter results by media type
        matching_results = [result for result in data.get('results', []) if result.get('mediaType') == overseerr_media_type]

        if matching_results:
            # Return the ID of the first matching result
            return matching_results[0].get('id')
        else:
            logging.warning(f"No matching {media_type} found in Overseerr search results for '{title}'.")
            return None
    except requests.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return None

def process_mdblist_items(items, mdblist_api_key, overseerr_url, overseerr_api_key):
    cookies = get_overseerr_cookies(overseerr_url)
    wanted_movies = []
    wanted_shows = []
    tmdb_cache = load_tmdb_cache()

    logging.debug(f"Processing {len(items)} items from MDBList")

    for item in items:
        try:
            imdb_id = item.get('imdb_id')
            if not imdb_id:
                logging.warning(f"Skipping item due to missing IMDB ID: {item.get('title', 'Unknown Title')}")
                continue

            tmdb_info = get_tmdb_info(imdb_id, mdblist_api_key, tmdb_cache)
            if not tmdb_info:
                logging.warning(f"Skipping item due to missing TMDB info: {item.get('title', 'Unknown Title')}")
                continue

            title = tmdb_info['title']
            year = tmdb_info['year']
            tmdb_id = tmdb_info['tmdb_id']
            detailed_type = tmdb_info['type']
            reported_media_type = item.get('mediatype', '').lower()

            logging.info(f"Processing item: {title} ({year}) - Reported Type: {reported_media_type}, Detailed Type: {detailed_type}")

            if reported_media_type != detailed_type:
                logging.warning(f"Media type mismatch for {title} ({year}): Reported as {reported_media_type}, but detailed type is {detailed_type}")
                logging.info(f"Searching Overseerr for correct TMDB ID for {title}")
                overseerr_tmdb_id = search_overseerr(title, reported_media_type, overseerr_url, overseerr_api_key)
                if overseerr_tmdb_id:
                    tmdb_id = overseerr_tmdb_id
                    tmdb_info['tmdb_id'] = tmdb_id
                    tmdb_info['type'] = reported_media_type
                    tmdb_cache[imdb_id] = tmdb_info
                    save_tmdb_cache(tmdb_cache)
                    logging.info(f"Updated TMDB cache with correct information for {title}")
                else:
                    logging.warning(f"Could not find matching {reported_media_type} in Overseerr for {title}. Skipping.")
                    continue

            # Process the item based on the reported_media_type
            if reported_media_type == 'movie':
                # Fetch detailed metadata from Overseerr
                movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                if movie_details:
                    release_date = get_release_date(movie_details, 'movie')  # Use detailed metadata
                    wanted_movies.append({
                        'imdb_id': imdb_id,
                        'tmdb_id': tmdb_id,
                        'title': title,
                        'year': year,
                        'release_date': release_date
                    })
                    logging.info(f"Movie added to wanted list: {title} ({year})")
                else:
                    logging.warning(f"Could not fetch movie details for {title} ({year})")
            elif reported_media_type == 'show':
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                if show_details:
                    show_item = {
                        'imdb_id': imdb_id,
                        'tmdb_id': tmdb_id,
                        'title': title,
                        'year': year,
                        'seasons': []
                    }
                    for season in show_details.get('seasons', []):
                        season_number = season.get('seasonNumber')
                        if season_number == 0:
                            continue  # Skip specials
                        season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number, cookies)
                        if season_details:
                            season_item = {
                                'season_number': season_number,
                                'episodes': []
                            }
                            for episode in season_details.get('episodes', []):
                                episode_number = episode.get('episodeNumber')
                                episode_title = episode.get('name', 'Unknown Episode Title')
                                release_date = get_release_date(episode, 'tv')
                                season_item['episodes'].append({
                                    'episode_number': episode_number,
                                    'title': episode_title,
                                    'release_date': release_date
                                })
                            show_item['seasons'].append(season_item)
                    wanted_shows.append(show_item)
                    logging.info(f"Show added to wanted list: {title} ({year})")
                else:
                    logging.warning(f"Could not fetch show details for {title} ({year})")

        except Exception as e:
            logging.error(f"Unexpected error processing item {title} ({year}): {str(e)}")

    logging.debug(f"Processed {len(items)} items. Found {len(wanted_movies)} wanted movies and {len(wanted_shows)} wanted shows.")
    return wanted_movies, wanted_shows

def get_mdblist_urls():
    mdblist_urls = get_setting('MDBList', 'urls')
    if not mdblist_urls:
        logging.error("MDBList URLs not set. Please configure in settings.")
        return []
    return [url.strip() for url in mdblist_urls.split(',')]

def fetch_items_from_mdblist(url, api_key):
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Accept': 'application/json'
    }
    if not url.endswith('/json'):
        url += '/json'

    try:
        logging.info(f"Fetching items from MDBList URL: {url}")
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data
    except requests.RequestException as e:
        logging.error(f"Error fetching items from MDBList: {e}")
        return []

def get_wanted_from_mdblists():
    mdblist_api_key = get_setting('MDBList', 'api_key')
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    if not all([mdblist_api_key, overseerr_url, overseerr_api_key]):
        logging.error("MDBList API key, Overseerr URL, or Overseerr API key not set. Please configure in settings.")
        return {'movies': [], 'episodes': []}

    url_list = get_mdblist_urls()
    all_wanted_movies = []
    all_wanted_shows = []

    for url in url_list:
        items = fetch_items_from_mdblist(url, mdblist_api_key)
        logging.info(f"Fetched {len(items)} items from MDBList URL: {url}")
        wanted_movies, wanted_shows = process_mdblist_items(items, mdblist_api_key, overseerr_url, overseerr_api_key)
        all_wanted_movies.extend(wanted_movies)
        all_wanted_shows.extend(wanted_shows)

    logging.info(f"Retrieved {len(all_wanted_movies)} wanted movies from all MDB Lists")
    logging.info(f"Retrieved {len(all_wanted_shows)} wanted shows from all MDB Lists")

    # Log details of wanted items
    for movie in all_wanted_movies:
        logging.info(f"Wanted movie: {movie['title']} ({movie['year']}) - IMDB: {movie['imdb_id']}, TMDB: {movie['tmdb_id']} - Release Date: {movie['release_date']}")

    wanted_episodes = []
    for show in all_wanted_shows:
        logging.info(f"Wanted show: {show['title']} ({show['year']}) - IMDB: {show['imdb_id']}, TMDB: {show['tmdb_id']}")
        for season in show.get('seasons', []):
            logging.info(f"  Season {season['season_number']}: {len(season['episodes'])} episodes")
            for episode in season['episodes']:
                wanted_episodes.append({
                    'imdb_id': show['imdb_id'],
                    'tmdb_id': show['tmdb_id'],
                    'title': show['title'],
                    'year': show['year'],
                    'season_number': season['season_number'],
                    'episode_number': episode['episode_number'],
                    'episode_title': episode['title'],
                    'release_date': episode['release_date']
                })

    return {'movies': all_wanted_movies, 'episodes': wanted_episodes}
