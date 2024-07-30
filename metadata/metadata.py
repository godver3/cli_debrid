import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from settings import get_setting
from database import get_all_media_items, update_release_date_and_state

REQUEST_TIMEOUT = 15  # seconds

def get_overseerr_headers(api_key: str) -> Dict[str, str]:
    return {
        'X-Api-Key': api_key,
        'Accept': 'application/json'
    }

def get_url(base_url: str, endpoint: str) -> str:
    return f"{base_url}{endpoint}"

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


def get_overseerr_cookies(overseerr_url: str) -> requests.cookies.RequestsCookieJar:
    session = requests.Session()
    session.get(overseerr_url)
    return session.cookies

def get_overseerr_show_details(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, cookies: requests.cookies.RequestsCookieJar) -> Dict[str, Any]:
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    response = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

def get_overseerr_show_episodes(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, season_number: int, cookies: requests.cookies.RequestsCookieJar) -> Dict[str, Any]:
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}/season/{season_number}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    response = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()

def get_overseerr_movie_details(overseerr_url: str, overseerr_api_key: str, tmdb_id: int, cookies: requests.cookies.RequestsCookieJar) -> Optional[Dict[str, Any]]:
    url = get_url(overseerr_url, f"/api/v1/movie/{tmdb_id}?language=en")
    headers = get_overseerr_headers(overseerr_api_key)
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
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
        response = requests.get(search_url, headers=headers)
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
    except requests.RequestException as e:
        logging.error(f"Error converting IMDB ID to TMDB ID: {e}")
        return None

def get_imdb_id_if_missing(item: Dict[str, Any], overseerr_url: str, overseerr_api_key: str, cookies: requests.cookies.RequestsCookieJar) -> Optional[str]:
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

        if 'media_type' not in item:
            media_type = get_media_type_if_missing(item, overseerr_url, overseerr_api_key)
            if not media_type:
                logging.warning(f"Could not determine media type for item: {item}")
                continue
            item['media_type'] = media_type

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

            if 'season_number' in item and 'episode_number' in item:
                # Process specific episode
                season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], item['season_number'], cookies)
                episode = next((ep for ep in season_details.get('episodes', []) if ep['episodeNumber'] == item['episode_number']), None)
                
                if episode:
                    episode_item = {
                        'imdb_id': item['imdb_id'],
                        'tmdb_id': item['tmdb_id'],
                        'title': item['title'],
                        'year': item['year'],
                        'season_number': item['season_number'],
                        'episode_number': item['episode_number'],
                        'episode_title': episode.get('name', 'Unknown Episode Title'),
                        'release_date': get_release_date(episode, 'tv'),
                        'media_type': 'episode'
                    }
                    processed_items['episodes'].append(episode_item)
                else:
                    logging.warning(f"Could not find episode details for item: {item}")
            else:
                # Process all episodes for all seasons
                for season in show_details.get('seasons', []):
                    season_number = season.get('seasonNumber')
                    if season_number == 0:
                        continue  # Skip season 0
                    
                    season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], season_number, cookies)
                    for episode in season_details.get('episodes', []):
                        episode_item = {
                            'imdb_id': item['imdb_id'],
                            'tmdb_id': item['tmdb_id'],
                            'title': item['title'],
                            'year': item['year'],
                            'season_number': season_number,
                            'episode_number': episode.get('episodeNumber'),
                            'episode_title': episode.get('name', 'Unknown Episode Title'),
                            'release_date': get_release_date(episode, 'tv'),
                            'media_type': 'episode'
                        }
                        processed_items['episodes'].append(episode_item)

        logging.debug(f"Processed item: {item}")

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes")
    for movie in processed_items['movies']:
        logging.debug(f"Processed movie: {movie}")

    for episode in processed_items['episodes']:
        logging.debug(f"Processed episode: {episode}")
        
    return processed_items
    
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
                    logging.info(f"{item['title']} has a release date of: {new_release_date}")
                
            else:
                logging.warning(f"Could not fetch details for {item['title']}")
        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}")
