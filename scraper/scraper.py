import logging
import re
from routes.api_tracker import api
from typing import List, Dict, Any, Tuple, Optional, Union
from difflib import SequenceMatcher
from utilities.settings import get_setting
import time
from database.database_reading import get_movie_runtime, get_episode_runtime, get_episode_count, get_all_season_episode_counts
from database.database_writing import update_anime_format, update_preferred_alias, get_preferred_alias
from fuzzywuzzy import fuzz
from metadata.metadata import get_tmdb_id_and_media_type, get_metadata, get_media_country_code
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

# Initialize DirectAPI at module level
direct_api = DirectAPI()

def convert_anime_episode_format(season: int, episode: int, total_episodes: int) -> Dict[str, str]:
    """Convert anime episode numbers into different formats."""
    logging.info(f"Converting anime episode format - Season: {season}, Episode: {episode}, Total Episodes: {total_episodes}")
    
    # No leading zeros format (x)
    no_zeros_format = f"{episode}"
    logging.info(f"No leading zeros format: {no_zeros_format}")

    # Regular season/episode format (SXXEXX)
    regular_format = f"S{season:02d}E{episode:02d}"
    logging.info(f"Regular format: {regular_format}")
    
    # Absolute episode format with E (EXXX)
    absolute_episode = ((season - 1) * total_episodes) + episode
    absolute_format_with_e = f"E{absolute_episode:03d}"
    logging.info(f"Absolute format with E: {absolute_format_with_e}")
    
    # Absolute episode format without E (XXX)
    absolute_format = f"{absolute_episode:03d}"
    logging.info(f"Absolute format without E: {absolute_format}")
    
    # Combined format (SXXEXXX)
    combined_format = f"S{season:02d}E{absolute_episode:03d}"
    logging.info(f"Combined format: {combined_format}")

    
    return {
        'no_zeros': no_zeros_format,
        'regular': regular_format,
        'absolute_with_e': absolute_format_with_e,
        'absolute': absolute_format,
        'combined': combined_format
    }

def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, version: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None, skip_cache_check: bool = False) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    logging.info(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, content_type={content_type}, version={version}, season={season}, episode={episode}, multi={multi}, genres={genres}, skip_cache_check={skip_cache_check}")

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
        is_anime = genres and 'anime' in [genre.lower() for genre in genres]
        episode_formats = None
        if is_anime and content_type.lower() == 'episode' and season is not None and episode is not None:
            logging.info(f"Detected anime content: {title}")
            season_episode_counts = get_all_season_episode_counts(tmdb_id)
            total_episodes = season_episode_counts.get(season, 13)  # Default to 13 if unknown
            logging.info(f"Total episodes for season {season}: {total_episodes}")
            episode_formats = convert_anime_episode_format(season, episode, total_episodes)
            logging.info(f"Generated episode formats for anime: {episode_formats}")

        # Initialize results lists
        all_filtered_results = []
        all_filtered_out_results = []

        # Parse scraping settings based on version to get language preference early
        scraping_versions = get_setting('Scraping', 'versions', {})
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
        language_setting = version_settings.get('language_code', '')
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
            content_type: str,
            version: str,
            version_settings: Dict[str, Any],
            season: int,
            episode: int,
            multi: bool,
            genres: List[str],
            episode_formats: Dict[str, str],
            is_anime: bool,
            is_alias: bool = False,
            alias_country: str = None,
            preferred_language: str = None,
            translated_title: str = None
        ) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Dict[str, float]]:
            start_time = time.time()
            task_timings = {}

            # Get country code for media item from metadata
            task_start = time.time()
            media_country_code = get_media_country_code(imdb_id, 'movie' if content_type.lower() == 'movie' else 'tv')
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
            task_timings['episode_counts'] = time.time() - task_start

            # Parse scraping settings based on version
            # version_settings already loaded above to get preferred_language
            # The correctly merged version_settings are now passed as an argument
            logging.info(f"Using scraping settings within _do_scrape for version '{version}': {version_settings}")

            # Use ScraperManager to handle scraping
            task_start = time.time()
            scraper_manager = ScraperManager(load_config())
            all_results = scraper_manager.scrape_all(
                imdb_id=imdb_id,
                title=search_title,
                year=year,
                content_type=content_type,
                season=season,
                episode=episode,
                multi=multi,
                genres=genres,
                episode_formats=episode_formats,
                tmdb_id=tmdb_id
            )
            task_timings['scraping'] = time.time() - task_start

            # Deduplicate results before filtering
            task_start = time.time()
            all_results = deduplicate_results(all_results)
            task_timings['deduplication'] = time.time() - task_start
            logging.debug(f"Total results after deduplication and before filtering: {len(all_results)}")

            task_start = time.time()
            
            # Extract titles and sizes for batch processing
            titles = [result.get('title', '') for result in all_results]
            sizes = [result.get('size', None) for result in all_results]
            
            # Batch process all titles
            parsed_results = batch_parse_torrent_info(titles, sizes)
            
            # Create normalized results
            normalized_results = []
            for result, parsed_info in zip(all_results, parsed_results):
                if 'parsing_error' in parsed_info or 'invalid_parse' in parsed_info:
                    logging.error(f"Error parsing title '{result.get('title', '')}'")
                    continue
                    
                normalized_result = result.copy()
                original_title = result.get('title', '')
                normalized_result['original_title'] = original_title  # Keep original torrent title
                normalized_result['parsed_title'] = parsed_info.get('title', original_title)  # Store parsed title separately
                normalized_result['resolution'] = parsed_info.get('resolution', 'Unknown')
                normalized_result['parsed_info'] = parsed_info
                if is_alias:
                    normalized_result['alias_country'] = alias_country
                normalized_results.append(normalized_result)
                
            task_timings['normalization'] = time.time() - task_start

            # Filter results
            task_start = time.time()
            filtered_results, pre_size_filtered_results = filter_results(
                normalized_results, tmdb_id, search_title, year, content_type,
                season, episode, multi, version_settings, runtime, episode_count,
                season_episode_counts, genres, matching_aliases,
                preferred_language=preferred_language,
                translated_title=translated_title
            )
            filtered_out_results = [result for result in normalized_results if result not in filtered_results]
            task_timings['filtering'] = time.time() - task_start

            return filtered_results, filtered_out_results, task_timings

        # First pass: Try original title, preferred alias, and translated title
        titles_to_try = [('original', title)]
        if not aliases_disabled and preferred_alias:
            # Ensure preferred alias is not the same as original title (case-insensitive)
            if preferred_alias.lower() != title.lower():
                titles_to_try.append(('preferred_alias', preferred_alias))
        if translated_title:
            # Ensure translated title is not the same as original or preferred alias (case-insensitive)
            if translated_title.lower() != title.lower() and (not preferred_alias or translated_title.lower() != preferred_alias.lower()):
                titles_to_try.append(('translated_title', translated_title))

        for source, search_title in titles_to_try:
            logging.info(f"First pass - Trying {source}: {search_title}")
            filtered_results, filtered_out_results, task_timings = _do_scrape(
                search_title=search_title,
                content_type=content_type,
                version=version,
                version_settings=version_settings,
                season=season,
                episode=episode,
                multi=multi,
                genres=genres,
                episode_formats=episode_formats,
                is_anime=is_anime,
                is_alias=(source != 'original'),
                alias_country=media_country_code if source != 'original' else None,
                preferred_language=preferred_language,
                translated_title=translated_title
            )
            
            if filtered_results:
                all_filtered_results.extend(filtered_results)
                if filtered_out_results:
                    all_filtered_out_results.extend(filtered_out_results)

        # If no results from first pass, try aliases
        if not aliases_disabled and not all_filtered_results and matching_aliases:
            logging.info("No results from first pass, trying aliases...")
            best_alias = None
            best_alias_results = []
            
            # Get the set of titles already tried to avoid retrying them as aliases
            tried_titles_lower = {t.lower() for _, t in titles_to_try}

            for alias in matching_aliases:
                # Skip if this alias was already tried (original, preferred, or translated) or is same as original
                if alias.lower() not in tried_titles_lower and alias.lower() != title.lower():
                    logging.info(f"Trying alias: {alias}")
                    filtered_results, filtered_out_results, task_timings = _do_scrape(
                        search_title=alias,
                        content_type=content_type,
                        version=version,
                        version_settings=version_settings,
                        season=season,
                        episode=episode,
                        multi=multi,
                        genres=genres,
                        episode_formats=episode_formats,
                        is_anime=is_anime,
                        is_alias=True,
                        alias_country=media_country_code,
                        preferred_language=preferred_language,
                        translated_title=translated_title
                    )
                    
                    if filtered_results and len(filtered_results) > len(best_alias_results):
                        logging.info(f"New best alias found: {alias} with {len(filtered_results)} results")
                        best_alias = alias
                        best_alias_results = filtered_results
                        if filtered_out_results:
                            all_filtered_out_results.extend(filtered_out_results)

            # If we found a better alias, update it as preferred and add its results
            if best_alias and best_alias_results:
                logging.info(f"Setting new preferred alias: {best_alias}")
                # Ensure alias update function exists and handles None season
                try:
                    update_preferred_alias(tmdb_id, imdb_id, best_alias, content_type, season)
                except Exception as alias_update_err:
                    logging.error(f"Error updating preferred alias: {alias_update_err}")
                all_filtered_results.extend(best_alias_results)

        # Deduplicate final results while preserving order
        seen = set()
        deduplicated_results = []
        for result in all_filtered_results:
            result_key = result.get('original_title', '')
            if result_key not in seen:
                seen.add(result_key)
                deduplicated_results.append(result)

        # Parse scraping settings for final sorting
        # version_settings already loaded and defaulted/merged above

        # Sort all results together
        def stable_rank_key(x):
            # Make sure is_anime flag is set in each result
            if is_anime and 'is_anime' not in x:
                x['is_anime'] = is_anime
            # Pass preferred_language and translated_title to rank_result_key (Modification needed in rank_results function signature)
            return rank_result_key(
                x, deduplicated_results, title, year, season, episode, multi,
                content_type, version_settings,
                preferred_language=preferred_language, # Pass new arg
                translated_title=translated_title     # Pass new arg
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

        # Log final results
        logging.debug(f"Total scrape results after trying all titles: {len(deduplicated_results)}")
        for result in deduplicated_results:
            logging.info(f"-- Final result: {result.get('original_title')}")

        return deduplicated_results, all_filtered_out_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return [], []  # Return empty lists in case of an error
