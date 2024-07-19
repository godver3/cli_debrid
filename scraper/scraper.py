import asyncio
import re
from typing import List, Dict, Any, Tuple
from difflib import SequenceMatcher
import PTN
import aiohttp
import logging
from .zilean import scrape_zilean
from .torrentio import scrape_torrentio
from .knightcrawler import scrape_knightcrawler
from settings import get_setting
from logging_config import get_logger

# Set up logger for this module
logger = get_logger()

TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_API_KEY = get_setting('TMDB', 'api_key')

async def imdb_id_to_title(imdb_id: str) -> str:
    async with aiohttp.ClientSession() as session:
        search_url = f"{TMDB_API_URL}/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
        async with session.get(search_url) as response:
            if response.status == 200:
                data = await response.json()
                if 'movie_results' in data and data['movie_results']:
                    return data['movie_results'][0]['title']
                elif 'tv_results' in data and data['tv_results']:
                    return data['tv_results'][0]['name']
    return ""

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def detect_season_pack(title: str) -> str:
    # Regular expression to match season patterns
    season_patterns = [
        r'S(\d+)(?:-S(\d+))?',  # Matches S01 or S01-S03
        r'Season (\d+)(?:-(\d+))?',  # Matches Season 1 or Season 1-3
        r'Saison (\d+)(?:-(\d+))?',  # Matches French "Saison 1" or "Saison 1-3"
    ]

    # Check for single episode pattern first
    episode_pattern = r'S\d+E\d+'
    if re.search(episode_pattern, title, re.IGNORECASE):
        return 'N/A'

    for pattern in season_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            start_season = int(match.group(1))
            end_season = int(match.group(2)) if match.group(2) else start_season
            return ','.join(str(s) for s in range(start_season, end_season + 1))

    # Check for complete series or multiple seasons
    if re.search(r'complete series|all seasons', title, re.IGNORECASE):
        return 'Complete'

    # If no clear pattern is found, return 'Unknown'
    return 'Unknown'

def get_resolution_rank(quality: str) -> int:
    quality = quality.lower()
    parsed = PTN.parse(quality)
    resolution = parsed.get('resolution')
    if resolution:
        if '4k' in resolution.lower() or '2160p' in resolution.lower():
            return 3
        elif '1080p' in resolution.lower():
            return 2
        elif '720p' in resolution.lower():
            return 1
        elif '480p' in resolution.lower():
            return 0
    return -1  # For unknown resolutions, assign the lowest rank

def extract_season_episode(text: str) -> Tuple[int, int]:
    season_episode_pattern = r'S(\d+)(?:E(\d+))?'
    match = re.search(season_episode_pattern, text, re.IGNORECASE)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else None
        return season, episode
    return None, None

def extract_title_and_se(torrent_name: str) -> Tuple[str, int, int]:
    parsed = PTN.parse(torrent_name)
    title = parsed.get('title', torrent_name)
    season = parsed.get('season')
    episode = parsed.get('episode')
    return title, season, episode

def rank_result_key(result: Dict[str, Any], query: str, query_season: int, query_episode: int, multi: bool) -> Tuple:
    torrent_title = result.get('title', '')
    extracted_title, torrent_season, torrent_episode = extract_title_and_se(torrent_title)

    # Ensure season and episode are integers
    query_season = int(query_season) if query_season is not None else None
    query_episode = int(query_episode) if query_episode is not None else None

    # Check for season/episode pattern in the torrent title
    se_pattern = f"S{query_season:02d}E{query_episode:02d}" if query_season is not None and query_episode is not None else None
    se_match = 1 if se_pattern and se_pattern in torrent_title else 0

    title_similarity = similarity(extracted_title, query)
    season_match = 1 if query_season == torrent_season else 0
    episode_match = 1 if query_episode == torrent_episode else 0

    resolution_rank = max(get_resolution_rank(result.get('title', '')), 0)

    size = result.get('size', 0)
    hdr_preference = 1 if 'HDR' in torrent_title.upper() else 0

    season_pack = detect_season_pack(torrent_title)
    is_queried_season_pack = (season_pack != 'N/A' and season_pack != 'Unknown' and str(query_season) in season_pack.split(','))

    # Adjust scoring based on multi flag
    if multi:
        season_pack_bonus = 30 if is_queried_season_pack else 0
        exact_match_bonus = season_pack_bonus
    else:
        exact_match_bonus = 20 if (se_match and season_match and episode_match) else (10 if is_queried_season_pack else 0)

    single_episode_preference = 5 if not multi and query_episode is not None and season_pack == 'N/A' else 0

    return (-exact_match_bonus, -single_episode_preference, -resolution_rank, -hdr_preference, -se_match, -season_match, -episode_match, -title_similarity, -size, season_pack)

async def scrape(imdb_id: str, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    all_results = []

    title = await imdb_id_to_title(imdb_id)

    if not title:
        logger.warning(f"Could not find title for IMDb ID: {imdb_id}")
        return []

    scrapers = [
        (scrape_torrentio, 'torrentio'),
        (scrape_knightcrawler, 'knightcrawler')
    ]

    scraper_tasks = [scraper_func(imdb_id, content_type, season, episode) for scraper_func, _ in scrapers]
    scraper_results = await asyncio.gather(*scraper_tasks, return_exceptions=True)

    for (_, scraper_name), result in zip(scrapers, scraper_results):
        if isinstance(result, Exception):
            logger.error(f"Error in {scraper_name} scraper: {str(result)}")
            continue

        if isinstance(result, tuple):
            _, scraper_results = result
        else:
            scraper_results = result

        for item in scraper_results:
            item['scraper'] = scraper_name
            all_results.append(item)

    # Filter out results with low title similarity
    similarity_threshold = 0.7
    filtered_results = []
    for result in all_results:
        extracted_title = extract_title_and_se(result.get('title', ''))[0]
        title_similarity = similarity(extracted_title, title)
        if title_similarity > similarity_threshold:
            filtered_results.append(result)
        else:
            logger.debug(f"Discarding result due to low title similarity ({title_similarity}): {result.get('title', '')}")

    sorted_results = sorted(filtered_results, key=lambda x: rank_result_key(x, title, season, episode, multi))

    # Log detailed traits for each result
    for result in sorted_results:
        extracted_title, torrent_season, torrent_episode = extract_title_and_se(result.get('title', ''))
        se_pattern = f"S{season:02d}E{episode:02d}" if season is not None and episode is not None else None
        se_match = 1 if se_pattern and se_pattern in result.get('title', '') else 0
        title_similarity = similarity(extracted_title, title)
        season_match = 1 if season == torrent_season else 0
        episode_match = 1 if episode == torrent_episode else 0
        resolution_rank = max(get_resolution_rank(result.get('title', '')), 0)
        hdr_preference = 1 if 'HDR' in result.get('title', '').upper() else 0
        season_pack = detect_season_pack(result.get('title', ''))
        is_queried_season_pack = (season_pack != 'N/A' and season_pack != 'Unknown' and str(season) in season_pack.split(','))

        logger.debug(f"Result Title: {result.get('title', '')}")
        logger.debug(f" - Extracted Title: {extracted_title}")
        logger.debug(f" - Torrent Season: {torrent_season}, Torrent Episode: {torrent_episode}")
        logger.debug(f" - SE Pattern: {se_pattern}, SE Match: {se_match}")
        logger.debug(f" - Title Similarity: {title_similarity}")
        logger.debug(f" - Season Match: {season_match}, Episode Match: {episode_match}")
        logger.debug(f" - Resolution Rank: {resolution_rank}")
        logger.debug(f" - HDR Preference: {hdr_preference}")
        logger.debug(f" - Season Pack: {season_pack}")
        logger.debug(f" - Is Queried Season Pack: {is_queried_season_pack}")

    logger.debug(f"Number of results after sorting: {len(sorted_results)}")
    logger.debug(f"Top 5 results: {sorted_results[:5]}")

    return sorted_results[:5]
