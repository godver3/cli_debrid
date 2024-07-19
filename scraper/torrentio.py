import aiohttp
import logging
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import quote_plus

TORRENTIO_URL = "https://torrentio.strem.fun"

async def scrape_torrentio(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        url = construct_url(imdb_id, content_type, season, episode)
        #logging.info(f"Fetching Torrentio data from URL: {url}")
        response = await fetch_data(url)
        if not response or 'streams' not in response:
            #logging.warning(f"No streams found for IMDb ID: {imdb_id}")
            return url, []
        parsed_results = parse_results(response['streams'])
        return url, parsed_results
    except Exception as e:
        #logging.error(f"Error in scrape_torrentio: {str(e)}")
        return "", []

def construct_url(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> str:
    opts = "sort=qualitysize|qualityfilter=480p,scr,cam"
    if content_type == "movie":
        return f"{TORRENTIO_URL}/{opts}/stream/movie/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{TORRENTIO_URL}/{opts}/stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{TORRENTIO_URL}/{opts}/stream/series/{imdb_id}.json"
    else:
        #logging.error("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

async def fetch_data(url: str) -> Dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.json()
    return {}

def parse_results(streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for stream in streams:
        try:
            title = stream.get('title', '')
            title_parts = title.split('\n')

            if len(title_parts) >= 3:  # TV Show format
                name = title_parts[0].strip()
                size_info = title_parts[2].strip()
            elif len(title_parts) == 2:  # Movie format
                name = title_parts[0].strip()
                size_info = title_parts[1].strip()
            else:
                continue  # Skip if the format is unexpected

            size = parse_size(size_info)

            info_hash = stream.get("infoHash", "")
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            if stream.get('fileIdx') is not None:
                magnet_link += f'&dn={quote_plus(name)}&so={stream["fileIdx"]}'

            results.append({
                'title': name,
                'size': size,
                'source': 'Torrentio',
                'magnet': magnet_link
            })
        except Exception as e:
            #logging.error(f"Error parsing result: {str(e)}")
            continue
    return results

def parse_size(size_info: str) -> float:
    size_match = re.search(r'💾\s*([\d.]+)\s*(\w+)', size_info)
    if size_match:
        size, unit = size_match.groups()
        size = float(size)
        if unit.lower() == 'gb':
            return size
        elif unit.lower() == 'mb':
            return size / 1024
    return 0
