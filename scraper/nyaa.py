import sys
import os
import logging
from typing import List, Dict, Any, Optional
from nyaapy.nyaasi.nyaa import Nyaa
from nyaapy.torrent import Torrent
from scraper.functions import *
from database.database_writing import update_anime_format, get_anime_format

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
    
    # Get stored format preference
    preferred_format = get_anime_format(tmdb_id)
    if preferred_format:
        logging.info(f"Found preferred anime format for {title}: {preferred_format}")
        # Try preferred format first
        format_pattern = episode_formats[preferred_format]
        results = _scrape_nyaa_with_format(title, year, format_pattern)
        if results:
            logging.info(f"Found {len(results)} results using preferred format {preferred_format}")
            return results
        logging.info(f"No results found with preferred format {preferred_format}, trying other formats")
    
    # Try all formats if no preferred format or no results with preferred format
    for format_type, format_pattern in episode_formats.items():
        logging.info(f"Trying anime format {format_type} for {title}")
        results = _scrape_nyaa_with_format(title, year, format_pattern)
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
                tmdb_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Main Nyaa scraping function."""
    if content_type.lower() == 'episode' and episode_formats and tmdb_id:
        return scrape_nyaa_anime_episode(title, year, season, episode, episode_formats, tmdb_id)
    
    # Set up default settings
    settings = {
        "categories": "1_2" if content_type.lower() == 'episode' else "1_0",
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }
    
    try:
        results = scrape_nyaa_instance(settings, title, year, content_type, season, episode, False)
        return results
    except Exception as e:
        logging.error(f"Error scraping Nyaa: {str(e)}")
        return []

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
