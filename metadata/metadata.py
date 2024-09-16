import logging
import grpc
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from settings import get_setting
import metadata_service_pb2
import metadata_service_pb2_grpc
import json

REQUEST_TIMEOUT = 15  # seconds

def get_metadata_stub():
    channel = grpc.insecure_channel('localhost:50051')
    return metadata_service_pb2_grpc.MetadataServiceStub(channel)

def parse_json_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

def get_metadata_battery_url(endpoint: str) -> str:
    return f"{get_setting('Metadata Battery', 'url')}/api/{endpoint}"

def api_request(endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    logging.warning("api_request is deprecated and should be replaced with gRPC calls")
    url = get_metadata_battery_url(endpoint)
    try:
        stub = get_metadata_stub()
        if 'movie/metadata' in endpoint:
            response = stub.GetMovieMetadata(metadata_service_pb2.IMDbRequest(imdb_id=params['imdb_id']))
        elif 'show/metadata' in endpoint:
            response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=params['imdb_id']))
        elif 'movie/release_dates' in endpoint:
            response = stub.GetMovieReleaseDates(metadata_service_pb2.IMDbRequest(imdb_id=params['imdb_id']))
        elif 'show/seasons' in endpoint:
            response = stub.GetShowSeasons(metadata_service_pb2.IMDbRequest(imdb_id=params['imdb_id']))
        elif 'tmdb_to_imdb' in endpoint:
            response = stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id=params['tmdb_id']))
        else:
            logging.error(f"Unsupported endpoint: {endpoint}")
            return {}
        return {k: parse_json_string(v) for k, v in response.metadata.items()} if hasattr(response, 'metadata') else {'imdb_id': response.imdb_id}
    except grpc.RpcError as e:
        logging.error(f"gRPC error fetching data for {endpoint}: {str(e)}")
        return {}

def get_metadata(imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None, item_media_type: Optional[str] = None) -> Dict[str, Any]:
    if not imdb_id and not tmdb_id:
        raise ValueError("Either imdb_id or tmdb_id must be provided")

    stub = get_metadata_stub()

    if tmdb_id and not imdb_id:
        tmdb_response = stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id=str(tmdb_id)))
        imdb_id = tmdb_response.imdb_id
        if not imdb_id:
            logging.error(f"Could not find IMDb ID for TMDB ID {tmdb_id}")
            return {}

    media_type = item_media_type.lower() if item_media_type else 'movie'
    if media_type == 'tv':
        response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    else:
        response = stub.GetMovieMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))

    metadata = {k: parse_json_string(v) for k, v in response.metadata.items()}
    logging.debug(f"API response for IMDb ID {imdb_id}: {metadata}")

    # Process common fields
    metadata['imdb_id'] = metadata.get('ids', {}).get('imdb') or imdb_id
    metadata['tmdb_id'] = int(metadata.get('ids', {}).get('tmdb', 0)) or tmdb_id
    metadata['title'] = metadata.get('title', 'Unknown Title')
    
    # Update the year handling
    year = metadata.get('year')
    metadata['year'] = int(year) if year is not None else None
    
    metadata['genres'] = metadata.get('genres', [])
    metadata['runtime'] = metadata.get('runtime')

    is_anime = 'anime' in [genre.lower() for genre in metadata.get('genres', [])]
    metadata['genres'] = ['anime'] if is_anime else []
    logging.debug(f"{media_type.capitalize()} {metadata['title']} is{'not' if not is_anime else ''} tagged as anime. Genres: {metadata['genres']}")

    if media_type == 'movie':
        metadata['release_date'] = get_release_date(metadata)
    elif media_type == 'tv':
        metadata['first_aired'] = parse_date(metadata.get('first_aired'))
        seasons_response = stub.GetShowSeasons(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
        metadata['seasons'] = {season.season_number: {"episode_count": season.episode_count} for season in seasons_response.seasons}

    logging.debug(f"Extracted metadata: {metadata}")
    return metadata

def create_episode_item(show_item: Dict[str, Any], season_number: int, episode_number: str, episode_data: Dict[str, Any], is_anime: bool) -> Dict[str, Any]:
    return {
        'imdb_id': show_item['imdb_id'],
        'tmdb_id': show_item['tmdb_id'],
        'title': show_item['title'],
        'year': show_item['year'],
        'season_number': int(season_number),
        'episode_number': int(episode_number),
        'episode_title': episode_data.get('title', 'Unknown Episode Title'),
        'release_date': parse_date(episode_data.get('first_aired')) or show_item.get('first_aired'),
        'media_type': 'episode',
        'genres': ['anime'] if is_anime else [],
        'runtime': episode_data.get('runtime') or show_item.get('runtime'),
        'airtime': show_item.get('airs', {}).get('time', '19:00')
    }

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    processed_items = {'movies': [], 'episodes': []}
    stub = get_metadata_stub()

    for item in media_items:
        logging.debug(f"Processing item: {item}")

        try:
            metadata = get_metadata(imdb_id=item.get('imdb_id'), tmdb_id=item.get('tmdb_id'), item_media_type=item.get('media_type'))
            if not metadata:
                logging.warning(f"Could not fetch metadata for item: {item}")
                continue

            if item['media_type'].lower() == 'movie':
                processed_items['movies'].append(metadata)
            elif item['media_type'].lower() in ['tv', 'show']:
                seasons_response = stub.GetShowSeasons(metadata_service_pb2.IMDbRequest(imdb_id=metadata['imdb_id']))
                for season in seasons_response.seasons:
                    for episode_number in range(1, season.episode_count + 1):
                        episode_response = stub.GetEpisodeMetadata(metadata_service_pb2.EpisodeRequest(
                            imdb_id=metadata['imdb_id'],
                            season_number=season.season_number,
                            episode_number=episode_number
                        ))
                        episode_data = {k: parse_json_string(v) for k, v in episode_response.metadata.items()}
                        episode_item = create_episode_item(metadata, season.season_number, episode_number, episode_data, metadata['genres'])
                        processed_items['episodes'].append(episode_item)

        except Exception as e:
            logging.error(f"Error processing item {item}: {str(e)}", exc_info=True)

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes")
    return processed_items

def get_release_date(media_details: Dict[str, Any]) -> str:
    stub = get_metadata_stub()
    imdb_id = media_details.get('imdb_id')
    media_type = media_details.get('media_type', 'movie').lower()

    if media_type == 'movie':
        release_dates_response = stub.GetMovieReleaseDates(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
        release_dates = json.loads(release_dates_response.release_dates)
        logging.debug(f"Release dates for IMDb ID {imdb_id}: {release_dates}")
        
        current_date = datetime.now()
        digital_physical_releases = []
        theatrical_releases = []
        all_releases = []

        def process_release(release):
            if isinstance(release, dict):
                release_date_str = release.get('date')
                if release_date_str:
                    try:
                        release_date = datetime.strptime(release_date_str, "%Y-%m-%d")
                        all_releases.append(release_date)
                        release_type = release.get('type', 'unknown').lower()
                        if release_type in ['digital', 'physical']:
                            digital_physical_releases.append(release_date)
                        elif release_type in ['theatrical', 'premiere']:
                            theatrical_releases.append(release_date)
                    except ValueError:
                        logging.warning(f"Invalid date format: {release_date_str}")
            elif isinstance(release, str) and release != "battery":
                logging.warning(f"Unexpected release format: {release}")

        for item in release_dates:
            if isinstance(item, dict):
                for country_releases in item.values():
                    if isinstance(country_releases, list):
                        for release in country_releases:
                            process_release(release)
            elif isinstance(item, str) and item != "battery" and item != "trakt":
                logging.warning(f"Unexpected item in release_dates: {item}")

        # Prioritize release dates
        if digital_physical_releases:
            return min(digital_physical_releases).strftime("%Y-%m-%d")
        
        old_theatrical_releases = [date for date in theatrical_releases if date < current_date - timedelta(days=180)]
        if old_theatrical_releases:
            return max(old_theatrical_releases).strftime("%Y-%m-%d")

        old_releases = [date for date in all_releases if date < current_date - timedelta(days=180)]
        if old_releases:
            return max(old_releases).strftime("%Y-%m-%d")
        
        if all_releases:
            return min(all_releases).strftime("%Y-%m-%d")
        
        logging.warning(f"No valid release date found for IMDb ID: {imdb_id}")
        return 'Unknown'

    elif media_type in ['tv', 'show']:  # Handle both 'tv' and 'show'
        air_date = parse_date(media_details.get('first_aired'))
        return air_date if air_date else 'Unknown'
    
    else:
        logging.error(f"Unknown media type: {media_type}")
        return 'Unknown'

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
    
    stub = get_metadata_stub()
    tmdb_response = stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id=str(tmdb_id)))
    return tmdb_response.imdb_id

def refresh_release_dates():
    from database import get_all_media_items, update_release_date_and_state
    logging.info("Starting refresh_release_dates function")
    
    logging.info("Fetching items to refresh")
    items_to_refresh = get_all_media_items(state="Unreleased") + get_all_media_items(state="Wanted")
    logging.info(f"Found {len(items_to_refresh)} items to refresh")

    stub = get_metadata_stub()

    for index, item in enumerate(items_to_refresh, 1):
        logging.info(f"Processing item {index}/{len(items_to_refresh)}: {item['title']} (Type: {item['media_type']}, IMDb ID: {item['imdb_id']})")
        try:
            imdb_id = item['imdb_id']
            media_type = 'movie' if item['media_type'] == 'movie' else 'tv'

            logging.info(f"Fetching metadata for IMDb ID: {imdb_id}")
            if media_type == 'movie':
                response = stub.GetMovieMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
            else:
                response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
            metadata = {k: parse_json_string(v) for k, v in response.metadata.items()}

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
    stub = get_metadata_stub()
    seasons_response = stub.GetShowSeasons(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    all_seasons = {season.season_number: season.episode_count for season in seasons_response.seasons}
    return sum(all_seasons.get(season, 0) for season in seasons)

def get_all_season_episode_counts(imdb_id: str) -> Dict[int, int]:
    stub = get_metadata_stub()
    seasons_response = stub.GetShowSeasons(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    return {season.season_number: season.episode_count for season in seasons_response.seasons if season.season_number != 0}

def get_show_airtime_by_imdb_id(imdb_id: str) -> str:
    DEFAULT_AIRTIME = "19:00"
    stub = get_metadata_stub()
    response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    metadata = {k: parse_json_string(v) for k, v in response.metadata.items()}
    return metadata.get('airs', {}).get('time', DEFAULT_AIRTIME)

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

# Call the test function
if __name__ == "__main__":
    test_metadata_processing()

def get_tmdb_id_and_media_type(imdb_id: str) -> Tuple[Optional[int], Optional[str]]:
    stub = get_metadata_stub()
    
    # Try to get movie metadata
    movie_response = stub.GetMovieMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    if movie_response.metadata:
        movie_data = {k: parse_json_string(v) for k, v in movie_response.metadata.items()}
        return int(movie_data.get('ids', {}).get('tmdb', 0)), 'movie'
    
    # If not a movie, try to get show metadata
    show_response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    if show_response.metadata:
        show_data = {k: parse_json_string(v) for k, v in show_response.metadata.items()}
        return int(show_data.get('ids', {}).get('tmdb', 0)), 'tv'
    
    logging.error(f"Could not determine media type for IMDb ID {imdb_id}")
    return None, None
    
def get_runtime(imdb_id: str, media_type: str) -> Optional[int]:
    stub = get_metadata_stub()
    
    if media_type == 'movie':
        response = stub.GetMovieMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    else:
        response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    
    metadata = {k: parse_json_string(v) for k, v in response.metadata.items()}
    runtime = metadata.get('runtime')
    
    if runtime is not None:
        try:
            return int(runtime)
        except ValueError:
            logging.warning(f"Invalid runtime value for {imdb_id}: {runtime}")
    
    return None

def get_episode_airtime(imdb_id: str) -> Optional[str]:
    stub = get_metadata_stub()
    response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=imdb_id))
    
    metadata = {k: parse_json_string(v) for k, v in response.metadata.items()}
    airs = metadata.get('airs', {})
    airtime = airs.get('time')
    
    if airtime:
        try:
            parsed_time = datetime.strptime(airtime, "%H:%M")
            return parsed_time.strftime("%H:%M")
        except ValueError:
            logging.warning(f"Invalid airtime format for {imdb_id}: {airtime}")
    
    return None