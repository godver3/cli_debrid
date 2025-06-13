import logging
import re
from routes.api_tracker import api
from typing import List, Dict, Any, Tuple, Optional, Union
from difflib import SequenceMatcher
from utilities.settings import get_setting
import time
from datetime import datetime, timedelta, timezone
from database.database_reading import get_movie_runtime, get_episode_runtime, get_episode_count, get_all_season_episode_counts
from database.database_writing import update_anime_format, update_preferred_alias, get_preferred_alias
from fuzzywuzzy import fuzz
import os
from utilities.plex_functions import filter_genres
import pykakasi
from babelfish import Language
from .scraper_manager import ScraperManager
from queues.config_manager import load_config
import unicodedata
import sys
from PTT import parse_title
from pathlib import Path
from scraper.functions import *
from cli_battery.app.direct_api import DirectAPI
import json
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    # Fallback or raise error if zoneinfo is critical and not available
    logging.warning("zoneinfo module not found. Timezone conversion for air dates might be limited.")
    ZoneInfo = None # Define ZoneInfo as None to handle checks later
    ZoneInfoNotFoundError = Exception # Use base Exception for catch block

# Initialize DirectAPI at module level
direct_api = DirectAPI()

def convert_anime_episode_format(season: int, episode: int, season_episode_counts: Dict[int, int]) -> Dict[str, str]:
    """Convert anime episode numbers into different formats, including correctly padded absolute numbers."""
    logging.info(f"Converting anime episode format - Season: {season}, Episode: {episode}, Counts: {season_episode_counts}")

    # --- Calculate Absolute Episode Number ---
    absolute_episode = 0
    # Sort seasons to ensure correct order
    # Ensure keys are integers and handle potential non-integer keys from bad metadata
    sorted_seasons = sorted([s for s in season_episode_counts.keys() if isinstance(s, int) and s < season])
    for s_num in sorted_seasons:
        absolute_episode += season_episode_counts.get(s_num, 0)
    absolute_episode += episode
    logging.info(f"Calculated absolute episode number: {absolute_episode}")

    # --- Determine Padding for Absolute Number ---
    total_show_episodes = sum(season_episode_counts.values()) if season_episode_counts else 0
    padding = 4 if total_show_episodes > 999 else 3
    logging.info(f"Total show episodes: {total_show_episodes}, using padding: {padding}")
    
    # No leading zeros format (x)
    no_zeros_format = f"{episode}"
    logging.info(f"No leading zeros format: {no_zeros_format}")

    # Regular season/episode format (SXXEXX)
    regular_format = f"S{season:02d}E{episode:02d}"
    logging.info(f"Regular format: {regular_format}")
    
    # Absolute episode format with E (EXXX or EXXXX)
    absolute_format_with_e = f"E{absolute_episode:0{padding}d}"
    logging.info(f"Absolute format with E: {absolute_format_with_e}")
    
    # Absolute episode format without E (XXX or XXXX)
    absolute_format = f"{absolute_episode:0{padding}d}"
    logging.info(f"Absolute format without E: {absolute_format}")
    
    # Combined format (SXXEXXX or SXXEXXXX)
    combined_format = f"S{season:02d}E{absolute_episode:0{padding}d}"
    logging.info(f"Combined format: {combined_format}")
    
    formats = {
        'no_zeros': no_zeros_format,
        'regular': regular_format,
        'absolute_with_e': absolute_format_with_e,
        'absolute': absolute_format,
        'combined': combined_format
    }

    # For very long running shows, also add a simple absolute episode search
    # This helps catch formats like "One Piece 1071" vs "One Piece 01071"
    if padding == 4:
        formats['absolute_no_padding'] = str(absolute_episode)

    return formats

def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, version: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None, skip_cache_check: bool = False, check_pack_wantedness: bool = False) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    from metadata.metadata import get_tmdb_id_and_media_type, get_metadata, get_media_country_code
    logging.info(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, content_type={content_type}, version={version}, season={season}, episode={episode}, multi={multi}, genres={genres}, skip_cache_check={skip_cache_check}, check_pack_wantedness={check_pack_wantedness}")
    logging.debug(f"[scrape_main] Initializing for '{title}' ({year}).")

    # Store original season/episode and initialize scene numbers
    original_season = season
    original_episode = episode 
    scene_season = None
    scene_episode = None
    
    xem_applied = False # Flag to track if XEM logic actually modified/confirmed season/episode

    # NEW: Fetch show's season episode structure for accurate num_items calculation in ranking
    show_season_episode_counts_for_query = {} # Default to empty
    if content_type.lower() == 'episode':
        try:
            # This function is already available and used in database_reading.py
            show_season_episode_counts_for_query = get_all_season_episode_counts(tmdb_id)
            logging.info(f"Fetched show_season_episode_counts for {tmdb_id} (for ranking num_items): {show_season_episode_counts_for_query}")
        except Exception as e:
            logging.error(f"Failed to get_all_season_episode_counts for {tmdb_id} (for ranking num_items): {e}", exc_info=True)
            # show_season_episode_counts_for_query remains empty on error

    try:
        start_time = time.time()
        
        # Initialize language variables
        preferred_language = None
        translated_title = None
        media_country_code = None # Initialize

        # Handle "No Version" case
        if version == "No Version":
            version = None
            
        # Get media country code - needed regardless of alias usage
        try:
            media_country_code = get_media_country_code(imdb_id, 'movie' if content_type.lower() == 'movie' else 'tv')
            logging.info(f"Media country code: {media_country_code}")
        except Exception as country_err:
            logging.error(f"Error getting media country code for {imdb_id}: {country_err}", exc_info=True)

        # Check if alias usage is disabled
        aliases_disabled = os.path.exists(os.path.join(os.path.dirname(__file__), '.alias_disabled'))
        if aliases_disabled:
            logging.info("Alias usage is temporarily disabled")
            preferred_alias = None
            matching_aliases = []
        else:
            # Get preferred alias first
            preferred_alias = get_preferred_alias(tmdb_id, imdb_id, content_type, season)
            if preferred_alias:
                logging.info(f"Found preferred alias: {preferred_alias}")

            # Get all available aliases
            item_aliases = {}
            if content_type.lower() == 'movie':
                item_aliases, _ = direct_api.get_movie_aliases(imdb_id)
            else:
                item_aliases, _ = direct_api.get_show_aliases(imdb_id)

            # media_country_code already fetched above
            
            matching_aliases = []
            if item_aliases and media_country_code in item_aliases:
                matching_aliases = [alias for alias in item_aliases[media_country_code] if alias.lower() != title.lower()]
                matching_aliases = list(dict.fromkeys(matching_aliases))
                logging.info(f"Found {len(matching_aliases)} matching aliases: {matching_aliases}")

        # Initialize anime-specific variables
        genres = filter_genres(genres)
        is_anime = genres and any('anime' in g.lower() for g in genres)
        episode_formats = None
        if is_anime and content_type.lower() == 'episode' and season is not None:
            # For season packs (multi=True), we don't have an episode number, but we need to generate
            # formats to find the pack. We'll use episode 1 of the requested season as a proxy.
            # For single episodes, the episode number will be present.
            target_episode = episode if episode is not None else 1

            logging.info(f"Detected anime content: {title}")
            season_episode_counts = get_all_season_episode_counts(tmdb_id)
            episode_formats = convert_anime_episode_format(season, target_episode, season_episode_counts)
            
            if multi:
                logging.info(f"Generated episode formats for anime season pack search (using E{target_episode}): {episode_formats}")
            else:
                logging.info(f"Generated episode formats for anime episode: {episode_formats}")

        # --- XEM Mapping Lookup ---
        target_air_date = None # Initialize target air date
        if content_type.lower() == 'episode' and season is not None and original_episode is not None:
            try:
                logging.info(f"Checking for XEM mapping for {title} S{season}E{original_episode} (IMDb: {imdb_id})")
                show_metadata, meta_source = direct_api.get_show_metadata(imdb_id)
                airs_data = None # Initialize airs_data
                if show_metadata:
                    # Store airs data if available
                    airs_data = show_metadata.get('airs')
                    xem_mapping_list = show_metadata.get('xem_mapping')
                    trakt_seasons_data = show_metadata.get('seasons') # Get Trakt season structure

                    # --- Extract Target Air Date ---
                    try:
                        target_air_date = None # Initialize to ensure it's defined in all paths

                        if isinstance(trakt_seasons_data, dict) and season in trakt_seasons_data:
                            current_season_trakt_data = trakt_seasons_data[season]

                            if isinstance(current_season_trakt_data, dict) and \
                               'episodes' in current_season_trakt_data and \
                               isinstance(current_season_trakt_data['episodes'], dict):
                                
                                episodes_dict_for_season = current_season_trakt_data['episodes']
                                
                                if original_episode in episodes_dict_for_season:
                                    episode_data = episodes_dict_for_season[original_episode]
                                    
                                    if isinstance(episode_data, dict) and 'first_aired' in episode_data:
                                        air_date_full_utc_str = episode_data['first_aired']
                                        if isinstance(air_date_full_utc_str, str) and air_date_full_utc_str:
                                            # --- START Timezone-Aware Air Date Calculation ---
                                            try:
                                                # 1. Parse the UTC timestamp string
                                                if air_date_full_utc_str.endswith('Z'):
                                                     air_date_full_utc_str = air_date_full_utc_str[:-1] + '+00:00'

                                                utc_dt = datetime.fromisoformat(air_date_full_utc_str)
                                                if utc_dt.tzinfo is None:
                                                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                                                elif utc_dt.tzinfo.utcoffset(utc_dt) != timedelta(0):
                                                    utc_dt = utc_dt.astimezone(timezone.utc)

                                                show_timezone_str = None
                                                if ZoneInfo and isinstance(airs_data, dict) and isinstance(airs_data.get('timezone'), str):
                                                    show_timezone_str = airs_data['timezone']

                                                if show_timezone_str:
                                                    try:
                                                        target_tz = ZoneInfo(show_timezone_str)
                                                        local_dt = utc_dt.astimezone(target_tz)
                                                        target_air_date = local_dt.strftime('%Y-%m-%d')
                                                        logging.info(f"Calculated target air date using show timezone '{show_timezone_str}': {target_air_date} for S{season}E{original_episode}")
                                                    except ZoneInfoNotFoundError:
                                                        logging.warning(f"Show timezone '{show_timezone_str}' not found. Falling back to UTC date.")
                                                    except Exception as tz_conv_err:
                                                        logging.error(f"Error converting UTC to show timezone '{show_timezone_str}': {tz_conv_err}. Falling back to UTC date.", exc_info=True)
                                                else:
                                                    logging.info(f"Show timezone info not available or invalid in airs_data: {airs_data}. Falling back to UTC date.")

                                                if target_air_date is None:
                                                    target_air_date = utc_dt.strftime('%Y-%m-%d')
                                                    logging.info(f"Using UTC date as target air date: {target_air_date} for S{season}E{original_episode}")

                                            except ValueError as format_err:
                                                logging.error(f"Could not parse 'first_aired' UTC string '{air_date_full_utc_str}': {format_err}")
                                                target_air_date = None 
                                            except Exception as conv_err:
                                                logging.error(f"Unexpected error during air date calculation for S{season}E{original_episode}: {conv_err}", exc_info=True)
                                                target_air_date = None
                                            # --- END Timezone-Aware Air Date Calculation ---
                                        elif air_date_full_utc_str is None:
                                             logging.warning(f"Target episode S{season}E{original_episode} has a null 'first_aired' date.")
                                             # target_air_date remains None
                                        else:
                                             logging.warning(f"'first_aired' for S{season}E{original_episode} is not a string or is empty: {air_date_full_utc_str}")
                                             # target_air_date remains None
                                    else:
                                        logging.warning(f"Could not find 'first_aired' key or valid value in episode data for S{season}E{original_episode}. Episode data: {episode_data}")
                                        # target_air_date remains None
                                else:
                                    logging.warning(f"Could not find episode key {original_episode} (integer) in 'episodes' for season {season}. Available episodes: {list(episodes_dict_for_season.keys())}")
                                    # target_air_date remains None
                            else:
                                logging.warning(f"Season {season} data in Trakt metadata does not contain a valid 'episodes' dictionary. Season data keys: {list(current_season_trakt_data.keys()) if isinstance(current_season_trakt_data, dict) else type(current_season_trakt_data)}")
                                # target_air_date remains None
                        else:
                             logging.warning(f"Could not find season {season} (integer key) data in Trakt metadata or trakt_seasons_data is not a dictionary. trakt_seasons_data type: {type(trakt_seasons_data)}")
                             # target_air_date remains None
                    except Exception as date_err:
                         logging.error(f"Error extracting target air date for S{season}E{original_episode}: {date_err}", exc_info=True)
                         target_air_date = None # Ensure it's None on outer exception
                    # --- End Extract Target Air Date ---

                    if isinstance(xem_mapping_list, list):
                        logging.info(f"Found XEM mapping list with {len(xem_mapping_list)} entries in metadata.")
                        found_mapping = False
                        
                        # --- 1. Attempt Standard SxxExx Lookup --- 
                        logging.debug(f"Attempting standard XEM lookup for TVDB S{season}E{original_episode}")
                        for mapping_entry in xem_mapping_list:
                            tvdb_info = mapping_entry.get('tvdb')
                            scene_info = mapping_entry.get('scene')
                            if tvdb_info and scene_info and \
                               tvdb_info.get('season') == season and \
                               tvdb_info.get('episode') == original_episode:
                                
                                scene_season = scene_info.get('season')
                                scene_ep = scene_info.get('episode')
                                
                                if scene_season is not None and scene_ep is not None and \
                                   (scene_season != season or scene_ep != original_episode):
                                    logging.info(f"XEM Standard Mapping Found: TVDB S{season}E{original_episode} maps to Scene S{scene_season}E{scene_ep}")
                                    # Update the season and episode numbers to be used for scraping
                                    season = scene_season 
                                    episode = scene_ep
                                    found_mapping = True
                                    xem_applied = True # Mark XEM as applied
                                    break # Stop searching once mapping is found
                                elif scene_season is not None and scene_ep is not None:
                                    # Mapping exists but scene number is the same
                                    logging.info(f"XEM Standard Mapping Found: TVDB S{season}E{original_episode} maps to Scene S{scene_season}E{scene_ep} (no change needed). ")
                                    found_mapping = True
                                    xem_applied = True # Mark XEM as confirmed
                                    break
                        
                        # --- 2. Attempt Absolute Lookup (if standard failed and Trakt data available) --- 
                        if not found_mapping and isinstance(trakt_seasons_data, dict):
                            logging.debug(f"Standard XEM lookup failed. Calculating Trakt absolute number for S{season}E{original_episode}")
                            # Calculate Trakt absolute episode number
                            trakt_absolute_ep = 0
                            calculated_trakt_abs = False
                            try:
                                # Iterate through Trakt seasons in order (ensure keys are integers for sorting)
                                # Keys are already integers, directly sort them
                                sorted_trakt_seasons = sorted(k for k in trakt_seasons_data.keys() if isinstance(k, int))
                                for s_num in sorted_trakt_seasons:
                                    # Access using integer key s_num
                                    if s_num < season:
                                        # Use 'episode_count' if available, else count episodes
                                        # Access season data using integer key s_num
                                        count = trakt_seasons_data[s_num].get('episode_count')
                                        if count is None:
                                             # Access episodes using integer key s_num
                                             count = len(trakt_seasons_data[s_num].get('episodes', {}))
                                        trakt_absolute_ep += count
                                    elif s_num == season:
                                        trakt_absolute_ep += original_episode
                                        calculated_trakt_abs = True
                                        break
                                    else: # s_num > season (shouldn't happen if S/E valid)
                                        break 
                            except Exception as abs_calc_err:
                                logging.error(f"Error calculating Trakt absolute episode number: {abs_calc_err}")
                                calculated_trakt_abs = False
                            
                            if calculated_trakt_abs:
                                logging.info(f"Calculated Trakt absolute episode: {trakt_absolute_ep}. Attempting absolute XEM lookup.")
                                for mapping_entry in xem_mapping_list:
                                    tvdb_info = mapping_entry.get('tvdb')
                                    scene_info = mapping_entry.get('scene')
                                    # Match based on TVDB absolute number
                                    if tvdb_info and scene_info and tvdb_info.get('absolute') == trakt_absolute_ep:
                                        scene_season = scene_info.get('season')
                                        scene_ep = scene_info.get('episode')
                                        tvdb_abs = tvdb_info.get('absolute') # Get TVDB absolute from mapping

                                        # --- Prioritize TVDB Absolute for Anime --- 
                                        if is_anime and tvdb_abs is not None:
                                            if tvdb_abs != original_episode or scene_season != season:
                                                 logging.info(f"XEM Absolute Mapping (Anime Priority): Trakt Abs {trakt_absolute_ep} maps to Scene S{scene_season}E{scene_ep}. Targeting TVDB ABS number {tvdb_abs}.")
                                                 season = scene_season # Still use scene season
                                                 episode = scene_ep # CORRECT: Use the scene episode number from the mapping
                                                 found_mapping = True
                                                 xem_applied = True # Mark XEM as applied
                                                 break 
                                            else:
                                                 logging.info(f"XEM Absolute Mapping (Anime Priority): Trakt Abs {trakt_absolute_ep} maps to Scene S{scene_season}E{scene_ep}. Using TVDB ABS {tvdb_abs} (no change needed). ")
                                                 # Ensure episode is set correctly even if no change needed in S/E for consistency
                                                 season = scene_season
                                                 episode = scene_ep
                                                 found_mapping = True
                                                 xem_applied = True # Mark XEM as confirmed
                                                 break
                                        # --- Fallback/Non-Anime: Use Scene SxxExx ---
                                        elif scene_season is not None and scene_ep is not None: 
                                            if scene_season != season or scene_ep != original_episode:
                                                logging.info(f"XEM Absolute Mapping (Standard): TVDB Absolute {trakt_absolute_ep} maps to Scene S{scene_season}E{scene_ep}")
                                                season = scene_season
                                                episode = scene_ep
                                                found_mapping = True
                                                xem_applied = True # Mark XEM as applied
                                                break
                                            else:
                                                logging.info(f"XEM Absolute Mapping (Standard): TVDB Absolute {trakt_absolute_ep} maps to Scene S{scene_season}E{scene_ep} (no S/E change needed). ")
                                                found_mapping = True
                                                xem_applied = True # Mark XEM as confirmed
                                                break
                        
                        if not found_mapping:
                             logging.info(f"No specific XEM mapping entry found for Trakt S{season}E{original_episode} via standard or absolute lookup.")
                    else:
                        logging.info(f"No XEM mapping list found in metadata for {imdb_id}.")

                    # --- START ADDED LOGGING ---
                    logging.info(f"Pre-Scrape Check: Using target_air_date='{target_air_date}' for comparison after calculation/heuristic.")
                    # --- END ADDED LOGGING ---

                else:
                    logging.warning(f"Could not retrieve show metadata for {imdb_id} to check XEM mapping or air date.")
            except Exception as e:
                logging.error(f"Error during XEM mapping or air date lookup for {imdb_id} S{season}E{original_episode}: {e}", exc_info=True)
        
        # Store the final season/episode used after potential XEM mapping
        if xem_applied:
            scene_season = season
            scene_episode = episode
            # --- START ADDED LOGGING ---
            logging.info(f"Pre-Scrape Check: Using XEM-mapped season={scene_season}, episode={scene_episode} for scraping.")
            # --- END ADDED LOGGING ---
        else:
            # --- START ADDED LOGGING ---
            logging.info(f"Pre-Scrape Check: No XEM mapping applied. Using original season={original_season}, episode={original_episode} for scraping.")
            # --- END ADDED LOGGING ---
            # Ensure season/episode passed to _do_scrape are the originals if no XEM
            season = original_season
            episode = original_episode

        # Initialize results lists
        all_filtered_results = []
        all_filtered_out_results = []
        logging.debug(f"[scrape_main] Initialized all_filtered_results: {len(all_filtered_results)}, all_filtered_out_results: {len(all_filtered_out_results)}")

        # Parse scraping settings based on version to get language preference early
        scraping_versions = get_setting('Scraping', 'versions', {})

        # Strip asterisks from version if it exists
        if version:
            version = version.strip('*')

        version_settings = scraping_versions.get(version, {})
        if not version_settings: # Use default settings if version not found
            logging.info(f"Version '{version}' not found, using default settings.")
            # Define default version settings here if needed, or rely on _do_scrape defaults
            # Example default:
            version_settings = {
                'enable_hdr': True,
                'max_resolution': '2160p',
                'resolution_wanted': '<=',
                'language_code': None # Default language
                # Add other default settings as necessary
            }

        # Fetch translated title based on language settings
        language_setting = version_settings.get('language_code') or '' # Ensure language_setting is a string
        languages_to_try = [lang.strip() for lang in language_setting.split(',') if lang.strip() and lang.strip().lower() != 'en']
        logging.info(f"Languages to attempt translation for (from version '{version}' settings): {languages_to_try}")

        preferred_language = None # Language for which translation was successful
        translated_title = None

        if languages_to_try:
            for lang_code in languages_to_try:
                try:
                    logging.info(f"Attempting to fetch translated title for language: {lang_code}")
                    current_translated_title = None
                    if content_type.lower() == 'movie':
                        current_translated_title, _ = direct_api.get_movie_title_translation(imdb_id, lang_code)
                    else: # episode or show
                        current_translated_title, _ = direct_api.get_show_title_translation(imdb_id, lang_code)

                    if current_translated_title:
                        translated_title = current_translated_title
                        preferred_language = lang_code # Store the successful language code
                        logging.info(f"Found translated title ({preferred_language}): {translated_title}")
                        break # Stop searching once a translation is found
                    else:
                        logging.info(f"No translated title found for language: {lang_code}")
                except Exception as e:
                    logging.error(f"Error fetching translated title for {imdb_id} language {lang_code}: {e}", exc_info=True)
                    # Continue to the next language if an error occurs

            if not translated_title:
                logging.info(f"No translated title found for any of the specified languages: {languages_to_try}")

        def _do_scrape(
            search_title: str,
            original_media_title: str,
            content_type: str,
            version: str,
            version_settings: Dict[str, Any],
            season: int,
            episode: int,
            multi: bool,
            genres: List[str],
            episode_formats: Dict[str, str],
            is_anime: bool,
            imdb_id_for_fallback: str,
            direct_api_instance: DirectAPI,
            is_alias: bool = False,
            alias_country: str = None,
            preferred_language: str = None,
            translated_title: str = None,
            target_air_date: Optional[str] = None,
            scene_season_map: Optional[int] = None,
            scene_episode_map: Optional[int] = None,
            check_pack_wantedness: bool = False
        ) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Dict[str, float]]:
            start_time = time.time()
            task_timings = {}
            logging.debug(f"[_do_scrape] Starting for search_title: '{search_title}'.")

            # Get country code for media item from metadata
            task_start = time.time()
            media_country_code = get_media_country_code(imdb_id_for_fallback, 'movie' if content_type.lower() == 'movie' else 'tv')
            task_timings['country_code_lookup'] = time.time() - task_start
            logging.info(f"Media country code from metadata: {media_country_code}")

            # Ensure content_type is correctly set
            task_start = time.time()
            if content_type.lower() not in ['movie', 'episode']:
                logging.warning(f"Invalid content_type: {content_type}. Defaulting to 'movie'.")
                content_type = 'movie'
            task_timings['content_type_check'] = time.time() - task_start

            # Get media info for bitrate calculation
            task_start = time.time()
            media_item = {
                'title': search_title,
                'media_type': 'movie' if content_type.lower() == 'movie' else 'episode',
                'tmdb_id': tmdb_id
            }
            enhanced_media_items = get_media_info_for_bitrate([media_item])
            if enhanced_media_items:
                episode_count = enhanced_media_items[0]['episode_count']
                runtime = enhanced_media_items[0]['runtime']
            else:
                episode_count = 1
                runtime = 100 if content_type.lower() == 'movie' else 30
            task_timings['media_info'] = time.time() - task_start

            # Pre-calculate episode counts for TV shows
            task_start = time.time()
            season_episode_counts = {}
            if content_type.lower() == 'episode':
                season_episode_counts = get_all_season_episode_counts(tmdb_id)
                logging.debug(f"Fetched season_episode_counts for tmdb_id '{tmdb_id}': {season_episode_counts}") # Keep this one for now
            task_timings['episode_counts'] = time.time() - task_start

            # Parse scraping settings based on version
            # version_settings already loaded above to get preferred_language
            # The correctly merged version_settings are now passed as an argument
            logging.info(f"Using scraping settings within _do_scrape for version '{version}': {version_settings}")

            # Use ScraperManager to handle scraping
            task_start = time.time()
            scraper_manager = ScraperManager(load_config())
            # Determine if the current search is using the translated title
            is_translated = bool(translated_title and search_title == translated_title)
            all_results = scraper_manager.scrape_all(
                imdb_id=imdb_id_for_fallback,
                title=search_title,
                year=year,
                content_type=content_type,
                season=season,
                episode=episode,
                multi=multi,
                genres=genres,
                episode_formats=episode_formats,
                tmdb_id=tmdb_id,
                is_translated_search=is_translated, # Pass the flag here
                is_anime=is_anime # Pass the is_anime flag
            )
            task_timings['scraping'] = time.time() - task_start
            logging.debug(f"[_do_scrape] scraper_manager.scrape_all for '{search_title}' returned: {len(all_results)} raw items.")

            # Deduplicate results before filtering
            task_start = time.time()
            all_results = deduplicate_results(all_results)
            task_timings['deduplication'] = time.time() - task_start
            logging.debug(f"[_do_scrape] After deduplication for '{search_title}': {len(all_results)} items.")
            logging.debug(f"Total results after deduplication and before filtering: {len(all_results)}")

            task_start = time.time()
            
            # Extract titles and sizes for batch processing
            titles = [result.get('title', '') for result in all_results]
            sizes = [result.get('size', None) for result in all_results]
            
            # Batch process all titles
            parsed_results = batch_parse_torrent_info(titles, sizes)
            
            # Create normalized results and capture parsing failures
            normalized_results = []
            items_that_failed_normalization = [] # New list to capture parsing failures

            for result, parsed_info in zip(all_results, parsed_results):
                if 'parsing_error' in parsed_info or 'invalid_parse' in parsed_info:
                    logging.error(f"Error parsing title '{result.get('title', '')}' for scrape of '{search_title}'")
                    items_that_failed_normalization.append(result)
                    continue
                    
                # --- Populate scraper_type and scraper_instance from source if missing ---
                current_scraper_type = result.get('scraper_type')
                current_scraper_instance = result.get('scraper_instance')
                source_field = result.get('source')

                if isinstance(source_field, str) and source_field: # Check if source_field is a non-empty string
                    if not current_scraper_type or current_scraper_type == 'N/A':
                        if '_' in source_field:
                            parts = source_field.rsplit('_', 1)
                            result['scraper_type'] = parts[0]
                            if len(parts) > 1:
                                result['scraper_instance'] = parts[1]
                            else:
                                result['scraper_instance'] = 'Unknown' # Should not happen if '_' is present
                        else:
                            # If source does not contain '_', assume source is the scraper_type and instance is unknown
                            result['scraper_type'] = source_field
                            result['scraper_instance'] = 'Unknown' # Or a default instance identifier
                    elif (not current_scraper_instance or current_scraper_instance == 'N/A') and '_' in source_field:
                        # Scraper type might be present, but instance might be missing.
                        # This case is less likely if type is already there but let's be safe.
                        parts = source_field.rsplit('_', 1)
                        if len(parts) > 1 and (parts[0] == current_scraper_type or current_scraper_type is None or current_scraper_type == 'N/A'):
                            # Only update instance if type matches or type was also missing
                            if current_scraper_type is None or current_scraper_type == 'N/A':
                                result['scraper_type'] = parts[0] # Update type as well if it was missing
                            result['scraper_instance'] = parts[1]
                    
                normalized_result = result.copy()
                original_title = result.get('title', '')
                normalized_result['original_title'] = original_title  # Keep original torrent title
                normalized_result['parsed_title'] = parsed_info.get('title', original_title)  # Store parsed title separately
                normalized_result['resolution'] = parsed_info.get('resolution', 'Unknown')
                normalized_result['parsed_info'] = parsed_info
                # --- Pass through original target absolute episode (for anime XEM reverse look-up) ---
                if episode_formats and isinstance(episode_formats, dict):
                    abs_str = episode_formats.get('absolute_no_padding') or episode_formats.get('absolute')
                    if abs_str and str(abs_str).isdigit():
                        normalized_result['target_abs_episode'] = int(abs_str.lstrip('0') or '0')
                if is_alias:
                    normalized_result['alias_country'] = alias_country
                normalized_results.append(normalized_result)
                
            task_timings['normalization'] = time.time() - task_start

            # Filter results
            task_start = time.time()
            # --- Pass imdb_id_for_fallback and direct_api_instance ---
            filtered_results, pre_size_filtered_results = filter_results(
                normalized_results, tmdb_id, original_media_title, year, content_type,
                season, episode, multi, version_settings, runtime, episode_count,
                season_episode_counts, genres, matching_aliases,
                imdb_id=imdb_id_for_fallback, 
                direct_api=direct_api_instance, 
                preferred_language=preferred_language,
                translated_title=translated_title,
                target_air_date=target_air_date,
                check_pack_wantedness=check_pack_wantedness,
                current_scrape_target_version=version # Pass the 'version' from _do_scrape's scope
            )
            filtered_out_results = [result for result in normalized_results if result not in filtered_results]
            task_timings['filtering'] = time.time() - task_start

            # Combine all types of filtered out items for this specific scrape attempt
            comprehensive_filtered_out_list = filtered_out_results + items_that_failed_normalization
            logging.debug(f"[_do_scrape] For '{search_title}', comprehensive_filtered_out_list length: {len(comprehensive_filtered_out_list)}")

            # --- Attach scene mapping info to results --- 
            if scene_season_map is not None and scene_episode_map is not None:
                logging.debug(f"Attaching scene mapping S{scene_season_map}E{scene_episode_map} to {len(filtered_results)} results.")
                for result in filtered_results:
                    result['xem_scene_mapping'] = {'season': scene_season_map, 'episode': scene_episode_map}
            # --- End attaching scene mapping --- 

            logging.info(f"_do_scrape task timings for '{search_title}': {task_timings}")
            logging.debug(f"[_do_scrape] Returning for '{search_title}': passed_final_filters={len(filtered_results)}, comprehensive_filtered_out_list={len(comprehensive_filtered_out_list)}")
            return filtered_results, comprehensive_filtered_out_list, task_timings # Return the comprehensive list

        # Determine titles to scrape with
        titles_to_try = []
        tried_titles_lower = set()

        # 1. Add original title
        logging.info(f"Adding original title for scraping: {title}")
        titles_to_try.append(('original', title))
        tried_titles_lower.add(title.lower())

        # 2. Add translated title
        if translated_title and translated_title.lower() not in tried_titles_lower:
            logging.info(f"Adding translated title: {translated_title}")
            titles_to_try.append(('translated_title', translated_title))
            tried_titles_lower.add(translated_title.lower())

        # 3. Add preferred alias
        if not aliases_disabled and preferred_alias and preferred_alias.lower() not in tried_titles_lower:
            logging.info(f"Adding preferred alias: {preferred_alias}")
            titles_to_try.append(('preferred_alias', preferred_alias))
            tried_titles_lower.add(preferred_alias.lower())

        # 4. Add all other matching country aliases
        if not aliases_disabled and matching_aliases:
            for alias in matching_aliases:
                if alias.lower() not in tried_titles_lower:
                    logging.info(f"Adding country alias: {alias}")
                    titles_to_try.append(('country_alias', alias))
                    tried_titles_lower.add(alias.lower())

        # Execute scraping based on the determined titles
        for source, search_title in titles_to_try:
            logging.info(f"Scraping with {source}: {search_title}")
            logging.debug(f"[scrape_main] Calling _do_scrape for '{search_title}' (source: {source}).")

            # --- START ADDED LOGGING ---
            logging.info(f"Calling _do_scrape with: search_title='{search_title}', original_media_title='{title}', season={season}, episode={episode}, target_air_date='{target_air_date}'")
            # --- END ADDED LOGGING ---

            filtered_results, filtered_out_results, task_timings = _do_scrape(
                search_title=search_title,
                original_media_title=title, # Pass the original title here
                content_type=content_type,
                version=version,
                version_settings=version_settings,
                season=season, # Use potentially XEM-modified season
                episode=episode, # Use potentially XEM-modified episode
                multi=multi,
                genres=genres,
                episode_formats=episode_formats,
                is_anime=is_anime,
                imdb_id_for_fallback=imdb_id,
                direct_api_instance=direct_api,
                is_alias=(source != 'original'),
                alias_country=media_country_code if source != 'original' else None,
                preferred_language=preferred_language,
                translated_title=translated_title, # Always pass the actual translation
                target_air_date=target_air_date, # Pass target_air_date here
                # Pass the determined scene mapping (or None) to _do_scrape
                scene_season_map=scene_season, # This is the XEM-mapped season (or None)
                scene_episode_map=scene_episode, # This is the XEM-mapped episode (or None)
                check_pack_wantedness=check_pack_wantedness # Pass parameter
            )
            logging.debug(f"[scrape_main] _do_scrape for '{search_title}' returned: passed={len(filtered_results)}, filtered_out={len(filtered_out_results if filtered_out_results else [])}")
            
            if filtered_results:
                all_filtered_results.extend(filtered_results)
            if filtered_out_results: # Ensure filtered_out_results is not None
                all_filtered_out_results.extend(filtered_out_results)
            logging.debug(f"[scrape_main] After '{search_title}' scrape: all_filtered_results: {len(all_filtered_results)}, all_filtered_out_results: {len(all_filtered_out_results)}")

        # Deduplicate final results while preserving order
        logging.debug(f"[scrape_main] Before final deduplication: all_filtered_results: {len(all_filtered_results)}")
        seen = set()
        deduplicated_results = []
        for result in all_filtered_results:
            result_key = result.get('original_title', '')
            if result_key not in seen:
                seen.add(result_key)
                deduplicated_results.append(result)
        logging.debug(f"[scrape_main] After final deduplication (these are 'passed' results): deduplicated_results: {len(deduplicated_results)}")

        # Parse scraping settings for final sorting
        # version_settings already loaded and defaulted/merged above

        # Sort all results together
        def stable_rank_key(x):
            # Make sure is_anime flag is set in each result
            if is_anime and 'is_anime' not in x:
                x['is_anime'] = is_anime
            # Pass preferred_language and translated_title to rank_result_key (Modification needed in rank_results function signature)
            return rank_result_key(
                x, deduplicated_results, title, year, scene_season if xem_applied else original_season, scene_episode if xem_applied else original_episode, multi, # Pass original title to ranker
                content_type, version_settings,
                preferred_language=preferred_language, # Pass new arg
                translated_title=translated_title,     # Pass new arg
                show_season_episode_counts=show_season_episode_counts_for_query # MODIFIED: Pass the fetched counts
            )

        # Apply ultimate sort order if present
        if get_setting('Scraping', 'ultimate_sort_order')=='Size: large to small':
            deduplicated_results = sorted(deduplicated_results, key=stable_rank_key)
            deduplicated_results = sorted(deduplicated_results, key=lambda x: x.get('size', 0), reverse=True)
        elif get_setting('Scraping', 'ultimate_sort_order')=='Size: small to large':
            deduplicated_results = sorted(deduplicated_results, key=stable_rank_key)
            deduplicated_results = sorted(deduplicated_results, key=lambda x: x.get('size', 0))
        else:
            deduplicated_results = sorted(deduplicated_results, key=stable_rank_key)

        # --- Apply Minimum Scrape Score Filter ---
        minimum_scrape_score_setting = get_setting('Scraping', 'minimum_scrape_score', 0.0)
        if minimum_scrape_score_setting != 0.0: # Check if the filter is enabled (not the default disabled value)
            initial_count = len(deduplicated_results)
            results_passing_score = []
            for result in deduplicated_results:
                total_score = result.get('score_breakdown', {}).get('total_score')
                if total_score is not None and total_score >= minimum_scrape_score_setting:
                    results_passing_score.append(result)
                else:
                    # Log rejection due to score
                    logging.info(f"Rejected due to score: Score {total_score} < Minimum {minimum_scrape_score_setting} for '{result.get('original_title')}'")

            deduplicated_results = results_passing_score
            final_count = len(deduplicated_results)
            if initial_count != final_count:
                 logging.info(f"Applied minimum scrape score filter ({minimum_scrape_score_setting}): {initial_count} -> {final_count} results.")
        # --- End Minimum Scrape Score Filter ---

        # Log final results
        logging.info(f"Final sorted results for '{title}' ({year}): {len(deduplicated_results)}")
        for result in deduplicated_results:
            score = result.get('score_breakdown', {}).get('total_score', 'N/A')
            logging.info(f"  - Score: {score} | Title: {result.get('original_title')}")

        # --- SCALE SCORES TO 0.01 - 100 ---
        if get_setting('Debug', 'scale_final_scores', False):
            scale_total_scores(deduplicated_results)

        logging.info(f"[scrape_main] Returning: passed_results={len(deduplicated_results)}, filtered_out_results={len(all_filtered_out_results)}")
        return deduplicated_results, all_filtered_out_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        logging.debug(f"[scrape_main] Error, returning empty lists.")
        return [], []  # Return empty lists in case of an error

def scale_total_scores(results: List[Dict[str, Any]], score_key: str = 'score_breakdown.total_score', min_val: float = 0.01, max_val: float = 100.0):
    """
    Linearly scale the total_score in each result's score_breakdown to [min_val, max_val].
    Modifies results in-place.
    """
    def get_nested(d, path):
        for k in path.split('.'):
            d = d[k]
        return d

    def set_nested(d, path, value):
        keys = path.split('.')
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = value

    scores = []
    for r in results:
        try:
            scores.append(get_nested(r, score_key))
        except Exception:
            continue

    if not scores:
        return  # Nothing to scale

    min_score = min(scores)
    max_score = max(scores)

    for r in results:
        try:
            raw = get_nested(r, score_key)
            if max_score == min_score:
                scaled = max_val
            else:
                scaled = min_val + (raw - min_score) * (max_val - min_val) / (max_score - min_score)
            set_nested(r, score_key, round(scaled, 2))
        except Exception:
            continue
