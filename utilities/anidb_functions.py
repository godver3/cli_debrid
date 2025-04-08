import logging
import sys
import os
from typing import Dict, Any, Optional, List
import requests
import time
from datetime import datetime, timedelta
import re

# Add the root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utilities.settings import get_setting

# Jikan API constants
JIKAN_API_URL = "https://api.jikan.moe/v4"
MIN_REQUEST_INTERVAL = timedelta(seconds=1)  # Jikan has a rate limit of 60 requests per minute
last_request_time = datetime.min

# Cache for episode data
_episode_cache: Dict[int, List[Dict[str, Any]]] = {}
_cache_expiry: Dict[int, datetime] = {}
CACHE_DURATION = timedelta(hours=24)  # Cache episode data for 24 hours

def _make_request(endpoint: str, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """Make a rate-limited request to Jikan API."""
    global last_request_time
    
    try:
        # Respect rate limiting
        now = datetime.now()
        time_since_last = now - last_request_time
        if time_since_last < MIN_REQUEST_INTERVAL:
            sleep_time = (MIN_REQUEST_INTERVAL - time_since_last).total_seconds()
            logging.debug(f"[AniDB] Rate limiting: Sleeping for {sleep_time:.2f} seconds.")
            time.sleep(sleep_time)
        
        # Make request
        url = f"{JIKAN_API_URL}/{endpoint}"
        logging.debug(f"[AniDB] Making Jikan request: URL={url}, Params={params}")
        response = requests.get(url, params=params, timeout=10)
        last_request_time = datetime.now()
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            logging.warning("[AniDB] Jikan API rate limit hit (429). Waiting and retrying once...")
            time.sleep(5) # Wait longer after a 429
            response = requests.get(url, params=params, timeout=15)
            last_request_time = datetime.now()
            if response.status_code == 200:
                 logging.info("[AniDB] Jikan retry successful.")
                 return response.json()
            else:
                 logging.error(f"[AniDB] Jikan retry failed: Status={response.status_code}, URL={url}, Response={response.text}")
                 return None
        else:
            logging.error(f"[AniDB] Jikan request failed: Status={response.status_code}, URL={url}, Response={response.text}")
            return None
            
    except requests.exceptions.Timeout:
        logging.error(f"[AniDB] Jikan request timed out: URL={url}")
        return None
    except Exception as e:
        logging.error(f"[AniDB] Error making Jikan request to {url}: {str(e)}", exc_info=True)
        return None

def _search_anime(title: str) -> Optional[Dict[str, Any]]:
    """Search for an anime by title using Jikan API."""
    try:
        # Try searching with common season indicators removed first
        clean_title = re.sub(r'\b(season|s)\s*\d+\b', '', title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\b(part|pt)\s*\d+\b', '', clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'\b\d+(nd|rd|th)\s+(season|part)\b', '', clean_title, flags=re.IGNORECASE).strip()
        
        search_titles = [title]
        if clean_title.lower() != title.lower():
             search_titles.append(clean_title)
             
        best_match = None
        
        for search_term in search_titles:
            params = {
                'q': search_term,
                'type': 'tv',  # Focus on TV series
                'limit': 5     # Get a few matches to potentially find the right season
            }
            logging.debug(f"[AniDB] Searching Jikan for anime title: '{search_term}'")
            result = _make_request('anime', params)
            
            if result and result.get('data'):
                 # Simple heuristic: prefer exact title match if possible, otherwise first result
                 # In the future, could add more complex matching logic here if needed
                 found_exact = False
                 for anime in result['data']:
                      if anime.get('title','').lower() == title.lower():
                           best_match = anime
                           found_exact = True
                           logging.info(f"[AniDB] Found exact title match on Jikan for '{search_term}': '{anime.get('title')}' (MAL ID: {anime.get('mal_id')})")
                           break # Stop searching this term
                 
                 if found_exact:
                      break # Stop searching other terms
                      
                 # If no exact match, take the first result of the first successful search
                 if not best_match:
                      best_match = result['data'][0]
                      logging.info(f"[AniDB] Best Jikan match for '{search_term}' (no exact match found): '{best_match.get('title')}' (MAL ID: {best_match.get('mal_id')})")
                      # Continue searching other terms in case a later one yields an exact match
                 
        if not best_match:
            logging.warning(f"[AniDB] No anime found on Jikan for title variations: '{title}'")
            return None
            
        logging.info(f"[AniDB] Final selected Jikan match for '{title}': '{best_match.get('title')}' (MAL ID: {best_match.get('mal_id')})")
        return best_match
        
    except Exception as e:
        logging.error(f"[AniDB] Error searching anime '{title}': {str(e)}", exc_info=True)
        return None

def _get_episode_details(mal_id: int, episode_number: int) -> Optional[Dict[str, Any]]:
    """Get episode details from Jikan API."""
    try:
        now = datetime.now()
        
        # Check cache first
        if mal_id in _episode_cache and mal_id in _cache_expiry and now <= _cache_expiry[mal_id]:
            logging.debug(f"[AniDB] Using cached episodes for MAL ID {mal_id}")
            # Find the specific episode from cache
            logging.debug(f"[AniDB] Searching cache for MAL ID {mal_id}, Episode {episode_number}")
            for episode in _episode_cache[mal_id]:
                # Jikan uses 'mal_id' for episode number within the episode list endpoint response
                if episode.get('mal_id') == episode_number: 
                    logging.info(f"[AniDB] Found episode details from Jikan cache: MAL ID={mal_id}, Episode#={episode_number}, Title='{episode.get('title')}'")
                    return episode
            logging.warning(f"[AniDB] Episode {episode_number} not found in Jikan cache for MAL ID {mal_id}. Will attempt fetch if needed, but may indicate inconsistency.")
            # Don't return None yet, allow fetch attempt below if cache was just stale
            
        # --- Fetching logic ---
        logging.debug(f"[AniDB] Cache miss or expired/not found for MAL ID {mal_id}. Fetching episodes page by page.")
        
        all_episodes = []
        page = 1
        while True:
            params = {'page': page}
            result = _make_request(f'anime/{mal_id}/episodes', params=params)
            
            if not result or not result.get('data'):
                if page == 1: # Failed on the first page
                    logging.warning(f"[AniDB] Failed to fetch any episodes for MAL ID {mal_id} (page 1).")
                else: # Reached end of pagination
                    logging.debug(f"[AniDB] Finished fetching episodes for MAL ID {mal_id}. Total pages: {page-1}.")
                break # Exit loop
                
            episodes_on_page = result['data']
            all_episodes.extend(episodes_on_page)
            logging.debug(f"[AniDB] Fetched page {page} with {len(episodes_on_page)} episodes for MAL ID {mal_id}.")
            
            # Check pagination
            pagination = result.get('pagination', {})
            if not pagination.get('has_next_page'):
                logging.debug(f"[AniDB] No more episode pages for MAL ID {mal_id}.")
                break # Exit loop
                
            page += 1
            # Add a small delay between page fetches to be kind to the API
            time.sleep(0.5) 

        if not all_episodes:
             logging.warning(f"[AniDB] No episodes were ultimately fetched for MAL ID {mal_id}.")
             return None

        # Cache the fetched episodes
        _episode_cache[mal_id] = all_episodes
        _cache_expiry[mal_id] = now + CACHE_DURATION
        logging.info(f"Cached {len(_episode_cache[mal_id])} episodes for anime {mal_id} after fetching.")
        
        # --- Search Fetched/Cached Data ---
        logging.debug(f"[AniDB] Searching fetched/updated cache for MAL ID {mal_id}, Episode {episode_number}")
        for episode in _episode_cache[mal_id]:
            # Jikan uses 'mal_id' for episode number within the episode list endpoint response
            if episode.get('mal_id') == episode_number: 
                logging.info(f"[AniDB] Found episode details from Jikan (fetched/updated cache): MAL ID={mal_id}, Episode#={episode_number}, Title='{episode.get('title')}'")
                return episode
                
        logging.warning(f"[AniDB] Episode {episode_number} not found in Jikan data for MAL ID {mal_id} even after fetch.")
        return None
        
    except Exception as e:
        logging.error(f"[AniDB] Error getting episode details for MAL ID {mal_id}, Episode {episode_number}: {str(e)}", exc_info=True)
        return None

def _get_related_anime(mal_id: int) -> List[Dict[str, Any]]:
    """Get related anime to check for split cours/seasons."""
    try:
        logging.debug(f"[AniDB:_get_related_anime] Fetching relations for MAL ID: {mal_id}")
        result = _make_request(f'anime/{mal_id}/relations')
        if not result or not result.get('data'):
            logging.debug(f"[AniDB:_get_related_anime] No relations data found for MAL ID: {mal_id}")
            return []
            
        # Look for sequels and related series
        related = []
        for relation in result['data']:
            relation_type = relation.get('relation')
            # Include more relation types that might indicate a sequence
            if relation_type in ['Sequel', 'Prequel', 'Alternative version', 'Parent story', 'Other']: 
                for entry in relation.get('entry', []):
                    if entry.get('type') == 'anime':
                        entry['relation_type'] = relation_type # Add relation type for context
                        related.append(entry)
                        logging.debug(f"[AniDB:_get_related_anime] Found related '{relation_type}' anime for MAL ID {mal_id}: '{entry.get('name')}' (MAL ID: {entry.get('mal_id')})")
                        
        logging.debug(f"[AniDB:_get_related_anime] Found {len(related)} potentially relevant relations for MAL ID: {mal_id}")
        return related
        
    except Exception as e:
        logging.error(f"Error getting related anime for MAL ID {mal_id}: {str(e)}")
        return []

def _find_correct_season_mal_id(base_anime_data: Dict[str, Any], target_season: int) -> Optional[int]:
    """
    Given a base anime entry and a target season number, find the MAL ID 
    corresponding to that season by checking relations.
    Returns the MAL ID for the target season, or None if not found.
    """
    base_mal_id = base_anime_data.get('mal_id')
    base_title = base_anime_data.get('title', 'Unknown')
    logging.debug(f"[_find_correct_season_mal_id] Starting search. Base MAL ID: {base_mal_id} ('{base_title}'), Target Season: {target_season}")

    if not base_mal_id:
        logging.warning("[_find_correct_season_mal_id] Base MAL ID is missing.")
        return None

    # Simple check: If the base title itself indicates the target season, return its ID
    base_title_lower = base_title.lower()
    patterns = [rf'\bseason\s+{target_season}\b', rf'\b{target_season}(st|nd|rd|th)\s+season\b', rf'\bs{target_season}\b']
    if any(re.search(pattern, base_title_lower) for pattern in patterns):
        logging.debug(f"[_find_correct_season_mal_id] Base title '{base_title}' matches target season {target_season}. Returning base MAL ID: {base_mal_id}")
        return base_mal_id
        
    # Check if the base entry is likely Season 1 (no 'season X' in title, target season > 1)
    is_likely_base_s1 = target_season > 1 and not any(re.search(rf'\bseason\s+\d+\b|\b\d+(st|nd|rd|th)\s+season\b|\bs\d+\b', base_title_lower) for i in range(2, 10))

    if target_season == 1:
        # If target is S1, assume the base MAL ID is correct unless it explicitly says otherwise
        if not any(re.search(rf'\bseason\s+([2-9])\b|\b([2-9])(st|nd|rd|th)\s+season\b|\bs([2-9])\b', base_title_lower)):
             logging.debug(f"[_find_correct_season_mal_id] Target is Season 1, and base title '{base_title}' doesn't indicate a later season. Assuming base MAL ID {base_mal_id} is correct.")
             return base_mal_id
        else:
             logging.debug(f"[_find_correct_season_mal_id] Target is Season 1, but base title '{base_title}' indicates a later season. Need to check prequels.")
             # Fall through to relation check

    logging.debug(f"[_find_correct_season_mal_id] Base MAL ID {base_mal_id} title doesn't match S{target_season}. Checking relations...")
    related = _get_related_anime(base_mal_id)
    
    # TODO: Implement a more robust traversal (e.g., build a graph)
    # Simple approach for now: Look for sequels if base is likely S1, look for prequels otherwise.

    if is_likely_base_s1:
        # Look for sequels matching target_season - 1 depth
        # This assumes a linear S1 -> S2 -> S3... structure in relations
        current_mal_id = base_mal_id
        current_season = 1
        
        while current_season < target_season:
            logging.debug(f"[_find_correct_season_mal_id] Traversing sequels from MAL ID {current_mal_id} (Current Season {current_season})")
            relations = _get_related_anime(current_mal_id)
            sequel_found = False
            for rel in relations:
                if rel.get('relation_type') == 'Sequel':
                    next_mal_id = rel.get('mal_id')
                    next_title = rel.get('name', '')
                    logging.debug(f"[_find_correct_season_mal_id] Found potential sequel: '{next_title}' (MAL ID: {next_mal_id})")
                    # TODO: Better check if this sequel IS the next season
                    current_mal_id = next_mal_id
                    current_season += 1
                    sequel_found = True
                    if current_season == target_season:
                        logging.info(f"[_find_correct_season_mal_id] Found MAL ID {current_mal_id} for target Season {target_season} via sequel traversal.")
                        return current_mal_id
                    break # Move to check relations of the found sequel
            if not sequel_found:
                 logging.warning(f"[_find_correct_season_mal_id] Could not find sequel for season {current_season + 1} starting from MAL ID {base_mal_id}. Failed to reach target Season {target_season}.")
                 return None # Dead end in sequel chain

    else: # Base title might be S2+, or target is S1 but base title indicated S2+
        # Look for prequels matching target_season depth
        current_mal_id = base_mal_id
        # Estimate base season from title if possible
        current_season = target_season # Placeholder, refine below
        for i in range(9, 0, -1):
             patterns = [rf'\bseason\s+{i}\b', rf'\b{i}(st|nd|rd|th)\s+season\b', rf'\bs{i}\b']
             if any(re.search(pattern, base_title_lower) for pattern in patterns):
                  current_season = i
                  break
        
        logging.debug(f"[_find_correct_season_mal_id] Estimated base season as {current_season} from title '{base_title}'. Looking for prequels.")

        while current_season > target_season:
            logging.debug(f"[_find_correct_season_mal_id] Traversing prequels from MAL ID {current_mal_id} (Current Season {current_season})")
            relations = _get_related_anime(current_mal_id)
            prequel_found = False
            for rel in relations:
                if rel.get('relation_type') == 'Prequel':
                    next_mal_id = rel.get('mal_id')
                    next_title = rel.get('name', '')
                    logging.debug(f"[_find_correct_season_mal_id] Found potential prequel: '{next_title}' (MAL ID: {next_mal_id})")
                    current_mal_id = next_mal_id
                    current_season -= 1
                    prequel_found = True
                    if current_season == target_season:
                        logging.info(f"[_find_correct_season_mal_id] Found MAL ID {current_mal_id} for target Season {target_season} via prequel traversal.")
                        return current_mal_id
                    break # Move to check relations of the found prequel
            if not prequel_found:
                 logging.warning(f"[_find_correct_season_mal_id] Could not find prequel for season {current_season - 1} starting from MAL ID {base_mal_id}. Failed to reach target Season {target_season}.")
                 return None # Dead end in prequel chain

    logging.warning(f"[_find_correct_season_mal_id] Could not definitively find MAL ID for target Season {target_season} starting from {base_mal_id}.")
    return None # Fallback if traversal fails

def get_anidb_metadata_for_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Get anime metadata for a given item using Jikan API."""
    try:
        # --- Start Enhanced Logging ---
        item_title = item.get('title', 'Unknown Title')
        # **Use the season/episode numbers passed in the item dictionary**
        item_season_num = int(item.get('season_number', 0))
        item_ep_num = int(item.get('episode_number', 0))
        logging.info(f"--- Getting Jikan Metadata (v2) ---")
        logging.info(f"[JikanMeta] Input Item: Title='{item_title}', S={item_season_num}, E={item_ep_num}")
        logging.debug(f"[JikanMeta] Full Input Item Data: {item}")
        # --- End Enhanced Logging ---

        if item_season_num <= 0 or item_ep_num <= 0:
             logging.warning(f"[JikanMeta] Invalid input season ({item_season_num}) or episode ({item_ep_num}). Cannot proceed.")
             return None

        # Search for the anime using the item's title
        base_anime_data = _search_anime(item.get('title', ''))
        if not base_anime_data:
            logging.warning(f"[JikanMeta] Jikan search failed for '{item_title}', cannot get metadata.")
            return None
            
        base_mal_id = base_anime_data.get('mal_id')
        found_title = base_anime_data.get('title')
        logging.info(f"[JikanMeta] Initial search found MAL ID: {base_mal_id}, Title: '{found_title}'")

        # --- Determine the correct MAL ID for the target season ---
        correct_mal_id = _find_correct_season_mal_id(base_anime_data, item_season_num)
        
        if not correct_mal_id:
             logging.warning(f"[JikanMeta] Could not find a specific MAL ID for Season {item_season_num}. Using base MAL ID {base_mal_id} as fallback.")
             correct_mal_id = base_mal_id # Fallback
        else:
             logging.info(f"[JikanMeta] Identified MAL ID {correct_mal_id} for Season {item_season_num}.")
             # Optionally, fetch the metadata for this specific MAL ID if needed (e.g., for accurate year)
             # For now, we primarily need it for episode lookup.

        # Get episode details using the CORRECT MAL ID and the INPUT episode number
        episode_data = None
        if correct_mal_id:
            logging.debug(f"[JikanMeta] Fetching episode details for Correct MAL ID {correct_mal_id}, Input Episode Number: {item_ep_num}")
            episode_data = _get_episode_details(correct_mal_id, item_ep_num)
            if episode_data:
                 logging.info(f"[JikanMeta] Found episode details: Episode MAL ID={episode_data.get('mal_id')}, Title='{episode_data.get('title')}'")
            else:
                 logging.warning(f"[JikanMeta] Failed to find details for Episode {item_ep_num} using Correct MAL ID {correct_mal_id}.")
                 # If this fails, it might indicate an issue with Jikan data or the traversal logic.
        else:
            logging.warning("[JikanMeta] No MAL ID identified for episode lookup.")

        # Extract year from the BASE aired date initially found (might not be accurate for later seasons)
        # TODO: Potentially fetch metadata for `correct_mal_id` to get a more accurate year.
        year = ''
        try:
            if base_anime_data.get('aired', {}).get('from'):
                year = base_anime_data['aired']['from'][:4]
                logging.debug(f"[JikanMeta] Extracted Year {year} from base MAL ID {base_mal_id}")
        except Exception as e_year:
            logging.warning(f"[JikanMeta] Could not extract year from aired data: {base_anime_data.get('aired', {})}. Error: {e_year}")
            
        # Build metadata USING THE INPUT season/episode numbers
        final_title = base_anime_data.get('title', item.get('title', 'Unknown')) # Use base title for consistency? Or maybe season-specific?
        final_episode_title = episode_data.get('title', item.get('episode_title', '')) if episode_data else item.get('episode_title', '')
        
        metadata = {
            'title': final_title,
            'year': year or item.get('year', ''), # Use year from base search for now
            'episode_title': final_episode_title,
            'episode_number': item_ep_num, # Use INPUT episode number
            'season_number': item_season_num # Use INPUT season number
        }
        
        logging.info(f"[JikanMeta] Generated Metadata for Item '{item_title}' S{item_season_num}E{item_ep_num}: {metadata}")
        logging.info(f"--- Finished Jikan Metadata (v2) ---")
        return metadata
        
    except Exception as e:
        item_title_err = item.get('title', 'Unknown')
        logging.error(f"[JikanMeta] Error fetching Jikan metadata for item '{item_title_err}': {str(e)}", exc_info=True)
        logging.info(f"--- Finished Jikan Metadata (with error) ---")
        return None

def format_filename_with_anidb(item: Dict[str, Any], original_extension: str) -> Optional[str]:
    """Format filename using anime metadata from Jikan."""
    try:
        if not get_setting('Debug', 'use_anidb_metadata', False):
            logging.debug("[AniDB Format] Anime metadata (Jikan) is disabled in settings.")
            return None
            
        # Only process anime episodes
        item_type = item.get('type')
        # Check is_anime flag which should be set based on genre during scraping/parsing
        is_anime_flag = item.get('is_anime', False) 
        
        if item_type != 'episode' or not is_anime_flag:
            logging.debug(f"[AniDB Format] Item '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')} is not an anime episode (Type: {item_type}, IsAnime: {is_anime_flag}), skipping Jikan formatting.")
            return None
            
        logging.info(f"[AniDB Format] Attempting Jikan formatting for anime item: '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')}")
        # Get metadata
        metadata = get_anidb_metadata_for_item(item)
        if not metadata:
            logging.warning(f"[AniDB Format] Failed to get Jikan metadata for '{item.get('title')}', cannot format filename using Jikan.")
            return None # Fallback to default formatting later
            
        # Get the template from settings
        template = get_setting('Debug', 'anidb_episode_template',
                             '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title}')
        
        # Prepare template variables using fetched metadata
        # Ensure required keys exist, falling back to original item data if necessary
        template_vars = {
            'title': metadata.get('title') or item.get('title', 'Unknown'),
            'year': metadata.get('year') or item.get('year', ''),
            'season_number': int(metadata.get('season_number', item.get('season_number', 0))),
            'episode_number': int(metadata.get('episode_number', item.get('episode_number', 0))),
            'episode_title': metadata.get('episode_title') or item.get('episode_title', ''),
            # Include other common vars from the item for template flexibility
            'imdb_id': item.get('imdb_id', ''),
            'tmdb_id': item.get('tmdb_id', ''),
            'version': item.get('version', '').strip('*'),
            'quality': item.get('quality', ''),
            'original_filename': os.path.splitext(item.get('filled_by_file', ''))[0], 
            'content_source': item.get('content_source', ''),
            'resolution': item.get('resolution', '')
        }
        
        logging.info(f"[AniDB Format] Using template: '{template}'")
        logging.info(f"[AniDB Format] Using metadata for formatting: {template_vars}")
        
        # Perform formatting, handle potential errors
        try:
            filename = template.format(**template_vars)
            logging.debug(f"[AniDB Format] Formatted path/filename (before extension): {filename}")
        except KeyError as e:
             logging.error(f"[AniDB Format] Template formatting failed. Missing key: {e}. Template: '{template}', Vars: {template_vars}", exc_info=True)
             return None # Cannot format
        except Exception as e_format:
             logging.error(f"[AniDB Format] Template formatting failed. Error: {e_format}. Template: '{template}', Vars: {template_vars}", exc_info=True)
             return None # Cannot format

        # Add extension if configured
        final_filename = filename
        if get_setting('Debug', 'symlink_preserve_extension', True):
            if original_extension and not original_extension.startswith('.'):
                original_extension = f".{original_extension}"
            final_filename = f"{filename}{original_extension}"
            logging.debug(f"[AniDB Format] Final formatted path/filename with extension: {final_filename}")
        else:
             logging.debug(f"[AniDB Format] Final formatted path/filename (no extension): {final_filename}")
            
        # Return the formatted path relative to the base library directory
        return final_filename 
        
    except Exception as e:
        logging.error(f"[AniDB Format] Error formatting filename with Jikan metadata for '{item.get('title')}': {str(e)}", exc_info=True)
        return None # Fallback

# Example usage
if __name__ == "__main__":
    # Add your example usage here
    pass 