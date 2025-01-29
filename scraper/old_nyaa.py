import sys
import os
import time
import random

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from typing import List, Dict, Any, Optional
from api_tracker import api
from urllib.parse import quote, urlencode
from bs4 import BeautifulSoup
import re
from scraper.functions import *
from database.database_writing import update_anime_format, get_anime_format

def scrape_nyaa_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    """Scrape Nyaa using multiple fallback instances."""
    nyaa_instances = [
        "https://nyaa.si/",  # Primary instance first
        "https://nyaa.land/",
        "https://nyaa.iss.ink/",
    ]
    
    sort = settings.get('sort', 'seeders')
    order = settings.get('order', 'desc')
    category = settings.get('categories', '1_2')  # Default to anime category
    params = f'c={category}&s={sort}&o={order}'
    
    # Normalize the title and build query
    title = title.replace(".", " ").strip()
    if content_type.lower() == 'movie':
        query = f"{title} {year}"
    else:
        query = title
        if episode is not None and not multi:
            query += f" {episode:02d}"  # Just episode number with leading zero
    
    encoded_query = quote(query).replace('.', '+').replace('%20', '+')
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }
    
    all_errors = []
    for base_url in nyaa_instances:
        full_url = f"{base_url}?{params}&q={encoded_query}"
        logging.info(f"Attempting to scrape {full_url}")
        
        try:
            headers['Referer'] = base_url
            response = api.get(full_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                results = parse_nyaa_results(response.content)
                if results:
                    logging.info(f"Successfully scraped {len(results)} results from {base_url}")
                    return results
                else:
                    logging.info(f"No results found at {base_url} for query: {query}")
                    # Try alternative query without the year if present
                    if str(year) in query:
                        alt_query = query.replace(str(year), "").strip()
                        alt_encoded_query = quote(alt_query).replace('.', '+').replace('%20', '+')
                        alt_url = f"{base_url}?{params}&q={alt_encoded_query}"
                        logging.info(f"Trying alternative query: {alt_url}")
                        
                        alt_response = api.get(alt_url, headers=headers, timeout=10)
                        if alt_response.status_code == 200:
                            alt_results = parse_nyaa_results(alt_response.content)
                            if alt_results:
                                logging.info(f"Successfully scraped {len(alt_results)} results with alternative query")
                                return alt_results
            else:
                error_msg = f"HTTP {response.status_code} from {base_url}"
                logging.warning(error_msg)
                all_errors.append(error_msg)
                
        except Exception as e:
            error_msg = f"Error with {base_url}: {str(e)}"
            logging.error(error_msg)
            all_errors.append(error_msg)
        
        time.sleep(random.uniform(2, 4))
    
    if all_errors:
        logging.error(f"All Nyaa instances failed. Errors: {'; '.join(all_errors)}")
    return []

def parse_nyaa_results(content: bytes) -> List[Dict[str, Any]]:
    """Parse HTML content from Nyaa."""
    results = []
    try:
        soup = BeautifulSoup(content, 'html.parser')
        
        # First, check if we got a valid page structure
        rows = soup.select("tr.danger,tr.default,tr.success")
        if not rows:
            logging.warning("No valid result rows found in HTML response")
            # Log a sample of the HTML for debugging
            logging.debug(f"Response HTML sample: {str(soup)[:500]}")
            return []
            
        for row in rows:
            try:
                # Find magnet link
                magnet_link = row.find('a', {'href': re.compile(r'(magnet:)+[^"]*')})
                if not magnet_link:
                    continue
                magnet_link = magnet_link.get('href', '')
                
                # Find title
                title_links = row.find_all('a', {'class': None})
                if len(title_links) < 2:
                    continue
                title = title_links[1].get('title', title_links[1].text.strip())
                
                # Find size and seeders
                cells = row.find_all('td', {'class': 'text-center'})
                if len(cells) < 4:
                    continue
                
                size = cells[1].text.strip()
                try:
                    seeders = int(cells[3].text.strip())
                except (ValueError, TypeError):
                    seeders = 0
                
                size_gb = convert_size_to_gb(size)
                
                result = {
                    'title': title,
                    'size': size_gb,
                    'source': 'OldNyaa',
                    'magnet': magnet_link,
                    'seeders': seeders
                }
                results.append(result)
            except Exception as e:
                logging.error(f"Error parsing individual Nyaa result: {str(e)}")
                continue
                
        return results
    except Exception as e:
        logging.error(f"Error parsing Nyaa results: {str(e)}")
        return []

def convert_size_to_gb(size: str) -> float:
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

def test_nyaa_scraper(title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, **kwargs) -> List[Dict[str, Any]]:
    """
    Test entrypoint for the Nyaa scraper.
    
    Args:
        title (str): The title to search for.
        year (int): The year of the content.
        content_type (str): The type of content ('movie' or 'show').
        season (int, optional): The season number for TV shows.
        episode (int, optional): The episode number for TV shows.
        multi (bool, optional): Whether to search for multiple episodes.
        **kwargs: Additional settings for the Nyaa scraper.
    
    Returns:
        List[Dict[str, Any]]: A list of scraped results.
    """
    # Set up a mock instance and settings
    instance = "TestNyaa"
    settings = {
        "url": kwargs.get("url", "https://nyaa.si"),
        "categories": kwargs.get("categories", "1_0"),
        "filter": kwargs.get("filter", "0"),
        "sort": kwargs.get("sort", "seeders"),
        "order": kwargs.get("order", "desc"),
    }
    
    # Use a mock IMDB ID for testing
    imdb_id = "tt0000000"
    
    try:
        results = scrape_nyaa_instance(instance, settings, imdb_id, title, year, content_type, season, episode, multi)
        print(f"Scraped {len(results)} results from Nyaa:")
        for result in results:
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
        return results
    
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
    instance = "Nyaa"
    settings = {
        "url": "https://nyaa.si",
        "categories": "1_2",  # Use anime category
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }
    
    try:
        # Use a mock IMDB ID since it's not needed for the actual search
        mock_imdb_id = "tt0000000"
        results = scrape_nyaa_instance(instance, settings, mock_imdb_id, search_query, year, "episode", None, None, False)
        if results:
            return results  # Already in the correct format from scrape_nyaa_instance
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
    instance = "Nyaa"
    settings = {
        "url": "https://nyaa.si",
        "categories": "1_0",
        "filter": "0",
        "sort": "seeders",
        "order": "desc"
    }
    
    # Use a mock IMDB ID since it's not needed for the actual search
    mock_imdb_id = "tt0000000"
    
    try:
        results = scrape_nyaa_instance(instance, settings, mock_imdb_id, title, year, content_type, season, episode, False)
        if results:
            return [
                {
                    'title': item['title'],
                    'size': item['size'],
                    'seeders': item['seeders'],
                    'leechers': item.get('leechers', 0),
                    'downloads': item.get('downloads', 0),
                    'magnet': item['magnet'],
                    'hash': item.get('hash'),
                    'source': 'nyaa'
                }
                for item in results
            ]
    except Exception as e:
        logging.error(f"Error scraping Nyaa: {str(e)}")
        return []
    
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
    results = test_nyaa_scraper("Akira", 1988, "movie", url="https://nyaa.si", categories="1_2", filter="2", sort="size", order="desc")
    for result in results[:5]:  # Print first 5 results
        print(f"- {result['title']} ({result['size']:.2f} GB, {result['seeders']} seeders)")
    print(f"Total results: {len(results)}") 