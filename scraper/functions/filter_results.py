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

def filter_results(results: List[Dict[str, Any]], tmdb_id: str, title: str, year: int, content_type: str, season: int, episode: int, multi: bool, version_settings: Dict[str, Any], runtime: int, episode_count: int, season_episode_counts: Dict[int, int], genres: List[str], matching_aliases: List[str] = None) -> List[Dict[str, Any]]:

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
    #logging.debug(f"Version settings: resolution={max_resolution}({resolution_wanted}), size={min_size_gb}-{max_size_gb}GB, HDR={enable_hdr}")
    #logging.debug(f"Filter patterns - in: {filter_in}, out: {filter_out}")
    
    # Pre-compile patterns
    filter_in_patterns = filter_in if filter_in else []
    filter_out_patterns = filter_out if filter_out else []
    adult_pattern = re.compile('|'.join(adult_terms), re.IGNORECASE) if disable_adult else None
    
    # Determine content type specific settings
    is_movie = content_type.lower() == 'movie'
    is_episode = content_type.lower() == 'episode'
    is_anime = genres and 'anime' in [genre.lower() for genre in genres]
    is_ufc = False
    
    # Pre-normalize query title and aliases
    normalized_query_title = normalize_title(title).lower()
    normalized_aliases = [normalize_title(alias).lower() for alias in (matching_aliases or [])]
    similarity_threshold = float(version_settings.get('similarity_threshold_anime', 0.35)) if is_anime else float(version_settings.get('similarity_threshold', 0.8))
    
    #logging.debug(f"Content type: {'movie' if is_movie else 'episode'}, Anime: {is_anime}, Title similarity threshold: {similarity_threshold}")
    
    # Cache season episode counts for multi-episode content
    total_episodes = sum(season_episode_counts.values()) if is_episode else 0
    
    for result in results:
        try:
            result['filter_reason'] = "Passed all filters"  # Default reason
            original_title = result.get('original_title', result.get('title', ''))
            logging.debug(f"Processing result: {original_title}")
            
            # Quick UFC check
            if "UFC" in original_title.upper():
                is_ufc = True
                similarity_threshold = 0.35
                #logging.debug("UFC content detected, lowering similarity threshold")
            
            # Get parsed info from result (should be already parsed by PTT)
            parsed_info = result.get('parsed_info', {})
            if not parsed_info:
                result['filter_reason'] = "Missing parsed info"
                logging.debug("❌ Failed: Missing parsed info")
                continue
            
            # Store original title in parsed_info
            parsed_info['original_title'] = original_title
            
            # If season_episode_info is not in parsed_info, detect it
            if 'season_episode_info' not in parsed_info:
                from scraper.functions.common import detect_season_episode_info
                parsed_info['season_episode_info'] = detect_season_episode_info(original_title)
                logging.debug(f"Detected season_episode_info: {parsed_info['season_episode_info']}")
                
                # Special handling for documentary tags that might be misinterpreted as episode titles
                if 'episode_title' in parsed_info and parsed_info.get('episode_title', '').upper() == 'DOC':
                    # This is likely a documentary tag, not an episode title
                    parsed_info['documentary'] = True
                    del parsed_info['episode_title']
                    # Re-detect season/episode info after fixing the parsed_info
                    parsed_info['season_episode_info'] = detect_season_episode_info(parsed_info)
                    logging.debug(f"Corrected season_episode_info after DOC handling: {parsed_info['season_episode_info']}")
            
            result['parsed_info'] = parsed_info
            
            # Title similarity check
            normalized_result_title = normalize_title(parsed_info.get('title', original_title)).lower()
            normalized_query = normalized_query_title
            
            # If this is a documentary, add it back to the result title for comparison
            if parsed_info.get('documentary', False):
                normalized_result_title = f"{normalized_result_title} documentary"
            
            title_sim = fuzz.ratio(normalized_result_title, normalized_query) / 100.0
            
            # Check against main title and aliases
            if title_sim < similarity_threshold:
                # Try matching against aliases if available
                alias_similarities = [fuzz.ratio(normalized_result_title, alias) / 100.0 for alias in normalized_aliases]
                best_alias_sim = max(alias_similarities) if alias_similarities else 0
                
                if best_alias_sim >= similarity_threshold:
                    title_sim = best_alias_sim  # Use the best alias similarity
                    logging.debug(f"✓ Passed title similarity check via alias with score {title_sim:.2f}")
                else:
                    result['filter_reason'] = f"Title similarity too low (main={title_sim:.2f}, best_alias={best_alias_sim:.2f})"
                    logging.debug(f"❌ Failed: Title similarity {title_sim:.2f} below threshold {similarity_threshold}")
                    logging.debug(f"  - Main title comparison: '{normalized_result_title}' vs '{normalized_query_title}'")
                    if normalized_aliases:
                        logging.debug(f"  - Best alias comparison: '{normalized_result_title}' vs '{normalized_aliases[alias_similarities.index(best_alias_sim)]}'")
                    continue
            #logging.debug("✓ Passed title similarity check")
            
            # Resolution check
            detected_resolution = parsed_info.get('resolution', 'Unknown')
            if not resolution_filter(detected_resolution, max_resolution, resolution_wanted):
                result['filter_reason'] = f"Resolution mismatch (max: {max_resolution}, wanted: {resolution_wanted})"
                logging.debug(f"❌ Failed: Resolution {detected_resolution} doesn't match criteria {resolution_wanted} {max_resolution}")
                continue
            #logging.debug("✓ Passed resolution check")
            
            # HDR check
            if not enable_hdr and parsed_info.get('is_hdr', False):
                result['filter_reason'] = "HDR content when HDR is disabled"
                logging.debug("❌ Failed: HDR content not allowed")
                continue
            #logging.debug("✓ Passed HDR check")
            
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
                #logging.debug("✓ Passed year check")
            
            elif is_episode:
                # Add year check for TV shows
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

                season_episode_info = parsed_info.get('season_episode_info', {})
                #logging.debug(f"Season episode info: {season_episode_info}")
                
                # Check if title contains "complete" - consider it as having all episodes
                if 'complete' in original_title.lower():
                    #logging.debug("Complete series pack detected")
                    season_episode_info['season_pack'] = 'Complete'
                    season_episode_info['seasons'] = list(season_episode_counts.keys())
                    season_episode_info['episodes'] = list(range(1, max(season_episode_counts.values()) + 1))
                    result['parsed_info']['season_episode_info'] = season_episode_info
                
                if multi:
                    #logging.debug(f"Multi-episode mode: season={season}, season_pack={season_episode_info.get('season_pack')}, seasons={season_episode_info.get('seasons')}")
                    
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
                    if season_pack == 'N/A':
                        result['filter_reason'] = "Single episode result when searching for multi"
                        logging.debug("❌ Failed: Single episode in multi mode")
                        continue
                    elif season_pack == 'Complete':
                        #logging.debug("Complete series pack accepted")
                        pass
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
                    #logging.debug("✓ Passed multi-episode checks")
                else:
                    #logging.debug(f"Single episode mode: S{season}E{episode}")
                    
                    result_seasons = season_episode_info.get('seasons', [])
                    result_episodes = season_episode_info.get('episodes', [])
                    
                    if not is_anime:
                        if result_seasons and season not in result_seasons:
                            result['filter_reason'] = f"Season mismatch: expected S{season}, got {result_seasons}"
                            logging.debug(f"❌ Failed: Season mismatch - found {result_seasons} but needed {season}")
                            continue
                        elif not result_seasons:
                            logging.debug(f"⚠️ No season information found, will de-rank later")
                            
                        # Debug the season pack detection
                        season_pack = season_episode_info.get('season_pack', 'Unknown')
                        logging.debug(f"Season pack detection for '{result.get('title', '')}': {season_pack}")
                        logging.debug(f"Season info: {season_episode_info.get('seasons', [])} | Episode info: {season_episode_info.get('episodes', [])}")
                        
                        # Check for multi-season packs
                        if not multi and (season_pack == 'Complete' or (season_pack != 'N/A' and season_pack != 'Unknown' and ',' in season_pack)):
                            result['filter_reason'] = "Multi-season pack when searching for single episode"
                            logging.debug("❌ Failed: Multi-season pack in single episode mode")
                            continue
                        
                        # Check for single season packs (single season but no specific episode)
                        # This is the key check for season packs like "Below Deck 2013 S01 DOC FRENCH 1080p WEB H264-TFA"
                        if not multi and season_pack not in ['N/A', 'Unknown'] and not result_episodes:
                            # Check if the title contains the specific episode we're looking for
                            episode_pattern = f"S{season:02d}E{episode:02d}"
                            if not re.search(episode_pattern, result.get('title', ''), re.IGNORECASE):
                                result['filter_reason'] = "Season pack when searching for single episode"
                                logging.debug(f"❌ Failed: Season pack '{season_pack}' in single episode mode")
                                continue
                        
                        # Extra check: if we have season info but no episode info, and we're looking for a specific episode
                        if not multi and season_episode_info.get('seasons') and not season_episode_info.get('episodes'):
                            # This is likely a season pack
                            episode_pattern = f"S{season:02d}E{episode:02d}"
                            if not re.search(episode_pattern, result.get('title', ''), re.IGNORECASE):
                                result['filter_reason'] = "Season pack (has season but no episodes) when searching for single episode"
                                logging.debug(f"❌ Failed: Season pack with season {season_episode_info.get('seasons')} but no episodes in single episode mode")
                                continue
                            
                        # Also check if multiple episodes are detected
                        if not multi and len(result_episodes) > 1:
                            result['filter_reason'] = f"Multiple episodes detected: {result_episodes} when searching for single episode {episode}"
                            logging.debug(f"❌ Failed: Multiple episodes {result_episodes} in single episode mode")
                            continue
                    else:
                        # For anime, we need to handle season packs differently
                        # Mark this result as anime for ranking
                        result['is_anime'] = True
                        
                        season_pack = season_episode_info.get('season_pack', 'Unknown')
                        logging.debug(f"Anime season pack detection for '{result.get('title', '')}': {season_pack}")
                        logging.debug(f"Anime season info: {season_episode_info.get('seasons', [])} | Episode info: {season_episode_info.get('episodes', [])}")
                        
                        # Check for multi-season packs
                        if not multi and (season_pack == 'Complete' or (season_pack != 'N/A' and season_pack != 'Unknown' and ',' in season_pack)):
                            result['filter_reason'] = "Multi-season pack when searching for single episode"
                            logging.debug("❌ Failed: Multi-season pack in single episode mode")
                            continue
                        
                        # Check for single season packs (single season but no specific episode)
                        if not multi and season_pack not in ['N/A', 'Unknown'] and not result_episodes:
                            # Check if the title contains the specific episode we're looking for
                            episode_pattern = f"S{season:02d}E{episode:02d}"
                            if not re.search(episode_pattern, result.get('title', ''), re.IGNORECASE):
                                result['filter_reason'] = "Season pack when searching for single episode"
                                logging.debug(f"❌ Failed: Season pack '{season_pack}' in single episode mode")
                                continue
                        
                        # Extra check: if we have season info but no episode info, and we're looking for a specific episode
                        if not multi and season_episode_info.get('seasons') and not season_episode_info.get('episodes'):
                            # This is likely a season pack
                            episode_pattern = f"S{season:02d}E{episode:02d}"
                            if not re.search(episode_pattern, result.get('title', ''), re.IGNORECASE):
                                result['filter_reason'] = "Season pack (has season but no episodes) when searching for single episode"
                                logging.debug(f"❌ Failed: Season pack with season {season_episode_info.get('seasons')} but no episodes in single episode mode")
                                continue
                            
                        # Also check if multiple episodes are detected
                        if not multi and len(result_episodes) > 1:
                            result['filter_reason'] = f"Multiple episodes detected: {result_episodes} when searching for single episode {episode}"
                            logging.debug(f"❌ Failed: Multiple episodes {result_episodes} in single episode mode")
                            continue

                        # Special handling for anime based on the anime_format
                        anime_format = result.get('anime_format')
                        if anime_format and not result_seasons and not result_episodes:
                            # For anime with no detected season/episode, validate based on the format used
                            valid = False
                            
                            if anime_format == 'regular':
                                # Regular format should have correct season/episode
                                pattern = f"S{season:02d}E{episode:02d}"
                                valid = pattern.lower() in result.get('title', '').lower()
                                
                            elif anime_format == 'absolute' or anime_format == 'absolute_with_e':
                                # Calculate expected absolute episode number based on season episode counts if available
                                from web_scraper import get_all_season_episode_counts
                                try:
                                    season_episode_counts = get_all_season_episode_counts(tmdb_id)
                                    total_episodes_in_prev_seasons = sum(season_episode_counts.get(s, 13) for s in range(1, season))
                                    expected_abs_ep = total_episodes_in_prev_seasons + episode
                                except Exception as e:
                                    # Fallback to default 13 episodes per season if API call fails
                                    logging.warning(f"Failed to get episode counts, using default: {str(e)}")
                                    expected_abs_ep = ((season - 1) * 13) + episode
                                
                                # Check if the absolute episode number appears in the title
                                abs_pattern = f"{expected_abs_ep:03d}"
                                e_abs_pattern = f"E{expected_abs_ep:03d}"
                                valid = (abs_pattern in result.get('title', '') or 
                                         e_abs_pattern.lower() in result.get('title', '').lower())
                                
                            elif anime_format == 'no_zeros':
                                # Simple episode number format
                                valid = f" {episode} " in f" {result.get('title', '')} "
                                
                            elif anime_format == 'combined':
                                # Combined format (S01E018)
                                from web_scraper import get_all_season_episode_counts
                                try:
                                    season_episode_counts = get_all_season_episode_counts(tmdb_id)
                                    total_episodes_in_prev_seasons = sum(season_episode_counts.get(s, 13) for s in range(1, season))
                                    expected_abs_ep = total_episodes_in_prev_seasons + episode
                                except Exception as e:
                                    # Fallback to default 13 episodes per season if API call fails
                                    logging.warning(f"Failed to get episode counts, using default: {str(e)}")
                                    expected_abs_ep = ((season - 1) * 13) + episode
                                
                                pattern = f"S{season:02d}E{expected_abs_ep:03d}"
                                valid = pattern.lower() in result.get('title', '').lower()
                            
                            if not valid:
                                result['filter_reason'] = f"Anime format mismatch: {anime_format} format doesn't match S{season}E{episode}"
                                logging.debug(f"❌ Failed: Anime format mismatch - {anime_format} doesn't match S{season}E{episode}")
                                continue
                            else:
                                logging.debug(f"✓ Passed anime format validation with {anime_format}")
                        elif result_seasons and season not in result_seasons:
                            # Still do season validation for anime if season info is available
                            result['filter_reason'] = f"Season mismatch: expected S{season}, got {result_seasons}"
                            logging.debug(f"❌ Failed: Season mismatch - found {result_seasons} but needed {season}")
                            continue

                    if result_episodes and episode not in result_episodes:
                        result['filter_reason'] = f"Episode mismatch: expected E{episode}, got {result_episodes}"
                        logging.debug(f"❌ Failed: Episode mismatch {result_episodes}")
                        continue
                    #logging.debug("✓ Passed episode checks")
            
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
            #logging.debug(f"Size: {result['size']:.2f}GB, Bitrate: {bitrate:.2f}Mbps")
            
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
            #logging.debug("✓ Passed size checks")
            
            # Bitrate filters
            min_bitrate_mbps = float(version_settings.get('min_bitrate_mbps', 0.0))
            max_bitrate_mbps = float(version_settings.get('max_bitrate_mbps', float('inf')) or float('inf'))
            
            if result.get('bitrate', 0) > 0:
                if result['bitrate'] < min_bitrate_mbps:
                    result['filter_reason'] = f"Bitrate too low: {result['bitrate']:.2f} Mbps (min: {min_bitrate_mbps} Mbps)"
                    logging.debug(f"❌ Failed: Bitrate {result['bitrate']:.2f}Mbps below minimum {min_bitrate_mbps}Mbps")
                    continue
                if result['bitrate'] > max_bitrate_mbps:
                    result['filter_reason'] = f"Bitrate too high: {result['bitrate']:.2f} Mbps (max: {max_bitrate_mbps} Mbps)"
                    logging.debug(f"❌ Failed: Bitrate {result['bitrate']:.2f}Mbps above maximum {max_bitrate_mbps}Mbps")
                    continue
            #logging.debug("✓ Passed bitrate checks")
            
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
            #logging.debug("✓ Passed pattern checks")
            
            # Adult content check
            if adult_pattern and adult_pattern.search(original_title):
                result['filter_reason'] = "Adult content filtered"
                logging.debug("❌ Failed: Adult content detected")
                continue
            #logging.debug("✓ Passed adult content check")
            
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