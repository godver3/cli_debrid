import sys
import os
import logging
from typing import List, Dict, Any, Optional
from nyaapy.nyaasi.nyaa import Nyaa
from nyaapy.torrent import Torrent
from scraper.functions import *
from database.database_writing import update_anime_format, get_anime_format
import threading
import concurrent.futures
import time
import re
import random
import requests

# Helper - build proxy context for limited environment
from contextlib import contextmanager


@contextmanager
def _warp_proxy_context():
    """Temporarily enable the WARP proxy (if in limited env) for the duration of the context."""
    if os.environ.get("CLI_DEBRID_ENVIRONMENT_MODE", "full") == "full":
        # No-op – yield immediately
        logging.info("WARP proxy not needed - running in full environment mode")
        # Return a plain session for consistency
        plain_session = requests.Session()
        try:
            yield plain_session
        finally:
            plain_session.close()
        return

    proxy_url = os.environ.get("WARP_PROXY_URL", "http://warp:1080")
    logging.info("WARP proxy required - running in limited environment mode")

    # Create a separate session with proxy configuration instead of setting global env vars
    proxy_session = requests.Session()
    proxy_session.proxies = {
        'http': proxy_url,
        'https': proxy_url
    }

    logging.info(
        "WARP proxy enabled for Nyaa request – using %s with separate session",
        proxy_url
    )

    try:
        # Yield the proxy session instead of setting global environment variables
        yield proxy_session
    finally:
        # Clean up the proxy session
        proxy_session.close()
        logging.info("WARP proxy session closed")

def convert_size_to_gb(size: str) -> float:
    """Convert various size formats to GB."""
    size = size.lower().replace(' ', '')
    if 'kib' in size:
        return float(size.replace('kib', '').strip()) / (1024 * 1024)
    elif 'mib' in size:
        return float(size.replace('mib', '').strip()) / 1024
    elif 'gib' in size:
        return float(size.replace('gib', '').strip())
    elif 'tib' in size:
        return float(size.replace('tib', '').strip()) * 1024
    elif 'kb' in size:
        return float(size.replace('kb', '').strip()) / (1024 * 1024)
    elif 'mb' in size:
        return float(size.replace('mb', '').strip()) / 1024
    elif 'gb' in size:
        return float(size.replace('gb', '').strip())
    elif 'tb' in size:
        return float(size.replace('tb', '').strip()) * 1024
    else:
        # If no unit is found, assume it's in bytes
        return float(size) / (1024 * 1024 * 1024)

def process_torrent(torrent: Torrent) -> Dict[str, Any]:
    """Process a Torrent object into our standard dictionary format."""
    size_gb = convert_size_to_gb(torrent.size)
    return {
        'title': torrent.name,
        'size': size_gb,
        'source': 'Nyaa',
        'magnet': torrent.magnet,
        'seeders': int(torrent.seeders) if torrent.seeders is not None else 0,
        'leechers': int(torrent.leechers) if torrent.leechers is not None else 0,
        'downloads': int(torrent.completed_downloads) if torrent.completed_downloads is not None else 0,
        'url': torrent.url
    }

def contains_target_episode(results: List[Dict[str, Any]], target_episode: int, target_season: int) -> bool:
    """Check if any result contains the target episode number."""
    if not results or target_episode is None or target_season is None:
        return False
        
    for result in results:
        title = result.get('title', '').lower()
        
        # First, check for explicit SxxExx patterns (most reliable)
        explicit_patterns = [
            f"s{target_season:02d}e{target_episode:02d}",  # S03E01
            f"s{target_season}e{target_episode:02d}",      # S3E01
            f"s{target_season:02d}e{target_episode}",      # S03E1
            f"s{target_season}e{target_episode}"           # S3E1
        ]
        
        for pattern in explicit_patterns:
            if pattern in title:
                logging.debug(f"Found explicit SxxExx pattern '{pattern}' in title: {result.get('title')}")
                return True
        
        # For standalone episode patterns (E01, 01), we need to be more careful
        # Only match if there's no conflicting season information
        
        # Check if title contains any season information
        has_season_info = False
        season_in_title = None
        
        # Look for season patterns in the title
        season_patterns = [
            rf"\bs{target_season:02d}\b",  # S03
            rf"\bs{target_season}\b",      # S3
            rf"\bseason\s+{target_season:02d}\b",  # Season 03
            rf"\bseason\s+{target_season}\b",      # Season 3
        ]
        
        for pattern in season_patterns:
            if re.search(pattern, title):
                has_season_info = True
                season_in_title = target_season
                break
        
        # Also check for other seasons that might conflict
        other_season_patterns = [
            r"\bs0?[1-9]\b",  # S01, S1, S02, S2, etc.
            r"\bseason\s+0?[1-9]\b",  # Season 1, Season 01, etc.
        ]
        
        for pattern in other_season_patterns:
            match = re.search(pattern, title)
            if match:
                has_season_info = True
                # Extract the season number
                season_text = match.group()
                if 'season' in season_text:
                    season_num = season_text.replace('season', '').strip()
                else:
                    season_num = season_text[1:]  # Remove 's'
                
                try:
                    season_in_title = int(season_num)
                    if season_in_title != target_season:
                        logging.debug(f"Found conflicting season {season_in_title} in title: {result.get('title')}")
                        break
                except ValueError:
                    pass
        
        # If we found a conflicting season, don't match standalone episode patterns
        if has_season_info and season_in_title != target_season:
            continue
        
        # Now check for standalone episode patterns (only if no conflicting season)
        episode_patterns = [
            rf"\be{target_episode:02d}\b",  # E01 (word boundary)
            rf"\be{target_episode}\b",      # E1 (word boundary)
            rf"\b{target_episode:02d}\b",   # 01 (word boundary)
        ]
        
        for pattern in episode_patterns:
            if re.search(pattern, title):
                # Additional context check to avoid false positives
                match = re.search(pattern, title)
                if match:
                    start_pos = match.start()
                    end_pos = match.end()
                    
                    # Check if preceded by 's' or 'season' (likely a season number)
                    if start_pos > 0:
                        before_match = title[start_pos-1:start_pos+1]
                        if before_match.startswith('s') or before_match.startswith('season'):
                            continue
                    
                    # Check if followed by 'e' (likely part of SxxExx format)
                    if end_pos < len(title):
                        after_match = title[end_pos-1:end_pos+1]
                        if after_match.endswith('e'):
                            continue
                    
                    logging.debug(f"Found standalone episode pattern '{pattern}' in title: {result.get('title')}")
                    return True
    
    return False

def scrape_nyaa_with_retry(query: str, category: int, subcategory: int, filters: int, max_retries: int = 3, initial_delay: float = 1.0) -> List[Any]:
    """Scrape Nyaa with exponential backoff retry logic for HTTP errors."""
    
    for attempt in range(max_retries):
        try:
            logging.debug(f"Nyaa search attempt {attempt + 1}/{max_retries} for query: {query}")
            
            # Use the proxy context to get a session with proxy configuration
            # Note: Each retry creates a new session. For high-frequency retries,
            # consider implementing a session pool or keep-alive cache per thread.
            with _warp_proxy_context() as session:
                # Use the session for the Nyaa search
                results = _search_nyaa_with_session(query, category, subcategory, filters, session)
            return results
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Check for specific HTTP errors that should trigger retries
            is_retryable_error = (
                '429' in error_str or  # Rate limit
                '504' in error_str or  # Gateway timeout
                '502' in error_str or  # Bad gateway
                '503' in error_str or  # Service unavailable
                'timeout' in error_str or
                'connection' in error_str
            )
            
            if is_retryable_error and attempt < max_retries - 1:
                # Calculate exponential backoff with jitter
                delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                
                if '429' in error_str:
                    logging.warning(f"Nyaa rate limit (429) hit. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}")
                elif '504' in error_str:
                    logging.warning(f"Nyaa gateway timeout (504) hit. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}")
                else:
                    logging.warning(f"Nyaa request failed with retryable error. Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}. Error: {str(e)}")
                
                time.sleep(delay)
                continue
            else:
                # Either not a retryable error or we've exhausted retries
                if is_retryable_error:
                    logging.error(f"Nyaa request failed after {max_retries} attempts with retryable error: {str(e)}")
                else:
                    logging.error(f"Nyaa request failed with non-retryable error: {str(e)}")
                raise
    
    return []

def _search_nyaa_with_session(query: str, category: int, subcategory: int, filters: int, session: requests.Session) -> List[Any]:
    """Helper function to search Nyaa using a specific session."""
    # Since the Nyaa library uses requests.get() directly and we can't easily modify it,
    # we'll reimplement the search logic ourselves using our session
    # This ensures complete isolation from the global requests library
    
    import requests
    from nyaapy.nyaasi.nyaa import Nyaa
    from nyaapy.parser import parse_nyaa, parse_nyaa_rss
    from nyaapy.torrent import json_to_class
    
    # Reconstruct the search URL (same logic as Nyaa.search)
    base_url = Nyaa.URL
    user = None  # We don't use user searches
    page = 0
    sorting = "id"  # Sorting by id = sorting by date
    order = "desc"
    
    user_uri = f"user/{user}" if user else ""
    
    if page > 0:
        search_uri = "{}/{}?f={}&c={}_{}&q={}&p={}&s={}&o={}".format(
            base_url,
            user_uri,
            filters,
            category,
            subcategory,
            query,
            page,
            sorting,
            order,
        )
    else:
        search_uri = "{}/{}?f={}&c={}_{}&q={}&s={}&o={}".format(
            base_url,
            user_uri,
            filters,
            category,
            subcategory,
            query,
            sorting,
            order,
        )
    
    if not user:
        search_uri += "&page=rss"
    
    # Log proxy usage
    if hasattr(session, 'proxies') and session.proxies:
        logging.info(f"Nyaa using proxy: {session.proxies}")
    else:
        logging.info("Nyaa using direct connection (no proxy)")
    
    # Use our session to make the request
    http_response = session.get(search_uri)
    http_response.raise_for_status()
    
    # Parse the response using the same logic as Nyaa
    if user:
        json_data = parse_nyaa(
            request_text=http_response.content, limit=None, site=Nyaa.SITE
        )
    else:
        json_data = parse_nyaa_rss(
            request_text=http_response.content, limit=None, site=Nyaa.SITE
        )
    
    # Convert JSON data to Torrent objects (same as Nyaa)
    return json_to_class(json_data)

def scrape_nyaa_instance(settings: Dict[str, Any], title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, is_translated_search: bool = False) -> List[Dict[str, Any]]:
    """Scrape Nyaa using nyaapy with proper error handling."""
    # Map settings to nyaapy parameters
    category = 1 # Default to Anime
    subcategory = 2 # Default to English-translated

    if is_translated_search:
        category = 1 # Anime
        subcategory = 3 # Non-English-translated
        logging.info("Using Nyaa category 1_3 (Anime - Non-English-translated) due to translated search.")
    elif 'categories' in settings:
        try:
            cat_parts = settings['categories'].split('_')
            category = int(cat_parts[0])
            subcategory = int(cat_parts[1])
        except (ValueError, IndexError):
            logging.warning(f"Invalid categories format '{settings['categories']}', defaulting to 1_2.")
            category = 1
            subcategory = 2
    else:
        # Fallback logic if 'categories' not in settings (keep default 1_2 for anime, maybe adjust for others)
        if content_type.lower() != 'episode' and content_type.lower() != 'movie': # Assuming 'show' maps to anime here
            # You might want different defaults for non-anime/non-movie content types
            logging.warning(f"Content type '{content_type}' might need specific Nyaa category, defaulting to 1_2.")

    filters = int(settings.get('filter', '0'))
    
    # Normalize the title and build query
    title = title.replace(".", " ").strip()
    if content_type.lower() == 'movie':
        query = f"{title} {year}"
    else:
        query = title
        if episode is not None and not multi:
            query += f" {episode:02d}"  # Just episode number with leading zero
    
    try:
        # Use the new retry-enabled search function
        results = scrape_nyaa_with_retry(query, category, subcategory, filters)
        
        if not results and str(year) in query:
            # Try alternative query without the year
            alt_query = query.replace(str(year), "").strip()
            results = scrape_nyaa_with_retry(alt_query, category, subcategory, filters)
        
        processed_results = []
        for torrent in results:
            try:
                processed_result = process_torrent(torrent)
                processed_results.append(processed_result)
            except Exception as e:
                logging.error(f"Error processing torrent {torrent.name}: {str(e)}")
                continue
        
        # Sort by seeders if that was requested
        if settings.get('sort') == 'seeders' and settings.get('order') == 'desc':
            processed_results.sort(key=lambda x: x['seeders'], reverse=True)
        
        return processed_results
        
    except Exception as e:
        logging.error(f"Error scraping Nyaa: {str(e)}")
        return []

def test_nyaa_scraper(title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, **kwargs) -> List[Dict[str, Any]]:
    """Test entrypoint for the Nyaa scraper."""
    settings = {
        "categories": kwargs.get("categories", "1_2"),
        "filter": kwargs.get("filter", "0"),
        "sort": kwargs.get("sort", "seeders"),
        "order": kwargs.get("order", "desc"),
    }
    
    try:
        results = scrape_nyaa_instance(settings, title, year, content_type, season, episode, multi)
        print(f"Scraped {len(results)} results from Nyaa:")
        for result in results[:5]:  # Print first 5 results
            print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
        return results
    except Exception as e:
        print(f"Error testing Nyaa scraper: {str(e)}")
        return []

def scrape_nyaa_anime_episode(title: str, year: int, season: int, episode: int, episode_formats: Dict[str, str], tmdb_id: str, is_translated_search: bool = False) -> List[Dict[str, Any]]:
    """Scrape Nyaa for an anime episode using different format patterns."""
    all_results = []
    format_results = {}
    
    # Use the passed episode_formats instead of hardcoding
    if not episode_formats:
        # Fallback to hardcoded formats if none provided
        episode_formats = {
            'no_zeros': f"{episode}",
            'regular': f"S{season:02d}E{episode:02d}",
            'absolute_with_e': f"E{((season - 1) * 13) + episode:03d}",  # Using default 13 episodes per season
            'absolute': f"{((season - 1) * 13) + episode:03d}",  # Using default 13 episodes per season
            'combined': f"S{season:02d}E{((season - 1) * 13) + episode:03d}"  # Using default 13 episodes per season
        }
    
    # Define a function to scrape with a specific format
    def scrape_with_format(format_type, format_pattern):
        logging.info(f"Trying anime format {format_type} for {title}")
        # Pass the is_translated_search flag down
        results = _scrape_nyaa_with_format(title, year, format_pattern, is_translated_search)
        
        # Add the format type to each result
        for result in results:
            result['anime_format'] = format_type
            
        return format_type, results
    
    # Try formats sequentially and check if results contain target episode
    for format_type, format_pattern in episode_formats.items():
        try:
            # Add small delay between searches to prevent rate limiting
            if format_type != list(episode_formats.keys())[0]:  # Skip delay for first format
                time.sleep(0.5)  # Increased delay to 500ms for better rate limiting
            
            format_type, results = scrape_with_format(format_type, format_pattern)
            format_results[format_type] = results
            all_results.extend(results)
            logging.info(f"Found {len(results)} results using format {format_type}")
            
            # Check if these results contain the target episode
            if results and contains_target_episode(results, episode, season):
                logging.info(f"Found target episode S{season}E{episode} in results from format {format_type}, stopping search")
                break
            elif len(results) >= 10:
                # If we found many results but none contain the target episode, 
                # continue searching other formats to find the actual episode
                logging.info(f"Found {len(results)} results with format {format_type} but none contain target episode S{season}E{episode}, continuing search")
                continue
                
        except Exception as e:
            logging.error(f"Error scraping format {format_type}: {e}")
            # Continue to next format even if this one failed
            continue
    
    # Determine best format based on number of results that contain target episode
    best_format = None
    best_count = 0
    
    for format_type, results in format_results.items():
        if results and contains_target_episode(results, episode, season):
            count = len([r for r in results if contains_target_episode([r], episode, season)])
            if count > best_count:
                best_format = format_type
                best_count = count
    
    # If no format found target episode, use the one with most results
    if not best_format and format_results:
        best_format = max(format_results.items(), key=lambda x: len(x[1]))[0]
        best_count = len(format_results[best_format])
        logging.warning(f"No format found target episode S{season}E{episode}, using {best_format} with {best_count} total results")
    elif best_format:
        logging.info(f"Best format for {title} S{season}E{episode} is {best_format} with {best_count} matching results")
    
    # Update the database with the best format
    if best_format and best_count > 0:
        update_anime_format(tmdb_id, best_format)
        logging.info(f"Updated anime format preference to {best_format} for {title}")
    
    return all_results

def _scrape_nyaa_with_format(title: str, year: int, format_pattern: str, is_translated_search: bool = False) -> List[Dict[str, Any]]:
    """Helper function to scrape Nyaa with a specific episode format pattern."""
    # Remove dots and normalize spaces
    title = title.replace(".", " ").strip()
    search_query = f"{title} {format_pattern}"
    logging.debug(f"Searching Nyaa with query: {search_query}")
    
    # Set up default settings
    settings = {
        "categories": "1_0",  # Use anime category (all subcategories)
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }
    
    try:
        # Pass the is_translated_search flag to scrape_nyaa_instance
        results = scrape_nyaa_instance(settings, search_query, year, "episode", None, None, False, is_translated_search)
        return results
    except Exception as e:
        logging.error(f"Error scraping Nyaa with format {format_pattern}: {str(e)}")
        return []

def scrape_nyaa(title: str, year: int, content_type: str = 'movie', season: Optional[int] = None,
                episode: Optional[int] = None, episode_formats: Optional[Dict[str, str]] = None,
                tmdb_id: Optional[str] = None, multi: bool = False,
                is_translated_search: bool = False) -> List[Dict[str, Any]]:
    """Main Nyaa scraping function."""
    if content_type.lower() == 'episode' and tmdb_id:
        if multi:
            # For multi-episode requests, search for season packs instead of individual episodes
            return scrape_nyaa_anime_season(title, year, season, tmdb_id, episode_formats, is_translated_search)
        elif episode_formats:
            # For single episode requests with format info
            return scrape_nyaa_anime_episode(title, year, season, episode, episode_formats, tmdb_id, is_translated_search)
    
    # Set up default settings
    settings = {
        "categories": "1_0", # Default category to all anime
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }

    # If it's an anime movie being searched with translation, adjust category
    if content_type.lower() == 'movie' and is_translated_search:
        settings["categories"] = "1_3" # Anime - Non-English-translated

    try:
        # Pass the flag to the instance scraper
        results = scrape_nyaa_instance(settings, title, year, content_type, season, episode, multi, is_translated_search)
        return results
    except Exception as e:
        logging.error(f"Error scraping Nyaa: {str(e)}")
        return []

def scrape_nyaa_anime_season(title: str, year: int, season: int, tmdb_id: str, episode_formats: Dict[str, str], is_translated_search: bool = False) -> List[Dict[str, Any]]:
    """Scrape Nyaa for anime season packs."""
    
    # Use the passed episode_formats which are generated for the first episode of the season.
    if not episode_formats:
        logging.warning(f"No episode_formats provided for anime season search for '{title}' S{season}. Skipping.")
        return []
    
    # Reuse the episode search logic but mark results as potential season packs
    all_results = []
    
    def scrape_with_format(format_type, format_pattern):
        results = _scrape_nyaa_with_format(title, year, format_pattern, is_translated_search)
        
        # Mark results as potential season packs - filtering will determine actual type
        for result in results:
            result['is_anime'] = True
            result['anime_format'] = 'season_pack'  # Will be corrected by filtering if needed
            
        return results
    
    # Use ThreadPoolExecutor to scrape all formats simultaneously
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_format = {
            executor.submit(scrape_with_format, format_type, format_pattern): format_type
            for format_type, format_pattern in episode_formats.items()
        }
        
        # Collect results as they complete
        for future in concurrent.futures.as_completed(future_to_format):
            results = future.result()
            all_results.extend(results)
    
    return all_results

if __name__ == "__main__":
    # Test for a movie
    print("Testing Nyaa scraper for movie:")
    results = test_nyaa_scraper("Akira", 1988, "movie")
    for result in results[:5]:  # Print first 5 results
        print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
    print(f"Total results: {len(results)}")

    print("\nTesting Nyaa scraper for recent TV show (full series):")
    results = test_nyaa_scraper("Attack on Titan", 2013, "show")
    for result in results[:5]:  # Print first 5 results
        print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
    print(f"Total results: {len(results)}")

    print("\nTesting Nyaa scraper for recent TV show (specific season):")
    results = test_nyaa_scraper("Attack on Titan", 2013, "show", season=4)
    for result in results[:5]:  # Print first 5 results
        print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
    print(f"Total results: {len(results)}")

    print("\nTesting Nyaa scraper for recent TV show (specific episode):")
    results = test_nyaa_scraper("Attack on Titan", 2013, "show", season=4, episode=1)
    for result in results[:5]:  # Print first 5 results
        print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
    print(f"Total results: {len(results)}")

    print("\nTesting Nyaa scraper for movie with custom settings:")
    results = test_nyaa_scraper("Akira", 1988, "movie", categories="1_2", filter="2", sort="size", order="desc")
    for result in results[:5]:  # Print first 5 results
        print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
    print(f"Total results: {len(results)}")
