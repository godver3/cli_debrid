import aiohttp
import logging
from typing import List, Dict, Any
from database import get_title_by_imdb_id  # Import the function from database.py
from settings import get_setting

ZILEAN_URL = get_setting('Zilean', 'url')

async def scrape_zilean(imdb_id: str, content_type: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
    if not ZILEAN_URL:
        return []

    #logging.info(f"Fetching title for IMDb ID: {imdb_id}")
    title = get_title_by_imdb_id(imdb_id)
    if not title:
        return []

    #logging.info(f"Title: {title}, Season: {season}, Episode: {episode}")
    #logging.info(f"Season type: {type(season)}, Episode type: {type(episode)}")

    try:
        if season is not None and episode is not None:
            season = int(season)
            episode = int(episode)
            query = f"{title} S{season:02d}E{episode:02d}"
        elif season is not None:
            season = int(season)
            query = f"{title} S{season:02d}"
        else:
            query = title

        #logging.info(f"Constructed Query: {query}")

    except ValueError as ve:
        return []

    search_endpoint = f"{ZILEAN_URL}/dmm/search"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(search_endpoint, json={"queryText": query}) as response:
                if response.status == 200:
                    data = await response.json()
                    return parse_zilean_results(data)
                else:
                    error_data = await response.json()
                    #logging.error(f"Zilean API error: {error_data.get('detail', 'Unknown error')}")
                    return []
    except Exception as e:
        return []

def parse_zilean_results(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for item in data:
        result = {
            'title': item.get('filename', 'N/A'),
            'size': item.get('filesize', 0) / (1024 * 1024 * 1024),  # Convert to GB
            'source': 'Zilean',
            'magnet': f"magnet:?xt=urn:btih:{item.get('infoHash', '')}"
        }
        results.append(result)
    return results
