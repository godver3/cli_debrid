import logging
import requests
from settings import get_setting
from database import get_all_media_items, update_release_date_and_state, update_year
from datetime import datetime, timedelta, date
from typing import Optional

DEFAULT_TAKE = 100
DEFAULT_TOTAL_RESULTS = float('inf')
REQUEST_TIMEOUT = 3  # seconds
REQUEST_LIMIT = 5  # Limit of items to handle
PAGINATION_TAKE = 20  # Number of items to fetch per pagination request
MAX_RETRIES = 1
RETRY_DELAY = 2  # seconds
JITTER = 2  # seconds

def imdb_to_tmdb(overseerr_url: str, overseerr_api_key: str, imdb_id: str) -> Optional[int]:
    """
    Convert an IMDB ID to a TMDB ID using Overseerr's search endpoint.
    
    :param overseerr_url: The base URL of the Overseerr instance
    :param overseerr_api_key: The API key for Overseerr
    :param imdb_id: The IMDB ID to convert
    :return: The TMDB ID if found, None otherwise
    """
    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }
    
    search_url = f"{overseerr_url}/api/v1/search?query=imdb%3A{imdb_id}"
    
    try:
        response = requests.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        if data['results']:
            first_result = data['results'][0]
            return first_result.get('id')  # This is the TMDB ID
        else:
            logging.warning(f"No results found for IMDB ID: {imdb_id}")
            return None
    except requests.RequestException as e:
        logging.error(f"Error converting IMDB ID to TMDB ID: {e}")
        return None

def parse_date(date_str):
    """
    Parse a date string in various formats.

    :param date_str: String representing a date or None
    :return: datetime object if parsing is successful, None otherwise
    """
    if date_str is None:
        return None

    date_formats = [
        "%Y-%m-%d",  # Standard ISO 8601 date format
        "%Y-%m-%dT%H:%M:%S.%fZ",  # ISO 8601 with milliseconds and Z
        "%Y-%m-%dT%H:%M:%SZ",  # ISO 8601 with Z but no milliseconds
        "%Y-%m-%dT%H:%M:%S",  # ISO 8601 without Z
    ]

    for date_format in date_formats:
        try:
            return datetime.strptime(date_str, date_format)
        except (ValueError, TypeError):
            continue

    logging.warning(f"Unable to parse date: {date_str}")
    return None

def get_release_date(media_details, media_type):
    
    if media_details is None:
        return 'Unknown'
    
    current_date = datetime.now()
    general_release_date = parse_date(media_details.get('releaseDate'))
    if media_type == 'movie':
        releases = media_details.get('releases', {}).get('results', [])
        valid_releases = []
        type_3_releases = []
        for release in releases:
            for date in release.get('release_dates', []):
                parsed_date = parse_date(date.get('release_date'))
                if parsed_date:
                    if release.get('iso_3166_1') == 'US' and date.get('type') in [4, 5]:
                        valid_releases.append(parsed_date)
                    elif date.get('type') == 3:
                        type_3_releases.append(parsed_date)
        
        if valid_releases:
            # Check if all release dates are 2 years or older
            if all(release < current_date - timedelta(days=730) for release in valid_releases):
                return general_release_date.strftime("%Y-%m-%d") if general_release_date else 'Unknown'
            
            # If all releases are newer than 2 years, return the earliest one
            return min(valid_releases).strftime("%Y-%m-%d")
        else:
            # If no valid US releases of type 4 or 5, check for type 3 releases more than 6 months old
            old_type_3_releases = [date for date in type_3_releases if date < current_date - timedelta(days=180)]
            
            if old_type_3_releases:
                return max(old_type_3_releases).strftime("%Y-%m-%d")
            
            # If no suitable type 3 releases, check if there are any releases at all
            all_releases = [parse_date(date.get('release_date')) for release in releases for date in release.get('release_dates', [])]
            all_releases = [date for date in all_releases if date is not None]
            
            if all_releases:
                # If all releases are 2 years or older, use general release date
                if all(release < current_date - timedelta(days=730) for release in all_releases):
                    return general_release_date.strftime("%Y-%m-%d") if general_release_date else 'Unknown'
            
            # If no valid releases, return Unknown
            return 'Unknown'
    
    elif media_type == 'tv':
        # For TV shows, we'll use the airDate of the episode
        parsed_date = parse_date(media_details.get('airDate'))
        
        # If airDate is 2 years or older, use the general release date
        if parsed_date and parsed_date < current_date - timedelta(days=730):
            return general_release_date.strftime("%Y-%m-%d") if general_release_date else 'Unknown'
        
        return parsed_date.strftime("%Y-%m-%d") if parsed_date else 'Unknown'
    
    else:
        logging.error(f"Unknown media type: {media_type}")
        return 'Unknown'
        
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
            logging.debug(f"Fetching page {page} (skip={skip}, take={take})")
            response = requests.get(
                get_url(overseerr_url, f"/api/v1/request?take={take}&skip={skip}"),
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get('results', [])
            total_results = data.get('pageInfo', {}).get('total', data.get('total', 0))
            
            logging.debug(f"Page {page}: Received {len(results)} results. API reports total of {total_results}")
            
            if not results:
                logging.debug("No more results returned. Stopping pagination.")
                break

            wanted_content.extend(results)
            skip += take
            page += 1

            if len(results) < take:
                logging.debug("Received fewer results than requested. This is likely the last page.")
                break

        except requests.RequestException as e:
            logging.error(f"Error fetching wanted content from Overseerr: {e}")
            break
        except KeyError as e:
            logging.error(f"Unexpected response structure from Overseerr: {e}")
            logging.debug(f"Full response: {data}")
            break
        except Exception as e:
            logging.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    logging.info(f"Fetched a total of {len(wanted_content)} wanted content items from Overseerr")
    logging.debug(f"Pagination details: Pages fetched: {page - 1}, Last skip value: {skip - take}")
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
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Error fetching movie details for TMDB ID {tmdb_id}: {str(e)}")
        return None

def get_overseerr_cookies(overseerr_url):
    session = requests.Session()
    session.get(overseerr_url)
    return session.cookies

def get_wanted_from_overseerr():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
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
                logging.debug(f"Processing TV media: {media}")
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                imdb_id = show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID')
                show_title = show_details.get('name', 'Unknown Show Title')

                for season in range(1, show_details.get('numberOfSeasons', 0) + 1):
                    season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season, cookies)
                    for episode in season_details.get('episodes', []):
                        release_date = get_release_date(episode, 'tv')
                        release_year = episode.get('releaseDate', '')[:4]
                        year = release_year or media.get('year') or get_latest_us_type_3_year(episode, 'tv')
                        if year is None and release_date != 'Unknown':
                            year = release_date[:4]
                        episode_item = {
                            'imdb_id': imdb_id,
                            'tmdb_id': tmdb_id,
                            'title': show_title,
                            'episode_title': episode.get('name', 'Unknown Episode Title'),
                            'year': year,
                            'season_number': season,
                            'episode_number': episode.get('episodeNumber', 'Unknown Episode Number'),
                            'release_date': release_date
                        }
                        logging.debug(f"Appending episode: {episode_item}")
                        wanted_episodes.append(episode_item)
            elif media.get('mediaType') == 'movie':
                tmdb_id = media.get('tmdbId')
                movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                if movie_details is None:
                    logging.warning(f"Unable to fetch details for movie with TMDB ID: {tmdb_id}. Skipping.")
                    continue
                release_date = get_release_date(movie_details, 'movie')
                release_year = movie_details.get('releaseDate', '')[:4]
                year = release_year or media.get('year') or get_latest_us_type_3_year(movie_details, 'movie')
                if year is None and release_date != 'Unknown':
                    year = release_date[:4]
                
                movie_item = {
                    'imdb_id': movie_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                    'tmdb_id': tmdb_id,
                    'title': movie_details.get('title', 'Unknown Title'),
                    'year': year,
                    'release_date': release_date
                }
                wanted_movies.append(movie_item)

        logging.debug(f"Added {len(wanted_movies)} wanted movies from Overseerr.")
        logging.debug(f"Added {len(wanted_episodes)} wanted episodes from Overseerr")
        return {'movies': wanted_movies, 'episodes': wanted_episodes}
    except Exception as e:
        logging.error(f"Unexpected error while processing Overseerr response: {e}")
        return {'movies': [], 'episodes': []}

def map_collected_media_to_wanted():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return {'episodes': []}

    try:
        logging.debug("Starting map_collected_media_to_wanted function")
        cookies = get_overseerr_cookies(overseerr_url)
        wanted_episodes = []

        # Process collected and wanted episodes
        collected_episodes = get_all_media_items(state="Collected", media_type="episode")
        wanted_episodes_db = get_all_media_items(state="Wanted", media_type="episode")
        all_episodes = collected_episodes + wanted_episodes_db

        logging.info(f"Processing episodes for {len(set(episode['tmdb_id'] for episode in all_episodes))} unique shows")

        processed_tmdb_ids = set()
        for i, episode in enumerate(all_episodes, 1):
            try:
                tmdb_id = episode['tmdb_id']

                if i % 20 == 0:
                    logging.info(f"Processed {i}/{len(all_episodes)} episodes")

                if tmdb_id is None:
                    logging.warning(f"Skipping episode due to None TMDB ID")
                    continue

                if tmdb_id in processed_tmdb_ids:
                    continue

                processed_tmdb_ids.add(tmdb_id)

                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                if show_details:
                    known_seasons = set(ep['season_number'] for ep in all_episodes if ep['tmdb_id'] == tmdb_id and ep['season_number'] != 0)

                    for season in show_details.get('seasons', []):
                        season_number = season.get('seasonNumber')
                        if season_number == 0:
                            continue  # Skip season 0

                        season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number, cookies)
                        if season_details:
                            known_episodes_this_season = [ep for ep in all_episodes if ep['tmdb_id'] == tmdb_id and ep['season_number'] == season_number]
                            known_episode_numbers = set(ep['episode_number'] for ep in known_episodes_this_season)

                            for overseerr_episode in season_details.get('episodes', []):
                                if overseerr_episode['episodeNumber'] not in known_episode_numbers:
                                    release_date = get_release_date(overseerr_episode, 'tv')
                                    wanted_episodes.append({
                                        'imdb_id': show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                                        'tmdb_id': tmdb_id,
                                        'title': show_details.get('name', 'Unknown Show Title'),
                                        'episode_title': overseerr_episode.get('name', 'Unknown Episode Title'),
                                        'year': release_date[:4] if release_date != 'Unknown' else 'Unknown Year',
                                        'season_number': season_number,
                                        'episode_number': overseerr_episode['episodeNumber'],
                                        'release_date': release_date
                                    })

            except requests.exceptions.RequestException as e:
                logging.error(f"Error processing show TMDB ID: {tmdb_id}: {str(e)}")
            except Exception as e:
                logging.error(f"Unexpected error processing show TMDB ID: {tmdb_id}: {str(e)}")

        logging.info(f"Retrieved {len(wanted_episodes)} additional wanted episodes")

        # Log details of wanted episodes
        for episode in wanted_episodes:
            logging.info(f"Wanted episode: {episode['title']} S{episode['season_number']}E{episode['episode_number']} - {episode['episode_title']} - IMDB: {episode['imdb_id']}, TMDB: {episode['tmdb_id']} - Air Date: {episode['release_date']}")

        return {'episodes': wanted_episodes}
    except Exception as e:
        logging.error(f"Unexpected error while mapping collected media to wanted: {str(e)}")
        logging.exception("Traceback:")
        return {'episodes': []}

def get_latest_us_type_3_year(media_details, media_type):
    if media_details is None:
        return None
    
    if media_type == 'movie':
        releases = media_details.get('releases', {}).get('results', [])
        us_type_3_releases = []
        for release in releases:
            if release.get('iso_3166_1') == 'US':
                for date in release.get('release_dates', []):
                    if date.get('type') == 3:
                        parsed_date = parse_date(date.get('release_date'))
                        if parsed_date:
                            us_type_3_releases.append(parsed_date)
        
        if us_type_3_releases:
            return max(us_type_3_releases).year
    
    elif media_type == 'tv':
        # For TV shows, we'll use the first air date
        first_air_date = parse_date(media_details.get('first_air_date'))
        if first_air_date:
            return first_air_date.year
    
    return None

def refresh_release_dates():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return
    cookies = get_overseerr_cookies(overseerr_url)
    items_to_refresh = get_all_media_items(state="Unreleased") + get_all_media_items(state="Wanted")
    for item in items_to_refresh:
        try:
            if item['type'] == 'movie':
                details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                media_type = 'movie'
            else:  # TV show episode
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], item['season_number'], cookies)
                episode_details = next((ep for ep in season_details.get('episodes', []) if ep['episodeNumber'] == item['episode_number']), None)
                details = episode_details if episode_details else show_details
                media_type = 'tv'
            if details:
                new_release_date = get_release_date(details, media_type)
                latest_us_type_3_year = get_latest_us_type_3_year(details, media_type)
                
                if new_release_date == 'Unknown':
                    new_state = "Wanted"
                else:
                    release_date = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                    today = datetime.now().date()
                    
                    if release_date <= today:
                        new_state = "Wanted"
                    else:
                        new_state = "Unreleased"
                
                if new_state != item['state'] or new_release_date != item['release_date']:
                    update_release_date_and_state(item['id'], new_release_date, new_state)
                    if new_state != item['state']:
                        logging.info(f"Moved item {item['title']} from {item['state']} to {new_state} queue (Release date: {new_release_date})")
                    else:
                        logging.debug(f"Updated release date for {item['title']} to {new_release_date}")
                
                # Update the year if available
                if latest_us_type_3_year and latest_us_type_3_year != item['year']:
                    update_year(item['id'], latest_us_type_3_year)
                    logging.info(f"Updated year for {item['title']} to {latest_us_type_3_year}")
            else:
                logging.warning(f"Could not fetch details for {item['title']}")
        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}")