import logging
from api_tracker import api
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from settings import get_setting
from database import get_all_media_items, update_release_date_and_state
from content_checkers.trakt import get_trakt_headers
import ast

REQUEST_TIMEOUT = 15  # seconds
TRAKT_API_URL = "https://api.trakt.tv"
url = get_setting('Metadata Battery', 'url')

def get_overseerr_headers(api_key: str) -> Dict[str, str]:
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url: str, endpoint: str) -> str:
    return f"{base_url}{endpoint}"

def get_year_from_imdb_id(imdb_id: str) -> Optional[int]:
    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if 'metadata' in data and 'year' in data['metadata']:
            try:
                return int(data['metadata']['year'])
            except ValueError:
                logging.warning(f"Invalid year format for IMDb ID {imdb_id}: {data['metadata']['year']}")
                return None
        else:
            logging.warning(f"Year not found in metadata for IMDb ID {imdb_id}.")
            return None
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching metadata for IMDb ID {imdb_id}: {str(e)}")
        return None

def get_tmdb_id_and_media_type(imdb_id: str) -> (Optional[int], Optional[str]):
    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        tmdb_id = None
        media_type = data.get('type')

        if 'metadata' in data and 'ids' in data['metadata']:
            ids_str = data['metadata']['ids']
            try:
                ids_dict = ast.literal_eval(ids_str)
                tmdb_id = ids_dict.get('tmdb')
            except (ValueError, SyntaxError):
                logging.warning(f"Failed to parse 'ids' for IMDb ID {imdb_id}: {ids_str}")

        if tmdb_id is not None:
            try:
                tmdb_id = int(tmdb_id)
            except ValueError:
                logging.warning(f"Invalid TMDB ID format for IMDb ID {imdb_id}: {tmdb_id}")
                tmdb_id = None
        
        if tmdb_id is None or media_type is None:
            logging.warning(f"TMDB ID or media type not found for IMDb ID: {imdb_id}")
        
        return tmdb_id, media_type
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching metadata for IMDb ID {imdb_id}: {str(e)}")
        return None, None

def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if date_str is None:
        return None

    date_formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for date_format in date_formats:
        try:
            return datetime.strptime(date_str, date_format)
        except (ValueError, TypeError):
            continue

    logging.warning(f"Unable to parse date: {date_str}")
    return None

def get_release_date(media_details: Dict[str, Any]) -> str:
    imdb_id = media_details.get('imdb_id')
    media_type = media_details.get('media_type')
    url = f"{get_setting('Metadata Battery', 'url')}/api/release_dates/{imdb_id}"

    if media_type == 'movie':
        try:
            response = api.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            release_dates = response.json()

            current_date = datetime.now()
            us_releases = release_dates.get('release_dates', {}).get('us', [])            
            
            digital_physical_releases = []
            theatrical_releases = []
            all_releases = []

            for release in us_releases:
                release_date = datetime.strptime(release['date'], "%Y-%m-%d")
                release_type = release['type']

                all_releases.append(release_date)

                if release_type in ['digital', 'physical']:
                    digital_physical_releases.append(release_date)
                elif release_type in ['theatrical', 'premiere']:
                    theatrical_releases.append(release_date)

            # Priority 1: Digital or Physical release
            if digital_physical_releases:
                result = min(digital_physical_releases).strftime("%Y-%m-%d")
                return result
            
            # Priority 2: Theatrical release older than 180 days
            old_theatrical_releases = [date for date in theatrical_releases if date < current_date - timedelta(days=180)]
            if old_theatrical_releases:
                result = max(old_theatrical_releases).strftime("%Y-%m-%d")
                return result

            # Priority 3: Any release date older than 180 days
            old_releases = [date for date in all_releases if date < current_date - timedelta(days=180)]
            if old_releases:
                result = max(old_releases).strftime("%Y-%m-%d")
                return result
            
            # Priority 4: Earliest future release date
            if all_releases:
                result = min(all_releases).strftime("%Y-%m-%d")
                return result
            
            logging.warning(f"No valid release date found for IMDb ID: {imdb_id}, marking as Unknown.")
            return 'Unknown'

        except api.exceptions.RequestException as e:
            logging.error(f"Error fetching release dates for IMDb ID {imdb_id}: {str(e)}")
            return 'Unknown'
    elif media_type == 'tv':
        air_date = parse_date(media_details.get('airDate'))
        result = air_date.strftime("%Y-%m-%d") if air_date else 'Unknown'
        return result
    else:
        logging.error(f"Unknown media type: {media_type}")
        return 'Unknown'
    
def get_overseerr_cookies(overseerr_url: str) -> Optional[api.cookies.RequestsCookieJar]:
    try:
        session = api.Session()
        session.get(overseerr_url, timeout=REQUEST_TIMEOUT)
        return session.cookies
    except api.exceptions.RequestException as e:
        logging.error(f"Error getting Overseerr cookies: {str(e)}")
        return None

def get_overseerr_show_details(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, cookies: api.cookies.RequestsCookieJar) -> Optional[Dict[str, Any]]:
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    try:
        response = api.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching show details for TMDB ID {tmdb_id}: {str(e)}")
        return None

def get_overseerr_show_episodes(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, season_number: int, cookies: api.cookies.RequestsCookieJar) -> Optional[Dict[str, Any]]:
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}/season/{season_number}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    try:
        response = api.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching show episodes for TMDB ID {tmdb_id}, season {season_number}: {str(e)}")
        return None

def get_overseerr_movie_details(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, cookies: api.cookies.RequestsCookieJar) -> Optional[Dict[str, Any]]:
    url = get_url(overseerr_url, f"/api/v1/movie/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    try:
        response = api.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching movie details for TMDB ID {tmdb_id}: {str(e)}")
        return None

def get_latest_us_type_3_year(media_details: Dict[str, Any], media_type: str) -> Optional[int]:
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
        first_air_date = parse_date(media_details.get('first_air_date'))
        if first_air_date:
            return first_air_date.year
    
    return None

def imdb_to_tmdb(imdb_id: str) -> Optional[int]:
    url = f"{get_setting('Metadata Battery', 'url')}/api/metadata/{imdb_id}"
    
    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"Metadata response: {data}")

        if 'metadata' in data and 'ids' in data['metadata']:
            tmdb_id = data['metadata']['ids'].get('tmdb')
            
            if tmdb_id is not None:
                try:
                    tmdb_id = int(tmdb_id)
                    logging.info(f"Successfully converted IMDb ID {imdb_id} to TMDB ID {tmdb_id}")
                    return tmdb_id
                except ValueError:
                    logging.warning(f"Invalid TMDB ID format for IMDb ID {imdb_id}: {tmdb_id}")
            else:
                logging.warning(f"TMDB ID not found in metadata for IMDb ID: {imdb_id}")
        else:
            logging.warning(f"Required metadata not found for IMDb ID: {imdb_id}")
        
        return None
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching metadata for IMDb ID {imdb_id}: {str(e)}")
        return None

def get_imdb_id_if_missing(item: Dict[str, Any]) -> Optional[str]:
    if 'imdb_id' in item:
        return item['imdb_id']
    
    if 'tmdb_id' not in item:
        logging.warning(f"Cannot retrieve IMDb ID without TMDB ID: {item}")
        return None
    
    tmdb_id = item['tmdb_id']
    
    url = f"{get_setting('Metadata Battery', 'url')}/api/tmdb_to_imdb/{tmdb_id}"
    
    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"API response: {data}")

        imdb_id = data.get('imdb_id')
        if imdb_id:
            item['imdb_id'] = imdb_id
            return imdb_id
        else:
            logging.warning(f"IMDb ID not found for TMDB ID: {tmdb_id}")
        
        return None
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching IMDb ID for TMDB ID {tmdb_id}: {str(e)}")
        return None

def get_media_type_if_missing(item: Dict[str, Any]) -> str:
    if 'media_type' in item:
        return item['media_type']
    
    if 'imdb_id' not in item:
        logging.warning(f"Cannot retrieve media type without IMDb ID: {item}")
        return 'unknown'
    
    imdb_id = item['imdb_id']
    url = f"{get_setting('Metadata Battery', 'url')}/api/metadata/{imdb_id}"
    
    logging.info(f"Fetching media type for IMDb ID: {imdb_id}")
    logging.info(f"URL: {url}")

    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"API response: {data}")

        media_type = data.get('type')
        if media_type:
            item['media_type'] = media_type
            logging.info(f"Retrieved missing media_type: {media_type}")
            return media_type
        else:
            logging.warning(f"Media type not found for IMDb ID: {imdb_id}")
            return 'unknown'
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching media type for IMDb ID {imdb_id}: {str(e)}")
        return 'unknown'

def get_tmdb_id_if_missing(item: Dict[str, Any]) -> Optional[int]:
    if 'tmdb_id' in item:
        return item['tmdb_id']
    
    if 'imdb_id' not in item:
        logging.warning(f"Cannot retrieve TMDB ID without IMDb ID: {item}")
        return None
    
    imdb_id = item['imdb_id']
    url = f"{get_setting('Metadata Battery', 'url')}/api/metadata/{imdb_id}"
    
    logging.info(f"Fetching TMDB ID for IMDb ID: {imdb_id}")
    logging.info(f"URL: {url}")

    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        logging.info(f"Metadata response: {data}")

        if 'metadata' in data and 'ids' in data['metadata']:
            ids = data['metadata']['ids']
            tmdb_id = ids.get('tmdb')
            
            if tmdb_id is not None:
                try:
                    tmdb_id = int(tmdb_id)
                    item['tmdb_id'] = tmdb_id
                    logging.info(f"Retrieved missing TMDB ID: {tmdb_id}")
                    return tmdb_id
                except ValueError:
                    logging.warning(f"Invalid TMDB ID format for IMDb ID {imdb_id}: {tmdb_id}")
            else:
                logging.warning(f"TMDB ID not found in metadata for IMDb ID: {imdb_id}")
        else:
            logging.warning(f"Required metadata not found for IMDb ID: {imdb_id}")
        
        return None
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching metadata for IMDb ID {imdb_id}: {str(e)}")
        return None

def get_title_if_missing(item: Dict[str, Any], details: Dict[str, Any]) -> str:
    if 'title' not in item:
        item['title'] = details.get('title' if item['media_type'] == 'movie' else 'name', 'Unknown Title')
        logging.debug(f"Retrieved missing title: {item['title']}")
    return item['title']

def get_year_if_missing(item: Dict[str, Any], details: Dict[str, Any]) -> str:
    if 'year' not in item:
        release_date = details.get('releaseDate' if item['media_type'] == 'movie' else 'firstAirDate', '')
        item['year'] = release_date[:4] if release_date else str(get_latest_us_type_3_year(details, item['media_type']) or '')
        logging.debug(f"Retrieved missing year: {item['year']}")
    return item['year']

def get_release_date_if_missing(item: Dict[str, Any], details: Dict[str, Any]) -> str:
    if 'release_date' not in item:
        item['release_date'] = get_release_date(item)
        logging.debug(f"Retrieved missing release_date: {item['release_date']}")
    return item['release_date']

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    processed_items = {'movies': [], 'episodes': []}

    for item in media_items:
        logging.debug(f"Processing item: {item}")

        if 'tmdb_id' not in item:
            tmdb_id = get_tmdb_id_if_missing(item)
            if not tmdb_id:
                logging.warning(f"Could not find TMDB ID for item: {item}")
                continue
            item['tmdb_id'] = tmdb_id

        if 'imdb_id' not in item:
            imdb_id = get_imdb_id_if_missing(item)
            if not imdb_id:
                logging.warning(f"Could not find IMDb ID for item: {item}")
                continue
            item['imdb_id'] = imdb_id

        if item['media_type'] == 'movie':
            details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
            if not details:
                logging.warning(f"Could not fetch details for movie: {item}")
                continue

            if 'title' not in item:
                item['title'] = get_title_if_missing(item, details)
            if 'year' not in item:
                item['year'] = get_year_if_missing(item, details)
            if 'release_date' not in item:
                item['release_date'] = get_release_date_if_missing(item, details)
            
            # Check if the movie is tagged as anime
            genres = details.get('keywords', [])
            is_anime = False
            if isinstance(genres, list):
                is_anime = any(genre.get('name', '').lower() == 'anime' for genre in genres)
            else:
                logging.warning(f"Unexpected 'genres' format for movie {item['title']}: {genres}")
            
            item['genres'] = ['anime'] if is_anime else []
            logging.debug(f"Movie {item['title']} is{'not' if not is_anime else ''} tagged as anime. Genres: {item['genres']}")
            
            processed_items['movies'].append(item)

        else:  # TV show
            show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
            if not show_details:
                logging.warning(f"Could not fetch details for TV show: {item}")
                continue

            if 'title' not in item:
                item['title'] = get_title_if_missing(item, show_details)
            if 'year' not in item:
                item['year'] = get_year_if_missing(item, show_details)

            # Check if the show is tagged as anime
            genres = show_details.get('keywords', [])
            is_anime = False
            if isinstance(genres, list):
                is_anime = any(genre.get('name', '').lower() == 'anime' for genre in genres)
            else:
                logging.warning(f"Unexpected 'genres' format for show {item['title']}: {genres}")
            
            logging.debug(f"Show {item['title']} is{'not' if not is_anime else ''} tagged as anime. Genres: {genres}")

            # Get all season-episode counts
            season_episode_counts = get_all_season_episode_counts(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)

            # Get existing episodes
            existing_episodes = get_all_media_items(tmdb_id=item['tmdb_id'], media_type='episode')
            existing_episode_set = set((ep['season_number'], ep['episode_number']) for ep in existing_episodes)

            logging.debug(f"Processing TV show: {item['title']} (TMDB ID: {item['tmdb_id']})")
            logging.debug(f"Season-episode counts: {season_episode_counts}")

            absolute_episode_number = 1  # Start from 1
            for season_number, episode_count in season_episode_counts.items():
                logging.debug(f"Processing season {season_number} with {episode_count} episodes")
                season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], season_number, cookies)
                logging.debug(f"Episode details for season {season_number}: {season_details}")
                
                available_episodes = season_details.get('episodes', [])
                for episode in available_episodes:
                    episode_number = episode['episodeNumber']
                    logging.debug(f"Processing S{season_number}E{episode_number} (Absolute: {absolute_episode_number})")
                    
                    if (season_number, episode_number) not in existing_episode_set:
                        logging.debug(f"Episode S{season_number}E{episode_number} not in existing set, processing")
                        
                        episode_item = {
                            'imdb_id': item['imdb_id'],
                            'tmdb_id': item['tmdb_id'],
                            'title': item['title'],
                            'year': item['year'],
                            'season_number': season_number,
                            'episode_number': episode_number,
                            'episode_title': episode.get('name', 'Unknown Episode Title'),
                            'release_date': get_release_date(episode),
                            'media_type': 'episode',
                            'genres': ['anime'] if is_anime else []
                        }
                        logging.debug(f"Created episode item: {episode_item}")
                        processed_items['episodes'].append(episode_item)
                        logging.debug(f"Added episode: S{season_number}E{episode_number}")
                    else:
                        logging.debug(f"Episode S{season_number}E{episode_number} already exists in the database")
                    
                    absolute_episode_number += 1

        logging.debug(f"Processed item: {item}")

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes")
    for movie in processed_items['movies']:
        logging.debug(f"Processed movie: {movie}")

    for episode in processed_items['episodes']:
        logging.debug(f"Processed episode: {episode}")
        
    return processed_items
    
def refresh_release_dates():
    logging.info("Starting refresh_release_dates function")
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return

    logging.info("Getting Overseerr cookies")
    cookies = get_overseerr_cookies(overseerr_url)
    
    logging.info("Fetching items to refresh")
    items_to_refresh = get_all_media_items(state="Unreleased") + get_all_media_items(state="Wanted")
    logging.info(f"Found {len(items_to_refresh)} items to refresh")

    for index, item in enumerate(items_to_refresh, 1):
        logging.info(f"Processing item {index}/{len(items_to_refresh)}: {item['title']} (Type: {item['type']}, TMDB ID: {item['tmdb_id']})")
        try:
            if item['type'] == 'movie':
                logging.info(f"Fetching movie details for TMDB ID: {item['tmdb_id']}")
                details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                media_type = 'movie'
            else:  # TV show episode
                logging.info(f"Fetching TV show details for TMDB ID: {item['tmdb_id']}")
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                logging.info(f"Fetching season details for TMDB ID: {item['tmdb_id']}, Season: {item['season_number']}")
                season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], item['season_number'], cookies)
                logging.info(f"Finding episode details for Episode: {item['episode_number']}")
                episode_details = next((ep for ep in season_details.get('episodes', []) if ep['episodeNumber'] == item['episode_number']), None)
                details = episode_details if episode_details else show_details
                media_type = 'tv'

            if details:
                logging.info("Getting release date")
                new_release_date = get_release_date(details)
                logging.info(f"New release date: {new_release_date}")

                if new_release_date == 'Unknown':
                    new_state = "Wanted"
                else:
                    release_date = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                    today = datetime.now().date()

                    if release_date <= today:
                        new_state = "Wanted"
                    else:
                        new_state = "Unreleased"

                logging.info(f"New state: {new_state}")

                if new_state != item['state'] or new_release_date != item['release_date']:
                    logging.info("Updating release date and state in database")
                    update_release_date_and_state(item['id'], new_release_date, new_state)
                    logging.info(f"Updated: {item['title']} has a release date of: {new_release_date}")
                else:
                    logging.info("No changes needed for this item")

            else:
                logging.warning(f"Could not fetch details for {item['title']}")
        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}", exc_info=True)

    logging.info("Finished refresh_release_dates function")

def get_episode_count_for_seasons(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, seasons: List[int], cookies: api.cookies.RequestsCookieJar) -> int:
    total_episodes = 0
    
    for season_number in seasons:
        season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number, cookies)
        if season_details:
            total_episodes += len(season_details.get('episodes', []))
        else:
            logging.warning(f"Could not fetch details for season {season_number} of show with TMDB ID: {tmdb_id}")

    logging.debug(f"Total episodes for TMDB ID {tmdb_id}, seasons {seasons}: {total_episodes}")
    return total_episodes

def get_all_season_episode_counts(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, cookies: api.cookies.RequestsCookieJar) -> Dict[int, int]:
    episode_counts = {}
    show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
    
    if not show_details:
        logging.warning(f"Could not fetch show details for TMDB ID: {tmdb_id}")
        return episode_counts

    for season in show_details.get('seasons', []):
        season_number = season.get('seasonNumber')
        if season_number == 0:
            continue  # Skip special seasons

        season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number, cookies)
        if season_details:
            episode_counts[season_number] = len(season_details.get('episodes', []))
        else:
            logging.warning(f"Could not fetch details for season {season_number} of show with TMDB ID: {tmdb_id}")

    logging.debug(f"Episode counts for TMDB ID {tmdb_id}: {episode_counts}")
    return episode_counts

def get_show_airtime_by_imdb_id(imdb_id: str) -> str:
    """
    Get the airtime of a show using its IMDb ID.
    
    :param imdb_id: IMDb ID of the show
    :return: Airtime as a string (e.g., "22:00"), or "19:00" if not available or user isn't logged into Trakt
    """
    DEFAULT_AIRTIME = "19:00"

    headers = get_trakt_headers()
    if not headers:
        logging.warning("Failed to obtain Trakt headers. Using default airtime.")
        return DEFAULT_AIRTIME

    # First, search for the show using the IMDb ID
    search_url = f"{TRAKT_API_URL}/search/imdb/{imdb_id}?type=show"
    try:
        response = api.get(search_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        search_results = response.json()
        
        if not search_results:
            logging.warning(f"No show found for IMDb ID: {imdb_id}. Using default airtime.")
            return DEFAULT_AIRTIME
        
        # Get the Trakt ID of the show
        trakt_id = search_results[0]['show']['ids']['trakt']
        
        # Now fetch the full show data
        show_url = f"{TRAKT_API_URL}/shows/{trakt_id}?extended=full"
        show_response = api.get(show_url, headers=headers, timeout=REQUEST_TIMEOUT)
        show_response.raise_for_status()
        show_data = show_response.json()
        
        # Extract and return the airtime
        first_aired = show_data.get('first_aired')
        if first_aired:
            try:
                local_now = datetime.now()
                utc = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S.000Z")
                utc = utc.replace(tzinfo=timezone.utc)
                local_time = (utc.astimezone(local_now.tzname())).strftime('%H:%M')
                return local_time
            except ValueError as e:
                logging.error(f"Error parsing 'first_aired' for show with IMDb ID {imdb_id}: {e}. Using default airtime.")
                return DEFAULT_AIRTIME
        else:
            logging.warning(f"No 'first_aired' data found for show with IMDb ID: {imdb_id}. Using default airtime.")
            return DEFAULT_AIRTIME
    
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching show data from Trakt: {e}. Using default airtime.")
        return DEFAULT_AIRTIME
    except Exception as e:
        logging.error(f"Unexpected error in get_show_airtime_by_imdb_id for IMDb ID {imdb_id}: {e}. Using default airtime.")
        return DEFAULT_AIRTIME