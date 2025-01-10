import logging
import re
from typing import List, Dict, Any
from fuzzywuzzy import fuzz
from PTT import parse_title
from settings import get_setting
from scraper.functions.similarity_checks import improved_title_similarity, normalize_title
from scraper.functions.file_processing import compare_resolutions, parse_size, calculate_bitrate
from scraper.functions.other_functions import smart_search
from scraper.functions.adult_terms import adult_terms
from scraper.functions.common import *

def filter_results(results: List[Dict[str, Any]], tmdb_id: str, title: str, year: int, content_type: str, season: int, episode: int, multi: bool, version_settings: Dict[str, Any], runtime: int, episode_count: int, season_episode_counts: Dict[int, int], genres: List[str]) -> List[Dict[str, Any]]:

    filtered_results = []
    pre_size_filtered_results = []  # Track results before size filtering
    resolution_wanted = version_settings.get('resolution_wanted', '<=')
    max_resolution = version_settings.get('max_resolution', '2160p')
    min_size_gb = float(version_settings.get('min_size_gb', 0.01))
    max_size_gb = float(version_settings.get('max_size_gb', float('inf')) or float('inf'))
    filter_in = version_settings.get('filter_in', [])
    filter_out = version_settings.get('filter_out', [])
    enable_hdr = version_settings.get('enable_hdr', False)
    disable_adult = get_setting('Scraping', 'disable_adult', False)
    
    logging.debug(f"Starting filter_results with {len(results)} results")
    logging.debug(f"Version settings: resolution={max_resolution}({resolution_wanted}), size={min_size_gb}-{max_size_gb}GB, HDR={enable_hdr}")
    logging.debug(f"Filter patterns - in: {filter_in}, out: {filter_out}")
    
    # Pre-compile patterns
    filter_in_patterns = filter_in if filter_in else []
    filter_out_patterns = filter_out if filter_out else []
    adult_pattern = re.compile('|'.join(adult_terms), re.IGNORECASE) if disable_adult else None
    
    # Determine content type specific settings
    is_movie = content_type.lower() == 'movie'
    is_episode = content_type.lower() == 'episode'
    is_anime = genres and 'anime' in [genre.lower() for genre in genres]
    is_ufc = False
    
    # Pre-normalize query title
    normalized_query_title = normalize_title(title).lower()
    similarity_threshold = 0.35 if is_anime else 0.8
    
    logging.debug(f"Content type: {'movie' if is_movie else 'episode'}, Anime: {is_anime}, Title similarity threshold: {similarity_threshold}")
    
    # Cache season episode counts for multi-episode content
    total_episodes = sum(season_episode_counts.values()) if is_episode else 0
    
    for result in results:
        try:
            result['filter_reason'] = "Passed all filters"  # Default reason
            original_title = result.get('original_title', result.get('title', ''))
            logging.debug(f"\nProcessing result: {original_title}")
            
            # Quick UFC check
            if "UFC" in original_title.upper():
                is_ufc = True
                similarity_threshold = 0.35
                logging.debug("UFC content detected, lowering similarity threshold")
            
            # Get parsed info from result (should be already parsed by PTT)
            parsed_info = result.get('parsed_info', {})
            if not parsed_info:
                result['filter_reason'] = "Missing parsed info"
                logging.debug("❌ Failed: Missing parsed info")
                continue
            
            # Store original title in parsed_info
            parsed_info['original_title'] = original_title
            result['parsed_info'] = parsed_info
            
            # Title similarity check
            normalized_result_title = normalize_title(parsed_info.get('title', original_title)).lower()
            title_sim = fuzz.ratio(normalized_result_title, normalized_query_title) / 100.0
            logging.debug(f"Title similarity: {title_sim:.2f} ({normalized_result_title} vs {normalized_query_title})")
            
            if title_sim < similarity_threshold:
                result['filter_reason'] = f"Low title similarity: {title_sim:.2f}"
                logging.debug(f"❌ Failed: Title similarity {title_sim:.2f} below threshold {similarity_threshold}")
                continue
            logging.debug("✓ Passed title similarity check")
            
            # Resolution check
            detected_resolution = parsed_info.get('resolution', 'Unknown')
            if not resolution_filter(detected_resolution, max_resolution, resolution_wanted):
                result['filter_reason'] = f"Resolution mismatch (max: {max_resolution}, wanted: {resolution_wanted})"
                logging.debug(f"❌ Failed: Resolution {detected_resolution} doesn't match criteria {resolution_wanted} {max_resolution}")
                continue
            logging.debug("✓ Passed resolution check")
            
            # HDR check
            if not enable_hdr and parsed_info.get('is_hdr', False):
                result['filter_reason'] = "HDR content when HDR is disabled"
                logging.debug("❌ Failed: HDR content not allowed")
                continue
            logging.debug("✓ Passed HDR check")
            
            # Content type specific checks
            if is_movie and not is_ufc:
                parsed_year = parsed_info.get('year')
                if parsed_year:
                    if isinstance(parsed_year, list):
                        if not any(abs(int(py) - year) <= 1 for py in parsed_year):
                            result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                            logging.debug(f"❌ Failed: Year list {parsed_year} doesn't match {year}")
                            continue
                    elif abs(int(parsed_year) - year) > 1:
                        result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                        logging.debug(f"❌ Failed: Year {parsed_year} doesn't match {year}")
                        continue
                logging.debug("✓ Passed year check")
            
            elif is_episode:
                season_episode_info = parsed_info.get('season_episode_info', {})
                logging.debug(f"Season episode info: {season_episode_info}")
                
                # Check if title contains "complete" - consider it as having all episodes
                if 'complete' in original_title.lower():
                    logging.debug("Complete series pack detected")
                    season_episode_info['season_pack'] = 'Complete'
                    season_episode_info['seasons'] = list(season_episode_counts.keys())
                    season_episode_info['episodes'] = list(range(1, max(season_episode_counts.values()) + 1))
                    result['parsed_info']['season_episode_info'] = season_episode_info
                
                if multi:
                    logging.debug(f"Multi-episode mode: season={season}, season_pack={season_episode_info.get('season_pack')}, seasons={season_episode_info.get('seasons')}")
                    
                    episodes = season_episode_info.get('episodes', [])
                    if len(episodes) == 1:
                        result['filter_reason'] = "Single episode result when searching for multi"
                        logging.debug("❌ Failed: Single episode in multi mode")
                        continue

                    if episodes and episode not in episodes:
                        result['filter_reason'] = f"Multi-episode pack does not contain requested episode {episode}"
                        logging.debug(f"❌ Failed: Multi-pack missing episode {episode}")
                        continue

                    season_pack = season_episode_info.get('season_pack', 'Unknown')
                    if season_pack == 'Complete':
                        logging.debug("Complete series pack accepted")
                        pass
                    elif season_pack == 'N/A':
                        result['filter_reason'] = "Single episode result when searching for multi"
                        logging.debug("❌ Failed: Single episode in multi mode")
                        continue
                    elif season_pack == 'Unknown':
                        if len(episodes) < 2:
                            result['filter_reason'] = "Non-multi result when searching for multi"
                            logging.debug("❌ Failed: Not enough episodes for multi")
                            continue
                    else:
                        if season not in season_episode_info.get('seasons', []):
                            result['filter_reason'] = f"Season pack not containing the requested season: {season}"
                            logging.debug(f"❌ Failed: Season pack missing season {season}")
                            continue
                    logging.debug("✓ Passed multi-episode checks")
                else:
                    logging.debug(f"Single episode mode: S{season}E{episode}")
                    
                    result_seasons = season_episode_info.get('seasons', [])
                    result_episodes = season_episode_info.get('episodes', [])
                    
                    if not is_anime:
                        if not result_seasons or season not in result_seasons:
                            result['filter_reason'] = f"Season mismatch: expected S{season}, got {result_seasons}"
                            logging.debug(f"❌ Failed: Season mismatch {result_seasons}")
                            continue

                    if season_episode_info.get('multi_episode', False):
                        episode_range = result_episodes
                        if episode_range:
                            min_episode = min(episode_range)
                            max_episode = max(episode_range)
                            if not (min_episode <= episode <= max_episode):
                                result['filter_reason'] = f"Episode {episode} not in pack range {min_episode}-{max_episode}"
                                logging.debug(f"❌ Failed: Episode {episode} outside range {min_episode}-{max_episode}")
                                continue
                    else:
                        if not result_episodes or episode not in result_episodes:
                            result['filter_reason'] = f"Episode mismatch: expected E{episode}, got {result_episodes}"
                            logging.debug(f"❌ Failed: Episode mismatch {result_episodes}")
                            continue
                    logging.debug("✓ Passed episode checks")
            
            # Size calculation
            size_gb = parse_size(result.get('size', 0))
            if is_episode:
                scraper = result.get('scraper', '').lower()
                if scraper.startswith(('jackett', 'zilean')):
                    season_pack = season_episode_info.get('season_pack', 'Unknown')
                    if season_pack == 'N/A':
                        size_per_episode_gb = size_gb
                    else:
                        if season_pack == 'Complete':
                            episode_count = total_episodes
                        else:
                            season_numbers = [int(s) for s in season_pack.split(',')]
                            episode_count = sum(season_episode_counts.get(s, 0) for s in season_numbers)
                        size_per_episode_gb = size_gb / episode_count if episode_count > 0 else size_gb
                    result['size'] = size_per_episode_gb
                    bitrate = calculate_bitrate(size_per_episode_gb, runtime)
                else:
                    result['size'] = size_gb
                    bitrate = calculate_bitrate(size_gb, runtime)
            else:
                result['size'] = size_gb
                bitrate = calculate_bitrate(size_gb, runtime)
            
            result['bitrate'] = bitrate
            logging.debug(f"Size: {result['size']:.2f}GB, Bitrate: {bitrate:.2f}Mbps")
            
            # Add to pre-size filtered results
            pre_size_filtered_results.append(result)
            
            # Size filters
            if result['size'] > 0:
                if result['size'] < min_size_gb:
                    result['filter_reason'] = f"Size too small: {result['size']:.2f} GB (min: {min_size_gb} GB)"
                    logging.debug(f"❌ Failed: Size {result['size']:.2f}GB below minimum {min_size_gb}GB")
                    continue
                if result['size'] > max_size_gb:
                    result['filter_reason'] = f"Size too large: {result['size']:.2f} GB (max: {max_size_gb} GB)"
                    logging.debug(f"❌ Failed: Size {result['size']:.2f}GB above maximum {max_size_gb}GB")
                    continue
            logging.debug("✓ Passed size checks")
            
            # Pattern matching
            normalized_filter_title = normalize_title(original_title)
            
            if filter_out_patterns:
                matched_patterns = [pattern for pattern in filter_out_patterns if smart_search(pattern, normalized_filter_title)]
                if matched_patterns:
                    result['filter_reason'] = f"Matching filter_out pattern(s): {', '.join(matched_patterns)}"
                    logging.debug(f"❌ Failed: Matched filter_out patterns: {matched_patterns}")
                    continue
            
            if filter_in_patterns and not any(smart_search(pattern, normalized_filter_title) for pattern in filter_in_patterns):
                result['filter_reason'] = "Not matching any filter_in patterns"
                logging.debug("❌ Failed: No matching filter_in patterns")
                continue
            logging.debug("✓ Passed pattern checks")
            
            # Adult content check
            if adult_pattern and adult_pattern.search(original_title):
                result['filter_reason'] = "Adult content filtered"
                logging.debug("❌ Failed: Adult content detected")
                continue
            logging.debug("✓ Passed adult content check")
            
            # If we made it here, add to filtered results
            filtered_results.append(result)
            logging.debug("✓ Result accepted!")
            
        except Exception as e:
            logging.error(f"Error filtering result '{original_title}': {str(e)}", exc_info=True)
            result['filter_reason'] = f"Error during filtering: {str(e)}"
            continue
    
    logging.debug(f"\nFiltering complete: {len(filtered_results)}/{len(results)} results passed")
    return filtered_results, pre_size_filtered_results

def resolution_filter(result_resolution, max_resolution, resolution_wanted):
    comparison = compare_resolutions(result_resolution, max_resolution)
    if resolution_wanted == '<=':
        return comparison <= 0
    elif resolution_wanted == '==':
        return comparison == 0
    elif resolution_wanted == '>=':
        return comparison >= 0
    return False