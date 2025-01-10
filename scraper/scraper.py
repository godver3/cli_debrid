import logging
import re
from api_tracker import api
from typing import List, Dict, Any, Tuple, Optional, Union
from difflib import SequenceMatcher
from settings import get_setting
import time
from database.database_reading import get_movie_runtime, get_episode_runtime, get_episode_count, get_all_season_episode_counts
from database.database_writing import update_anime_format, update_preferred_alias, get_preferred_alias
from fuzzywuzzy import fuzz
from metadata.metadata import get_tmdb_id_and_media_type, get_metadata, get_media_country_code
import os
from utilities.plex_functions import filter_genres
from guessit import guessit
import pykakasi
from babelfish import Language
from .scraper_manager import ScraperManager
from config_manager import load_config
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

def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, version: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    logging.info(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, content_type={content_type}, version={version}, season={season}, episode={episode}, multi={multi}, genres={genres}")

    try:
        start_time = time.time()
        
        # Check for preferred alias first
        preferred_alias = get_preferred_alias(tmdb_id, imdb_id, content_type)
        if preferred_alias:
            logging.info(f"Found preferred alias: {preferred_alias}")
            title = preferred_alias

        # Get aliases if no preferred alias exists
        item_aliases = {}
        if content_type.lower() == 'movie':
            item_aliases, _ = direct_api.get_movie_aliases(imdb_id)
        else:
            item_aliases, _ = direct_api.get_show_aliases(imdb_id)
        logging.info(f"Alias list: {item_aliases}")

        media_country_code = get_media_country_code(imdb_id, 'movie' if content_type.lower() == 'movie' else 'tv')
        logging.info(f"Media country code (aliases): {media_country_code}")
        
        matching_aliases = []
        if item_aliases and media_country_code in item_aliases:
            matching_aliases = [alias for alias in item_aliases[media_country_code] if alias.lower() != title.lower()]
            logging.info(f"Matching aliases: {matching_aliases}")
        
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

        def _do_scrape(
            search_title: str,
            content_type: str,
            version: str,
            season: int,
            episode: int,
            multi: bool,
            genres: List[str],
            episode_formats: Dict[str, str],
            is_anime: bool,
            is_alias: bool = False,
            alias_country: str = None
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
            scraping_versions = get_setting('Scraping', 'versions', {})
            version_settings = scraping_versions.get(version, None)
            if version_settings is None:
                logging.warning(f"Version {version} not found in settings. Using default settings.")
                version_settings = {}

            task_start = time.time()
            normalized_title = normalize_title(search_title)
            task_timings['title_normalization'] = time.time() - task_start

            # Use ScraperManager to handle scraping
            task_start = time.time()
            scraper_manager = ScraperManager(load_config())
            all_results = scraper_manager.scrape_all(
                imdb_id=imdb_id,
                title=normalized_title,
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
                normalized_result['original_title'] = original_title
                normalized_result['title'] = parsed_info.get('title', original_title)
                normalized_result['parsed_info'] = parsed_info
                if is_alias:
                    normalized_result['alias_country'] = alias_country
                normalized_results.append(normalized_result)
                
            task_timings['normalization'] = time.time() - task_start

            # Filter results
            task_start = time.time()
            filtered_results, pre_size_filtered_results = filter_results(normalized_results, tmdb_id, normalized_title, year, content_type, season, episode, multi, version_settings, runtime, episode_count, season_episode_counts, genres)
            filtered_out_results = [result for result in normalized_results if result not in filtered_results]
            task_timings['filtering'] = time.time() - task_start

            # Process results
            for result in filtered_results:
                result['is_anime'] = is_anime
                result['media_country_code'] = media_country_code
                
                # Add multi-pack information
                original_title = result.get('original_title', result.get('title', ''))
                result['title'] = original_title  # Preserve the original title
                preprocessed_title = preprocess_title(original_title)
                preprocessed_title = normalize_title(preprocessed_title)
                season_episode_info = detect_season_episode_info(preprocessed_title)
                season_pack = season_episode_info['season_pack']
                is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
                result['is_multi_pack'] = is_multi_pack
                result['season_pack'] = season_pack

            # Sort results
            task_start = time.time()
            def stable_rank_key(x):
                return rank_result_key(x, filtered_results, normalized_title, year, season, episode, multi, content_type, version_settings)

            # Apply ultimate sort order if present
            if get_setting('Scraping', 'ultimate_sort_order')=='Size: large to small':
                filtered_results = sorted(filtered_results, key=stable_rank_key)
                filtered_results = sorted(filtered_results, key=lambda x: x.get('size', 0), reverse=True)
            elif get_setting('Scraping', 'ultimate_sort_order')=='Size: small to large':
                filtered_results = sorted(filtered_results, key=stable_rank_key)
                filtered_results = sorted(filtered_results, key=lambda x: x.get('size', 0))
            else:
                filtered_results = sorted(filtered_results, key=stable_rank_key)

            task_timings['sorting'] = time.time() - task_start

            return filtered_results, filtered_out_results, task_timings

        # First pass with original title or preferred alias
        filtered_results, filtered_out_results, task_timings = _do_scrape(
            search_title=title,
            content_type=content_type,
            version=version,
            season=season,
            episode=episode,
            multi=multi,
            genres=genres,
            episode_formats=episode_formats,
            is_anime=is_anime,
            is_alias=preferred_alias is not None,
            alias_country=media_country_code if preferred_alias else None
        )

        # If no results and we're not already using a preferred alias, try aliases
        if not filtered_results and not preferred_alias:
            logging.info(f"No valid results found with original title. Attempting to use aliases.")
            
            if matching_aliases:
                logging.info(f"Found {len(matching_aliases)} aliases to try: {matching_aliases}")
                for alias in matching_aliases:
                    logging.info(f"Trying alias: {alias}")
                    alias_results, alias_filtered_out, alias_timings = _do_scrape(
                        search_title=alias,
                        content_type=content_type,
                        version=version,
                        season=season,
                        episode=episode,
                        multi=multi,
                        genres=genres,
                        episode_formats=episode_formats,
                        is_anime=is_anime,
                        is_alias=True,
                        alias_country=media_country_code
                    )
                    if alias_results:
                        logging.info(f"Found {len(alias_results)} valid results using alias: {alias}")
                        # Store this successful alias as preferred for future use
                        update_preferred_alias(tmdb_id, imdb_id, alias, content_type)
                        filtered_results.extend(alias_results)
                        if alias_filtered_out:
                            filtered_out_results.extend(alias_filtered_out)
                        break
                    else:
                        logging.info(f"No valid results found with alias: {alias}")

        # Sanitize results for return
        def sanitize_result(result):
            def sanitize_value(value):
                if isinstance(value, (str, int, float, bool, type(None))):
                    return value
                elif isinstance(value, dict):
                    return {k: sanitize_value(v) for k, v in value.items()}
                elif isinstance(value, list):
                    return [sanitize_value(item) for item in value]
                return str(value)

            sanitized = {key: sanitize_value(value) for key, value in result.items()}
            if 'score_breakdown' in result:
                sanitized['score_breakdown'] = result['score_breakdown']
            return sanitized

        filtered_results = [sanitize_result(result) for result in filtered_results]
        filtered_out_results = [sanitize_result(result) for result in filtered_out_results] if filtered_out_results else None

        # Log final results
        logging.debug(f"Total scrape results: {len(filtered_results)}")
        for result in filtered_results:
            logging.info(f"-- Final result: {result.get('original_title')}")

        return filtered_results, filtered_out_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return [], []  # Return empty lists in case of an error
