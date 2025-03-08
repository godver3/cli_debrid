import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
import sys, os
import json
import time
from settings import get_setting
import re
import pytz
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import get_setting
from cli_battery.app.direct_api import DirectAPI
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.database import DatabaseManager

# Initialize DirectAPI at module level
direct_api = DirectAPI()

def parse_json_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

def get_metadata(imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None, item_media_type: Optional[str] = None, original_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:

    if not imdb_id and not tmdb_id:
        raise ValueError("Either imdb_id or tmdb_id must be provided")

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
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type=converted_item_media_type)
        if not imdb_id:
            logging.error(f"Could not find IMDb ID for TMDB ID {tmdb_id}")
            return {}
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
            'country': metadata.get('country', '').lower(),  # Add country code, defaulting to empty string
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

    if first_aired_str:
        try:
            # Parse the UTC datetime string
            first_aired_utc = datetime.strptime(first_aired_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

            # Convert UTC to local timezone using cross-platform function
            local_tz = _get_local_timezone()
            local_dt = first_aired_utc.astimezone(local_tz)
            
            # Format the local date (stripping time) as a string
            release_date = local_dt.strftime("%Y-%m-%d")
        except ValueError:
            logging.warning(f"Invalid datetime format: {first_aired_str}")
            release_date = 'Unknown'
    else:
        release_date = 'Unknown'

    # Handle airtime conversion
    airs = show_item.get('airs', {})
    airtime = airs.get('time')
    timezone_str = airs.get('timezone')

    if airtime and timezone_str:
        try:
            # Try parsing with seconds first (HH:MM:SS)
            try:
                air_time = datetime.strptime(airtime, "%H:%M:%S").time()
            except ValueError:
                # If that fails, try without seconds (HH:MM)
                air_time = datetime.strptime(airtime, "%H:%M").time()
            
            # Get the show's timezone
            show_tz = ZoneInfo(timezone_str)
            
            # Use the release date if available, otherwise use today's date
            if release_date != 'Unknown':
                base_date = datetime.strptime(release_date, "%Y-%m-%d").date()
            else:
                base_date = datetime.now(show_tz).date()

            # Combine date and time
            show_datetime = datetime.combine(base_date, air_time)
            show_datetime = show_datetime.replace(tzinfo=show_tz)
            
            # Get the local timezone dynamically
            local_tz = _get_local_timezone()
            local_airtime = show_datetime.astimezone(local_tz)
            
            # Format as HH:MM
            airtime = local_airtime.strftime("%H:%M")
        except (ValueError, ZoneInfoNotFoundError) as e:
            logging.warning(f"Error converting airtime: {e}")
            logging.warning(f"Invalid airtime or timezone: {airtime}, {timezone_str}")
            airtime = '19:00'  # Default fallback
    else:
        airtime = '19:00'  # Default fallback

    episode_item = {
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
        'airtime': airtime,
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
    from settings import get_setting
    from datetime import timezone
    import os
    
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
        
        # First try: Check for override in settings
        timezone_override = get_setting('Debug', 'timezone_override', '')
        if timezone_override and is_valid_timezone(timezone_override):
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(timezone_override)
            except Exception as e:
                logging.error(f"Error creating ZoneInfo for override {timezone_override}: {e}")
        
        # Second try: Try getting from environment variable
        tz_env = os.environ.get('TZ')
        if tz_env and is_valid_timezone(tz_env):
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(tz_env)
            except Exception as e:
                logging.error(f"Error creating ZoneInfo from TZ env {tz_env}: {e}")
        
        # Third try: Try tzlocal with exception handling
        try:
            local_tz = get_localzone()
            if hasattr(local_tz, 'zone') and is_valid_timezone(local_tz.zone):
                try:
                    from zoneinfo import ZoneInfo
                    return ZoneInfo(local_tz.zone)
                except Exception:
                    return local_tz
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

def process_metadata(media_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    from database.database_writing import update_blacklisted_date, update_media_item
    from database.core import get_db_connection
    from database.wanted_items import add_wanted_items
    from run_program import program_runner

    processed_items = {'movies': [], 'episodes': []}
    trakt_metadata = TraktMetadata()

    for index, item in enumerate(media_items, 1):
        try:
            logging.debug(f"Processing item {index}: content_source_detail={item.get('content_source_detail')}")
            if not trakt_metadata._check_rate_limit():
                logging.warning("Trakt rate limit reached. Waiting for 5 minutes before continuing.")
                time.sleep(300)  # Wait for 5 minutes

            metadata = get_metadata(
                imdb_id=item.get('imdb_id'), 
                tmdb_id=item.get('tmdb_id'), 
                item_media_type=item.get('media_type'),
                original_item=item  # Pass the original item to preserve content source info
            )
            if not metadata:
                logging.warning(f"Could not fetch metadata for item: {item}")
                continue

            if item['media_type'].lower() == 'movie':
                # Get physical release date if it's a movie
                physical_release_date = get_physical_release_date(metadata.get('imdb_id'))
                if physical_release_date:
                    metadata['physical_release_date'] = physical_release_date
                processed_items['movies'].append(metadata)
                logging.debug(f"Added movie with content_source_detail={metadata.get('content_source_detail')}")
            elif item['media_type'].lower() in ['tv', 'show']:
                is_anime = 'anime' in [genre.lower() for genre in metadata.get('genres', [])]
                
                # Check if this show is already Overseerr managed
                conn = get_db_connection()
                try:
                    cursor = conn.execute('''
                        SELECT COUNT(*) as count FROM media_items 
                        WHERE (imdb_id = ? OR tmdb_id = ?) 
                        AND type = 'episode' 
                        AND requested_season = TRUE
                    ''', (metadata.get('imdb_id'), metadata.get('tmdb_id')))
                    result = cursor.fetchone()
                    has_requested_episodes = result['count'] > 0
                finally:
                    conn.close()

                if has_requested_episodes and not item.get('requested_seasons'):
                    logging.info(f"Skipping show {metadata.get('title', 'Unknown')} as it is managed by Overseerr")
                    continue
                
                seasons = metadata.get('seasons')
                if seasons == 'None':  # Handle the case where seasons is the string 'None'
                    seasons = {}
                elif not isinstance(seasons, dict):
                    seasons = {}
                    
                if not seasons:
                    logging.error(f"No seasons data found for show {item['imdb_id']}")
                    if DatabaseManager.remove_metadata(item['imdb_id']):
                        logging.info(f"Retrying metadata fetch for show {item['imdb_id']}")
                        metadata, _ = DirectAPI.get_show_metadata(item['imdb_id'])
                    continue


                # Get the requested seasons if they exist
                requested_seasons = item.get('requested_seasons', [])
                
                # If we have specific seasons requested (e.g. from Overseerr)
                if requested_seasons:
                    logging.info(f"Processing specific requested seasons {requested_seasons} for show {metadata.get('title', 'Unknown')}")
                    seasons_to_process = requested_seasons
                else:
                    # For non-Overseerr sources, process all seasons
                    logging.info(f"Processing all seasons for show {metadata.get('title', 'Unknown')} from non-Overseerr source")
                    seasons_to_process = [int(s) for s in seasons.keys() if s != '0']  # Skip season 0 (specials)

                # Process the determined seasons
                all_episodes = []
                for season_number in seasons_to_process:
                    # Try both string and integer keys
                    season_data = seasons.get(str(season_number))
                    if season_data is None:  # Use explicit None check
                        season_data = seasons.get(season_number)  # Try integer key
                    
                    if season_data is None:  # Use explicit None check
                        # Use metadata's IMDb ID if available, otherwise fall back to item's TMDB ID
                        show_id = metadata.get('imdb_id') or f"TMDB:{item.get('tmdb_id')}"
                        logging.warning(f"Could not find season {season_number} data for show {show_id}")
                        continue

                    episodes = season_data.get('episodes', {})
                    if not episodes:
                        logging.warning(f"No episodes found for season {season_number}")
                        continue
                   
                    logging.info(f"Processing {len(episodes)} episodes for season {season_number}")
                    for episode_number, episode_data in episodes.items():
                        try:
                            episode_number = int(episode_number)
                            episode_item = create_episode_item(
                                metadata, 
                                season_number, 
                                episode_number, 
                                episode_data,
                                is_anime
                            )
                            # Only mark as requested_season if it was explicitly requested
                            if requested_seasons:
                                episode_item['requested_season'] = True
                            all_episodes.append(episode_item)
                        except Exception as e:
                            show_id = metadata.get('imdb_id') or f"TMDB:{item.get('tmdb_id')}"
                            logging.error(f"Error processing episode S{season_number:02d}E{episode_number} of show {show_id}: {str(e)}")
                            continue

                # Add all episodes to processed_items
                processed_items['episodes'].extend(all_episodes)
                logging.info(f"Added {len(all_episodes)} episodes from {'requested' if requested_seasons else 'all'} seasons")

                # Only add items with Overseerr versions if this is from an Overseerr webhook
                if item.get('from_overseerr'):
                    from settings import get_all_settings
                    content_sources = get_all_settings().get('Content Sources', {})
                    overseerr_settings = next((data for source, data in content_sources.items() if source.startswith('Overseerr')), {})
                    versions = overseerr_settings.get('versions', {})
                    # Add content source to episodes
                    for episode in all_episodes:
                        episode['content_source'] = 'overseerr_webhook'
                        from content_checkers.content_source_detail import append_content_source_detail
                        episode = append_content_source_detail(episode, source_type='Overseerr')
                    add_wanted_items(all_episodes, versions)

        except Exception as e:
            # Use the most specific identifier available
            show_id = (
                item.get('imdb_id') or 
                metadata.get('imdb_id') if metadata else None or 
                f"TMDB:{item.get('tmdb_id')}" if item.get('tmdb_id') else 'Unknown'
            )
            logging.error(f"Error processing item for show {show_id}: {str(e)}", exc_info=True)

    logging.info(f"Processed {len(processed_items['movies'])} movies and {len(processed_items['episodes'])} episodes")
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
                                item_dict['early_release'] = True
            elif media_type == 'episode':
                metadata, _ = DirectAPI.get_show_metadata(imdb_id)
                logging.info(f"Processing metadata for {title} S{season_number}E{episode_number}")
                
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
                                            logging.info(f"Converted UTC {first_aired_str} to local date {new_release_date}")
                                        except ValueError as e:
                                            logging.error(f"Invalid datetime format: {first_aired_str} - Error: {e}")
                                            new_release_date = 'Unknown'
                                    else:
                                        logging.warning("No first_aired date found in episode data")
                                        new_release_date = 'Unknown'
                
                logging.info(f"New release date: {new_release_date}")

                if new_release_date == "Unknown" or new_release_date is None:
                    new_state = "Wanted"
                else:
                    try:
                        release_date = datetime.strptime(new_release_date, "%Y-%m-%d").date()
                        today = datetime.now().date()
                        
                        # If it's an early release, set to Wanted regardless of release date
                        if item_dict.get('early_release', False):
                            new_state = "Wanted"
                            logging.info(f"Item is an early release, setting state to Wanted")
                        # Otherwise, set to Wanted only if it's past the release date
                        else:
                            new_state = "Wanted" if release_date <= today else "Unreleased"
                            logging.info(f"Item release date is {release_date}, today is {today}, setting state to {new_state}")
                    except ValueError:
                        new_state = "Wanted"

                logging.info(f"New state: {new_state}")

                if (new_state != item_dict['state'] or 
                    new_release_date != item_dict['release_date'] or 
                    item_dict.get('early_release', False) != item_dict.get('early_release_original', False) or
                    (media_type == 'movie' and new_physical_release_date != item_dict.get('physical_release_date_original'))):
                    
                    logging.info("Updating release date, state, physical release date, and early release flag in database")
                    update_release_date_and_state(
                        item_dict['id'], 
                        new_release_date, 
                        new_state, 
                        early_release=item_dict.get('early_release', False),
                        physical_release_date=new_physical_release_date if media_type == 'movie' else None
                    )
                    logging.info(f"Updated: {title} has a release date of: {new_release_date}" + 
                               (f" and physical release date of: {new_physical_release_date}" if media_type == 'movie' else ""))
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