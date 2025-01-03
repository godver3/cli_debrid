import logging
import re
from api_tracker import api
from typing import List, Dict, Any, Tuple, Optional, Union
from difflib import SequenceMatcher
from settings import get_setting
import time
from database.database_reading import get_movie_runtime, get_episode_runtime, get_episode_count, get_all_season_episode_counts
from fuzzywuzzy import fuzz
from metadata.metadata import get_tmdb_id_and_media_type, get_metadata
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

def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, version: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    logging.info(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, content_type={content_type}, version={version}, season={season}, episode={episode}, multi={multi}, genres={genres}")

    #logging.info(f"Pre-filter genres: {genres}")
    genres = filter_genres(genres)
    #logging.info(f"Post-filter genres: {genres}")


    try:
        start_time = time.time()
        task_timings = {}  # Dictionary to store timing information
        all_results = []

        #logging.info(f"Starting scraping for: {title} ({year}), Version: {version}")

        # Ensure content_type is correctly set
        task_start = time.time()
        if content_type.lower() not in ['movie', 'episode']:
            logging.warning(f"Invalid content_type: {content_type}. Defaulting to 'movie'.")
            content_type = 'movie'
        task_timings['content_type_check'] = time.time() - task_start

        # Get media info for bitrate calculation
        task_start = time.time()
        media_item = {
            'title': title,
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

        #logging.debug(f"Retrieved runtime for {title}: {runtime} minutes, Episode count: {episode_count}")

        # Parse scraping settings based on version
        scraping_versions = get_setting('Scraping', 'versions', {})
        version_settings = scraping_versions.get(version, None)
        if version_settings is None:
            logging.warning(f"Version {version} not found in settings. Using default settings.")
            version_settings = {}
        #logging.debug(f"Using version settings: {version_settings}")

        task_start = time.time()
        original_title = title
        title = normalize_title(title)
        task_timings['title_normalization'] = time.time() - task_start

        #logging.info(f"Normalized title: {title}")
        #logging.info(f"Original title: {original_title}")

        # Use ScraperManager to handle scraping
        task_start = time.time()
        scraper_manager = ScraperManager(load_config())
        all_results = scraper_manager.scrape_all(imdb_id, title, year, content_type, season, episode, multi, genres)
        task_timings['scraping'] = time.time() - task_start

        #logging.debug(f"Total results before filtering: {len(all_results)}")

        # Deduplicate results before filtering
        task_start = time.time()
        all_results = deduplicate_results(all_results)
        task_timings['deduplication'] = time.time() - task_start
        logging.debug(f"Total results after deduplication and before filtering: {len(all_results)}")

        #logging.info(f"Starting normalization of {len(all_results)} results")
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
            normalized_result['original_title'] = original_title  # Store original title
            normalized_result['title'] = parsed_info.get('title', original_title)
            normalized_result['parsed_info'] = parsed_info
            normalized_results.append(normalized_result)
            
        task_timings['normalization'] = time.time() - task_start
        #logging.info(f"Normalization complete. Processed {len(normalized_results)}/{len(all_results)} results")
        
        # Continue with the rest of the function...

        #logging.info(f"Filtering {len(normalized_results)} results")

        # Filter results
        task_start = time.time()
        filtered_results, pre_size_filtered_results = filter_results(normalized_results, tmdb_id, title, year, content_type, season, episode, multi, version_settings, runtime, episode_count, season_episode_counts, genres)
        filtered_out_results = [result for result in normalized_results if result not in filtered_results]
        task_timings['filtering'] = time.time() - task_start

        #logging.debug(f"Filtering took {time.time() - task_start:.2f} seconds")
        #logging.info(f"Total results after filtering: {len(filtered_results)}")
        logging.info(f"Total filtered out results: {len(filtered_out_results)}")

        for result in filtered_out_results:
            logging.info(f"-- Filtered out result: {result.get('original_title')} --- {result.get('filter_reason', 'Unknown')}")

        # Add is_multi_pack information to each result
        for result in filtered_results:
            original_title = result.get('original_title', result.get('title', ''))
            result['title'] = original_title
            size = result.get('size', 0)
            parsed_info = parse_torrent_info(original_title, size)
            parsed_info['original_title'] = original_title  # Ensure original title is preserved
            result['parsed_info'] = parsed_info
            result['original_title'] = original_title  # Store at top level too
            preprocessed_title = preprocess_title(original_title)
            preprocessed_title = normalize_title(preprocessed_title)
            season_episode_info = detect_season_episode_info(preprocessed_title)
            season_pack = season_episode_info['season_pack']
            is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
            result['is_multi_pack'] = is_multi_pack
            result['season_pack'] = season_pack

        # Sort results
        task_start = time.time()
        sorting_start = time.time()

        def stable_rank_key(x):
            parsed_info = x.get('parsed_info', {})
            primary_key = rank_result_key(x, filtered_results, title, year, season, episode, multi, content_type, version_settings)
            secondary_keys = (
                x.get('scraper', ''),
                x.get('title', ''),
                x.get('size', 0),
                x.get('seeders', 0)
            )
            return (primary_key, secondary_keys)

        
        # Apply ultimate sort order if present
        if get_setting('Scraping', 'ultimate_sort_order')=='Size: large to small':
            logging.info(f"Applying ultimate sort order: Size: large to small")
            final_results = sorted(filtered_results, key=stable_rank_key)
            final_results = sorted(final_results, key=lambda x: x.get('size', 0), reverse=True)
        elif get_setting('Scraping', 'ultimate_sort_order')=='Size: small to large':
            logging.info(f"Applying ultimate sort order: Size: small to large")
            final_results = sorted(filtered_results, key=stable_rank_key)
            final_results = sorted(final_results, key=lambda x: x.get('size', 0))
        else:
            #logging.info(f"Applying default sort order: None")
            final_results = sorted(filtered_results, key=stable_rank_key)

        # Apply soft max size if present
        if not final_results and get_setting('Scraping', 'soft_max_size_gb'):
            logging.info(f"No results within size limits. Applying soft_max_size logic.")
            final_results = sorted(pre_size_filtered_results, key=stable_rank_key)

            final_results = sorted(final_results, key=lambda x: x.get('size', float('inf')))

            if final_results:
                logging.info(f"Found {len(final_results)} soft max size results.")
            else:
                logging.warning("No results found even with soft max size applied")

        task_timings['sorting'] = time.time() - task_start

        #logging.debug(f"Sorting took {time.time() - sorting_start:.2f} seconds")

        logging.debug(f"Total scrape results: {len(final_results)}")
        
        for result in final_results:
            logging.info(f"-- Final result: {result.get('original_title')}")
        #logging.debug(f"Total scraping process took {time.time() - start_time:.2f} seconds")


        # Log to scraper.log
        if content_type.lower() == 'episode':
            scraper_logger.info(f"Scraping results for: {title} ({year}) Season: {season} Episode: {episode} Multi: {multi} Version: {version}")
        else:
            scraper_logger.info(f"Scraping results for: {title} ({year}) Multi: {multi} Version: {version}")

        scraper_logger.info("All result titles:")
        for result in all_results:
            scraper_logger.info(f"- {result.get('title', '')}")

        scraper_logger.info("Filtered out results:")
        for result in filtered_out_results:
            filter_reason = result.get('filter_reason', 'Unknown reason')
            scraper_logger.info(f"- {result.get('title', '')}: {filter_reason}")

        scraper_logger.info("Final results:")
        for result in final_results:
            result_info = (
                f"- {result.get('title', '')}: "
                f"Size: {result.get('size', 'N/A')} GB, "
                f"Length: {result.get('runtime', 'N/A')} minutes, "
                f"Bitrate: {result.get('bitrate', 'N/A')} Mbps, "
                f"Multi-pack: {'Yes' if result.get('is_multi_pack', False) else 'No'}, "
                f"Season pack: {result.get('season_pack', 'N/A')}, "
                f"Source: {result.get('source', 'N/A')}"
            )
            scraper_logger.info(result_info)

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
            
            # Ensure score_breakdown is preserved
            if 'score_breakdown' in result:
                sanitized['score_breakdown'] = result['score_breakdown']
            
            return sanitized

        final_results = [sanitize_result(result) for result in final_results]
        filtered_out_results = [sanitize_result(result) for result in filtered_out_results] if filtered_out_results else None

        # Generate timing report at the end
        total_time = time.time() - start_time
        #logging.info("\n=== Scraping Performance Report ===")
        for task, duration in task_timings.items():
            percentage = (duration / total_time) * 100
            #logging.info(f"{task.replace('_', ' ').title()}: {duration:.2f}s ({percentage:.1f}%)")
        #logging.info(f"Total Scraping Time: {total_time:.2f}s")
        #logging.info("===============================\n")

        return final_results, filtered_out_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return [], []  # Return empty lists in case of an error
