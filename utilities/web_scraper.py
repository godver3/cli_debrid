import logging
from typing import Dict, Any, List, Tuple, Optional
from utilities.settings import get_setting
from routes.api_tracker import api
from scraper.scraper import scrape
from debrid import extract_hash_from_magnet
from queues.adding_queue import AddingQueue
import re
from fuzzywuzzy import fuzz
from routes.poster_cache import get_cached_poster_url, cache_poster_url, get_cached_media_meta, cache_media_meta
import asyncio
import aiohttp
from database.poster_management import get_poster_url
from flask import request, url_for
from urllib.parse import urlparse
from debrid.base import DebridProvider
from debrid import get_debrid_provider
from debrid.real_debrid.client import RealDebridProvider
import time

# NEW ASYNC HELPER FUNCTION
async def _fetch_media_meta_async(session: aiohttp.ClientSession, tmdb_id: str, media_type: str, tmdb_api_key: Optional[str]) -> Optional[Tuple[Optional[str], str, list, float, str]]:
    """
    Asynchronously fetches media metadata (details and poster URL) from TMDB.
    This function is designed to be called in a batch for multiple items.
    It does not handle caching internally; caching should be done by the caller.
    Returns a tuple: (poster_url, overview, genres, vote_average, backdrop_path)
    Poster_url can be None if not found or if an error occurs during its fetch.
    """
    if not tmdb_api_key:
        placeholder_path = "static/images/placeholder.png"
        return (placeholder_path, "No overview available", [], 0.0, "")

    if media_type == 'tv' or media_type == 'show':
        details_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
    else:
        details_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
    
    details_data = None
    try:
        # Perform synchronous network call in a separate thread
        details_response = await asyncio.to_thread(api.get, details_url)
        details_response.raise_for_status()
        details_data = details_response.json()
    except Exception as e:
        logging.error(f"Error fetching TMDB details for {media_type} {tmdb_id}: {e}")
        return None 

    if not details_data:
        return None

    poster_url_val: Optional[str] = None
    try:
        poster_url_val = await get_poster_url(session, tmdb_id, media_type)
    except Exception as e:
        logging.error(f"Error fetching poster URL for {media_type} {tmdb_id} via get_poster_url: {e}")
        # Continue without poster if this specific call fails, poster_url_val remains None

    overview = details_data.get('overview', '')
    genres = [genre['name'] for genre in details_data.get('genres', [])]
    vote_average = details_data.get('vote_average', 0)
    backdrop_path_val = details_data.get('backdrop_path', '')
    if backdrop_path_val:
        backdrop_path_val = backdrop_path_val

    return (poster_url_val, overview, genres, vote_average, backdrop_path_val)


def search_trakt(search_term: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    trakt_client_id = get_setting('Trakt', 'client_id')
    tmdb_api_key = get_setting('TMDB', 'api_key')
    has_tmdb = bool(tmdb_api_key)
    
    if not trakt_client_id:
        logging.error("Trakt Client ID not set. Please configure in settings.")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }

    limit_per_page = 20
    search_url = f"https://api.trakt.tv/search/movie,show?query={api.utils.quote(search_term)}&extended=full&page=1&limit={limit_per_page}"
    if year:
        search_url += f"&years={year}"

    logging.info(f"Querying Trakt API (limit {limit_per_page}): {search_url}")

    max_retries = 3
    retry_delay = 1  
    attempt = 0
    raw_trakt_results = []

    while attempt < max_retries:
        try:
            response = api.get(search_url, headers=headers, timeout=30)
            response.raise_for_status()
            
            if 'X-RateLimit-Remaining' in response.headers:
                remaining = int(response.headers['X-RateLimit-Remaining'])
                if remaining < 5:
                    logging.warning(f"Trakt API rate limit running low. {remaining} requests remaining.")
                    if remaining == 0:
                        reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                        logging.error(f"Trakt API rate limit exceeded. Reset at timestamp: {reset_time}")
                        return [] 

            page_count = response.headers.get('X-Pagination-Page-Count', 'N/A')
            item_count = response.headers.get('X-Pagination-Item-Count', 'N/A')
            logging.info(f"Trakt Headers: Page Count={page_count}, Item Count={item_count}")

            raw_trakt_results = response.json()
            
            if raw_trakt_results:
                logging.info(f"Successfully received {len(raw_trakt_results)} result(s) from Trakt.")
                break 
            else:
                logging.info(f"No results found for the query.")
                return [] 

        except api.exceptions.RequestException as e:
            attempt += 1
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** (attempt - 1)) 
                logging.warning(f"Attempt {attempt} failed. Retrying in {wait_time} seconds. Error: {str(e)}")
                time.sleep(wait_time)
                continue
            else:
                logging.error(f"Error searching Trakt after {max_retries} attempts: {str(e)}")
                if isinstance(e, api.exceptions.HTTPError):
                    logging.error(f"HTTP Status Code: {e.response.status_code}")
                    logging.error(f"Response Headers: {e.response.headers}")
                return [] 
        except Exception as e:
            logging.error(f"Unexpected error searching Trakt: {str(e)}", exc_info=True)
            return [] 

    if not raw_trakt_results:
        logging.warning(f"No results obtained for search term: {search_term}")
        return []
    
    logging.info(f"Sorting {len(raw_trakt_results)} fetched results...")
    sorted_results = sorted(
        raw_trakt_results,
        key=lambda x: (
            x['movie' if x['type'] == 'movie' else 'show']['title'].lower() == search_term.lower(),
            (str(x['movie' if x['type'] == 'movie' else 'show']['year']) == str(year) if year else False),
            fuzz.ratio(search_term.lower(), x['movie' if x['type'] == 'movie' else 'show']['title'].lower()),
            x['movie' if x['type'] == 'movie' else 'show'].get('votes', 0)
        ),
        reverse=True
    )
    
    logging.info("Top results after sorting:")
    for res_log in sorted_results[:20]: 
        if res_log['type'] == 'movie':
            logging.debug(f"  Sorted Result: {res_log['movie']['title']} (Votes: {res_log['movie'].get('votes', 0)})")
        else:
            logging.debug(f"  Sorted Result: {res_log['show']['title']} (Votes: {res_log['show'].get('votes', 0)})")

    # --- Batch fetch metadata for cache misses ---
    all_media_metadata_map: Dict[Tuple[str, str], Optional[Tuple[Optional[str], str, list, float, str]]] = {}
    items_to_fetch_concurrently = []

    for result_item in sorted_results:
        media_type = result_item['type']
        item_details = result_item['movie' if media_type == 'movie' else 'show']
        tmdb_id = item_details['ids'].get('tmdb')

        if not tmdb_id:
            continue

        cached_poster = get_cached_poster_url(tmdb_id, media_type)
        cached_meta = get_cached_media_meta(tmdb_id, media_type)

        if cached_poster and cached_meta:
            all_media_metadata_map[(str(tmdb_id), media_type)] = cached_meta
        else:
            if has_tmdb: # Only fetch if TMDB API key is present
                items_to_fetch_concurrently.append({'tmdb_id': str(tmdb_id), 'media_type': media_type, 'title': item_details['title']})
            else: # No TMDB key, use placeholder data directly
                 placeholder_path = "static/images/placeholder.png"
                 all_media_metadata_map[(str(tmdb_id), media_type)] = (placeholder_path, "No overview available", [], 0.0, "")


    if items_to_fetch_concurrently:
        logging.info(f"Identified {len(items_to_fetch_concurrently)} items for concurrent metadata fetching.")
        
        async def _run_batch_fetch():
            async with aiohttp.ClientSession() as session:
                tasks = []
                for item_to_fetch in items_to_fetch_concurrently:
                    tasks.append(
                        _fetch_media_meta_async(
                            session, 
                            item_to_fetch['tmdb_id'], 
                            item_to_fetch['media_type'], 
                            tmdb_api_key
                        )
                    )
                
                fetched_results_list = await asyncio.gather(*tasks, return_exceptions=True)

                for i, meta_or_exception in enumerate(fetched_results_list):
                    item_info = items_to_fetch_concurrently[i]
                    key = (item_info['tmdb_id'], item_info['media_type'])
                    
                    if isinstance(meta_or_exception, Exception):
                        logging.error(f"Failed to fetch metadata for {item_info['title']} (TMDB ID: {item_info['tmdb_id']}): {meta_or_exception}")
                        all_media_metadata_map[key] = None # Mark as failed
                    elif meta_or_exception:
                        all_media_metadata_map[key] = meta_or_exception
                        # Cache the successfully fetched data
                        poster_p = meta_or_exception[0]
                        if poster_p: # poster_p could be None if get_poster_url failed
                            cache_poster_url(item_info['tmdb_id'], item_info['media_type'], poster_p)
                        cache_media_meta(item_info['tmdb_id'], item_info['media_type'], meta_or_exception)
                        logging.info(f"Fetched and cached metadata for {item_info['media_type']} {item_info['title']} (TMDB ID: {item_info['tmdb_id']})")
                    else:
                        all_media_metadata_map[key] = None # Fetch returned None (e.g. details 404)
                        logging.warning(f"No metadata returned from fetch for {item_info['media_type']} {item_info['title']} (TMDB ID: {item_info['tmdb_id']})")
        
        asyncio.run(_run_batch_fetch())
        logging.info("Finished batch metadata fetching.")

    # --- Process results using cached or newly fetched data ---
    converted_results = []
    filter_stats = {'total': len(sorted_results), 'no_tmdb': 0, 'no_poster': 0, 'no_year': 0}
    processed_count = 0
    results_to_process_limit = 100 

    for result in sorted_results:
        if processed_count >= results_to_process_limit:
            logging.info(f"Reached processing limit of {results_to_process_limit}. Skipping remaining {len(sorted_results) - processed_count} raw results.")
            break
            
        media_type = result['type']
        item = result['movie' if media_type == 'movie' else 'show']
        tmdb_id_str = str(item['ids'].get('tmdb'))

        if not item['ids'].get('tmdb'):
            filter_stats['no_tmdb'] += 1
            logging.debug(f"No TMDB ID found for {item['title']}")
            continue

        media_meta_tuple = all_media_metadata_map.get((tmdb_id_str, media_type))
        current_poster_path: Optional[str] = None

        if media_meta_tuple and media_meta_tuple[0]: # Check if meta exists and poster path (media_meta_tuple[0]) is not None or empty
            current_poster_path = media_meta_tuple[0]
        else: # Metadata fetch failed, or was placeholder, or poster path is None
            if has_tmdb: # If TMDB key exists, failed fetch means no poster.
                filter_stats['no_poster'] += 1
                logging.debug(f"No poster/valid metadata for {media_type} {item['title']} (TMDB ID: {tmdb_id_str}) after processing. Meta: {media_meta_tuple}")
                continue
            else: # No TMDB key, use placeholder if not already set (it should be)
                current_poster_path = "static/images/placeholder.png"
                if not media_meta_tuple: # Ensure media_meta_tuple is the placeholder structure
                     media_meta_tuple = (current_poster_path, "No overview available", [], 0.0, "")
        
        if not media_meta_tuple or len(media_meta_tuple) < 5 : # Should be caught by above, but as a safeguard
            logging.warning(f"Incomplete media_meta_tuple for {item['title']} ({tmdb_id_str}). Skipping. Meta: {media_meta_tuple}")
            filter_stats['no_poster'] += 1 
            continue

        if not item.get('year'):
            filter_stats['no_year'] += 1
            logging.debug(f"Skipping {media_type} {item['title']} (TMDB ID: {tmdb_id_str}) due to no year in Trakt data")
            continue
        
        # This check might be slightly redundant if the above logic correctly sets current_poster_path or continues
        if has_tmdb and (not current_poster_path or 'placeholder' in current_poster_path):
             filter_stats['no_poster'] += 1
             logging.debug(f"Skipping {media_type} {item['title']} (TMDB ID: {tmdb_id_str}) due to invalid/placeholder poster path when TMDB key is set: {current_poster_path}")
             continue

        converted_results.append({
            'mediaType': media_type,
            'id': tmdb_id_str, # Use tmdb_id_str which is confirmed to be string
            'title': item['title'],
            'year': item['year'],
            'posterPath': current_poster_path,
            'show_overview' if media_type == 'show' else 'overview': media_meta_tuple[1],
            'genres': media_meta_tuple[2],
            'voteAverage': media_meta_tuple[3],
            'backdropPath': media_meta_tuple[4],
            'votes': item.get('votes', 0)
        })
        processed_count += 1

    if len(converted_results) > 100:
        logging.info(f"Limiting final results from {len(converted_results)} to 100")
        converted_results = converted_results[:100]
    
    logging.info("=== Search Results Filtering Report ===")
    logging.info(f"Total raw results fetched from Trakt (limit {limit_per_page}): {filter_stats['total']}")
    logging.info(f"Items processed for metadata/filtering: {processed_count}")
    logging.info(f"Filtered out during processing due to:")
    logging.info(f"  - No TMDB ID: {filter_stats['no_tmdb']}")
    logging.info(f"  - No poster/metadata: {filter_stats['no_poster']}")
    logging.info(f"  - No year: {filter_stats['no_year']}")
    logging.info(f"Final results returned after filtering & limit: {len(converted_results)}")
    logging.info("=================================")
    
    return converted_results

def get_media_meta(tmdb_id: str, media_type: str) -> Optional[Tuple[str, str, list, float, str]]:
    tmdb_api_key = get_setting('TMDB', 'api_key')
    
    if not tmdb_api_key:
        logging.warning("TMDb API key not set. Using placeholder data.")
        placeholder_path = "static/images/placeholder.png"
        logging.info(f"Using placeholder path: {placeholder_path}")
        # Return tuple with placeholder values: (poster_url, overview, genres, vote_average, backdrop_path)
        # Use relative path for placeholder
        return (placeholder_path, "No overview available", [], 0.0, "")

    cached_poster_url = get_cached_poster_url(tmdb_id, media_type)
    cached_media_meta = get_cached_media_meta(tmdb_id, media_type)

    if cached_poster_url and cached_media_meta:
        logging.info(f"Using cached data for {media_type} {tmdb_id}")
        return cached_media_meta

    # Use the correct endpoints for TV shows and movies
    if media_type == 'tv' or media_type == 'show':
        details_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
    else:
        details_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
    
    try:
        # Fetch details
        details_response = api.get(details_url)
        details_response.raise_for_status()
        details_data = details_response.json()

        # Use asyncio to run the async get_poster_url function
        async def fetch_poster_url():
            async with aiohttp.ClientSession() as session:
                return await get_poster_url(session, tmdb_id, media_type)

        poster_url = asyncio.run(fetch_poster_url())
        
        overview = details_data.get('overview', '')
        genres = [genre['name'] for genre in details_data.get('genres', [])]
        vote_average = details_data.get('vote_average', 0)
        backdrop_path = details_data.get('backdrop_path', '')
        if backdrop_path:
            backdrop_path = backdrop_path  # Just return the path, not the full URL
        
        media_meta = (poster_url, overview, genres, vote_average, backdrop_path)
        
        if poster_url:
            cache_poster_url(tmdb_id, media_type, poster_url)
        cache_media_meta(tmdb_id, media_type, media_meta)
        logging.info(f"Cached metadata for {media_type} {tmdb_id}")

        return media_meta
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching media meta from TMDb: {e}")
        return None
    
def overseerr_tvshow(title: str, year: Optional[int] = None, media_id: Optional[int] = None, season: Optional[int] = None) -> List[Dict[str, Any]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    if media_id and season is not None:
        search_url = f"{overseerr_url}/api/v1/tv/{media_id}/season/{season}"
    else:
        search_url = f"{overseerr_url}/api/v1/tv/{media_id}"


    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data.get('seasons', False):
            # Grab TV show seasons
            seasons = data['seasons']
            
            logging.info(f"Sorted seasons: {seasons}")
            return seasons
        elif data.get('episodes', False):
            # Grab TV show seasons
            episodes = data['episodes']
            
            logging.info(f"Sorted episodes: {episodes}")
            return episodes
        else:
            logging.warning(f"No results found for show: {title}")
            return []
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return []
    
def parse_search_term(search_term: str) -> Tuple[str, Optional[int], Optional[int], Optional[int], bool]:
    # First, check if the entire search term is a year
    if re.match(r'^(19\d{2}|20\d{2})$', search_term):
        return search_term, None, None, None, True

    # Try to match a year at the end of the string
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b\s*$', search_term)
    if year_match:
        year = int(year_match.group(1))
        base_title = search_term[:year_match.start()].strip()
    else:
        year = None
        base_title = search_term

    # Match patterns like "S01E01", "s01e01", "S01", "s01"
    match = re.search(r'[Ss](\d+)(?:[Ee](\d+))?', base_title)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else None
        multi = episode is None  # Set multi to True if only season is specified
        return base_title, season, episode, year, multi

    return base_title, None, None, year, True  # Default to multi=True if no season/episode specified

async def fetch_poster_url(tmdb_id, media_type):
    async with aiohttp.ClientSession() as session:
        return await get_poster_url(session, tmdb_id, media_type)

def web_scrape(search_term: str, version: str) -> Dict[str, Any]:
    logging.info(f"Starting web scrape for search term: {search_term}, version: {version}")
    
    base_title, season, episode, year, multi = parse_search_term(search_term)
    logging.info(f"Parsed search term: title='{base_title}', season={season}, episode={episode}, year={year}, multi={multi}")
    
    search_results = search_trakt(base_title, year)
    if not search_results:
        logging.warning(f"No results found for search term: {base_title} ({year if year else 'no year specified'})")
        return {"error": "No results found"}

    tmdb_api_key = get_setting('TMDB', 'api_key')
    has_tmdb = bool(tmdb_api_key)

    detailed_results = []
    for result in search_results:
        if result['mediaType'] != 'person':
            tmdb_id = result['id']
            media_type = result['mediaType']
            title_for_logging = result['title']
            logging.info(f"Processing media tmdb_id: {tmdb_id}, type: {media_type}, title: {title_for_logging}")

            # Skip poster validation when we have no TMDB API key
            if has_tmdb:
                # Skip if no poster path or if it's a placeholder when we have TMDB
                if not result.get('posterPath') or 'placeholder' in result.get('posterPath', '').lower():
                    logging.info(f"web_scrape: Skipping {title_for_logging} ({tmdb_id}) - no initial poster path or placeholder in search_trakt result.")
                    continue

            cached_poster_url = get_cached_poster_url(tmdb_id, media_type)
            cached_media_meta = get_cached_media_meta(tmdb_id, media_type)

            if cached_poster_url and cached_media_meta:
                # Only validate cached poster URL if we have TMDB API key
                if has_tmdb and ('placeholder' in cached_poster_url.lower() or not cached_poster_url.startswith('https://image.tmdb.org/')):
                    logging.info(f"web_scrape: Skipping {title_for_logging} ({tmdb_id}) - invalid cached poster URL: {cached_poster_url}")
                    continue
                logging.info(f"Using cached data for {media_type} {result['title']} (TMDB ID: {tmdb_id})")
                poster_path = cached_poster_url
                media_meta = cached_media_meta
            else:
                logging.info(f"Fetching data for {media_type} {result['title']} (TMDB ID: {tmdb_id})")
                media_meta = get_media_meta(tmdb_id, media_type)
                if media_meta and media_meta[0]:  # Check if media_meta exists and has a valid poster URL
                    poster_path = asyncio.run(fetch_poster_url(tmdb_id, media_type))
                    if has_tmdb and (not poster_path or 'placeholder' in poster_path.lower() or not poster_path.startswith('https://image.tmdb.org/')):
                        logging.info(f"web_scrape: Skipping {title_for_logging} ({tmdb_id}) - invalid or missing fetched poster URL: {poster_path}")
                        continue
                    cache_poster_url(tmdb_id, media_type, poster_path)
                    cache_media_meta(tmdb_id, media_type, media_meta)
                    logging.info(f"Cached poster and metadata for {media_type} {result['title']} (TMDB ID: {tmdb_id})")
                else:
                    if has_tmdb:
                        logging.info(f"web_scrape: Skipping {title_for_logging} ({tmdb_id}) - no valid metadata or poster returned from get_media_meta.")
                        continue
                    else:
                        # Use placeholder if no TMDB API key
                        poster_path = "static/images/placeholder.png"
                        media_meta = (poster_path, "No overview available", [], 0.0, "")

            # Skip if overview is empty (likely incomplete metadata) - only if we have TMDB API
            if has_tmdb and (not media_meta or not media_meta[1] or not media_meta[1].strip()):
                overview_content = media_meta[1] if media_meta and len(media_meta) > 1 else "N/A"
                logging.info(f"web_scrape: Skipping {title_for_logging} ({tmdb_id}) - empty or missing overview. Overview: '{overview_content}'")
                continue

            detailed_result = {
                "id": tmdb_id,
                "title": result['title'],
                "year": result['year'],
                "media_type": media_type,
                "show_overview": media_meta[1],
                "poster_path": poster_path,
                "genre_ids": media_meta[2],
                "vote_average": media_meta[3],
                "backdrop_path": media_meta[4],
                "season": season,
                "episode": episode,
                "multi": multi,
                "imdb_id": result.get('imdb_id', '')
            }
            detailed_results.append(detailed_result)

    logging.info(f"Processed results: {detailed_results}")
    
    # Sort results to prioritize exact substring matches, then fuzzy match percentage
    detailed_results.sort(key=lambda x: (
        base_title.lower() in x['title'].lower(),  # Contains exact title first
        fuzz.ratio(base_title.lower(), x['title'].lower())  # Then by fuzzy match percentage
    ), reverse=True)
    
    return detailed_results

def get_tmdb_data(tmdb_id: int, media_type: str, season: Optional[int] = None, episode: Optional[int] = None) -> Dict[str, Any]:
    tmdb_api_key = get_setting('TMDB', 'api_key')
    
    if not tmdb_api_key:
        logging.error("TMDb API key not set. Please configure in settings.")
        return {}

    base_url = "https://api.themoviedb.org/3"
    if media_type == 'tv' or media_type == 'show':
        if season is not None and episode is not None:
            url = f"{base_url}/tv/{tmdb_id}/season/{season}/episode/{episode}?api_key={tmdb_api_key}"
        elif season is not None:
            url = f"{base_url}/tv/{tmdb_id}/season/{season}?api_key={tmdb_api_key}"
        else:
            url = f"{base_url}/tv/{tmdb_id}?api_key={tmdb_api_key}"
    else:
        url = f"{base_url}/movie/{tmdb_id}?api_key={tmdb_api_key}"

    try:
        response = api.get(url)
        response.raise_for_status()
        data = response.json()
        return data
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching TMDb data: {e}")
        return {}

def web_scrape_tvshow(media_id: int, title: str, year: int, season: Optional[int] = None, allow_specials: bool = False) -> Dict[str, Any]:
    from metadata.metadata import get_all_season_episode_counts, get_show_airtime_by_imdb_id
    logging.info(f"Starting web scrape for TV Show: {title}, media_id: {media_id}, allow_specials: {allow_specials}")
    
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID not set. Please configure in settings.")
        return {"error": "Trakt Client ID not set"}

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }

    # First, convert TMDB ID to Trakt ID
    tmdb_to_trakt_url = f"https://api.trakt.tv/search/tmdb/{media_id}?type=show"
    
    try:
        response = api.get(tmdb_to_trakt_url, headers=headers)
        response.raise_for_status()
        search_data = response.json()
        
        if not search_data:
            logging.error(f"No Trakt show found for TMDB ID: {media_id}")
            return {"error": "Show not found on Trakt"}
        
        trakt_id = search_data[0]['show']['ids']['trakt']
    except api.exceptions.RequestException as e:
        logging.error(f"Error converting TMDB ID to Trakt ID: {e}")
        return {"error": f"Error converting TMDB ID to Trakt ID: {str(e)}"}

    # Now use the Trakt ID for further requests
    if season is not None:
        search_url = f"https://api.trakt.tv/shows/{trakt_id}/seasons/{season}?extended=full"
    else:
        search_url = f"https://api.trakt.tv/shows/{trakt_id}/seasons?extended=full"

    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        trakt_data = response.json()

        if not trakt_data:
            logging.warning(f"No results found for show: {title}")
            return {"error": "No results found"}

        # Fetch TMDB data
        tmdb_data = get_tmdb_data(media_id, 'tv', season)

        if season is not None:
            # Fetch episode details
            return {
                "episode_results": [
                    {
                        "id": media_id,
                        "title": title,
                        "episode_title": episode.get('title', ''),
                        "season_id": episode['ids']['trakt'],
                        "season_num": episode['season'],
                        "episode_num": episode['number'],
                        "year": year,
                        "media_type": 'tv',
                        "still_path": get_tmdb_data(media_id, 'tv', episode['season'], episode['number']).get('still_path'),
                        "air_date": episode.get('first_aired'),
                        "vote_average": episode.get('rating', 0),
                        "multi": False
                    }
                    for episode in trakt_data
                    # Conditionally filter out special episodes (number 0) based on allow_specials flag
                    if allow_specials or episode['number'] != 0 
                ]
            }
        else:
            # Fetch season details
            tmdb_seasons = tmdb_data.get('seasons', [])
            tmdb_seasons_dict = {s['season_number']: s for s in tmdb_seasons}
            
            return {
                "episode_results": [
                    {
                        "id": media_id,
                        "title": title,
                        "season_id": season_data['ids']['trakt'],
                        "season_num": season_data['number'],
                        "year": year,
                        "media_type": 'tv',
                        "poster_path": tmdb_seasons_dict.get(season_data['number'], {}).get('poster_path') or tmdb_data.get('poster_path'), # Fallback to show poster
                        "air_date": season_data.get('first_aired') or tmdb_seasons_dict.get(season_data['number'], {}).get('air_date'),
                        "season_overview": season_data.get('overview', '') or tmdb_seasons_dict.get(season_data['number'], {}).get('overview', ''),
                        "episode_count": season_data.get('episode_count', 0) or tmdb_seasons_dict.get(season_data['number'], {}).get('episode_count', 0),
                        "multi": True
                    }
                    for season_data in trakt_data
                    # Conditionally filter out special seasons (number 0) based on allow_specials flag
                    if allow_specials or season_data['number'] != 0 
                ]
            }
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Trakt: {e}")
        return {"error": f"Error searching Trakt: {str(e)}"}

def trending_movies():
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID key not set. Please configure in settings.")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }
    api_url = "https://api.trakt.tv/movies/watched/weekly?extended=full"

    try:
        response = api.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        trending_movies = []
        for result in data:
            tmdb_id = result['movie']['ids']['tmdb']
            cached_poster_url = get_cached_poster_url(tmdb_id, 'movie')
            cached_media_meta = get_cached_media_meta(tmdb_id, 'movie')

            if cached_poster_url and cached_media_meta:
                #logging.info(f"Using cached data for movie {result['movie']['title']} (TMDB ID: {tmdb_id})")
                poster_path = cached_poster_url
                media_meta = cached_media_meta
            else:
                logging.info(f"Fetching data for movie {result['movie']['title']} (TMDB ID: {tmdb_id})")
                media_meta = get_media_meta(tmdb_id, 'movie')
                if media_meta:
                    poster_path = media_meta[0]
                    cache_poster_url(tmdb_id, 'movie', poster_path)
                    cache_media_meta(tmdb_id, 'movie', media_meta)
                    logging.info(f"Cached poster and metadata for movie {result['movie']['title']} (TMDB ID: {tmdb_id})")
                else:
                    if get_setting('TMDB', 'api_key') == "":
                        logging.warning("TMDB API key not set, using placeholder images")
                        
                        # Generate the placeholder URL
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        
                        # Check if the request is secure (HTTPS)
                        if request.is_secure:
                            # If it's secure, ensure the URL uses HTTPS
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='https').geturl()
                        else:
                            # If it's not secure, use HTTP
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='http').geturl()
                        
                        poster_path = placeholder_url

            # Check if TMDB API key is set
            tmdb_api_key = get_setting('TMDB', 'api_key', '')
            tmdb_api_key_set = bool(tmdb_api_key)

            trending_movies.append({
                "title": result['movie']['title'],
                "year": result['movie']['year'],
                "imdb_id": result['movie']['ids']['imdb'],
                "tmdb_id": tmdb_id,
                "rating": result['movie']['rating'],
                "watcher_count": result['watcher_count'],
                "poster_path": poster_path,
                "movie_overview": media_meta[1] if media_meta else '',
                "genre_ids": media_meta[2] if media_meta else [],
                "vote_average": media_meta[3] if media_meta else 0,
                "backdrop_path": media_meta[4] if media_meta else '',
                "tmdb_api_key_set": tmdb_api_key_set
            })

        return {"trendingMovies": trending_movies}
    except api.exceptions.RequestException as e:
        logging.error(f"Error retrieving Trakt Trending Movies: {e}")
        return []

def trending_shows():
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID key not set. Please configure in settings.")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }
    api_url = "https://api.trakt.tv/shows/watched/weekly?extended=full"

    try:
        response = api.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        trending_shows = []
        for result in data:
            tmdb_id = result['show']['ids']['tmdb']
            cached_poster_url = get_cached_poster_url(tmdb_id, 'tv')
            cached_media_meta = get_cached_media_meta(tmdb_id, 'tv')

            if cached_poster_url and cached_media_meta:
                #logging.info(f"Using cached data for show {result['show']['title']} (TMDB ID: {tmdb_id})")
                poster_path = cached_poster_url
                media_meta = cached_media_meta
            else:
                logging.info(f"Fetching data for show {result['show']['title']} (TMDB ID: {tmdb_id})")
                media_meta = get_media_meta(tmdb_id, 'tv')
                if media_meta:
                    poster_path = media_meta[0]
                    cache_poster_url(tmdb_id, 'tv', poster_path)
                    cache_media_meta(tmdb_id, 'tv', media_meta)
                    logging.info(f"Cached poster and metadata for show {result['show']['title']} (TMDB ID: {tmdb_id})")
                else:
                    if get_setting('TMDB', 'api_key') == "":
                        logging.warning("TMDB API key not set, using placeholder images")
                        
                        # Generate the placeholder URL
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        
                        # Check if the request is secure (HTTPS)
                        if request.is_secure:
                            # If it's secure, ensure the URL uses HTTPS
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='https').geturl()
                        else:
                            # If it's not secure, use HTTP
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='http').geturl()
                        
                        poster_path = placeholder_url
                    
            # Check if TMDB API key is set
            tmdb_api_key = get_setting('TMDB', 'api_key', '')
            tmdb_api_key_set = bool(tmdb_api_key)

            trending_shows.append({
                "title": result['show']['title'],
                "year": result['show']['year'],
                "imdb_id": result['show']['ids']['imdb'],
                "tmdb_id": tmdb_id,
                "rating": result['show']['rating'],
                "watcher_count": result['watcher_count'],
                "poster_path": poster_path,
                "show_overview": media_meta[1] if media_meta else '',
                "genre_ids": media_meta[2] if media_meta else [],
                "vote_average": media_meta[3] if media_meta else 0,
                "backdrop_path": media_meta[4] if media_meta else '',
                "tmdb_api_key_set": tmdb_api_key_set
            })

        return {"trendingShows": trending_shows}
    except api.exceptions.RequestException as e:
        logging.error(f"Error retrieving Trakt Trending Shows: {e}")
        return []

def process_media_selection(media_id: str, title: str, year: str, media_type: str, season: Optional[int], episode: Optional[int], multi: bool, version: str, genres: List[str], skip_cache_check: bool = True, background_cache_check: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from metadata.metadata import get_metadata
    logging.info(f"Processing media selection with skip_cache_check={skip_cache_check}, background_cache_check={background_cache_check}")
    logging.debug(f"[process_media_selection] Starting for '{title}' ({year}).")

    # Convert TMDB ID to IMDB ID using the metadata battery
    tmdb_id = int(media_id)
    metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)
    imdb_id = metadata.get('imdb_id')

    if not imdb_id:
        # Check if any Jackett scrapers are enabled
        from utilities.settings import load_config
        config = load_config()
        has_enabled_jackett = False
        
        for instance, settings in config.get('Scrapers', {}).items():
            if isinstance(settings, dict):
                if settings.get('type') == 'Jackett' and settings.get('enabled', False):
                    has_enabled_jackett = True
                    break
        
        if not has_enabled_jackett:
            logging.error(f"Could not find IMDB ID for TMDB ID {tmdb_id} and no Jackett scrapers are enabled")
            # Return an error structure that the route can handle
            return {"error": "Could not find IMDB ID for the selected media and no Jackett scrapers are enabled"}
        else:
            logging.info(f"No IMDB ID found for TMDB ID {tmdb_id}, but proceeding with Jackett scraper(s)")

    movie_or_episode = 'episode' if media_type == 'tv' or media_type == 'show' else 'movie'

    # Adjust multi flag based on season and episode
    if movie_or_episode == 'movie':
        multi = False
    elif season is not None and episode is None:
        multi = True

    # Check if anime is in the genres
    is_anime = genres and 'anime' in [genre.lower() for genre in genres]
    logging.info(f"Genres from frontend: {genres}")
    logging.info(f"Is anime detected from frontend genres: {is_anime}")

    # If anime is not detected in the frontend genres, check if it's in the metadata
    if not is_anime and metadata and 'genres' in metadata:
        metadata_genres = metadata.get('genres', [])
        is_anime_from_metadata = metadata_genres and 'anime' in [genre.lower() for genre in metadata_genres]
        logging.info(f"Genres from metadata: {metadata_genres}")
        logging.info(f"Is anime detected from metadata genres: {is_anime_from_metadata}")
        
        # If anime is detected in metadata but not in frontend genres, add it
        if is_anime_from_metadata and not is_anime:
            genres.append('anime')
            logging.info(f"Added anime to genres: {genres}")

    logging.info(f"Scraping parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, "
                   f"movie_or_episode={movie_or_episode}, season={season}, episode={episode}, multi={multi}, version={version}, genres={genres}")
    logging.debug(f"[process_media_selection] Calling scraper.scrape for '{title}'.")

    # Call the scraper function with the version parameter
    # scrape_results here are the "passed" torrents from the scrape function
    # filtered_out_results are those filtered by the scrape function
    passed_results_from_scrape, filtered_out_results_from_scrape = scrape(imdb_id, str(tmdb_id), title, int(year), movie_or_episode, version, season, episode, multi, genres)
    logging.debug(f"[process_media_selection] scraper.scrape for '{title}' returned: passed={len(passed_results_from_scrape)}, filtered_out={len(filtered_out_results_from_scrape if filtered_out_results_from_scrape else [])}")

    # Process the passed results (e.g., for cache status, hash extraction)
    processed_passed_results = []
    hashes = []
    for result in passed_results_from_scrape: # Iterate over the passed results from scrape
        if isinstance(result, dict):
            magnet_link = result.get('magnet')
            torrent_url = None
            
            # Check if this is a Jackett/Prowlarr source (which could be a torrent URL)
            source_info = result.get('source', '').lower() # Get the source information
            if magnet_link and ('jackett' in source_info or 'prowlarr' in source_info): # Check source_info instead of magnet_link
                # Try to detect if this is a torrent file URL
                if not magnet_link.startswith('magnet:'):
                    torrent_url = magnet_link
                    result['torrent_url'] = torrent_url
            
            # Process standard magnet links
            if magnet_link and magnet_link.startswith('magnet:'):
                try:
                    # Handle Jackett/Prowlarr magnet links
                    if 'jackett' in magnet_link.lower() or 'prowlarr' in magnet_link.lower():
                        file_param = re.search(r'file=([^&]+)', magnet_link)
                        if file_param:
                            from urllib.parse import unquote
                            decoded_name = unquote(file_param.group(1))
                            # Try to find a hash pattern in the decoded name
                            hash_match = re.search(r'[a-fA-F0-9]{40}', decoded_name)
                            if hash_match:
                                magnet_hash = hash_match.group(0).lower()
                                result['hash'] = magnet_hash
                                hashes.append(magnet_hash)
                                result['magnet'] = f"magnet:?xt=urn:btih:{magnet_hash}&dn={decoded_name}"
                    elif 'magnet:?xt=urn:btih:' in magnet_link:
                        magnet_hash = extract_hash_from_magnet(magnet_link)
                        result['hash'] = magnet_hash
                        hashes.append(magnet_hash)
                    # Make sure magnet_link is also available for the frontend
                    result['magnet_link'] = result['magnet']
                    processed_passed_results.append(result)
                except Exception as e:
                    logging.error(f"Error processing magnet link {magnet_link}: {str(e)}")
                    continue
            # Process torrent URLs
            elif torrent_url:
                # No hash extraction needed for torrent URLs, but include them in results
                processed_passed_results.append(result)
                logging.info(f"Added torrent URL result: {result['title']}")

    # Get the debrid provider and check its capabilities
    debrid_provider = get_debrid_provider()
    supports_cache_check = debrid_provider.supports_direct_cache_check
    supports_bulk_check = debrid_provider.supports_bulk_cache_checking
    
    # Check if this is a RealDebridProvider
    is_real_debrid = isinstance(debrid_provider, RealDebridProvider)
    
    logging.info(f"Debrid provider: supports_cache_check={supports_cache_check}, supports_bulk_check={supports_bulk_check}, is_real_debrid={is_real_debrid}")

    # Initialize all cache statuses as 'Not Checked'
    for result in processed_passed_results:
        result['cached'] = 'Not Checked'
        
        # Ensure both magnet and magnet_link are set (for UI and cache checking)
        if 'magnet' in result and not 'magnet_link' in result:
            result['magnet_link'] = result['magnet']
        elif 'magnet_link' in result and not 'magnet' in result:
            result['magnet'] = result['magnet_link']
            
        # Log some info about the result for debugging
        if 'torrent_url' in result:
            logging.info(f"Torrent URL result: {result['title']} (URL: {result['torrent_url'][:40]}...)")
        elif 'magnet_link' in result:
            logging.info(f"Magnet link result: {result['title']} (Link: {result['magnet_link'][:40]}...)")
        else:
            logging.warning(f"Result without magnet or torrent URL: {result['title']}")
    
    # Only perform cache check if explicitly requested and not in background mode
    if hashes and not skip_cache_check and not background_cache_check:
        # Always check exactly 5 hashes
        if len(hashes) > 5:
            logging.info(f"Limiting immediate cache check from {len(hashes)} to exactly 5 hashes")
            hashes_to_check = hashes[:5]
        else:
            hashes_to_check = hashes
            logging.info(f"Only {len(hashes)} hashes available for checking, less than the target of 5")
            
        logging.info(f"Performing immediate cache check for {len(hashes_to_check)} hashes")
        
        if supports_cache_check:
            try:
                if supports_bulk_check:
                    # If provider supports bulk checking, check all hashes at once
                    cache_status = debrid_provider.is_cached(hashes_to_check)
                    if isinstance(cache_status, bool):
                        # If we got a single boolean back, convert to dict
                        cache_status = {hash_value: cache_status for hash_value in hashes_to_check}
                    
                    # Update results with cache status
                    for result in processed_passed_results:
                        hash_value = result.get('hash')
                        if hash_value and hash_value in cache_status:
                            is_cached = cache_status[hash_value]
                            result['cached'] = 'Yes' if is_cached else 'No'
                else:
                    # Check hashes individually for providers that don't support bulk checking
                    checked_count = 0
                    for result in processed_passed_results:
                        hash_value = result.get('hash')
                        if hash_value and checked_count < 5:  # Limit to 5 checks
                            try:
                                is_cached = debrid_provider.is_cached(hash_value)
                                result['cached'] = 'Yes' if is_cached else 'No'
                                checked_count += 1
                            except Exception as e:
                                logging.error(f"Error checking individual cache status for {hash_value}: {e}")
                                result['cached'] = 'Error'
                
                # Mark all remaining results as N/A
                for result in processed_passed_results:
                    if result['cached'] == 'Not Checked':
                        result['cached'] = 'N/A'
                        
            except Exception as e:
                logging.error(f"Error checking cache status: {e}")
                # Fall back to N/A on error
                for result in processed_passed_results:
                    if result['cached'] == 'Not Checked':
                        result['cached'] = 'N/A'
        else:
            # If provider doesn't support direct checking but is RealDebrid, check first 5 results
            if is_real_debrid:
                logging.info("Using RealDebridProvider's is_cached method for exactly 5 results")
                torrent_ids_to_remove = []  # Track torrent IDs for removal
                
                # Only check first 5 results
                checked_count = 0
                for i, result in enumerate(processed_passed_results):
                    hash_value = result.get('hash')
                    if not hash_value or checked_count >= 5:  # Limit to 5 checks
                        continue
                        
                    checked_count += 1
                    try:
                        # Use the is_cached method which will add the torrent and return its cache status
                        # But we need to capture the torrent ID for later removal
                        magnet_link = f"magnet:?xt=urn:btih:{hash_value}"
                        cache_result = debrid_provider.is_cached(
                            magnet_link, 
                            result_title=result.get('title', f"Hash {hash_value}"),
                            result_index=i,
                            remove_uncached=True  # Explicitly set to remove uncached torrents
                        )
                        
                        result['cached'] = 'Yes' if cache_result else 'No'
                        
                        # Try to find the torrent ID using the hash
                        torrent_id = debrid_provider._all_torrent_ids.get(hash_value)
                        if torrent_id:
                            torrent_ids_to_remove.append(torrent_id)
                            logging.info(f"Added torrent ID {torrent_id} to removal list")
                    except Exception as e:
                        logging.error(f"Error checking cache for hash {hash_value}: {str(e)}")
                        result['cached'] = 'Error'
                
                # Remove all torrents after checking (even if they're cached)
                for torrent_id in torrent_ids_to_remove:
                    try:
                        debrid_provider.remove_torrent(torrent_id, "Removed after cache check")
                    except Exception as e:
                        logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
                
                # Mark all remaining results as N/A
                for result in processed_passed_results:
                    if result['cached'] == 'Not Checked':
                        result['cached'] = 'N/A'
            else:
                # Mark all results as N/A if provider doesn't support direct checking
                for result in processed_passed_results:
                    result['cached'] = 'N/A'
                logging.info("Provider does not support direct cache checking, marking all as N/A")
    else:
        logging.info(f"Skipping immediate cache check. Skip: {skip_cache_check}, Background: {background_cache_check}")
        
    # The filtered_out_results_from_scrape are already in the correct list format.
    # We return the processed passed results and the original filtered-out results.
    logging.info(f"[process_media_selection] Returning for '{title}': processed_passed_results={len(processed_passed_results)}, filtered_out_results_from_scrape={len(filtered_out_results_from_scrape if filtered_out_results_from_scrape else [])}")
    return processed_passed_results, filtered_out_results_from_scrape

def get_available_versions():
    scraping_versions = get_setting('Scraping', 'versions', default={})
    return list(scraping_versions.keys())

def get_media_details(media_id: str, media_type: str) -> Dict[str, Any]:
    from metadata.metadata import get_metadata, get_imdb_id_if_missing
    #logging.info(f"Fetching media details for ID: {media_id}, Type: {media_type}")

    # If media_id is a TMDB ID, convert it to IMDb ID
    if media_type == 'movie':
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(media_id)})
    else:
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(media_id)})

    if not imdb_id:
        logging.error(f"Could not find IMDB ID for TMDB ID: {media_id}")
        return {}

    # Fetch metadata using the IMDb ID
    metadata = get_metadata(imdb_id=imdb_id, item_media_type=media_type)

    if not metadata:
        logging.error(f"Could not fetch metadata for IMDb ID: {imdb_id}")
        return {}

    # Add additional details that might be needed
    metadata['media_type'] = media_type
    if media_type == 'tv':
        from metadata.metadata import get_all_season_episode_counts, get_show_airtime_by_imdb_id
        metadata['seasons'] = get_all_season_episode_counts(imdb_id)
        metadata['airtime'] = get_show_airtime_by_imdb_id(imdb_id)

    logging.info(f"Successfully fetched media details for {metadata.get('title', 'Unknown Title')} ({metadata.get('year', 'Unknown Year')})")
    return metadata

def parse_season_episode(search_term: str) -> Tuple[int, int, bool]:
    # Match patterns like "S01E01", "s01e01", "S01", "s01"
    match = re.search(r'[Ss](\d+)(?:[Ee](\d+))?', search_term)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else 1
        multi = not bool(match.group(2))  # If episode is not specified, set multi to True
        return season, episode, multi
    return 1, 1, True  # Default to S01E01 with multi=True if no match
    
def process_torrent_selection(torrent_index: int, torrent_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    logging.info(f"Processing torrent selection: {torrent_index}")
    
    if 0 <= torrent_index < len(torrent_results):
        selected_torrent = torrent_results[torrent_index]
        magnet_link = selected_torrent.get('magnet')
        
        if magnet_link:
            logging.info(f"Selected torrent: {selected_torrent}")
            logging.info(f"Magnet link: {magnet_link}")
            debrid_provider = get_debrid_provider()
            result = debrid_provider.add_to_debrid(magnet_link)
            if result:
                logging.info(f"Torrent result: {result}")
                if result == 'downloading' or result == 'queued':
                    logging.info("Uncached torrent added to Real-Debrid successfully")
                    return {
                        "success": True,
                        "message": "Uncached torrent added to Real-Debrid successfully",
                        "torrent_info": selected_torrent
                    }
                else:
                    logging.info("Cached torrent added to Real-Debrid successfully")
                    return {
                        "success": True,
                        "message": "Cached torrent added to Real-Debrid successfully",
                        "torrent_info": selected_torrent
                    }
            else:
                logging.error("Failed to add torrent to Real-Debrid")
                return {
                    "success": False,
                    "error": "Failed to add torrent to Real-Debrid"
                }
        else:
            logging.error("No magnet link found for the selected torrent")
            return {
                "success": False,
                "error": "No magnet link found for the selected torrent"
            }
    else:
        logging.error(f"Invalid torrent index: {torrent_index}")
        return {
            "success": False,
            "error": "Invalid torrent index"
        }

def parse_torrent_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed_results = []
    for result in results:
        parsed_result = {
            "name": result.get("name", "Unknown"),
            "size": result.get("size", "Unknown"),
            "seeders": result.get("seeders", 0),
            "magnet": result.get("magnet", "")
        }
        parsed_results.append(parsed_result)
    return parsed_results