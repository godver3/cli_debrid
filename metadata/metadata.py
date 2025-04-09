import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
import sys, os
import json
import time
from utilities.settings import get_setting
import re
import pytz
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utilities.settings import get_setting
from cli_battery.app.direct_api import DirectAPI
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.database import DatabaseManager
from database.database_reading import get_media_item_presence, get_all_media_items, get_show_episode_identifiers_from_db

# Initialize DirectAPI at module level
direct_api = DirectAPI()

# Initialize TraktMetadata if not already done globally for get_show_status
trakt_metadata_instance = TraktMetadata()

def parse_json_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

def get_tmdb_metadata(tmdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    """Fetch metadata directly from TMDB API."""
    tmdb_api_key = get_setting('TMDB', 'api_key')
    if not tmdb_api_key:
        logging.debug("No TMDB API key configured, skipping TMDB metadata fetch")
        return None

    base_url = "https://api.themoviedb.org/3"
    endpoint = f"/{'movie' if media_type.lower() == 'movie' else 'tv'}/{tmdb_id}"
    
    try:
        response = requests.get(
            f"{base_url}{endpoint}",
            params={'api_key': tmdb_api_key},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        # Extract relevant fields
        metadata = {
            'title': data.get('title') or data.get('name'),
            'year': int(data.get('release_date', '')[:4]) if data.get('release_date') else 
                   int(data.get('first_air_date', '')[:4]) if data.get('first_air_date') else None,
            'genres': [genre['name'] for genre in data.get('genres', [])],
            'runtime': data.get('runtime') or data.get('episode_run_time', [None])[0],
            'release_date': data.get('release_date') or data.get('first_air_date'),
            'overview': data.get('overview'),
            'original_language': data.get('original_language'),
            'vote_average': data.get('vote_average'),
            'tmdb_id': tmdb_id
        }
        
        logging.debug(f"Successfully fetched TMDB metadata for ID {tmdb_id}: {metadata}")
        return metadata
        
    except Exception as e:
        logging.error(f"Error fetching TMDB metadata for ID {tmdb_id}: {str(e)}")
        return None

def get_metadata(imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None, item_media_type: Optional[str] = None, original_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:

    if not imdb_id and not tmdb_id:
        raise ValueError("Either imdb_id or tmdb_id must be provided")

    # Try to get TMDB metadata first if we have a TMDB ID
    tmdb_metadata = None
    if tmdb_id:
        tmdb_metadata = get_tmdb_metadata(str(tmdb_id), item_media_type or 'movie')

    # Convert TMDB ID to IMDb ID if necessary
    if tmdb_id and not imdb_id:
        # Skip TMDB to IMDb conversion for episodes since we only need show-level metadata
        if item_media_type.lower() == 'episode':
            logging.debug(f"Skipping TMDB to IMDb conversion for episode with TMDB ID {tmdb_id}")
            return {}
            
        if item_media_type == "tv":
            converted_item_media_type = "show"
        else:
            converted_item_media_type = item_media_type

        # Check if any Jackett scrapers are enabled and if title contains UFC before attempting conversion
        from queues.config_manager import load_config
        config = load_config()
        has_enabled_jackett = False
        
        for instance, settings in config.get('Scrapers', {}).items():
            if isinstance(settings, dict):
                if settings.get('type') == 'Jackett' and settings.get('enabled', False):
                    has_enabled_jackett = True
                    break
        
        # Get the title from TMDB metadata or original item
        title = ''
        if tmdb_metadata:
            title = tmdb_metadata.get('title', '')
        if not title and original_item:
            title = original_item.get('title', '')
        
        # Try to convert TMDB to IMDB
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type=converted_item_media_type)
        
        # If conversion failed, check if we can proceed with Jackett
        if not imdb_id:
            if not has_enabled_jackett or 'UFC' not in title.upper():
                logging.error(f"Could not find IMDb ID for TMDB ID {tmdb_id}. This is only supported for UFC content with Jackett enabled.")
                return {}
            else:
                logging.info(f"No IMDb ID found for UFC content with TMDB ID {tmdb_id}, proceeding with Jackett scraper(s)")
                # Return metadata from TMDB if available, otherwise return minimal metadata
                if tmdb_metadata:
                    tmdb_metadata.update({
                        'tmdb_id': tmdb_id,
                        'content_source': original_item.get('content_source') if original_item else None,
                        'content_source_detail': original_item.get('content_source_detail') if original_item else None
                    })
                    return tmdb_metadata
                return {
                    'tmdb_id': tmdb_id,
                    'title': title or 'Unknown Title',
                    'year': original_item.get('year') if original_item else None,
                    'genres': original_item.get('genres', []) if original_item else [],
                    'runtime': None,
                    'airs': {},
                    'country': '',
                    'content_source': original_item.get('content_source') if original_item else None,
                    'content_source_detail': original_item.get('content_source_detail') if original_item else None
                }
        logging.info(f"Converted TMDB ID {tmdb_id} to IMDb ID {imdb_id}")

    media_type = item_media_type.lower() if item_media_type else 'movie'
    
    try:
        if media_type == 'movie':
            logging.info(f"Fetching movie metadata for IMDb ID: {imdb_id}")
            result = DirectAPI.get_movie_metadata(imdb_id)
            if result is None:
                logging.error(f"Failed to get movie metadata for IMDb ID: {imdb_id}")
                return {}
            metadata, _ = result
        else:
            logging.info(f"Fetching TV show metadata for IMDb ID: {imdb_id}")
            result = DirectAPI.get_show_metadata(imdb_id)
            if result is None:
                logging.error(f"Failed to get show metadata for IMDb ID: {imdb_id}")
                return {}
            metadata, _ = result

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
            'runtime': None,
            'airs': metadata.get('airs', {}),
            'country': (metadata.get('country') or '').lower(),  # Add country code, handling None
            # Preserve content source information if available
            'content_source': original_item.get('content_source') if original_item else None,
            'content_source_detail': original_item.get('content_source_detail') if original_item else None
        }
        logging.debug(f"Created processed_metadata with content_source_detail={processed_metadata.get('content_source_detail')}")

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

        #logging.info(f"Processed metadata: {processed_metadata}")

        logging.info(f"Genres: {processed_metadata['genres']}")
        logging.info("Checking for anime genre")
        is_anime = 'anime' in [genre.lower() for genre in processed_metadata['genres']]
        processed_metadata['genres'] = ['anime'] if is_anime else processed_metadata['genres']
        logging.info(f"Processed metadata: {processed_metadata}")

        if media_type == 'movie':
            processed_metadata['release_date'] = get_release_date(metadata, imdb_id)
        elif media_type == 'tv':
            processed_metadata['first_aired'] = parse_date(metadata.get('first_aired'))
            # Preserve the full seasons data structure
            seasons = metadata.get('seasons', {})
            if isinstance(seasons, dict):
                processed_metadata['seasons'] = seasons
            else:
                logging.error(f"Unexpected seasons data type: {type(seasons)}")
                processed_metadata['seasons'] = {}
            #logging.info(f"Season data structure: {json.dumps(seasons, indent=2)}")

        return processed_metadata

    except Exception as e:
        logging.error(f"Unexpected error fetching metadata for IMDb ID {imdb_id}: {str(e)}", exc_info=True)
        return {}

def create_episode_item(show_item: Dict[str, Any], season_number: int, episode_number: int, episode_data: Dict[str, Any], is_anime: bool) -> Dict[str, Any]:
    logging.info(f"Creating episode item for {show_item['title']} season {season_number} episode {episode_number}")
    logging.debug(f"Show item details: content_source_detail={show_item.get('content_source_detail')}")

    # Get the first_aired datetime string
    first_aired_str = episode_data.get('first_aired')
    release_date = 'Unknown'
    airtime = '19:00' # Default fallback airtime

    if first_aired_str:
        try:
            # Parse the UTC datetime string
            first_aired_utc = datetime.strptime(first_aired_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

            # Convert UTC to local timezone using cross-platform function
            local_tz = _get_local_timezone()
            premiere_dt_local_tz = first_aired_utc.astimezone(local_tz)

            # Use the localized datetime for both date and time
            release_date = premiere_dt_local_tz.strftime("%Y-%m-%d")
            airtime = premiere_dt_local_tz.strftime("%H:%M")
            logging.info(f"Calculated local release date: {release_date}, local airtime: {airtime} from UTC {first_aired_str}")

        except ValueError as e:
            logging.warning(f"Invalid datetime format or timezone conversion error: {first_aired_str} - {e}")
            # Keep release_date as 'Unknown' and airtime as default '19:00'
    else:
        # No first_aired string, try to use show's default airtime if available
        airs = show_item.get('airs', {})
        default_airtime_str = airs.get('time')
        if default_airtime_str:
            try:
                 # Try parsing with seconds first (HH:MM:SS)
                try:
                    air_time_obj = datetime.strptime(default_airtime_str, "%H:%M:%S").time()
                except ValueError:
                    # If that fails, try without seconds (HH:MM)
                    air_time_obj = datetime.strptime(default_airtime_str, "%H:%M").time()
                airtime = air_time_obj.strftime("%H:%M")
                logging.info(f"Using show's default airtime: {airtime} as fallback.")
            except ValueError:
                 logging.warning(f"Invalid show default airtime format: {default_airtime_str}. Using default 19:00.")
                 # airtime remains '19:00'
        else:
            logging.info("No first_aired data and no default show airtime. Using default 19:00.")
            # airtime remains '19:00'

    episode_item = {
        'imdb_id': show_item['imdb_id'],
        'tmdb_id': show_item['tmdb_id'],
        'title': show_item['title'],
        'year': show_item['year'],
        'season_number': int(season_number),
        'episode_number': int(episode_number),
        'episode_title': episode_data.get('title', f"Episode {episode_number}"),
        'release_date': release_date, # Calculated local date
        'media_type': 'episode',
        'genres': ['anime'] if is_anime else show_item.get('genres', []),
        'runtime': episode_data.get('runtime') or show_item.get('runtime'),
        'airtime': airtime, # Calculated local time
        'country': show_item.get('country', '').lower(),  # Add country code from show metadata
        'content_source': show_item.get('content_source'),  # Preserve content source
        'content_source_detail': show_item.get('content_source_detail')  # Preserve content source detail
    }
    
    logging.debug(f"Created episode item with content_source_detail={episode_item.get('content_source_detail')}")
    return episode_item

def _get_local_timezone():
    """Get the local timezone in a cross-platform way with multiple fallbacks."""
    # Suppress tzlocal debug messages
    import logging
    logging.getLogger('tzlocal').setLevel(logging.WARNING)
    
    from tzlocal import get_localzone
    from utilities.settings import get_setting
    from datetime import timezone
    import os
    import re
    
    try:
        def is_valid_timezone(tz_str):
            """Check if a timezone string is valid by attempting to create a ZoneInfo object."""
            if not tz_str:
                return False
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz_str)
                return True
            except Exception:
                return False
        
        def fix_common_timezone_errors(tz_str):
            """Fix common timezone format errors."""
            if not tz_str:
                return tz_str
                
            # Common timezone format errors and their corrections
            corrections = {
                # Common continent pluralization errors
                r'^Americas/': 'America/',
                r'^Europes/': 'Europe/',
                r'^Asias/': 'Asia/',
                r'^Africas/': 'Africa/',
                r'^Australias/': 'Australia/',
                
                # Common format corrections
                r'^EST$': 'America/New_York',
                r'^CST$': 'America/Chicago',
                r'^MST$': 'America/Denver',
                r'^PST$': 'America/Los_Angeles',
                r'^GMT$': 'Etc/GMT',
                
                # Remove any spaces in timezone strings
                r'\s+': '',
            }
            
            # Apply corrections
            corrected_tz = tz_str
            for pattern, replacement in corrections.items():
                corrected_tz = re.sub(pattern, replacement, corrected_tz)
                
            if corrected_tz != tz_str:
                logging.warning(f"Corrected timezone format from '{tz_str}' to '{corrected_tz}'")
                
            return corrected_tz
        
        # First try: Check for override in settings
        timezone_override = get_setting('Debug', 'timezone_override', '')
        if timezone_override:
            # Try to fix common format errors
            corrected_timezone = fix_common_timezone_errors(timezone_override)
            if is_valid_timezone(corrected_timezone):
                try:
                    from zoneinfo import ZoneInfo
                    return ZoneInfo(corrected_timezone)
                except Exception as e:
                    logging.error(f"Error creating ZoneInfo for override {corrected_timezone}: {e}")
        
        # Second try: Try getting from environment variable
        tz_env = os.environ.get('TZ')
        if tz_env:
            # Try to fix common format errors
            corrected_tz_env = fix_common_timezone_errors(tz_env)
            if is_valid_timezone(corrected_tz_env):
                try:
                    from zoneinfo import ZoneInfo
                    return ZoneInfo(corrected_tz_env)
                except Exception as e:
                    logging.error(f"Error creating ZoneInfo from TZ env {corrected_tz_env}: {e}")
        
        # Third try: Try tzlocal with exception handling
        try:
            local_tz = get_localzone()
            if hasattr(local_tz, 'zone'):
                corrected_zone = fix_common_timezone_errors(local_tz.zone)
                if is_valid_timezone(corrected_zone):
                    try:
                        from zoneinfo import ZoneInfo
                        return ZoneInfo(corrected_zone)
                    except Exception:
                        pass
            return local_tz
        except Exception as e:
            logging.error(f"Error getting local timezone from tzlocal: {str(e)}")
        
        # Fourth try: Try common timezone files directly
        common_zones = ['America/New_York', 'UTC', 'Etc/UTC']
        for zone in common_zones:
            if is_valid_timezone(zone):
                try:
                    from zoneinfo import ZoneInfo
                    return ZoneInfo(zone)
                except Exception:
                    continue
    
    except Exception as e:
        logging.error(f"Unexpected error in timezone detection: {str(e)}")
    
    # Final fallback: Always return UTC if everything else fails
    logging.warning("All timezone detection methods failed, falling back to UTC")
    return timezone.utc

def update_existing_episodes_states(conn, tmdb_id: str, all_requested_seasons: set):
    """Update states of existing episodes based on requested seasons."""
    try:
        # Get all existing episodes for this show in one query
        cursor = conn.execute('''
            SELECT id, season_number, state 
            FROM media_items 
            WHERE tmdb_id = ? AND type = 'episode'
        ''', (tmdb_id,))
        existing_episodes = cursor.fetchall()

        # Create lists for bulk updates
        to_unblacklist = []  # Episodes to change from Blacklisted to Wanted
        to_blacklist = []    # Episodes to change from Wanted to Blacklisted

        for db_episode in existing_episodes:
            season_number = db_episode['season_number']
            if season_number in all_requested_seasons:
                if db_episode['state'] == 'Blacklisted':
                    to_unblacklist.append(db_episode['id'])
            else:
                if db_episode['state'] == 'Wanted':
                    to_blacklist.append(db_episode['id'])

        # Perform bulk updates
        if to_unblacklist:
            placeholders = ','.join('?' * len(to_unblacklist))
            conn.execute(f'''
                UPDATE media_items 
                SET state = 'Wanted', blacklisted_date = NULL 
                WHERE id IN ({placeholders})
            ''', to_unblacklist)
            logging.info(f"De-blacklisted {len(to_unblacklist)} existing episodes")

        if to_blacklist:
            placeholders = ','.join('?' * len(to_blacklist))
            conn.execute(f'''
                UPDATE media_items 
                SET state = 'Blacklisted', blacklisted_date = ? 
                WHERE id IN ({placeholders})
            ''', [datetime.now(timezone.utc)] + to_blacklist)
            logging.info(f"Blacklisted {len(to_blacklist)} existing episodes")

        conn.commit()
    except Exception as e:
        logging.error(f"Error updating existing episodes states: {str(e)}")
        conn.rollback()

def get_physical_release_date(imdb_id: Optional[str] = None) -> Optional[str]:
    """Get the earliest physical release date for a movie."""
    if not imdb_id:
        return None

    release_dates, _ = DirectAPI.get_movie_release_dates(imdb_id)
    if not release_dates:
        return None

    physical_releases = []
    for country, country_releases in release_dates.items():
        for release in country_releases:
            if release.get('type', '').lower() == 'physical' and release.get('date'):
                try:
                    release_date = datetime.strptime(release.get('date'), "%Y-%m-%d")
                    physical_releases.append(release_date)
                except ValueError:
                    continue

    return min(physical_releases).strftime("%Y-%m-%d") if physical_releases else None

def get_show_status(imdb_id: str) -> str:
    """Get the status of a TV show from Trakt."""
    # Use the existing trakt_metadata_instance
    global trakt_metadata_instance
    try:
        # Ensure rate limit is checked if needed within TraktMetadata methods
        # trakt_metadata_instance._check_rate_limit() # Assuming TraktMetadata handles this internally
        
        search_result = trakt_metadata_instance._search_by_imdb(imdb_id)
        if search_result and search_result.get('type') == 'show':
            show = search_result.get('show')
            if show and show.get('ids') and show['ids'].get('slug'):
                slug = show['ids']['slug']
                
                # Get the full show data using the slug
                url = f"{trakt_metadata_instance.base_url}/shows/{slug}?extended=full"
                response = trakt_metadata_instance._make_request(url)
                if response and response.status_code == 200:
                    show_data = response.json()
                    status = show_data.get('status', '').lower()
                    logging.debug(f"Trakt status for {imdb_id}: {status}")
                    return status
                else:
                     logging.warning(f"Failed to get full show data for slug {slug} (IMDb: {imdb_id}). Status code: {response.status_code if response else 'N/A'}")
            else:
                logging.warning(f"Could not find show slug in search result for IMDb ID: {imdb_id}")
        elif search_result:
            logging.warning(f"Trakt search for IMDb ID {imdb_id} did not return a show. Type: {search_result.get('type')}")
        else:
            logging.warning(f"No Trakt search result found for IMDb ID: {imdb_id}")

    except Exception as e:
        logging.error(f"Error getting show status for {imdb_id}: {str(e)}")
    return '' # Return empty string on failure

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    from database.database_writing import update_blacklisted_date, update_media_item
    from database.core import get_db_connection
    from database.wanted_items import add_wanted_items
    from queues.run_program import program_runner
    from database.database_reading import get_media_item_presence, get_show_episode_identifiers_from_db # Ensure imports

    processed_items = {'movies': [], 'episodes': []}
    global trakt_metadata_instance

    for index, item in enumerate(media_items, 1):
        try:
            logging.debug(f"Processing item {index}: Type={item.get('media_type')}, IMDb={item.get('imdb_id')}, TMDB={item.get('tmdb_id')}, Detail={item.get('content_source_detail')}")
            
            # Check Trakt rate limit before fetching metadata
            if not trakt_metadata_instance._check_rate_limit():
                logging.warning("Trakt rate limit reached. Waiting for 5 minutes before continuing.")
                time.sleep(300)  # Wait for 5 minutes

            # Fetch metadata first
            metadata = get_metadata(
                imdb_id=item.get('imdb_id'),
                tmdb_id=item.get('tmdb_id'),
                item_media_type=item.get('media_type'),
                original_item=item
            )
            if not metadata:
                logging.warning(f"Could not fetch metadata for item: {item}. Skipping.")
                continue

            item_media_type_lower = item.get('media_type', '').lower()
            show_imdb_id = metadata.get('imdb_id')
            show_tmdb_id = metadata.get('tmdb_id')

            # --- Skip Logic ---
            if item_media_type_lower == 'movie':
                # Movie skip logic remains the same
                presence_state = get_media_item_presence(imdb_id=show_imdb_id, tmdb_id=show_tmdb_id)
                if presence_state == "Collected":
                    logging.info(f"Skipping collected movie: {metadata.get('title')} (IMDb: {show_imdb_id}, TMDB: {show_tmdb_id})")
                    continue
            elif item_media_type_lower in ['tv', 'show']:
                # Attempt to skip ended shows where all known episodes are already in the DB

                # 1. Ensure we have an IMDb ID for the status check
                if not show_imdb_id and show_tmdb_id:
                     logging.info(f"Attempting to convert TMDB ID {show_tmdb_id} to IMDb ID for status check.")
                     converted_imdb_id, _ = DirectAPI.tmdb_to_imdb(str(show_tmdb_id), media_type='show')
                     if converted_imdb_id:
                         show_imdb_id = converted_imdb_id
                         logging.info(f"Successfully converted TMDB:{show_tmdb_id} to IMDb:{show_imdb_id}")
                     else:
                         logging.warning(f"Could not convert TMDB ID {show_tmdb_id} to IMDb ID. Cannot perform 'ended' status check for skip logic.")
                
                # 2. Check Trakt status *first* if IMDb ID is available
                if show_imdb_id:
                    show_status = get_show_status(show_imdb_id)
                    
                    # 3. Only if status is 'ended', proceed to check episode presence
                    if show_status == 'ended':
                        logging.info(f"Show '{metadata.get('title')}' (IMDb: {show_imdb_id}) has status 'ended'. Checking if all episodes are present in DB.")
                        
                        seasons_metadata = metadata.get('seasons')
                        # Ensure we have seasons data and an ID (IMDb or TMDB) for the DB check
                        if seasons_metadata and isinstance(seasons_metadata, dict) and (show_imdb_id or show_tmdb_id):
                            try:
                                # Use the optimized function to get existing S/E tuples
                                existing_episodes_in_db = get_show_episode_identifiers_from_db(imdb_id=show_imdb_id, tmdb_id=show_tmdb_id)

                                all_metadata_episodes_found_in_db = True
                                total_metadata_episodes = 0
                                # Iterate through metadata to check if all episodes exist in DB set
                                for season_num_str, season_data in seasons_metadata.items():
                                    if not isinstance(season_data, dict) or 'episodes' not in season_data or not isinstance(season_data['episodes'], dict): continue
                                    try: season_num_int = int(season_num_str)
                                    except ValueError: continue

                                    for episode_num_str in season_data['episodes'].keys():
                                        total_metadata_episodes += 1
                                        try: episode_num_int = int(episode_num_str)
                                        except ValueError: continue

                                        if (season_num_int, episode_num_int) not in existing_episodes_in_db:
                                            all_metadata_episodes_found_in_db = False
                                            break
                                    if not all_metadata_episodes_found_in_db: break
                                
                                logging.debug(f"Checked {total_metadata_episodes} episodes from metadata. All found in DB: {all_metadata_episodes_found_in_db}")

                                # 4. If show is ended AND all episodes found, skip
                                if all_metadata_episodes_found_in_db and total_metadata_episodes > 0:
                                    logging.info(f"Skipping ended show '{metadata.get('title')}' (IMDb: {show_imdb_id}) as all {total_metadata_episodes} known episodes are already present in the database.")
                                    continue # Skip to the next item in the main loop
                                elif not all_metadata_episodes_found_in_db:
                                     logging.info(f"Show '{metadata.get('title')}' is ended, but not all episodes are present in DB. Proceeding.")
                                # else: total_metadata_episodes == 0 or other edge cases - proceed

                            except Exception as check_error:
                                logging.error(f"Error during episode presence check for ended show {show_imdb_id}: {check_error}. Proceeding.", exc_info=True)
                        elif not seasons_metadata or not isinstance(seasons_metadata, dict):
                            logging.warning(f"Cannot perform episode presence check for ended show {show_imdb_id}: Invalid or missing 'seasons' data. Proceeding.")
                        elif not show_imdb_id and not show_tmdb_id:
                             logging.warning(f"Cannot perform episode presence check for ended show {metadata.get('title')}: No IMDb or TMDB ID. Proceeding.")
                             
                    # If status is not 'ended' or unknown, proceed directly to processing
                    elif show_status: # If status is known but not 'ended'
                         logging.info(f"Show '{metadata.get('title')}' (IMDb: {show_imdb_id}) status is '{show_status}' (not 'ended'). Proceeding with processing.")
                    else: # If status check failed or returned empty
                         logging.warning(f"Could not determine Trakt status for show '{metadata.get('title')}' (IMDb: {show_imdb_id}). Proceeding with processing.")
                
                # If IMDb ID was not available for status check, proceed directly to processing
                elif not show_imdb_id:
                    logging.info(f"No IMDb ID for show '{metadata.get('title')}' (TMDB: {show_tmdb_id}). Cannot check 'ended' status for skipping. Proceeding with processing.")


            # --- Original Processing Logic (if not skipped) ---
            logging.debug(f"Proceeding with processing for {item_media_type_lower} {metadata.get('title')}")
            if item_media_type_lower == 'movie':
                # Movie processing logic remains the same
                physical_release_date = get_physical_release_date(show_imdb_id) # Use ID from metadata
                if physical_release_date:
                    metadata['physical_release_date'] = physical_release_date
                processed_items['movies'].append(metadata)
                logging.debug(f"Added movie with content_source_detail={metadata.get('content_source_detail')}")
            
            elif item_media_type_lower in ['tv', 'show']:
                # TV Show processing logic using the already fetched metadata
                is_anime = 'anime' in [genre.lower() for genre in metadata.get('genres', [])]
                
                # Overseerr check remains the same
                conn = get_db_connection()
                try:
                    cursor = conn.execute('''
                        SELECT COUNT(*) as count FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?)
                        AND type = 'episode'
                        AND requested_season = TRUE
                    ''', (show_imdb_id, show_tmdb_id)) # Use IDs from metadata
                    result = cursor.fetchone()
                    has_requested_episodes = result['count'] > 0 if result else False
                finally:
                    conn.close()

                if has_requested_episodes and not item.get('requested_seasons'):
                    logging.info(f"Skipping show {metadata.get('title', 'Unknown')} as it is managed by Overseerr and this request didn't specify seasons.")
                    continue

                seasons = metadata.get('seasons') # Use already fetched seasons data
                # Handle potential 'None' string or non-dict type
                if seasons == 'None' or not isinstance(seasons, dict):
                    logging.error(f"Invalid seasons data found for show {show_imdb_id or show_tmdb_id} even after initial checks.")
                    # Attempt refetch logic
                    if DatabaseManager.remove_metadata(show_imdb_id or str(show_tmdb_id)):
                        logging.info(f"Retrying metadata fetch for show {show_imdb_id or show_tmdb_id}")
                        refetch_id = show_imdb_id or show_tmdb_id
                        if refetch_id:
                            new_metadata, _ = DirectAPI.get_show_metadata(str(refetch_id))
                            if new_metadata and isinstance(new_metadata, dict):
                                seasons = new_metadata.get('seasons')
                                if not seasons or not isinstance(seasons, dict):
                                     logging.error("Refetch failed to yield valid seasons data.")
                                     continue # Skip item if refetch failed
                            else:
                                logging.error("Refetch failed to get valid metadata.")
                                continue # Skip item if refetch failed
                        else:
                            logging.error("Cannot retry metadata fetch, no valid ID found")
                            continue # Skip item if cannot refetch
                    # If DatabaseManager.remove_metadata returned False, log and continue.
                    else:
                        logging.error("Failed to remove potentially bad metadata cache. Proceeding without valid seasons data may cause issues.")
                        continue # Skip item if cannot remove bad cache

                # Determine seasons to process (existing logic)
                # This part now runs if the initial seasons data was valid, or if refetch was successful
                requested_seasons = item.get('requested_seasons', [])
                content_source_id = item.get('content_source_detail') # Use item's detail
                allow_specials_for_source = False
                if content_source_id:
                    from queues.config_manager import load_config
                    config = load_config()
                    content_sources_config = config.get('Content Sources', {})
                    source_settings = content_sources_config.get(content_source_id, {})
                    if isinstance(source_settings, dict):
                        # Correct indentation
                        allow_specials_for_source = source_settings.get('allow_specials', False)
                        logging.debug(f"'allow_specials' for source '{content_source_id}': {allow_specials_for_source}")
                    else:
                         logging.warning(f"Settings for source '{content_source_id}' are not a dictionary. Assuming allow_specials=False.")
                else:
                    logging.debug(f"No content_source_detail found for item {metadata.get('title')}, allow_specials defaults to False.")

                if requested_seasons:
                    logging.info(f"Processing specific requested seasons {requested_seasons} for show {metadata.get('title', 'Unknown')}")
                    seasons_to_process = requested_seasons
                else:
                    logging.info(f"Processing seasons for show {metadata.get('title', 'Unknown')} from non-Overseerr source (allow_specials={allow_specials_for_source})")
                    valid_season_keys = []
                    for s_key in seasons.keys():
                        try:
                            s_num = int(s_key)
                            if allow_specials_for_source or s_num != 0:
                                valid_season_keys.append(s_num)
                        except ValueError:
                             logging.warning(f"Skipping non-integer season key '{s_key}' during season processing.")
                    seasons_to_process = valid_season_keys

                # Process the determined seasons (existing logic)
                all_episodes = []
                for season_number in seasons_to_process:
                    season_data = seasons.get(str(season_number)) # Prefer string key first
                    if season_data is None:
                        season_data = seasons.get(season_number) # Try integer key

                    if season_data is None or not isinstance(season_data, dict):
                        logging.warning(f"Could not find or invalid season {season_number} data for show {show_imdb_id or show_tmdb_id}")
                        continue

                    episodes_in_season = season_data.get('episodes', {})
                    if not episodes_in_season or not isinstance(episodes_in_season, dict):
                        logging.warning(f"No valid episodes found for season {season_number}")
                        continue
                   
                    logging.info(f"Processing {len(episodes_in_season)} episodes for season {season_number}")
                    for episode_number_str, episode_data in episodes_in_season.items():
                        try:
                            episode_number = int(episode_number_str)
                            episode_item = create_episode_item(
                                metadata,
                                season_number,
                                episode_number,
                                episode_data,
                                is_anime
                            )
                            if requested_seasons:
                                episode_item['requested_season'] = True
                            all_episodes.append(episode_item)
                        except ValueError:
                             logging.error(f"Skipping episode with non-integer key '{episode_number_str}' in S{season_number} for show {show_imdb_id or show_tmdb_id}")
                             continue
                        except Exception as e:
                            logging.error(f"Error processing episode S{season_number:02d}E{episode_number_str} of show {show_imdb_id or show_tmdb_id}: {str(e)}")
                            continue

                processed_items['episodes'].extend(all_episodes)
                logging.info(f"Added {len(all_episodes)} episodes from {'requested seasons' if requested_seasons else 'source rules'} seasons")

                # Overseerr webhook logic (existing)
                if item.get('from_overseerr'):
                    from utilities.settings import get_all_settings
                    content_sources = get_all_settings().get('Content Sources', {})
                    overseerr_settings = next((data for source, data in content_sources.items() if source.startswith('Overseerr')), None)
                    if overseerr_settings and isinstance(overseerr_settings, dict):
                        # Correct indentation
                        versions = overseerr_settings.get('versions', {})
                        # Add content source to episodes
                        for episode in all_episodes:
                            episode['content_source'] = 'overseerr_webhook'
                            from content_checkers.content_source_detail import append_content_source_detail
                            episode = append_content_source_detail(episode, source_type='Overseerr')
                        add_wanted_items(all_episodes, versions)
                    else:
                         logging.warning("Could not find Overseerr settings or settings were invalid. Skipping add_wanted_items for Overseerr webhook.")


        except Exception as e:
            # Use the most specific identifier available (existing logic)
            show_id_for_error = (
                item.get('imdb_id') or 
                (metadata.get('imdb_id') if metadata else None) or
                (f"TMDB:{item.get('tmdb_id')}" if item.get('tmdb_id') else None) or
                 (f"TMDB:{metadata.get('tmdb_id')}" if metadata and metadata.get('tmdb_id') else 'Unknown')
            )
            logging.error(f"Error processing item for {show_id_for_error}: {str(e)}", exc_info=True)

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes after checks.")
    return processed_items

def get_release_date(media_details: Dict[str, Any], imdb_id: Optional[str] = None) -> str:
    if not media_details:
        logging.warning("No media details provided for release date")
        return 'Unknown'
        
    if not imdb_id:
        logging.warning("Attempted to get release date with None IMDB ID")
        return media_details.get('released', 'Unknown')

    release_dates, _ = DirectAPI.get_movie_release_dates(imdb_id)
    logging.info(f"Processing release dates for IMDb ID: {imdb_id}")

    if not release_dates:
        logging.warning(f"No release dates found for IMDb ID: {imdb_id}")
        return media_details.get('released', 'Unknown')

    #logging.debug(f"Release dates: {release_dates}")
    
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
                    if release_type in ['digital', 'physical', 'tv']:  
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
    import content_checkers.trakt as trakt
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
                if not metadata:
                    logging.warning(f"No metadata found for movie {title} ({imdb_id})")
                    new_release_date = 'Unknown'
                    new_physical_release_date = None
                else:
                    logging.info("Getting release date and physical release date")
                    new_release_date = get_release_date(metadata, imdb_id)
                    new_physical_release_date = get_physical_release_date(imdb_id)
                    logging.info(f"Physical release date: {new_physical_release_date}")

                # Store original values for comparison
                item_dict['early_release_original'] = item_dict.get('early_release', False)
                item_dict['physical_release_date_original'] = item_dict.get('physical_release_date')

                # Check Trakt for early releases if setting is enabled AND the item is not flagged to skip this check
                trakt_early_releases = get_setting('Scraping', 'trakt_early_releases', False)
                skip_early_release_check = item_dict.get('no_early_release', False)
                
                if trakt_early_releases and not skip_early_release_check:
                    logging.info(f"Checking Trakt for early releases for {title} ({imdb_id})")
                    trakt_id = trakt.fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
                    if trakt_id and isinstance(trakt_id, list) and len(trakt_id) > 0:
                        # Ensure we are checking the correct media type from Trakt search results
                        found_movie = False
                        for result in trakt_id:
                            if result.get('type') == 'movie':
                                trakt_movie_data = result.get('movie')
                                if trakt_movie_data and trakt_movie_data.get('ids') and trakt_movie_data['ids'].get('trakt'):
                                    trakt_id_num = str(trakt_movie_data['ids']['trakt'])
                                    logging.debug(f"Found Trakt movie ID {trakt_id_num} for {imdb_id}")
                                    trakt_lists = trakt.fetch_items_from_trakt(f"/movies/{trakt_id_num}/lists/personal/popular")
                                    if trakt_lists: # Ensure trakt_lists is not None
                                        for trakt_list in trakt_lists:
                                            if re.search(r'(latest|new).*?(releases)', trakt_list['name'], re.IGNORECASE):
                                                logging.info(f"Movie {title} ({imdb_id}) found in early release list: {trakt_list['name']}")
                                                item_dict['early_release'] = True
                                                found_movie = True
                                                break # Exit inner loop (lists)
                                    if found_movie:
                                        break # Exit outer loop (search results)
                                else:
                                     logging.warning(f"Trakt search result for {imdb_id} did not contain expected movie ID structure: {result}")
                        if not found_movie:
                             logging.info(f"Did not find {title} ({imdb_id}) in any relevant Trakt early release lists.")
                    else:
                        logging.info(f"No Trakt ID found for {imdb_id} via search, cannot check early release lists.")
                elif skip_early_release_check:
                    logging.info(f"Skipping Trakt early release check for {title} ({imdb_id}) due to no_early_release flag.")
                
                # Calculate new state for movies
                new_state = item_dict['state'] # Default to current state
                today = datetime.now().date()

                if item_dict.get('early_release', False):
                    new_state = "Wanted"
                    logging.info(f"Movie is an early release, setting state to Wanted")
                elif new_physical_release_date and new_physical_release_date != 'Unknown':
                    try:
                        physical_release_dt = datetime.strptime(new_physical_release_date, "%Y-%m-%d").date()
                        if physical_release_dt <= today:
                             new_state = "Wanted"
                             logging.info(f"Physical release date {physical_release_dt} is past, setting state to Wanted")
                        else:
                             new_state = "Unreleased"
                             logging.info(f"Physical release date {physical_release_dt} is in the future, setting state to Unreleased")
                    except ValueError:
                         logging.warning(f"Invalid physical release date format: {new_physical_release_date}. Keeping state as {new_state}")
                         new_state = "Wanted" # Or handle as appropriate
                elif new_release_date and new_release_date != 'Unknown':
                    try:
                        release_dt = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                        if release_dt <= today:
                            new_state = "Wanted"
                            logging.info(f"Release date {release_dt} is past, setting state to Wanted")
                        else:
                            new_state = "Unreleased"
                            logging.info(f"Release date {release_dt} is in the future, setting state to Unreleased")
                    except ValueError:
                        logging.warning(f"Invalid release date format: {new_release_date}. Keeping state as {new_state}")
                        new_state = "Wanted" # Or handle as appropriate
                else:
                    # If no valid dates and not early release, likely keep existing or set to Wanted
                    new_state = "Wanted" 
                    logging.info(f"No valid release dates found, setting state to Wanted")
                
                # For movies, airtime is not relevant
                new_airtime = None

            elif media_type == 'episode':
                metadata, _ = DirectAPI.get_show_metadata(imdb_id)
                logging.info(f"Processing metadata for {title} S{season_number}E{episode_number}")
                
                # Fetch the latest airtime for the show
                new_airtime = get_episode_airtime(imdb_id)
                logging.info(f"New airtime from metadata: {new_airtime}")
                
                if not metadata or not isinstance(metadata, dict):
                    logging.warning(f"Invalid or missing metadata for show {imdb_id}")
                    new_release_date = 'Unknown'
                else:
                    seasons = metadata.get('seasons', {})
                    if not isinstance(seasons, dict):
                        logging.warning(f"Invalid seasons data for show {imdb_id}")
                        new_release_date = 'Unknown'
                    else:
                        season_data = seasons.get(str(season_number), {})
                        if not isinstance(season_data, dict):
                            logging.warning(f"Invalid season data for show {imdb_id} season {season_number}")
                            new_release_date = 'Unknown'
                        else:
                            episodes = season_data.get('episodes', {})
                            if not isinstance(episodes, dict):
                                logging.warning(f"Invalid episodes data for show {imdb_id} season {season_number}")
                                new_release_date = 'Unknown'
                            else:
                                episode_data = episodes.get(str(episode_number))
                                if not episode_data or not isinstance(episode_data, dict):
                                    logging.warning(f"No valid data found for S{season_number}E{episode_number}")
                                    new_release_date = 'Unknown'
                                else:
                                    first_aired_str = episode_data.get('first_aired')
                                    logging.info(f"First aired date from metadata: {first_aired_str}")
                                    
                                    if first_aired_str:
                                        try:
                                            # Parse the UTC datetime string
                                            first_aired_utc = datetime.strptime(first_aired_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                                            first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

                                            # Convert UTC to local timezone
                                            local_tz = _get_local_timezone()
                                            local_dt = first_aired_utc.astimezone(local_tz)

                                            # Format the local date
                                            new_release_date = local_dt.strftime("%Y-%m-%d")
                                            logging.info(f"Calculated local release date {new_release_date} from UTC {first_aired_str}")
                                        except ValueError as e:
                                            logging.error(f"Invalid datetime format or conversion error: {first_aired_str} - Error: {e}")
                                            new_release_date = 'Unknown'
                                    else:
                                        logging.warning("No first_aired date found in episode data")
                                        new_release_date = 'Unknown'
                
                logging.info(f"New release date: {new_release_date}")

                # Calculate new state for episodes (moved calculation here for clarity)
                new_state = item_dict['state'] # Default to current state
                if new_release_date == "Unknown" or new_release_date is None:
                    new_state = "Wanted"
                    logging.info("Release date is Unknown, setting state to Wanted")
                else:
                    try:
                        release_date_dt = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                        today = datetime.now().date()

                        # If it's an early release, set to Wanted regardless of release date
                        # Note: early_release flag is typically for movies, but check just in case
                        if item_dict.get('early_release', False):
                            new_state = "Wanted"
                            logging.info(f"Episode marked as early release, setting state to Wanted")
                        # Otherwise, set to Wanted only if it's past the release date
                        else:
                            new_state = "Wanted" if release_date_dt <= today else "Unreleased"
                            logging.info(f"Episode release date is {release_date_dt}, today is {today}, setting state to {new_state}")
                    except ValueError:
                        logging.warning(f"Invalid release date format: {new_release_date}. Setting state to Wanted.")
                        new_state = "Wanted"

            # Check if any relevant field has changed before updating DB
            # Ensure all variables used here are defined for both movie and episode branches
            # Need to add check for no_early_release flag if it changes
            if (new_state != item_dict['state'] or
                new_release_date != item_dict.get('release_date') or # Use .get for safety
                (media_type == 'episode' and new_airtime != item_dict.get('airtime')) or # Only check airtime for episodes
                item_dict.get('early_release', False) != item_dict.get('early_release_original', False) or
                # Compare the current no_early_release state with its original state (if it existed)
                item_dict.get('no_early_release', False) != item_dict.get('no_early_release_original', False) or 
                (media_type == 'movie' and new_physical_release_date != item_dict.get('physical_release_date_original'))):

                logging.info(f"Changes detected for {title}. Current state: {item_dict['state']}, New state: {new_state}. Updating database.")
                # Store the original no_early_release value before potential update
                item_dict['no_early_release_original'] = item_dict.get('no_early_release', False)
                update_release_date_and_state(
                    item_dict['id'],
                    new_release_date,
                    new_state,
                    airtime=new_airtime,
                    early_release=item_dict.get('early_release', False),
                    physical_release_date=new_physical_release_date if media_type == 'movie' else None,
                    # Pass the current no_early_release value (which might be False if not set)
                    no_early_release=item_dict.get('no_early_release', False) 
                )
                log_msg = f"Updated: {title} has a release date of: {new_release_date}"
                if media_type == 'movie':
                     log_msg += f" and physical release date of: {new_physical_release_date}"
                if media_type == 'episode':
                     log_msg += f" and airtime of: {new_airtime}"
                logging.info(log_msg)
            else:
                logging.info("No changes needed for this item")

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
    logging.debug(f"Raw seasons data received from DirectAPI for {imdb_id}: {all_seasons}")
    return {int(season): data['episode_count'] for season, data in all_seasons.items()}

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

def get_media_country_code(imdb_id: str, media_type: str) -> Optional[str]:
    """
    Get the country code for a media item from metadata.
    Args:
        imdb_id: The IMDb ID of the media item
        media_type: Either 'movie' or 'tv'
    Returns:
        The two-letter country code in lowercase, or None if not found
    """
    try:
        if media_type == 'movie':
            metadata, _ = direct_api.get_movie_metadata(imdb_id)
        else:
            metadata, _ = direct_api.get_show_metadata(imdb_id)
        
        if metadata and isinstance(metadata, dict):
            country = metadata.get('country', '').lower()
            return country if country else None
        return None
    except Exception as e:
        logging.error(f"Error retrieving country code for {imdb_id}: {str(e)}")
        return None

def get_episode_airtime(imdb_id: str) -> Optional[str]:
    """Get the show's airtime converted to the user's local time."""
    DEFAULT_AIRTIME = "19:00" # Default if conversion fails
    try:
        metadata, _ = DirectAPI.get_show_metadata(imdb_id)
        if not metadata or not isinstance(metadata, dict):
            logging.warning(f"Could not retrieve valid metadata for show {imdb_id}")
            return DEFAULT_AIRTIME

        airs = metadata.get('airs')
        if not airs or not isinstance(airs, dict):
            logging.warning(f"No 'airs' data found in metadata for show {imdb_id}")
            return DEFAULT_AIRTIME

        time_str = airs.get('time')
        timezone_str = airs.get('timezone')

        if not time_str or not timezone_str:
            logging.warning(f"Missing time ('{time_str}') or timezone ('{timezone_str}') in 'airs' data for show {imdb_id}")
            return DEFAULT_AIRTIME

        # Get the show's timezone
        try:
            show_tz = ZoneInfo(timezone_str)
        except ZoneInfoNotFoundError:
            logging.error(f"Invalid timezone identifier '{timezone_str}' for show {imdb_id}. Falling back to default.")
            return DEFAULT_AIRTIME
        except Exception as e:
             logging.error(f"Error creating ZoneInfo for '{timezone_str}': {e}. Falling back to default.")
             return DEFAULT_AIRTIME

        # Parse the airtime string
        try:
            air_time_obj = datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            logging.error(f"Invalid airtime format '{time_str}' for show {imdb_id}. Falling back to default.")
            return DEFAULT_AIRTIME

        # Create a naive datetime object for today with the show's airtime
        now_naive = datetime.now()
        show_air_datetime_naive = datetime.combine(now_naive.date(), air_time_obj)

        # Make the naive datetime aware using the show's timezone
        show_air_datetime_aware = show_air_datetime_naive.replace(tzinfo=show_tz)

        # Get the user's local timezone
        local_tz = _get_local_timezone()
        if not local_tz:
             logging.error("Could not determine local timezone. Falling back to default airtime.")
             return DEFAULT_AIRTIME

        # Convert the show's airtime to the user's local timezone
        local_air_datetime = show_air_datetime_aware.astimezone(local_tz)

        # Format the time part
        local_airtime_str = local_air_datetime.strftime("%H:%M")
        logging.info(f"Converted airtime for {imdb_id}: {time_str} {timezone_str} -> {local_airtime_str} (local)")
        return local_airtime_str

    except Exception as e:
        logging.error(f"Error calculating local airtime for {imdb_id}: {str(e)}", exc_info=True)
        return DEFAULT_AIRTIME

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