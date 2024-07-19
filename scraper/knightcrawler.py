import aiohttp
import logging
import re
from types import SimpleNamespace
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus
from settings import get_setting
from logging_config import get_logger

logger = get_logger()
TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_API_KEY = get_setting('TMDB', 'api_key')
KNIGHTCRAWLER_URL = get_setting('Knightcrawler', 'url')

async def scrape_knightcrawler(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> Tuple[str, List[Dict[str, Any]]]:
    #print(f"Starting scrape_knightcrawler for IMDb ID: {imdb_id}, Content Type: {content_type}, Season: {season}, Episode: {episode}")
    try:
        url = construct_url(imdb_id, content_type, season, episode)
        #print(f"Constructed URL: {url}")
        
        response = await fetch_data(url)
        #print(f"Fetch data response: {response}")
        
        if not response:
            logger.warning(f"No response received for IMDb ID: {imdb_id}")
            return url, []
        
        if 'streams' not in response:
            logger.warning(f"No 'streams' key in response for IMDb ID: {imdb_id}")
            return url, []
        
        #print(f"Number of streams found: {len(response['streams'])}")
        
        parsed_results = parse_results(response['streams'])
        #print(f"Number of parsed results: {len(parsed_results)}")
        
        return url, parsed_results
    except Exception as e:
        logger.error(f"Error in scrape_knightcrawler: {str(e)}", exc_info=True)
        return "", []

def construct_url(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> str:
    opts = "sort=qualitysize|qualityfilter=480p,scr,cam"
    if content_type == "movie":
        return f"{KNIGHTCRAWLER_URL}/{opts}/stream/movie/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{KNIGHTCRAWLER_URL}/{opts}/stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{KNIGHTCRAWLER_URL}/{opts}/stream/series/{imdb_id}.json"
    else:
        #print("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

async def fetch_data(url: str) -> Dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.json()
    return {}

def parse_seeds(title: str) -> int:
    seeds_match = re.search(r'👤\s*(\d+)', title)
    return int(seeds_match.group(1)) if seeds_match else 0

def parse_source(name: str) -> str:
    return name.split('\n')[0].strip() if name else "unknown"

def parse_results(streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for stream in streams:
        try:
            title = stream.get('title', '')
            title_parts = title.split('\n')

            name = title_parts[1].strip() if len(title_parts) > 1 else ''
            size_info = title_parts[2].strip() if len(title_parts) > 2 else ''

            size = parse_size(size_info)
            quality = parse_quality(stream.get('name', ''))

            info_hash = stream.get("infoHash", "")
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            if stream.get('fileIdx') is not None:
                magnet_link += f'&dn={name}&so={stream["fileIdx"]}'

            results.append({
                'title': name,
                'size': size,
                'source': 'Knightcrawler',
                'magnet': magnet_link
            })
        except Exception as e:
            logger.error(f"Error parsing result: {str(e)}")
            continue
    return results

def parse_size(size_info: str) -> float:
    size_match = re.search(r'([\d.]+)\s*(\w+)', size_info)
    if size_match:
        size, unit = size_match.groups()
        size = float(size)
        if unit.lower() == 'gb':
            return size
        elif unit.lower() == 'mb':
            return size / 1024
    return 0

def parse_quality(name: str) -> str:
    quality_match = re.search(r'\n(.+)$', name)
    return quality_match.group(1).strip() if quality_match else "unknown"
