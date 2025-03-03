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

def scrape_nyaa_instance(settings: Dict[str, Any], title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    """Scrape Nyaa using nyaapy."""
    # Map settings to nyaapy parameters
    category = int(settings.get('categories', '1_2').split('_')[0])  # Get main category
    subcategory = int(settings.get('categories', '1_2').split('_')[1])  # Get subcategory
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
        results = Nyaa.search(keyword=query, category=category, subcategory=subcategory, filters=filters)
        if not results and str(year) in query:
            # Try alternative query without the year
            alt_query = query.replace(str(year), "").strip()
            results = Nyaa.search(keyword=alt_query, category=category, subcategory=subcategory, filters=filters)
        
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

def scrape_nyaa_anime_episode(title: str, year: int, season: int, episode: int, episode_formats: Dict[str, str], tmdb_id: str) -> List[Dict[str, Any]]:
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
        results = _scrape_nyaa_with_format(title, year, format_pattern)
        
        # Add the format type to each result
        for result in results:
            result['anime_format'] = format_type
            
        return format_type, results
    
    # Use ThreadPoolExecutor to scrape all formats simultaneously
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Submit all scraping tasks
        future_to_format = {
            executor.submit(scrape_with_format, format_type, format_pattern): format_type
            for format_type, format_pattern in episode_formats.items()
        }
        
        # Collect results as they complete
        for future in concurrent.futures.as_completed(future_to_format):
            format_type, results = future.result()
            format_results[format_type] = results
            all_results.extend(results)
            logging.info(f"Found {len(results)} results using format {format_type}")
    
    # Determine best format based on number of results
    if format_results:
        best_format = max(format_results.items(), key=lambda x: len(x[1]))[0]
        best_count = len(format_results[best_format])
        logging.info(f"Best format for {title} is {best_format} with {best_count} results")
        
        # Update the database with the best format
        if best_count > 0:
            update_anime_format(tmdb_id, best_format)
            logging.info(f"Updated anime format preference to {best_format} for {title}")
    
    return all_results

def _scrape_nyaa_with_format(title: str, year: int, format_pattern: str) -> List[Dict[str, Any]]:
    """Helper function to scrape Nyaa with a specific episode format pattern."""
    # Remove dots and normalize spaces
    title = title.replace(".", " ").strip()
    search_query = f"{title} {format_pattern}"
    logging.debug(f"Searching Nyaa with query: {search_query}")
    
    # Set up default settings
    settings = {
        "categories": "1_2",  # Use anime category
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }
    
    try:
        results = scrape_nyaa_instance(settings, search_query, year, "episode", None, None, False)
        return results
    except Exception as e:
        logging.error(f"Error scraping Nyaa with format {format_pattern}: {str(e)}")
        return []

def scrape_nyaa(title: str, year: int, content_type: str = 'movie', season: Optional[int] = None, 
                episode: Optional[int] = None, episode_formats: Optional[Dict[str, str]] = None,
                tmdb_id: Optional[str] = None, multi: bool = False) -> List[Dict[str, Any]]:
    """Main Nyaa scraping function."""
    if content_type.lower() == 'episode' and tmdb_id:
        if multi:
            # For multi-episode requests, search for season packs instead of individual episodes
            return scrape_nyaa_anime_season(title, year, season, tmdb_id)
        elif episode_formats:
            # For single episode requests with format info
            return scrape_nyaa_anime_episode(title, year, season, episode, episode_formats, tmdb_id)
    
    # Set up default settings
    settings = {
        "categories": "1_2" if content_type.lower() == 'episode' else "1_0",
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }
    
    try:
        results = scrape_nyaa_instance(settings, title, year, content_type, season, episode, multi)
        return results
    except Exception as e:
        logging.error(f"Error scraping Nyaa: {str(e)}")
        return []

def scrape_nyaa_anime_season(title: str, year: int, season: int, tmdb_id: str) -> List[Dict[str, Any]]:
    """Scrape Nyaa for anime season packs."""
    all_results = []
    
    # Define search patterns for season packs
    season_patterns = [
        f"Season {season}",
        f"S{season:01d}",
        f"S{season:02d}",
        "batch",
        "complete"
    ]
    
    # Define a function to scrape with a specific season pattern
    def scrape_with_pattern(pattern):
        logging.info(f"Searching for anime season pack with pattern: {pattern}")
        search_query = f"{title} {pattern}"
        logging.debug(f"Searching Nyaa with query: {search_query}")
        
        # Set up default settings
        settings = {
            "categories": "1_2",  # Use anime category
            "filter": "0",
            "sort": "seeders",
            "order": "desc"
        }
        
        try:
            results = scrape_nyaa_instance(settings, search_query, year, "episode", season, None, True)
            
            # Mark results as season packs
            for result in results:
                result['is_anime'] = True
                result['anime_format'] = 'season_pack'
                
                # Add season pack info to parsed_info if it doesn't exist
                if 'parsed_info' not in result:
                    result['parsed_info'] = {}
                
                if 'season_episode_info' not in result['parsed_info']:
                    result['parsed_info']['season_episode_info'] = {
                        'season_pack': 'Complete',
                        'seasons': [season],
                        'episodes': []  # Will be filled in by filter_results
                    }
            
            return results
        except Exception as e:
            logging.error(f"Error scraping Nyaa with pattern {pattern}: {str(e)}")
            return []
    
    # Use ThreadPoolExecutor to scrape all patterns simultaneously
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Submit all scraping tasks
        future_to_pattern = {
            executor.submit(scrape_with_pattern, pattern): pattern
            for pattern in season_patterns
        }
        
        # Collect results as they complete
        for future in concurrent.futures.as_completed(future_to_pattern):
            pattern = future_to_pattern[future]
            results = future.result()
            all_results.extend(results)
            logging.info(f"Found {len(results)} results using pattern {pattern}")
    
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
