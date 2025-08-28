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
from collections import defaultdict
import iso8601
from collections import Counter

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utilities.settings import get_setting
from cli_battery.app.direct_api import DirectAPI
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.database import DatabaseManager
from database.database_reading import get_media_item_presence, get_all_media_items, get_show_episode_identifiers_from_db, get_media_item_ids

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
            'runtime': data.get('runtime') or (data.get('episode_run_time') or [None])[0],
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

    # Enhanced logging for input parameters
    logging.debug(f"get_metadata called with: imdb_id='{imdb_id}', tmdb_id='{tmdb_id}', item_media_type='{item_media_type}'")
    if original_item:
        logging.debug(f"Original item details for call: title='{original_item.get('title')}', source='{original_item.get('content_source_detail')}'")

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
        imdb_id, conversion_source = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type=converted_item_media_type)
        
        # If conversion failed, check if we can proceed with Jackett
        if not imdb_id:
            if not has_enabled_jackett or 'UFC' not in title.upper():
                logging.error(f"Could not find IMDb ID for TMDB ID {tmdb_id}. This is only supported for UFC content with Jackett enabled. A metadata refresh might resolve this.")
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

    # Log the decision point for media_type
    media_type_decision_input = item_media_type.lower() if item_media_type else '<<<MISSING_OR_EMPTY>>>'
    logging.debug(f"For imdb_id='{imdb_id}', tmdb_id='{tmdb_id}', raw item_media_type was '{item_media_type}'. Effective input for type decision: '{media_type_decision_input}'")

    media_type = item_media_type.lower() if item_media_type else 'movie'
    
    try:
        if media_type == 'movie':
            logging.info(f"Fetching movie metadata for IMDb ID: {imdb_id} (Determined type: movie)")
            result = DirectAPI.get_movie_metadata(imdb_id)
            if result is None:
                logging.error(f"Failed to get movie metadata for IMDb ID: {imdb_id}")
                return {}
            metadata, _ = result
        else:
            logging.info(f"Fetching TV show metadata for IMDb ID: {imdb_id} (Determined type: {media_type})")
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
    # logging.debug(f"Creating episode item for {show_item['title']} season {season_number} episode {episode_number}")
    # logging.debug(f"Show item details: content_source_detail={show_item.get('content_source_detail')}")

    # Get the first_aired datetime string
    first_aired_str = episode_data.get('first_aired')
    release_date = 'Unknown'
    airtime = '19:00' # Default fallback airtime

    if first_aired_str:
        try:
            # Use iso8601 library for robust parsing
            first_aired_utc = iso8601.parse_date(first_aired_str)
            # iso8601.parse_date handles the 'Z' automatically, making it UTC

            # Ensure it's timezone-aware (should be redundant, but safe)
            if first_aired_utc.tzinfo is None:
                 first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

            # Convert UTC to local timezone using cross-platform function
            local_tz = _get_local_timezone()
            premiere_dt_local_tz = first_aired_utc.astimezone(local_tz)

            # Use the localized datetime for both date and time
            release_date = premiere_dt_local_tz.strftime("%Y-%m-%d")
            airtime = premiere_dt_local_tz.strftime("%H:%M")
            # logging.debug(f"Calculated local release date: {release_date}, local airtime: {airtime} from UTC {first_aired_str}")

        except (ValueError, iso8601.ParseError) as e: # Catch iso8601.ParseError too
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
                # logging.debug(f"Using show's default airtime: {airtime} as fallback.")
            except ValueError:
                 logging.warning(f"Invalid show default airtime format: {default_airtime_str}. Using default 19:00.")
                 # airtime remains '19:00'
        else:
            # logging.debug("No first_aired data and no default show airtime. Using default 19:00.")
            # airtime remains '19:00'
            pass

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
    
    # logging.debug(f"Created episode item with content_source_detail={episode_item.get('content_source_detail')}")
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

def get_theatrical_release_date(imdb_id: Optional[str] = None) -> Optional[str]:
    """Get the oldest theatrical release date for a movie."""
    if not imdb_id:
        return None

    release_dates, _ = DirectAPI.get_movie_release_dates(imdb_id)
    if not release_dates:
        return None

    theatrical_releases = []
    for country, country_releases in release_dates.items():
        for release in country_releases:
            if release.get('type', '').lower() in ['theatrical', 'theatrical (limited)', 'limited'] and release.get('date'):
                try:
                    release_date = datetime.strptime(release.get('date'), "%Y-%m-%d")
                    theatrical_releases.append(release_date)
                except ValueError:
                    continue

    return min(theatrical_releases).strftime("%Y-%m-%d") if theatrical_releases else None

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
    from database.database_reading import get_media_item_ids

    processed_items = {'movies': [], 'episodes': []}
    global trakt_metadata_instance
    global direct_api

    # This setting is primarily for add_wanted_items, but good to be aware of
    # enable_granular_version_additions = get_setting('Debug', 'enable_granular_version_additions', False)

    movie_imdb_ids_to_fetch = set()
    show_imdb_ids_to_fetch = set()
    items_by_imdb_id = defaultdict(list)
    items_by_tmdb_id_only = defaultdict(list)

    # --- Step 1: Collect IMDb IDs and organize items ---
    logging.debug(f"Starting metadata processing for {len(media_items)} items. Collecting IDs...")
    for item in media_items:
        imdb_id = item.get('imdb_id')
        tmdb_id = item.get('tmdb_id')
        media_type = item.get('media_type', '').lower()
        original_item_copy = item.copy()

        resolved_imdb = False
        if not imdb_id and tmdb_id:
            try:
                conversion_media_type = 'show' if media_type in ['tv', 'show', 'episode'] else 'movie'
                converted_imdb_id, _ = direct_api.tmdb_to_imdb(str(tmdb_id), media_type=conversion_media_type)
                if converted_imdb_id:
                    imdb_id = converted_imdb_id
                    item['imdb_id'] = imdb_id
                    logging.debug(f"Converted TMDB {tmdb_id} to IMDb {imdb_id} for {media_type if media_type != 'episode' else 'show'}")
                    resolved_imdb = True
                else:
                    logging.warning(f"Could not convert TMDB ID {tmdb_id} to IMDb ID for {media_type if media_type != 'episode' else 'show'}.")
                    items_by_tmdb_id_only[tmdb_id].append(original_item_copy)
                    continue
            except Exception as e:
                logging.error(f"Error during TMDB to IMDb conversion for TMDB ID {tmdb_id}: {e}")
                items_by_tmdb_id_only[tmdb_id].append(original_item_copy)
                continue

        if imdb_id:
            items_by_imdb_id[imdb_id].append(original_item_copy)
            if media_type == 'movie':
                movie_imdb_ids_to_fetch.add(imdb_id)
            elif media_type in ['tv', 'show', 'episode']:
                show_imdb_ids_to_fetch.add(imdb_id)
        elif tmdb_id and not resolved_imdb:
             items_by_tmdb_id_only[tmdb_id].append(original_item_copy)
        else:
            logging.warning(f"Skipping item from processing due to missing IMDb/TMDB ID: Title '{original_item_copy.get('title', 'N/A')}', Details: {original_item_copy.get('content_source_detail', 'N/A')}")

    # --- Step 2: Bulk Fetch Metadata ---
    bulk_movie_metadata = {}
    bulk_show_metadata = {}
    missing_movie_imdb_ids = set()
    missing_show_imdb_ids = set()

    if movie_imdb_ids_to_fetch:
        logging.debug(f"Bulk fetching metadata for {len(movie_imdb_ids_to_fetch)} movie IMDb IDs...")
        try:
            if not trakt_metadata_instance._check_rate_limit():
                logging.warning("Trakt rate limit potentially hit before bulk movie fetch.")
            fetched_movies = direct_api.get_bulk_movie_metadata(list(movie_imdb_ids_to_fetch))
            bulk_movie_metadata.update(fetched_movies)
            fetched_count = sum(1 for m in fetched_movies.values() if m)
            logging.debug(f"Successfully fetched metadata for {fetched_count} movies from battery.")
            missing_movie_imdb_ids = {id_ for id_ in movie_imdb_ids_to_fetch if bulk_movie_metadata.get(id_) is None}
            if missing_movie_imdb_ids:
                 logging.debug(f"{len(missing_movie_imdb_ids)} movie IMDb IDs not found in battery bulk fetch.")
        except Exception as e:
             logging.error(f"Error during bulk movie metadata fetch: {e}", exc_info=True)
             missing_movie_imdb_ids = movie_imdb_ids_to_fetch

    if show_imdb_ids_to_fetch:
        logging.debug(f"Bulk fetching metadata for {len(show_imdb_ids_to_fetch)} show IMDb IDs...")
        try:
            if not trakt_metadata_instance._check_rate_limit():
                logging.warning("Trakt rate limit potentially hit before bulk show fetch.")
            fetched_shows = direct_api.get_bulk_show_metadata(list(show_imdb_ids_to_fetch))
            bulk_show_metadata.update(fetched_shows)
            fetched_count = sum(1 for m in fetched_shows.values() if m)
            logging.debug(f"Successfully fetched metadata for {fetched_count} shows from battery.")
            missing_show_imdb_ids = {id_ for id_ in show_imdb_ids_to_fetch if bulk_show_metadata.get(id_) is None}
            if missing_show_imdb_ids:
                 logging.debug(f"{len(missing_show_imdb_ids)} show IMDb IDs not found in battery bulk fetch.")
        except Exception as e:
             logging.error(f"Error during bulk show metadata fetch: {e}", exc_info=True)
             missing_show_imdb_ids = show_imdb_ids_to_fetch

    # --- Step 2.5: Individual Fetch for Missing Items ---
    if missing_movie_imdb_ids or missing_show_imdb_ids:
        logging.debug(f"Attempting individual metadata fetch for {len(missing_movie_imdb_ids)} movies and {len(missing_show_imdb_ids)} shows missing from battery.")
        ids_to_fetch_individually = {}
        for id_ in missing_movie_imdb_ids:
             if id_ in items_by_imdb_id: ids_to_fetch_individually[id_] = items_by_imdb_id[id_][0]
        for id_ in missing_show_imdb_ids:
             if id_ in items_by_imdb_id: ids_to_fetch_individually[id_] = items_by_imdb_id[id_][0]

        fetched_individually_count = 0
        for imdb_id_val, representative_item_val in ids_to_fetch_individually.items():
            try:
                if not trakt_metadata_instance._check_rate_limit():
                    logging.warning("Trakt rate limit reached during individual fetches. Waiting (handled by TraktMetadata).")
                
                logging.debug(f"Fetching individual metadata for missing IMDb: {imdb_id_val}")
                fetched_metadata = get_metadata(
                    imdb_id=imdb_id_val,
                    tmdb_id=representative_item_val.get('tmdb_id'),
                    item_media_type=representative_item_val.get('media_type'),
                    original_item=representative_item_val
                )
                if fetched_metadata:
                    fetched_individually_count += 1
                    media_type_val = representative_item_val.get('media_type', '').lower()
                    if media_type_val == 'movie':
                        bulk_movie_metadata[imdb_id_val] = fetched_metadata
                    elif media_type_val in ['tv', 'show', 'episode']:
                         bulk_show_metadata[imdb_id_val] = fetched_metadata
                    logging.debug(f"Successfully fetched and added metadata for missing IMDb: {imdb_id_val}")
                else:
                     logging.warning(f"Individual fetch failed for missing IMDb: {imdb_id_val}")
            except Exception as e:
                logging.error(f"Error during individual metadata fetch for {imdb_id_val}: {e}", exc_info=True)
        logging.debug(f"Finished individual fetch process. Successfully fetched metadata for {fetched_individually_count} items.")

    # --- Step 3: Removed Pre-fetch DB presence/state info in bulk ---
    # db_item_states = {} # Removed

    # --- Step 4: Process Items Using Fetched Data ---
    logging.debug("Processing items using fetched metadata...")
    processed_count = 0
    processed_imdb_ids = set()

    for imdb_id, original_items_list in items_by_imdb_id.items():
        if imdb_id in processed_imdb_ids: continue
        processed_imdb_ids.add(imdb_id)

        representative_item = original_items_list[0]
        effective_metadata_fetch_type = representative_item.get('media_type', '').lower()
        if effective_metadata_fetch_type == 'episode':
            effective_metadata_fetch_type = 'show'

        metadata = None
        if effective_metadata_fetch_type == 'movie' and imdb_id in bulk_movie_metadata:
            metadata = bulk_movie_metadata[imdb_id]
        elif effective_metadata_fetch_type in ['tv', 'show'] and imdb_id in bulk_show_metadata:
            metadata = bulk_show_metadata[imdb_id]

            # --- Check for staleness from bulk data ---
            if metadata and 'item_updated_at' in metadata:
                from cli_battery.app.metadata_manager import MetadataManager
                if MetadataManager.is_metadata_stale(metadata['item_updated_at']):
                    logging.info(f"Bulk metadata for {imdb_id} is stale (updated: {metadata['item_updated_at']}). Forcing a refresh.")
                    try:
                        refreshed_metadata, _ = direct_api.force_refresh_metadata(imdb_id)
                        if refreshed_metadata:
                            logging.info(f"Successfully force-refreshed metadata for {imdb_id}.")
                            metadata = refreshed_metadata
                            bulk_show_metadata[imdb_id] = refreshed_metadata # Update cache
                        else:
                            logging.error(f"Force-refresh failed for stale item {imdb_id}. Proceeding with stale data.")
                    except Exception as e_refresh:
                        logging.error(f"Exception during force-refresh for stale item {imdb_id}: {e_refresh}", exc_info=True)
            # --- End staleness check ---

            if metadata and not isinstance(metadata.get('seasons'), dict):
                logging.warning(f"Bulk metadata for show {imdb_id} lacks structured seasons. Attempting re-fetch for structure...")
                try:
                    individual_metadata = get_metadata(
                        imdb_id=imdb_id,
                        tmdb_id=metadata.get('tmdb_id') or representative_item.get('tmdb_id'),
                        item_media_type='show',
                        original_item=representative_item
                    )
                    if individual_metadata and isinstance(individual_metadata.get('seasons'), dict):
                        logging.info(f"Successfully re-fetched structured metadata for {imdb_id} including seasons.")
                        metadata = individual_metadata
                        bulk_show_metadata[imdb_id] = metadata
                    else:
                        logging.error(f"Individual metadata re-fetch for {imdb_id} failed to provide structured seasons.")
                except Exception as e_ind:
                    logging.error(f"Error during individual metadata re-fetch for {imdb_id}: {e_ind}", exc_info=True)

        if not metadata:
            logging.warning(f"Could not retrieve metadata for IMDb ID: {imdb_id} (effective type: {effective_metadata_fetch_type}). Skipping {len(original_items_list)} related item(s).")
            continue
        
        for item_from_input_list in original_items_list:
            current_item_metadata = metadata.copy()
            item_media_type_lower = item_from_input_list.get('media_type', '').lower()

            processed_count += 1
            
            try:
                current_item_metadata['content_source'] = item_from_input_list.get('content_source')
                current_item_metadata['content_source_detail'] = item_from_input_list.get('content_source_detail')
                current_item_metadata['imdb_id'] = imdb_id
                current_item_metadata['tmdb_id'] = item_from_input_list.get('tmdb_id') or current_item_metadata.get('ids', {}).get('tmdb') or metadata.get('tmdb_id')
                
                current_item_metadata['versions'] = item_from_input_list.get('versions', {})

                if item_media_type_lower == 'movie':
                    physical_release_date = get_physical_release_date(imdb_id)
                    if physical_release_date: current_item_metadata['physical_release_date'] = physical_release_date
                    
                    theatrical_release_date = get_theatrical_release_date(imdb_id)
                    if theatrical_release_date: current_item_metadata['theatrical_release_date'] = theatrical_release_date
                    
                    current_item_genres = current_item_metadata.get('genres', [])
                    is_anime_movie = 'anime' in [str(g).lower() for g in current_item_genres]
                    if is_anime_movie and 'anime' not in current_item_genres :
                        current_item_metadata['genres'] = ['anime'] + [g for g in current_item_genres if str(g).lower() != 'anime']
                    elif is_anime_movie :
                        current_item_metadata['genres'] = ['anime'] + [g for g in current_item_genres if str(g).lower() != 'anime']

                    current_item_metadata['is_anime'] = is_anime_movie
                    current_item_metadata['release_date'] = get_release_date(current_item_metadata, imdb_id)
                    current_item_metadata['media_type'] = 'movie'

                    processed_items['movies'].append(current_item_metadata)
                    logging.debug(f"Prepared movie {current_item_metadata.get('title')} for processing with versions: {current_item_metadata.get('versions')}.")

                elif item_media_type_lower in ['tv', 'show', 'episode']:
                    current_item_show_genres = current_item_metadata.get('genres', [])
                    is_anime_show = 'anime' in [str(g).lower() for g in current_item_show_genres]
                    if is_anime_show and 'anime' not in current_item_show_genres:
                        current_item_metadata['genres'] = ['anime'] + [g for g in current_item_show_genres if str(g).lower() != 'anime']
                    elif is_anime_show:
                        current_item_metadata['genres'] = ['anime'] + [g for g in current_item_show_genres if str(g).lower() != 'anime']
                    seasons_data_from_metadata = current_item_metadata.get('seasons')
                    if seasons_data_from_metadata == 'None' or not isinstance(seasons_data_from_metadata, dict):
                        logging.error(f"Invalid or missing seasons data in metadata for show {imdb_id}. Skipping episode generation for this item.")
                        continue

                    requested_specific_seasons = item_from_input_list.get('requested_seasons', [])
                    
                    all_season_numbers_in_metadata = set()
                    for s_key in seasons_data_from_metadata.keys():
                        try: all_season_numbers_in_metadata.add(int(s_key))
                        except ValueError: 
                            if str(s_key) == "0": all_season_numbers_in_metadata.add(0)
                            else: logging.warning(f"Non-integer season key '{s_key}' in metadata for {imdb_id}")
                    
                    seasons_to_process_for_this_item_instance = set()

                    if requested_specific_seasons:
                        valid_requested = {s_num for s_num in requested_specific_seasons if s_num in all_season_numbers_in_metadata}
                        if len(valid_requested) != len(requested_specific_seasons):
                            logging.warning(f"Item for {imdb_id} requested seasons {requested_specific_seasons}, but metadata only contains {all_season_numbers_in_metadata}. Processing valid: {valid_requested}")
                        seasons_to_process_for_this_item_instance = valid_requested
                    else:
                        content_source_id = item_from_input_list.get('content_source')
                        allow_specials_setting_for_source = False
                        if content_source_id:
                            from queues.config_manager import load_config
                            config = load_config()
                            cs_config = config.get('Content Sources', {}).get(content_source_id, {})
                            if isinstance(cs_config, dict): allow_specials_setting_for_source = cs_config.get('allow_specials', False)
                        
                        seasons_to_process_for_this_item_instance = {
                            s_num for s_num in all_season_numbers_in_metadata if allow_specials_setting_for_source or s_num != 0
                        }
                   
                    if not seasons_to_process_for_this_item_instance:
                        logging.info(f"No seasons determined for active processing for show {current_item_metadata.get('title')} (IMDb {imdb_id}) from item {item_from_input_list.get('content_source_detail', '')}. Specific requested: {requested_specific_seasons}, Metadata seasons: {all_season_numbers_in_metadata}.")
                        
                        # Try to manually fetch seasons data as a fallback
                        if not all_season_numbers_in_metadata:
                            logging.info(f"Attempting manual seasons fetch for {imdb_id} as fallback...")
                            try:
                                # Try to force refresh the metadata to get seasons data
                                from cli_battery.app.direct_api import DirectAPI
                                refreshed_metadata, _ = DirectAPI.force_refresh_metadata(imdb_id)
                                if refreshed_metadata and 'seasons' in refreshed_metadata:
                                    refreshed_seasons = refreshed_metadata['seasons']
                                    if isinstance(refreshed_seasons, dict) and refreshed_seasons:
                                        # Extract season numbers from the refreshed data
                                        refreshed_season_numbers = [int(season_num) for season_num in refreshed_seasons.keys() if str(season_num).isdigit() and int(season_num) > 0]
                                        if refreshed_season_numbers:
                                            logging.info(f"Successfully fetched {len(refreshed_season_numbers)} seasons via fallback for {imdb_id}: {refreshed_season_numbers}")
                                            # Update the seasons data and retry processing
                                            seasons_data_from_metadata = refreshed_seasons
                                            all_season_numbers_in_metadata = set(refreshed_season_numbers)
                                            
                                            # Recalculate seasons to process
                                            if requested_specific_seasons:
                                                valid_requested = {s_num for s_num in requested_specific_seasons if s_num in all_season_numbers_in_metadata}
                                                if len(valid_requested) != len(requested_specific_seasons):
                                                    logging.warning(f"Item for {imdb_id} requested seasons {requested_specific_seasons}, but refreshed metadata only contains {all_season_numbers_in_metadata}. Processing valid: {valid_requested}")
                                                seasons_to_process_for_this_item_instance = valid_requested
                                            else:
                                                content_source_id = item_from_input_list.get('content_source')
                                                allow_specials_setting_for_source = False
                                                if content_source_id:
                                                    from queues.config_manager import load_config
                                                    config = load_config()
                                                    cs_config = config.get('Content Sources', {}).get(content_source_id, {})
                                                    if isinstance(cs_config, dict): allow_specials_setting_for_source = cs_config.get('allow_specials', False)
                                                
                                                seasons_to_process_for_this_item_instance = {
                                                    s_num for s_num in all_season_numbers_in_metadata if allow_specials_setting_for_source or s_num != 0
                                                }
                                            
                                            # If we now have seasons to process, continue with the processing
                                            if seasons_to_process_for_this_item_instance:
                                                logging.info(f"Retrying processing with {len(seasons_to_process_for_this_item_instance)} seasons for {imdb_id}")
                                            else:
                                                logging.warning(f"Fallback seasons fetch succeeded but no valid seasons to process for {imdb_id}")
                                                continue
                                        else:
                                            logging.warning(f"Fallback seasons fetch returned no valid season numbers for {imdb_id}")
                                            continue
                                    else:
                                        logging.warning(f"Fallback seasons fetch returned invalid seasons data for {imdb_id}")
                                        continue
                                else:
                                    logging.warning(f"Fallback metadata refresh failed or returned no seasons data for {imdb_id}")
                                    continue
                            except Exception as fallback_error:
                                logging.error(f"Error during fallback seasons fetch for {imdb_id}: {fallback_error}")
                                continue
                        else:
                            # We have season numbers but none are valid for processing
                            continue

                    current_item_episodes = []
                    for season_num_to_process in seasons_to_process_for_this_item_instance:
                        season_detail = seasons_data_from_metadata.get(str(season_num_to_process)) or seasons_data_from_metadata.get(season_num_to_process)
                        if not season_detail or not isinstance(season_detail.get('episodes'), dict):
                            logging.warning(f"Season {season_num_to_process} details or its episodes not found/invalid in metadata for {imdb_id}.")
                            continue
                        
                        for ep_num_str, ep_data in season_detail['episodes'].items():
                            try:
                                ep_num_int = int(ep_num_str)
                                episode_item_obj = create_episode_item(
                                    current_item_metadata,
                                    season_num_to_process,
                                    ep_num_int,
                                    ep_data,
                                    is_anime_show
                                )
                                episode_item_obj['media_type'] = 'episode'
                                episode_item_obj['versions'] = item_from_input_list.get('versions', {})
                                current_item_episodes.append(episode_item_obj)
                            except ValueError:
                                logging.warning(f"Invalid episode number format '{ep_num_str}' for S{season_num_to_process}, IMDb {imdb_id}.")
                            except Exception as e_create_ep:
                                logging.error(f"Error creating episode S{season_num_to_process}E{ep_num_str} for {imdb_id}: {e_create_ep}", exc_info=True)
                    
                    processed_items['episodes'].extend(current_item_episodes)
                    if current_item_episodes:
                        logging.debug(f"Prepared {len(current_item_episodes)} episodes for show {current_item_metadata.get('title')} (IMDb {imdb_id}) from item {item_from_input_list.get('content_source_detail','')} based on seasons: {seasons_to_process_for_this_item_instance} with versions: {item_from_input_list.get('versions', {})}.")
            except Exception as e:
                logging.error(f"Error during main processing loop for item (IMDb {imdb_id}, Title {item_from_input_list.get('title', 'N/A')}, Detail {item_from_input_list.get('content_source_detail', 'N/A')}): {str(e)}", exc_info=True)

    if items_by_tmdb_id_only:
         logging.warning(f"{len(items_by_tmdb_id_only)} items had only TMDB ID after conversion attempt. These items will be processed by add_wanted_items if they contain sufficient TMDB-only metadata (e.g., for UFC/Jackett). process_metadata primarily enriches with IMDb based info.")
         for tmdb_id_key, original_tmdb_items_list in items_by_tmdb_id_only.items():
             for tmdb_item_orig in original_tmdb_items_list:
                 minimal_metadata = get_tmdb_metadata(str(tmdb_id_key), tmdb_item_orig.get('media_type', 'movie'))
                 
                 processed_item_shell = {
                     'tmdb_id': str(tmdb_id_key),
                     'imdb_id': None,
                     'media_type': tmdb_item_orig.get('media_type', 'movie'),
                     'title': (minimal_metadata.get('title') if minimal_metadata else None) or tmdb_item_orig.get('title', 'Unknown Title'),
                     'year': (minimal_metadata.get('year') if minimal_metadata else None) or tmdb_item_orig.get('year'),
                     'genres': (minimal_metadata.get('genres') if minimal_metadata else None) or tmdb_item_orig.get('genres', []),
                     'runtime': (minimal_metadata.get('runtime') if minimal_metadata else None) or tmdb_item_orig.get('runtime'),
                     'release_date': (minimal_metadata.get('release_date') if minimal_metadata else None) or tmdb_item_orig.get('release_date'),
                     'overview': (minimal_metadata.get('overview') if minimal_metadata else None) or tmdb_item_orig.get('overview'),
                     'versions': tmdb_item_orig.get('versions', {}),
                     'content_source': tmdb_item_orig.get('content_source'),
                     'content_source_detail': tmdb_item_orig.get('content_source_detail')
                 }
                 if processed_item_shell['media_type'] == 'movie':
                     processed_items['movies'].append(processed_item_shell)
                     logging.debug(f"Prepared shell movie (TMDB-only) {processed_item_shell.get('title')} for processing with versions: {processed_item_shell.get('versions')}.")
                 elif tmdb_item_orig.get('media_type') in ['tv', 'show', 'episode'] and minimal_metadata:
                     logging.warning(f"TMDB-only TV show {tmdb_id_key} will be passed as a shell. Episode expansion might not occur here.")
                 else:
                      logging.warning(f"TMDB-only item {tmdb_id_key} of type {tmdb_item_orig.get('media_type')} could not be processed into a movie/episode shell here.")

    logging.debug(f"Finished processing. Prepared {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes for database operations.")
    return processed_items

def get_release_date(media_details: Dict[str, Any], imdb_id: Optional[str] = None) -> str:
    if not media_details:
        logging.warning("No media details provided for release date")
        return 'Unknown'
        
    if not imdb_id:
        logging.warning("Attempted to get release date with None IMDB ID")
        return media_details.get('released', 'Unknown')

    release_dates, _ = DirectAPI.get_movie_release_dates(imdb_id)
    logging.debug(f"Processing release dates for IMDb ID: {imdb_id}")

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
                    elif release_type in ['theatrical', 'theatrical (limited)', 'limited']: # Added 'limited' here
                        theatrical_releases.append(release_date)
                    elif release_type == 'premiere':
                        premiere_releases.append(release_date)
                except ValueError:
                    logging.warning(f"Invalid date format: {release_date_str}")

    median_theatrical_date = None
    if theatrical_releases:
        theatrical_releases.sort()
        median_index = len(theatrical_releases) // 2
        if median_index < len(theatrical_releases):
            median_theatrical_date = theatrical_releases[median_index]
            logging.debug(f"Median theatrical release date for {imdb_id}: {median_theatrical_date.strftime('%Y-%m-%d')}")

    # Filter out digital releases that are too close to the median theatrical release
    if median_theatrical_date and len(digital_physical_releases) == 1:
        plausible_digital_releases = [
            d for d in digital_physical_releases 
            if abs((d - median_theatrical_date).days) > 3
        ]
        if len(plausible_digital_releases) < len(digital_physical_releases):
            logging.info(f"Ignored {len(digital_physical_releases) - len(plausible_digital_releases)} digital release(s) for {imdb_id} due to proximity to median theatrical release.")
    else:
        plausible_digital_releases = digital_physical_releases

    if plausible_digital_releases:
        return min(plausible_digital_releases).strftime("%Y-%m-%d")

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
    if 'imdb_id' in item and item.get('imdb_id'):
        return item['imdb_id']
    
    if 'tmdb_id' not in item:
        logging.warning(f"Cannot retrieve IMDb ID without TMDB ID: {item}")
        return None
    
    tmdb_id = item['tmdb_id']
    media_type = item.get('media_type')
    
    # Convert 'tv' to 'show' for DirectAPI compatibility
    api_media_type = 'show' if media_type == 'tv' else media_type
    
    imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type=api_media_type)
    return imdb_id

def refresh_release_dates():
    from database import get_all_media_items, update_release_date_and_state
    import content_checkers.trakt as trakt
    logging.info("Starting refresh_release_dates function")
    
    logging.info("Fetching items to refresh")
    items_to_refresh = list(get_all_media_items(state="Unreleased")) \
                     + list(get_all_media_items(state="Wanted")) \
                     + list(get_all_media_items(state="Sleeping")) \
                     + list(get_all_media_items(state="Final_Check")) \
                     + list(get_all_media_items(state="Scraping"))
    logging.info(f"Found {len(items_to_refresh)} items to refresh")

    for index, item in enumerate(items_to_refresh, 1):
        try:
            item_dict = dict(item)
            title = item_dict.get('title', 'Unknown Title')
            media_type = item_dict.get('type', 'Unknown Type').lower()
            imdb_id = item_dict.get('imdb_id')
            season_number = item_dict.get('season_number')
            episode_number = item_dict.get('episode_number')
            db_item_id = item_dict.get('id')

            existing_release_date = item_dict.get('release_date')
            def is_valid_date_str(date_str):
                if not date_str or str(date_str).lower() in ['unknown', 'none']: return False
                try: datetime.strptime(str(date_str), '%Y-%m-%d'); return True
                except (ValueError, TypeError): return False

            logging.info(f"Processing item {index}/{len(items_to_refresh)}: {title} ({media_type}) - IMDb ID: {imdb_id}, DB ID: {db_item_id}")

            if not imdb_id:
                logging.warning(f"Skipping item {index} (DB ID: {db_item_id}) due to missing imdb_id")
                continue

            new_release_date = None
            new_physical_release_date = None
            new_theatrical_release_date = None
            new_airtime = None

            if media_type == 'movie':
                metadata, source = DirectAPI.get_movie_metadata(imdb_id)
                if not metadata:
                    logging.warning(f"No metadata found for movie {title} ({imdb_id})")
                    if is_valid_date_str(existing_release_date):
                        new_release_date = existing_release_date
                        logging.warning(f"Metadata fetch failed for {title} ({imdb_id}), but preserving existing valid release date: {new_release_date}")
                    else:
                        new_release_date = 'Unknown'
                    new_physical_release_date = None
                    new_theatrical_release_date = None
                else:
                    logging.info("Getting release date, physical release date, and theatrical release date")
                    fetched_release_date = get_release_date(metadata, imdb_id)
                    new_physical_release_date = get_physical_release_date(imdb_id)
                    new_theatrical_release_date = get_theatrical_release_date(imdb_id)
                    logging.info(f"Physical release date: {new_physical_release_date}")
                    logging.info(f"Theatrical release date: {new_theatrical_release_date}")

                    if fetched_release_date == 'Unknown' and is_valid_date_str(existing_release_date):
                        new_release_date = existing_release_date
                        logging.warning(f"Fetched release date was 'Unknown' for {title} ({imdb_id}), but preserving existing valid release date: {new_release_date}")
                    else:
                        new_release_date = fetched_release_date

                item_dict['early_release_original'] = item_dict.get('early_release', False)
                item_dict['physical_release_date_original'] = item_dict.get('physical_release_date')
                item_dict['theatrical_release_date_original'] = item_dict.get('theatrical_release_date')
                trakt_early_releases = get_setting('Scraping', 'trakt_early_releases', False)
                skip_early_release_check = item_dict.get('no_early_release', False)
                if trakt_early_releases and not skip_early_release_check:
                    logging.info(f"Checking Trakt for early releases for {title} ({imdb_id})")
                    trakt_id = trakt.fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
                    if trakt_id and isinstance(trakt_id, list) and len(trakt_id) > 0:
                        found_movie = False
                        for result in trakt_id:
                            if result.get('type') == 'movie':
                                trakt_movie_data = result.get('movie')
                                if trakt_movie_data and trakt_movie_data.get('ids') and trakt_movie_data['ids'].get('trakt'):
                                    trakt_id_num = str(trakt_movie_data['ids']['trakt'])
                                    logging.debug(f"Found Trakt movie ID {trakt_id_num} for {imdb_id}")
                                    trakt_lists = trakt.fetch_items_from_trakt(f"/movies/{trakt_id_num}/lists/personal/popular")
                                    if trakt_lists:
                                        for trakt_list in trakt_lists:
                                            if re.search(r'(latest|new).*?(releases)', trakt_list['name'], re.IGNORECASE):
                                                logging.info(f"Movie {title} ({imdb_id}) found in early release list: {trakt_list['name']}")
                                                item_dict['early_release'] = True
                                                found_movie = True
                                                break
                                    if found_movie:
                                        break
                                else:
                                     logging.warning(f"Trakt search result for {imdb_id} did not contain expected movie ID structure: {result}")
                        if not found_movie:
                             logging.info(f"Did not find {title} ({imdb_id}) in any relevant Trakt early release lists.")
                    else:
                        logging.info(f"No Trakt ID found for {imdb_id} via search, cannot check early release lists.")
                elif skip_early_release_check:
                    logging.info(f"Skipping Trakt early release check for {title} ({imdb_id}) due to no_early_release flag.")
                
                new_state = item_dict['state']
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
                         new_state = "Wanted"
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
                        new_state = "Wanted"
                else:
                    new_state = "Wanted"
                    logging.info(f"No valid release dates found, setting state to Wanted")
                
                new_airtime = None

            elif media_type == 'episode':
                metadata, source = DirectAPI.get_show_metadata(imdb_id)
                logging.info(f"Processing metadata for {title} S{season_number}E{episode_number}")

                new_airtime = get_episode_airtime(imdb_id, season_number, episode_number)
                logging.info(f"New airtime from metadata: {new_airtime}")

                if not metadata or not isinstance(metadata, dict):
                    logging.warning(f"Invalid or missing metadata for show {imdb_id}")
                    if is_valid_date_str(existing_release_date):
                        new_release_date = existing_release_date
                        logging.warning(f"Metadata fetch failed for show {imdb_id}, preserving existing valid date: {new_release_date}")
                    else: new_release_date = 'Unknown'
                else:
                    seasons = metadata.get('seasons', {})
                    if not isinstance(seasons, dict):
                        logging.warning(f"Invalid seasons data for show {imdb_id}")
                        new_release_date = 'Unknown'
                    else:
                        season_data = seasons.get(str(season_number)) or seasons.get(season_number) or {}
                        if not isinstance(season_data, dict):
                            logging.warning(f"Invalid season data for show {imdb_id} season {season_number}")
                            new_release_date = 'Unknown'
                        else:
                            episodes = season_data.get('episodes', {})
                            if not isinstance(episodes, dict):
                                logging.warning(f"Invalid episodes data for show {imdb_id} season {season_number}")
                                new_release_date = 'Unknown'
                            else:
                                season_key_lookup = str(season_number) # This is for the log message
                                episode_key_lookup_for_log = str(episode_number) # For the log message

                                # Attempt to get episode data using integer key first, then string key as a fallback
                                episode_data = episodes.get(episode_number) # episode_number is an int
                                if episode_data is None:
                                    episode_data = episodes.get(str(episode_number)) # Fallback

                                if not episode_data or not isinstance(episode_data, dict):
                                    logging.debug(f"Lookup Failure Details for DB ID: {db_item_id}")
                                    logging.debug(f"  IMDb ID: {imdb_id}")
                                    logging.debug(f"  DB Season Number: {season_number!r} (Type: {type(season_number).__name__})")
                                    logging.debug(f"  DB Episode Number: {episode_number!r} (Type: {type(episode_number).__name__})")
                                    logging.debug(f"  Season Key Used: '{season_key_lookup}'")
                                    logging.debug(f"  Episode Key Used (for logging): '{episode_key_lookup_for_log}'")
                                    logging.debug(f"  (Actual episode keys attempted for lookup: {episode_number} (int), then {str(episode_number)} (str))")
                                    logging.debug(f"  Metadata Source: {source}")
                                    logging.debug(f"  Available Season Keys in Battery: {list(seasons.keys())}")
                                    logging.debug(f"  Available Episode Keys for Season '{season_key_lookup}': {list(episodes.keys())}")
                                    logging.debug(f"  Result of episodes.get({episode_number}) (int key) then episodes.get({str(episode_number)}) (str key): {episode_data!r}")

                                    logging.warning(f"No valid data found for S{season_number}E{episode_number} in fetched metadata.")
                                    if is_valid_date_str(existing_release_date):
                                        new_release_date = existing_release_date
                                        logging.warning(f"Episode data lookup failed, preserving existing valid release date: {new_release_date}")
                                    else:
                                        new_release_date = 'Unknown'
                                        logging.warning("Episode data lookup failed and no valid existing date found. Setting to Unknown.")
                                else:
                                    first_aired_str = episode_data.get('first_aired')
                                    logging.info(f"First aired date from metadata: {first_aired_str}")
                                    if first_aired_str:
                                        try:
                                            # Use iso8601 library for robust parsing
                                            first_aired_dt_obj = iso8601.parse_date(first_aired_str)
                                            
                                            # If the parsed datetime is naive, assume it's UTC
                                            if first_aired_dt_obj.tzinfo is None:
                                                first_aired_dt_obj = first_aired_dt_obj.replace(tzinfo=timezone.utc)
                                            
                                            local_tz = _get_local_timezone()
                                            local_dt = first_aired_dt_obj.astimezone(local_tz)
                                            new_release_date = local_dt.strftime("%Y-%m-%d")
                                            logging.info(f"Calculated local release date {new_release_date} from original aired string {first_aired_str}")
                                        except (ValueError, iso8601.ParseError) as e: # Catch iso8601.ParseError as well
                                            logging.error(f"Invalid datetime format or conversion error: {first_aired_str} - Error: {e}")
                                            if is_valid_date_str(existing_release_date):
                                                new_release_date = existing_release_date
                                                logging.warning(f"Date parsing failed for S{season_number}E{episode_number}, preserving existing valid release date: {new_release_date}")
                                            else: new_release_date = 'Unknown'
                                    else:
                                        logging.warning("No first_aired date found in episode data")
                                        if is_valid_date_str(existing_release_date):
                                            new_release_date = existing_release_date
                                            logging.warning(f"No first_aired found for S{season_number}E{episode_number}, preserving existing valid release date: {new_release_date}")
                                        else: new_release_date = 'Unknown'

                logging.info(f"New release date: {new_release_date}")

                new_state = item_dict['state']
                if new_release_date == "Unknown" or new_release_date is None:
                    new_state = "Wanted"
                    logging.info("Release date is Unknown, setting state to Wanted")
                else:
                    try:
                        release_date_dt = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                        today = datetime.now().date()
                        if item_dict.get('early_release', False): new_state = "Wanted"; logging.info(f"Episode marked as early release, setting state to Wanted")
                        else: new_state = "Wanted" if release_date_dt <= today else "Unreleased"; logging.info(f"Episode release date is {release_date_dt}, today is {today}, setting state to {new_state}")
                    except ValueError: new_state = "Wanted"; logging.warning(f"Invalid release date format: {new_release_date}. Setting state to Wanted.")

            if (new_state != item_dict['state'] or
                new_release_date != item_dict.get('release_date') or
                (media_type == 'episode' and new_airtime != item_dict.get('airtime')) or
                item_dict.get('early_release', False) != item_dict.get('early_release_original', False) or
                item_dict.get('no_early_release', False) != item_dict.get('no_early_release_original', False) or
                (media_type == 'movie' and new_physical_release_date != item_dict.get('physical_release_date_original')) or
                (media_type == 'movie' and new_theatrical_release_date != item_dict.get('theatrical_release_date_original'))):

                logging.info(f"Changes detected for {title} (DB ID: {db_item_id}). Current state: {item_dict['state']}, New state: {new_state}. Updating database.")
                item_dict['no_early_release_original'] = item_dict.get('no_early_release', False)
                update_release_date_and_state(
                    db_item_id, new_release_date, new_state, airtime=new_airtime,
                    early_release=item_dict.get('early_release', False),
                    physical_release_date=new_physical_release_date if media_type == 'movie' else None,
                    theatrical_release_date=new_theatrical_release_date if media_type == 'movie' else None,
                    no_early_release=item_dict.get('no_early_release', False)
                )
                log_msg = f"Updated DB for ID {db_item_id}: State={new_state}, ReleaseDate={new_release_date}"
                if media_type == 'movie': 
                    log_msg += f" and physical release date of: {new_physical_release_date}"
                    log_msg += f" and theatrical release date of: {new_theatrical_release_date}"
                if media_type == 'episode': log_msg += f" and airtime of: {new_airtime}"
                logging.info(log_msg)
            else:
                logging.info(f"No changes needed for {title} (DB ID: {db_item_id})")

        except Exception as e:
            logging.error(f"Error processing item {index} (DB ID: {item_dict.get('id', 'N/A')}): {str(e)}", exc_info=True)
            continue

    logging.info("Finished refresh_release_dates function")

def get_episode_count_for_seasons(imdb_id: str, seasons: List[int]) -> int:
    show_metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    all_seasons = show_metadata.get('seasons', {})
    
    total_episodes = 0
    for season_num in seasons: # season_num is an int
        # Try integer key first, then string key
        season_data = all_seasons.get(season_num)
        if season_data is None:
            season_data = all_seasons.get(str(season_num))
        
        if season_data and isinstance(season_data, dict):
            total_episodes += season_data.get('episode_count', 0)
            
    return total_episodes

def get_all_season_episode_counts(imdb_id: str) -> Dict[int, int]:
    show_metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    all_seasons = show_metadata.get('seasons', {})
    #logging.debug(f"Raw seasons data received from DirectAPI for {imdb_id}: {all_seasons}")
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

def get_episode_airtime(imdb_id: str, season_number: Optional[int] = None, episode_number: Optional[int] = None) -> Optional[str]:
    """Return the episode's airtime converted to the user's local time, preferring first_aired over airs.time."""
    DEFAULT_AIRTIME = "19:00"

    try:
        metadata, _ = DirectAPI.get_show_metadata(imdb_id)
        if not metadata or not isinstance(metadata, dict):
            logging.warning(f"Could not retrieve valid metadata for show {imdb_id}")
            return DEFAULT_AIRTIME

        # If specific episode requested, try to get its first_aired
        if season_number is not None and episode_number is not None:
            seasons = metadata.get('seasons', {})
            season_data = seasons.get(str(season_number)) or seasons.get(season_number)
            if season_data and isinstance(season_data, dict):
                episodes = season_data.get('episodes', {})
                if isinstance(episodes, dict):
                    episode_data = episodes.get(str(episode_number)) or episodes.get(episode_number)
                    if episode_data and isinstance(episode_data, dict):
                        first_aired = episode_data.get('first_aired')
                        if first_aired:
                            try:
                                # Parse and convert to local time
                                dt = iso8601.parse_date(first_aired)
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                local_tz = _get_local_timezone()
                                local_dt = dt.astimezone(local_tz)
                                airtime_str = local_dt.strftime("%H:%M")
                                logging.info(f"[Airtime] Used first_aired for {imdb_id} S{season_number}E{episode_number}: {first_aired} → {airtime_str} (local)")
                                return airtime_str
                            except (iso8601.ParseError, ValueError) as e:
                                logging.warning(f"Failed to parse first_aired '{first_aired}' for {imdb_id} S{season_number}E{episode_number}: {e}")

        # Fall back to using airs.time + airs.timezone
        airs = metadata.get('airs', {})
        time_str = airs.get('time')
        timezone_str = airs.get('timezone')

        if not time_str or not timezone_str:
            logging.warning(f"No usable first_aired or airs.time for {imdb_id}, defaulting to {DEFAULT_AIRTIME}")
            return DEFAULT_AIRTIME

        try:
            show_tz = ZoneInfo(timezone_str)
            air_time_obj = datetime.strptime(time_str, "%H:%M").time()
            today = datetime.now().date()
            show_air_dt = datetime.combine(today, air_time_obj).replace(tzinfo=show_tz)
            local_air_dt = show_air_dt.astimezone(_get_local_timezone())
            airtime_str = local_air_dt.strftime("%H:%M")
            logging.info(f"[Airtime Fallback] Used airs for {imdb_id}: {time_str} {timezone_str} → {airtime_str} (local)")
            return airtime_str
        except Exception as e:
            logging.error(f"Failed to convert airs.time for {imdb_id}: {e}")
            return DEFAULT_AIRTIME

    except Exception as e:
        logging.error(f"Unexpected error getting airtime for {imdb_id}: {e}", exc_info=True)
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
