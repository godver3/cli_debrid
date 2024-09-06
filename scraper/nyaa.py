import sys
import os
import time
import random

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from typing import List, Dict, Any
from api_tracker import api
from urllib.parse import quote, urlencode
from bs4 import BeautifulSoup
import re

def scrape_nyaa_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    nyaa_instances = [
        "https://nyaa.land/",
        "https://nyaa.si/",
        "https://nyaa.unblockninja.com/",
        "https://nyaa.iss.ink/",
    ]
    
    #categories = settings.get('categories', '1_0')
    #filter_option = settings.get('filter', '0')
    sort = settings.get('sort', 'seeders')
    order = settings.get('order', 'desc')
    
    params = f'&s={sort}&o={order}'
    
    if content_type.lower() == 'movie':
        query = f"{title} {year}"
    else:
        query = f"{title}"
        if season is not None:
            query += f" S{season:02d}"
            if episode is not None and not multi:
                query += f"E{episode:02d}"
        else:
            query += f" {year}"  # Add year for general series search
    
    encoded_query = quote(query).replace('.', '+').replace('%20', '+')    
    
    for base_url in nyaa_instances:
        full_url = f"{base_url}?f=0{params}&q={encoded_query}"
        logging.info(f"Scraping {full_url}")
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': base_url,
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            response = api.get(full_url, headers=headers, timeout=10)
            if response.status_code == 200:
                results = parse_nyaa_results(response.content)
                if results:
                    logging.info(f"Successfully scraped {len(results)} results from {base_url}")
                    return results
            else:
                logging.warning(f"Nyaa API error for {base_url}: Status code {response.status_code}")
        except api.exceptions.RequestException as e:
            logging.error(f"Error scraping Nyaa instance {base_url}: {str(e)}")
        
        # Add a small delay between requests to avoid overwhelming the servers
        time.sleep(random.uniform(1, 3))
    
    logging.warning("Failed to scrape results from all Nyaa instances")
    return []

def parse_nyaa_results(content: bytes) -> List[Dict[str, Any]]:
    results = []
    soup = BeautifulSoup(content, 'html.parser')
    
    for row in soup.select("tr.danger,tr.default,tr.success"):
        try:
            magnet_link = row.find('a', {'href': re.compile(r'(magnet:)+[^"]*')})['href']
            title = row.find_all('a', {'class': None})[1]['title']
            size = row.find_all('td', {'class': 'text-center'})[1].text.strip()
            seeders = int(row.find_all('td', {'class': 'text-center'})[3].text)
            
            size_gb = convert_size_to_gb(size)
            
            result = {
                'title': title,
                'size': size_gb,
                'source': 'Nyaa',
                'magnet': magnet_link,
                'seeders': seeders
            }
            results.append(result)
        except Exception as e:
            logging.error(f"Error parsing Nyaa result: {str(e)}", exc_info=True)
    
    return results

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