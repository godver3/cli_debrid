import logging
from api_tracker import api
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from settings import get_setting
from database import get_all_media_items, update_release_date_and_state

REQUEST_TIMEOUT = 15  # seconds
TRAKT_API_URL = "https://api.trakt.tv"

def get_metadata(imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None) -> Dict[str, Any]:
    if not imdb_id and not tmdb_id:
        raise ValueError("Either imdb_id or tmdb_id must be provided")

    url = f"{get_setting('Metadata Battery', 'url')}/api/metadata/"
    
    if tmdb_id:
        try:
            response = api.get(f"{get_setting('Metadata Battery', 'url')}/api/tmdb_to_imdb/{tmdb_id}", timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            imdb_id = response.json().get('imdb_id')
            if not imdb_id:
                logging.error(f"Could not find IMDb ID for TMDB ID {tmdb_id}")
                return {}
        except api.exceptions.RequestException as e:
            logging.error(f"Error fetching IMDb ID for TMDB ID {tmdb_id}: {str(e)}")
            return {}

    url += imdb_id

    try:
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        metadata = data.get('metadata', {})
        metadata['type'] = metadata.get('type') or data.get('type')
        
        if not metadata['type']:
            logging.error(f"Unknown media type for IMDb ID {imdb_id}")
            return {}

        # Extract and process common fields
        metadata['imdb_id'] = metadata.get('ids', {}).get('imdb') or imdb_id
        metadata['tmdb_id'] = int(metadata.get('ids', {}).get('tmdb', 0)) or tmdb_id
        metadata['title'] = metadata.get('title', 'Unknown Title')
        metadata['year'] = int(metadata.get('year', 0)) or None
        metadata['release_date'] = get_release_date(metadata)
        metadata['genres'] = metadata.get('genres', [])

        if metadata['type'] == 'tv':
            # Fetch seasons data
            seasons_url = f"{get_setting('Metadata Battery', 'url')}/api/seasons/{imdb_id}"
            seasons_response = api.get(seasons_url, timeout=REQUEST_TIMEOUT)
            seasons_response.raise_for_status()
            seasons_data = seasons_response.json()
            metadata['seasons'] = seasons_data.get('seasons', {})

        return metadata

    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching metadata: {str(e)}")
        return {}

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
    media_type = media_details.get('type')  # Change this line
    
    if not media_type:
        logging.warning(f"Media type not found for IMDb ID: {imdb_id}. Defaulting to 'movie'.")
        media_type = 'movie'  # Default to 'movie' if type is not found

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
        air_date = parse_date(media_details.get('first_aired'))  # Change this line
        result = air_date.strftime("%Y-%m-%d") if air_date else 'Unknown'
        return result
    else:
        logging.error(f"Unknown media type: {media_type}")
        return 'Unknown'

def get_imdb_id_if_missing(item: Dict[str, Any]) -> Optional[str]:
    if 'imdb_id' in item:
        return item['imdb_id']
    
    if 'tmdb_id' not in item:
        logging.warning(f"Cannot retrieve IMDb ID without TMDB ID: {item}")
        return None
    
    tmdb_id = item['tmdb_id']
    
    url = f"{get_setting('Metadata Battery', 'url')}/api/tmdb_to_imdb/{tmdb_id}"
    
    try:
        response = api.get(f"{get_setting('Metadata Battery', 'url')}/api/tmdb_to_imdb/{tmdb_id}", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        imdb_id = response.json().get('imdb_id')
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching IMDb ID for TMDB ID {tmdb_id}: {str(e)}")
        return None

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    processed_items = {'movies': [], 'episodes': []}

    for item in media_items:
        logging.debug(f"Processing item: {item}")

        try:
            metadata = get_metadata(imdb_id=item.get('imdb_id'), tmdb_id=item.get('tmdb_id'))
            if not metadata:
                logging.warning(f"Could not fetch metadata for item: {item}")
                continue

            if metadata['type'] == 'movie':
                process_movie(item, metadata, processed_items)
            elif metadata['type'] == 'tv':
                process_tv_show(item, metadata, processed_items)
            else:
                logging.warning(f"Unknown media type for item: {item}")

            logging.debug(f"Processed item: {item}")

        except Exception as e:
            logging.error(f"Error processing item {item}: {str(e)}", exc_info=True)

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes")
    return processed_items

def process_movie(item: Dict[str, Any], metadata: Dict[str, Any], processed_items: Dict[str, List[Dict[str, Any]]]):
    item['title'] = item.get('title') or metadata.get('title', 'Unknown Title')
    item['year'] = item.get('year') or metadata.get('year', '')
    item['release_date'] = item.get('release_date') or get_release_date(metadata)
    
    genres = metadata.get('genres', [])
    is_anime = 'anime' in [genre.lower() for genre in genres]
    
    item['genres'] = ['anime'] if is_anime else []
    logging.debug(f"Movie {item['title']} is{'not' if not is_anime else ''} tagged as anime. Genres: {item['genres']}")
    
    processed_items['movies'].append(item)

def process_tv_show(item: Dict[str, Any], metadata: Dict[str, Any], processed_items: Dict[str, List[Dict[str, Any]]]):
    show_item = {
        'imdb_id': metadata.get('imdb_id'),
        'tmdb_id': metadata.get('tmdb_id'),
        'title': metadata.get('title', 'Unknown Title'),
        'year': metadata.get('year'),
        'release_date': metadata.get('release_date'),
        'genres': metadata.get('genres', []),
        'media_type': 'tv'
    }
    
    is_anime = 'anime' in [genre.lower() for genre in show_item['genres']]
    logging.debug(f"Show {show_item['title']} is{'not' if not is_anime else ''} tagged as anime. Genres: {show_item['genres']}")

    seasons = metadata.get('seasons', {})
    for season_number, season_data in seasons.items():
        for episode in season_data.get('episodes', []):
            episode_item = create_episode_item(show_item, season_number, episode, is_anime)
            processed_items['episodes'].append(episode_item)
            logging.debug(f"Added episode: S{season_number}E{episode['episode']}")

    logging.debug(f"Processed {len(processed_items['episodes'])} episodes for show: {show_item['title']}")

def create_episode_item(show_item: Dict[str, Any], season_number: int, episode: Dict[str, Any], is_anime: bool) -> Dict[str, Any]:
    return {
        'imdb_id': show_item['imdb_id'],
        'tmdb_id': show_item['tmdb_id'],
        'title': show_item['title'],
        'year': show_item['year'],
        'season_number': int(season_number),
        'episode_number': episode.get('episode'),
        'episode_title': episode.get('title', 'Unknown Episode Title'),
        'release_date': get_release_date(episode) or show_item['release_date'],
        'media_type': 'episode',
        'genres': ['anime'] if is_anime else []
    }
    
def refresh_release_dates():
    logging.info("Starting refresh_release_dates function")
    
    logging.info("Fetching items to refresh")
    items_to_refresh = get_all_media_items(state="Unreleased") + get_all_media_items(state="Wanted")
    logging.info(f"Found {len(items_to_refresh)} items to refresh")

    for index, item in enumerate(items_to_refresh, 1):
        logging.info(f"Processing item {index}/{len(items_to_refresh)}: {item['title']} (Type: {item['type']}, IMDb ID: {item['imdb_id']})")
        try:
            imdb_id = item['imdb_id']
            media_type = 'movie' if item['type'] == 'movie' else 'tv'

            logging.info(f"Fetching metadata for IMDb ID: {imdb_id}")
            metadata = get_metadata(imdb_id=imdb_id)

            if metadata:
                logging.info("Getting release date")
                new_release_date = get_release_date(metadata)
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
                logging.warning(f"Could not fetch metadata for {item['title']}")
        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}", exc_info=True)

    logging.info("Finished refresh_release_dates function")

def get_episode_count_for_seasons(imdb_id: str, seasons: List[int]) -> int:
    """
    Get the total episode count for specified seasons of a show using its IMDb ID.
    
    :param imdb_id: IMDb ID of the show
    :param seasons: List of season numbers to count episodes for
    :return: Total number of episodes across the specified seasons
    """
    total_episodes = 0

    try:
        url = f"{get_setting('Metadata Battery', 'url')}/api/seasons/{imdb_id}"
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        all_seasons = data.get('seasons', {})
        for season_number in seasons:
            season_data = all_seasons.get(str(season_number), {})
            episode_count = season_data.get('episode_count', 0)
            total_episodes += episode_count

        logging.debug(f"Total episodes for IMDb ID {imdb_id}, seasons {seasons}: {total_episodes}")
        return total_episodes

    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching season data from Metadata Battery: {e}")
        return 0
    except Exception as e:
        logging.error(f"Unexpected error in get_episode_count_for_seasons for IMDb ID {imdb_id}: {e}")
        return 0

def get_all_season_episode_counts(imdb_id: str) -> Dict[int, int]:
    """
    Get episode counts for all seasons of a show using its IMDb ID.
    
    :param imdb_id: IMDb ID of the show
    :return: Dictionary with season numbers as keys and episode counts as values
    """
    episode_counts = {}

    try:
        url = f"{get_setting('Metadata Battery', 'url')}/api/seasons/{imdb_id}"
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        seasons = data.get('seasons', {})
        for season_number, season_data in seasons.items():
            if season_number == '0':
                continue  # Skip special seasons
            episode_count = season_data.get('episode_count', 0)
            episode_counts[int(season_number)] = episode_count

        logging.debug(f"Episode counts for IMDb ID {imdb_id}: {episode_counts}")
        return episode_counts

    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching season data from Metadata Battery: {e}")
        return {}
    except Exception as e:
        logging.error(f"Unexpected error in get_all_season_episode_counts for IMDb ID {imdb_id}: {e}")
        return {}

def get_show_airtime_by_imdb_id(imdb_id: str) -> str:
    """
    Get the airtime of a show using its IMDb ID.
    
    :param imdb_id: IMDb ID of the show
    :return: Airtime as a string (e.g., "20:00"), or "19:00" if not available
    """
    DEFAULT_AIRTIME = "19:00"

    try:
        url = f"{get_setting('Metadata Battery', 'url')}/api/metadata/{imdb_id}"
        response = api.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        metadata = data.get('metadata', {})
        airs = metadata.get('airs', {})
        airtime = airs.get('time')

        if airtime:
            return airtime
        else:
            logging.warning(f"No airtime found for show with IMDb ID: {imdb_id}. Using default airtime.")
            return DEFAULT_AIRTIME

    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching show data from Metadata Battery: {e}. Using default airtime.")
        return DEFAULT_AIRTIME
    except Exception as e:
        logging.error(f"Unexpected error in get_show_airtime_by_imdb_id for IMDb ID {imdb_id}: {e}. Using default airtime.")
        return DEFAULT_AIRTIME