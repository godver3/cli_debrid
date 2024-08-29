import logging
from api_tracker import api
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from settings import get_setting
from database import get_all_media_items, update_release_date_and_state
from content_checkers.trakt import load_trakt_credentials, ensure_trakt_auth, get_trakt_headers

REQUEST_TIMEOUT = 15  # seconds
TRAKT_API_URL = "https://api.trakt.tv"

def get_overseerr_headers(api_key: str) -> Dict[str, str]:
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url: str, endpoint: str) -> str:
    return f"{base_url}{endpoint}"

def get_year_from_imdb_id(imdb_id: str) -> Optional[int]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return None

    # Check the content type and convert IMDb ID to TMDB ID
    tmdb_id, media_type = get_tmdb_id_and_media_type(overseerr_url, overseerr_api_key, imdb_id)
    if not tmdb_id or not media_type:
        logging.warning(f"Could not determine TMDB ID and media type for IMDb ID {imdb_id}.")
        return None

    # Fetch details based on the media type
    if media_type == 'movie':
        details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, get_overseerr_cookies(overseerr_url))
    elif media_type == 'tv':
        details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, get_overseerr_cookies(overseerr_url))
    else:
        logging.error(f"Unknown media type: {media_type} for IMDb ID {imdb_id}.")
        return None

    if not details:
        logging.warning(f"Could not fetch details for TMDB ID {tmdb_id} with media type {media_type}.")
        return None

    # Extract and return the release year
    if media_type == 'movie':
        release_date = parse_date(details.get('releaseDate'))
    elif media_type == 'tv':
        release_date = parse_date(details.get('firstAirDate'))
    else:
        release_date = None

    if release_date:
        return release_date.year
    else:
        logging.warning(f"No release date found for item with TMDB ID {tmdb_id}.")
        return None

def get_tmdb_id_and_media_type(overseerr_url: str, overseerr_api_key: str, imdb_id: str) -> (Optional[int], Optional[str]):
    headers = get_overseerr_headers(overseerr_api_key)
    search_url = f"{overseerr_url}/api/v1/search?query=imdb%3A{imdb_id}"

    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data['results']:
            result = data['results'][0]  # Assume the first result is the correct one
            return result.get('id'), result.get('mediaType')
        else:
            logging.warning(f"No results found for IMDb ID: {imdb_id}")
            return None, None
    except api.exceptions.RequestException as e:
        logging.error(f"Error converting IMDb ID to TMDB ID: {e}")
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

def get_release_date(media_details: Dict[str, Any], media_type: str) -> str:
    if media_details is None:
        logging.debug("Media details are None.")
        return 'Unknown'
    
    current_date = datetime.now()
    general_release_date = parse_date(media_details.get('releaseDate'))
    
    if media_type == 'movie':
        releases = media_details.get('releases', {}).get('results', [])
        type_4_5_us_releases = []
        type_4_5_global_releases = []
        type_3_releases = []

        for release in releases:
            for date in release.get('release_dates', []):
                parsed_date = parse_date(date.get('release_date'))
                if parsed_date:
                    if release.get('iso_3166_1') == 'US' and date.get('type') in [4, 5]:
                        type_4_5_us_releases.append(parsed_date)
                    elif date.get('type') in [4, 5]:
                        type_4_5_global_releases.append(parsed_date)
                    elif date.get('type') == 3:
                        type_3_releases.append(parsed_date)

        if type_4_5_us_releases:
            return min(type_4_5_us_releases).strftime("%Y-%m-%d")
        elif type_4_5_global_releases:
            return min(type_4_5_global_releases).strftime("%Y-%m-%d")
        
        old_type_3_releases = [date for date in type_3_releases if date < current_date - timedelta(days=180)]
        if old_type_3_releases:
            return max(old_type_3_releases).strftime("%Y-%m-%d")

        if general_release_date and general_release_date < current_date - timedelta(days=180):
            return general_release_date.strftime("%Y-%m-%d")
        
        logging.debug("No valid type 4, 5 US or global release or old type 3 release found, marking as Unknown.")
        return 'Unknown'

    elif media_type == 'tv':
        air_date = parse_date(media_details.get('airDate'))
        return air_date.strftime("%Y-%m-%d") if air_date else 'Unknown'
    
    else:
        logging.error(f"Unknown media type: {media_type}")
        return 'Unknown'


def get_overseerr_cookies(overseerr_url: str) -> api.cookies.RequestsCookieJar:
    session = api.Session()
    session.get(overseerr_url)
    return session.cookies

def get_overseerr_show_details(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, cookies: api.cookies.RequestsCookieJar) -> Dict[str, Any]:
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    response = api.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

def get_overseerr_show_episodes(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, season_number: int, cookies: api.cookies.RequestsCookieJar) -> Dict[str, Any]:
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}/season/{season_number}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    response = api.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

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

def imdb_to_tmdb(overseerr_url: str, overseerr_api_key: str, imdb_id: str, media_type: str) -> Optional[int]:
    headers = get_overseerr_headers(overseerr_api_key)
    search_url = f"{overseerr_url}/api/v1/search?query=imdb%3A{imdb_id}"
    
    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        if data['results']:
            # Filter results based on media_type
            matching_results = [result for result in data['results'] if result.get('mediaType') == media_type]
            
            if matching_results:
                return matching_results[0].get('id')  # Return the TMDB ID of the first matching result
            else:
                logging.warning(f"No results found for IMDB ID: {imdb_id} with media type: {media_type}")
                return None
        else:
            logging.warning(f"No results found for IMDB ID: {imdb_id}")
            return None
    except api.exceptions.RequestException as e:
        logging.error(f"Error converting IMDB ID to TMDB ID: {e}")
        return None

def get_imdb_id_if_missing(item: Dict[str, Any], overseerr_url: str, overseerr_api_key: str, cookies: api.cookies.RequestsCookieJar) -> Optional[str]:
    if 'imdb_id' in item:
        return item['imdb_id']
    
    if 'tmdb_id' not in item or 'media_type' not in item:
        logging.warning(f"Cannot retrieve IMDb ID without TMDB ID and media type: {item}")
        return None
    
    tmdb_id = item['tmdb_id']
    media_type = item['media_type']
    
    if media_type == 'movie':
        details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
    elif media_type == 'tv':
        details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
    else:
        logging.warning(f"Unknown media type: {media_type}")
        return None
    
    if details:
        imdb_id = details.get('externalIds', {}).get('imdbId')
        if imdb_id:
            item['imdb_id'] = imdb_id
            logging.debug(f"Retrieved missing IMDb ID: {imdb_id}")
            return imdb_id
    
    logging.warning(f"Could not retrieve IMDb ID for item: {item}")
    return None

def get_media_type_if_missing(item: Dict[str, Any], overseerr_url: str, overseerr_api_key: str) -> str:
    if 'media_type' not in item:
        item['media_type'] = get_media_type(overseerr_url, overseerr_api_key, item['imdb_id'])
        logging.debug(f"Retrieved missing media_type: {item['media_type']}")
    return item['media_type']

def get_tmdb_id_if_missing(item: Dict[str, Any], overseerr_url: str, overseerr_api_key: str) -> int:
    if 'tmdb_id' not in item:
        item['tmdb_id'] = imdb_to_tmdb(overseerr_url, overseerr_api_key, item['imdb_id'], item['media_type'])
        logging.debug(f"Retrieved missing tmdb_id: {item['tmdb_id']}")
    return item['tmdb_id']

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
        item['release_date'] = get_release_date(details, item['media_type'])
        logging.debug(f"Retrieved missing release_date: {item['release_date']}")
    return item['release_date']

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return {'movies': [], 'episodes': []}

    cookies = get_overseerr_cookies(overseerr_url)
    processed_items = {'movies': [], 'episodes': []}

    for item in media_items:
        logging.debug(f"Processing item: {item}")

        if 'tmdb_id' not in item:
            tmdb_id = get_tmdb_id_if_missing(item, overseerr_url, overseerr_api_key)
            if not tmdb_id:
                logging.warning(f"Could not find TMDB ID for item: {item}")
                continue
            item['tmdb_id'] = tmdb_id

        if 'imdb_id' not in item:
            imdb_id = get_imdb_id_if_missing(item, overseerr_url, overseerr_api_key, cookies)
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

            absolute_episode_number = 0
            for season_number, episode_count in season_episode_counts.items():
                for episode_number in range(1, episode_count + 1):
                    absolute_episode_number += 1
                    if (season_number, episode_number) not in existing_episode_set:
                        episode_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], season_number, cookies)
                        episode = next((ep for ep in episode_details.get('episodes', []) if ep['episodeNumber'] == episode_number), None)
                        
                        if episode:
                            episode_item = {
                                'imdb_id': item['imdb_id'],
                                'tmdb_id': item['tmdb_id'],
                                'title': item['title'],
                                'year': item['year'],
                                'season_number': season_number,
                                'episode_number': episode_number,
                                'episode_title': episode.get('name', 'Unknown Episode Title'),
                                'release_date': get_release_date(episode, 'tv'),
                                'media_type': 'episode',
                                'genres': ['anime'] if is_anime else []
                            }
                            logging.debug(f"Created episode item: {episode_item}")
                            processed_items['episodes'].append(episode_item)
                            logging.debug(f"Added episode: S{season_number}E{episode_number}")
                        else:
                            logging.warning(f"Could not find episode details for S{season_number}E{episode_number} of show: {item['title']}")

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
                new_release_date = get_release_date(details, media_type)
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
        if 'first_aired' in show_data:
            local_now = datetime.now()
            utc = datetime.strptime(show_data['first_aired'], "%Y-%m-%dT%H:%M:%S.000Z")
            utc = utc.replace(tzinfo=timezone.utc)
            local_time = (utc.astimezone(local_now.tzname())).strftime('%H:%M')
            return local_time
        else:
            logging.warning(f"No airtime found for show with IMDb ID: {imdb_id}. Using default airtime.")
            return DEFAULT_AIRTIME
    
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching show data from Trakt: {e}. Using default airtime.")
        return DEFAULT_AIRTIME