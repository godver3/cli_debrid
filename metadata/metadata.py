import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
import sys, os
import json
import time
from settings import get_setting
import content_checkers.trakt as trakt
import re
import pytz
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import get_setting
from cli_battery.app.direct_api import DirectAPI
from cli_battery.app.trakt_metadata import TraktMetadata

def parse_json_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

def get_metadata(imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None, item_media_type: Optional[str] = None) -> Dict[str, Any]:

    if not imdb_id and not tmdb_id:
        raise ValueError("Either imdb_id or tmdb_id must be provided")

    # Convert TMDB ID to IMDb ID if necessary
    if tmdb_id and not imdb_id:
        logging.info(f"Converting TMDB ID {tmdb_id} to IMDb ID")
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id))
        if not imdb_id:
            logging.error(f"Could not find IMDb ID for TMDB ID {tmdb_id}")
            return {}
        logging.info(f"Converted TMDB ID {tmdb_id} to IMDb ID {imdb_id}")

    media_type = item_media_type.lower() if item_media_type else 'movie'
    logging.info(f"Processing item as {media_type}")
    
    try:
        if media_type == 'movie':
            logging.info(f"Fetching movie metadata for IMDb ID: {imdb_id}")
            metadata, _ = DirectAPI.get_movie_metadata(imdb_id)
        else:
            logging.info(f"Fetching TV show metadata for IMDb ID: {imdb_id}")
            metadata, _ = DirectAPI.get_show_metadata(imdb_id)

        if not metadata:
            logging.warning(f"No metadata returned for IMDb ID: {imdb_id}")
            return {}
       
        # If metadata is a string, try to parse it as JSON
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse metadata string as JSON for IMDb ID: {imdb_id}")
                return {}

        if not isinstance(metadata, dict):
            logging.error(f"Unexpected metadata format for IMDb ID: {imdb_id}. Expected dict, got {type(metadata)}")
            return {}

        processed_metadata = {
            'imdb_id': imdb_id,  # Default to the input imdb_id
            'tmdb_id': tmdb_id,  # Default to the input tmdb_id
            'title': metadata.get('title', 'Unknown Title'),
            'year': None,
            'genres': [],
            'runtime': None
        }

        # Handle the 'ids' field
        ids = metadata.get('ids', {})
        if isinstance(ids, dict):
            processed_metadata['imdb_id'] = ids.get('imdb') or imdb_id
            processed_metadata['tmdb_id'] = ids.get('tmdb') or tmdb_id
        elif isinstance(ids, str):
            parsed_ids = parse_json_string(ids)
            if isinstance(parsed_ids, dict):
                processed_metadata['imdb_id'] = parsed_ids.get('imdb') or imdb_id
                processed_metadata['tmdb_id'] = parsed_ids.get('tmdb') or tmdb_id

        # Handle 'year' field
        year = metadata.get('year')
        if isinstance(year, int):
            processed_metadata['year'] = year
        elif isinstance(year, str) and year.isdigit():
            processed_metadata['year'] = int(year)

        # Handle 'genres' field
        genres = metadata.get('genres', [])
        if isinstance(genres, list):
            processed_metadata['genres'] = genres
        elif isinstance(genres, str):
            processed_metadata['genres'] = genres.split(',') if genres else []

        # Handle 'runtime' field
        runtime = metadata.get('runtime')
        if isinstance(runtime, int):
            processed_metadata['runtime'] = runtime
        elif isinstance(runtime, str) and runtime.isdigit():
            processed_metadata['runtime'] = int(runtime)

        logging.info(f"Processed metadata: {processed_metadata}")

        logging.info(f"Genres: {processed_metadata['genres']}")
        logging.info("Checking for anime genre")
        is_anime = 'anime' in [genre.lower() for genre in processed_metadata['genres']]
        processed_metadata['genres'] = ['anime'] if is_anime else processed_metadata['genres']

        if media_type == 'movie':
            processed_metadata['release_date'] = get_release_date(metadata, imdb_id)
        elif media_type == 'tv':
            processed_metadata['first_aired'] = parse_date(metadata.get('first_aired'))
            processed_metadata['seasons'] = metadata.get('seasons', {})

        return processed_metadata

    except Exception as e:
        logging.error(f"Unexpected error fetching metadata for IMDb ID {imdb_id}: {str(e)}", exc_info=True)
        return {}

def create_episode_item(show_item: Dict[str, Any], season_number: int, episode_number: int, episode_data: Dict[str, Any], is_anime: bool) -> Dict[str, Any]:
    logging.info(f"Creating episode item for {show_item['title']} season {season_number} episode {episode_number} airtime {show_item.get('airs', {}).get('time', '19:00')}")
    
    # Parse the first_aired date
    first_aired_utc = parse_date(episode_data.get('first_aired'))
    
    # Convert UTC to local timezone if a valid date is available
    if first_aired_utc and first_aired_utc != 'Unknown':
        utc_dt = datetime.strptime(first_aired_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        local_tz = pytz.timezone(time.tzname[0])
        local_dt = utc_dt.astimezone(local_tz)
        release_date = local_dt.strftime("%Y-%m-%d")
    else:
        release_date = 'Unknown'
    
    logging.info(f"Local TZ: {time.tzname[0]} Release date: {release_date}")

    return {
        'imdb_id': show_item['imdb_id'],
        'tmdb_id': show_item['tmdb_id'],
        'title': show_item['title'],
        'year': show_item['year'],
        'season_number': int(season_number),
        'episode_number': int(episode_number),
        'episode_title': episode_data.get('title', f"Episode {episode_number}"),
        'release_date': release_date,
        'media_type': 'episode',
        'genres': ['anime'] if is_anime else show_item.get('genres', []),
        'runtime': episode_data.get('runtime') or show_item.get('runtime'),
        'airtime': show_item.get('airs', {}).get('time', '19:00')
    }

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    processed_items = {'movies': [], 'episodes': []}
    trakt_metadata = TraktMetadata()

    for index, item in enumerate(media_items, 1):
        try:
            if not trakt_metadata._check_rate_limit():
                logging.warning("Trakt rate limit reached. Waiting for 5 minutes before continuing.")
                time.sleep(300)  # Wait for 5 minutes

            metadata = get_metadata(imdb_id=item.get('imdb_id'), tmdb_id=item.get('tmdb_id'), item_media_type=item.get('media_type'))
            if not metadata:
                logging.warning(f"Could not fetch metadata for item: {item}")
                continue

            if item['media_type'].lower() == 'movie':
                processed_items['movies'].append(metadata)
            elif item['media_type'].lower() in ['tv', 'show']:
                is_anime = 'anime' in [genre.lower() for genre in metadata.get('genres', [])]
                
                seasons = metadata.get('seasons', {})
                for season_number, season_data in seasons.items():
                    episodes = season_data.get('episodes', {})
                    for episode_number, episode_data in episodes.items():
                        episode_item = create_episode_item(
                            metadata, 
                            int(season_number), 
                            int(episode_number), 
                            episode_data,
                            is_anime
                        )
                        processed_items['episodes'].append(episode_item)

        except Exception as e:
            logging.error(f"Error processing item {item}: {str(e)}", exc_info=True)

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes")
    return processed_items

def get_release_date(media_details: Dict[str, Any], imdb_id: Optional[str] = None) -> str:
    if not imdb_id:
        logging.warning("Attempted to get release date with None IMDB ID")
        return media_details.get('released', 'Unknown')

    release_dates, _ = DirectAPI.get_movie_release_dates(imdb_id)
    logging.info(f"Processing release dates for IMDb ID: {imdb_id}")

    if not release_dates:
        logging.warning(f"No release dates found for IMDb ID: {imdb_id}")
        return media_details.get('released', 'Unknown')

    logging.debug(f"Release dates: {release_dates}")
    
    current_date = datetime.now()
    digital_physical_releases = []
    theatrical_releases = []
    premiere_releases = []
    all_releases = []

    for country, country_releases in release_dates.items():
        for release in country_releases:
            release_date_str = release.get('date')
            if release_date_str:
                try:
                    release_date = datetime.strptime(release_date_str, "%Y-%m-%d")
                    all_releases.append(release_date)
                    release_type = release.get('type', 'unknown').lower()
                    if release_type in ['digital', 'physical']:
                        digital_physical_releases.append(release_date)
                    elif release_type == 'theatrical':
                        theatrical_releases.append(release_date)
                    elif release_type == 'premiere':
                        premiere_releases.append(release_date)
                except ValueError:
                    logging.warning(f"Invalid date format: {release_date_str}")

    if digital_physical_releases:
        return min(digital_physical_releases).strftime("%Y-%m-%d")

    old_theatrical_releases = [date for date in theatrical_releases if date < current_date - timedelta(days=180)]
    if old_theatrical_releases:
        return max(old_theatrical_releases).strftime("%Y-%m-%d")

    old_premiere_releases = [date for date in premiere_releases if date < current_date - timedelta(days=730)]  # 24 months
    if old_premiere_releases:
        return max(old_premiere_releases).strftime("%Y-%m-%d")

    # If we've reached this point, there are no suitable release dates
    logging.warning(f"No valid release date found for IMDb ID: {imdb_id}.")
    return "Unknown"

def parse_date(date_str: Optional[str]) -> Optional[str]:
    if date_str is None:
        return None

    date_formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]

    for date_format in date_formats:
        try:
            return datetime.strptime(date_str, date_format).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue

    logging.warning(f"Unable to parse date: {date_str}")
    return None

def get_imdb_id_if_missing(item: Dict[str, Any]) -> Optional[str]:
    if 'imdb_id' in item:
        return item['imdb_id']
    
    if 'tmdb_id' not in item:
        logging.warning(f"Cannot retrieve IMDb ID without TMDB ID: {item}")
        return None
    
    tmdb_id = item['tmdb_id']
    
    imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id))
    return imdb_id

def refresh_release_dates():
    from database import get_all_media_items, update_release_date_and_state
    logging.info("Starting refresh_release_dates function")
    
    logging.info("Fetching items to refresh")
    items_to_refresh = get_all_media_items(state="Unreleased") + get_all_media_items(state="Wanted") + get_all_media_items(state="Sleeping")
    logging.info(f"Found {len(items_to_refresh)} items to refresh")

    for index, item in enumerate(items_to_refresh, 1):
        try:
            # Convert sqlite3.Row to dict
            item_dict = dict(item)
            
            title = item_dict.get('title', 'Unknown Title')
            media_type = item_dict.get('type', 'Unknown Type').lower()
            imdb_id = item_dict.get('imdb_id')
            season_number = item_dict.get('season_number')
            episode_number = item_dict.get('episode_number')

            logging.info(f"Processing item {index}/{len(items_to_refresh)}: {title} ({media_type}) - IMDb ID: {imdb_id}")
            
            if not imdb_id:
                logging.warning(f"Skipping item {index} due to missing imdb_id")
                continue

            if media_type == 'movie':
                metadata, _ = DirectAPI.get_movie_metadata(imdb_id)
            else:
                metadata, _ = DirectAPI.get_show_metadata(imdb_id)
            
            if metadata:
                logging.info("Getting release date")
                #logging.info(f"Metadata: {metadata}")
                if media_type == 'movie':
                    new_release_date = get_release_date(metadata, imdb_id)

                    # Check Trakt for early releases if setting is enabled
                    trakt_early_releases = get_setting('Scraping', 'trakt_early_releases', False)
                    if trakt_early_releases:
                        logging.info("Checking Trakt for early releases")
                        trakt_id = trakt.fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
                        if trakt_id and isinstance(trakt_id, list) and len(trakt_id) > 0:
                            trakt_id = str(trakt_id[0]['movie']['ids']['trakt'])
                            trakt_lists = trakt.fetch_items_from_trakt(f"/movies/{trakt_id}/lists/personal/popular")
                            for trakt_list in trakt_lists:
                                if re.search(r'(latest|new).*?(releases)', trakt_list['name'], re.IGNORECASE):
                                    logging.info(f"Movie found in early release list: {trakt_list['name']}")
                                    new_release_date = datetime.now().strftime("%Y-%m-%d")
                                
                else:
                    if season_number is not None and episode_number is not None:
                        episode_data = metadata.get('seasons', {}).get(str(season_number), {}).get('episodes', {}).get(str(episode_number), {})
                        new_release_date = parse_date(episode_data.get('first_aired'))
                    else:
                        new_release_date = parse_date(metadata.get('first_aired'))
                logging.info(f"New release date: {new_release_date}")

                if new_release_date == "Unknown" or new_release_date is None:
                    new_state = "Wanted"
                else:
                    release_date = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                    today = datetime.now().date()
                    new_state = "Wanted" if release_date <= today else "Unreleased"

                logging.info(f"New state: {new_state}")

                if new_state != item_dict['state'] or new_release_date != item_dict['release_date']:
                    logging.info("Updating release date and state in database")
                    update_release_date_and_state(item_dict['id'], new_release_date, new_state)
                    logging.info(f"Updated: {title} has a release date of: {new_release_date}")
                else:
                    logging.info("No changes needed for this item")
            else:
                logging.warning(f"Could not fetch metadata for {title}")

        except Exception as e:
            logging.error(f"Error processing item {index}: {str(e)}", exc_info=True)
            continue

    logging.info("Finished refresh_release_dates function")

def get_episode_count_for_seasons(imdb_id: str, seasons: List[int]) -> int:
    show_metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    all_seasons = show_metadata.get('seasons', {})
    return sum(all_seasons.get(str(season), {}).get('episode_count', 0) for season in seasons)

def get_all_season_episode_counts(imdb_id: str) -> Dict[int, int]:
    show_metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    all_seasons = show_metadata.get('seasons', {})
    return {int(season): data['episode_count'] for season, data in all_seasons.items() if season != '0'}

def get_show_airtime_by_imdb_id(imdb_id: str) -> str:
    DEFAULT_AIRTIME = "19:00"
    show_metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    if show_metadata is None:
        logging.warning(f"Failed to retrieve show metadata for IMDb ID: {imdb_id}")
        return DEFAULT_AIRTIME
    return show_metadata.get('airs', {}).get('time', DEFAULT_AIRTIME)

def test_metadata_processing():
    test_items = [
        # Movies
        {'imdb_id': 'tt0111161', 'media_type': 'movie'},  # The Shawshank Redemption
        {'imdb_id': 'tt0068646', 'media_type': 'movie'},  # The Godfather
        {'tmdb_id': 155, 'media_type': 'movie'},  # The Dark Knight

        # TV Shows
        {'imdb_id': 'tt0944947', 'media_type': 'tv'},  # Game of Thrones
        {'imdb_id': 'tt0903747', 'media_type': 'tv'},  # Breaking Bad
        {'tmdb_id': 1396, 'media_type': 'tv'},  # Breaking Bad (using TMDB ID)
    ]

    processed_data = process_metadata(test_items)

    print("Processed Movies:")
    for movie in processed_data['movies']:
        print(f"- {movie['title']} ({movie['year']}) - IMDb: {movie['imdb_id']}, TMDB: {movie['tmdb_id']}")

    print("\nProcessed TV Show Episodes:")
    for episode in processed_data['episodes'][:10]:  # Limiting to first 10 episodes for brevity
        print(f"- {episode['title']} S{episode['season_number']:02d}E{episode['episode_number']:02d}: {episode['episode_title']}")

    print(f"\nTotal movies processed: {len(processed_data['movies'])}")
    print(f"Total episodes processed: {len(processed_data['episodes'])}")

def extract_tmdb_id(data):
    if isinstance(data, dict):
        ids = data.get('ids')
        if isinstance(ids, str):
            try:
                ids = json.loads(ids.replace("'", '"'))  # Convert single quotes to double quotes
            except json.JSONDecodeError:
                logging.error(f"Failed to parse 'ids' string: {ids}")
                return None
        elif not isinstance(ids, dict):
            logging.error(f"Unexpected 'ids' format: {type(ids)}")
            return None
        
        tmdb_id = ids.get('tmdb')
        return tmdb_id
    logging.error(f"Unexpected data format for IMDb ID: {type(data)}")
    return None

def get_tmdb_id_and_media_type(imdb_id: str) -> Tuple[Optional[int], Optional[str]]:
    def parse_data(data):
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed
            except json.JSONDecodeError:
                logging.error(f"Failed to parse data as JSON for IMDb ID {imdb_id}")
                return None
        return data

    # Try to get movie metadata
    movie_data, _ = DirectAPI.get_movie_metadata(imdb_id)
    movie_data = parse_data(movie_data)

    if movie_data is not None:
        tmdb_id = extract_tmdb_id(movie_data)
        if tmdb_id:
            return int(tmdb_id), 'movie'
    
    # If not a movie, try to get show metadata
    show_data, _ = DirectAPI.get_show_metadata(imdb_id)
   
    show_data = parse_data(show_data)
    if show_data is not None:
        tmdb_id = extract_tmdb_id(show_data)
        if tmdb_id:
            logging.info(f"Found TMDB ID for show: {tmdb_id}")
            return int(tmdb_id), 'tv'
    
    logging.error(f"Could not determine media type for IMDb ID {imdb_id}")
    return None, None
    
def get_runtime(imdb_id: str, media_type: str) -> Optional[int]:
    if media_type == 'movie':
        metadata, _ = DirectAPI.get_movie_metadata(imdb_id)
    else:
        metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    
    runtime = metadata.get('runtime')
    
    if runtime is not None:
        try:
            return int(runtime)
        except ValueError:
            logging.warning(f"Invalid runtime value for {imdb_id}: {runtime}")
    
    return None

def get_episode_airtime(imdb_id: str) -> Optional[str]:
    metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    airs = metadata.get('airs', {})
    airtime = airs.get('time')
   
    if airtime:
        try:
            parsed_time = datetime.strptime(airtime, "%H:%M")
            logging.info(f"Parsed airtime: {parsed_time} for {imdb_id}")
            return parsed_time.strftime("%H:%M")
        except ValueError:
            logging.warning(f"Invalid airtime format for {imdb_id}: {airtime}")
    
    return None

def main():
    print("Testing metadata routes:")

    # Test movie metadata
    print("\n1. Get movie metadata:")
    movie_imdb_id = "tt0111161"  # The Shawshank Redemption
    movie_metadata, source = DirectAPI.get_movie_metadata(movie_imdb_id)
    print(f"Movie metadata for {movie_imdb_id}:")
    print(json.dumps(movie_metadata, indent=2))
    print(f"Source: {source}")

    # Test show metadata
    print("\n2. Get show metadata:")
    show_imdb_id = "tt1190634"  
    show_metadata, source = DirectAPI.get_show_metadata(show_imdb_id)
    print(f"Show metadata for {show_imdb_id}:")
    print(json.dumps(show_metadata, indent=2))
    print(f"Source: {source}")

    # Test TMDB to IMDB conversion
    print("\n3. Get IMDb ID from TMDB ID:")
    tmdb_id = "155"  # The Dark Knight
    imdb_id, source = DirectAPI.tmdb_to_imdb(tmdb_id)
    print(f"IMDb ID for TMDB ID {tmdb_id}: {imdb_id}")
    print(f"Source: {source}")

if __name__ == "__main__":
    main()