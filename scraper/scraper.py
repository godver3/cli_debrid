import PTN
import requests
import logging
import re
from typing import List, Dict, Any, Tuple
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from .zilean import scrape_zilean
from .torrentio import scrape_torrentio
from .knightcrawler import scrape_knightcrawler
from .comet import scrape_comet
from settings import get_setting
from database import get_item_state

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

def rank_result_key(result: Dict[str, Any], query: str, query_year: int, query_season: int, query_episode: int, multi: bool) -> Tuple:
    torrent_title = result.get('title', '')
    parsed = PTN.parse(torrent_title)
    extracted_title = parsed.get('title', torrent_title)
    torrent_year = parsed.get('year')
    torrent_season, torrent_episode = parsed.get('season'), parsed.get('episode')

    # Ensure season and episode are integers
    query_season = int(query_season) if query_season is not None else None
    query_episode = int(query_episode) if query_episode is not None else None

    # Check for season/episode pattern in the torrent title
    se_pattern = f"S{query_season:02d}E{query_episode:02d}" if query_season is not None and query_episode is not None else None
    se_match = 1 if se_pattern and se_pattern in torrent_title else 0

    title_similarity = similarity(extracted_title, query)
    season_match = 1 if query_season == torrent_season else 0
    episode_match = 1 if query_episode == torrent_episode else 0
    year_match = 2 if query_year == torrent_year else (1 if abs(query_year - (torrent_year or 0)) <= 1 else 0)

    resolution_rank = max(get_resolution_rank(torrent_title), 0)

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

    return (-year_match, -exact_match_bonus, -single_episode_preference, -resolution_rank, -hdr_preference, -se_match, -season_match, -episode_match, -title_similarity, -size, season_pack)

def scrape(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    try:
        all_results = []

        logging.debug(f"Scraping for: {title} ({year})")

        # Get filter out terms
        filter_out_terms = get_setting('Logging', 'filter_out', default='').split(',')
        filter_out_terms = [term.strip().lower() for term in filter_out_terms if term.strip()]

        # Run scrapers concurrently using ThreadPoolExecutor
        def run_scraper(scraper_func, scraper_name):
            try:
                scraper_results = scraper_func(imdb_id, content_type, season, episode)
                if isinstance(scraper_results, tuple):
                    _, scraper_results = scraper_results
                for item in scraper_results:
                    item['scraper'] = scraper_name
                return scraper_results
            except Exception as e:
                logging.error(f"Error in {scraper_name} scraper: {str(e)}")
                return []

        # Define scrapers and check if they are enabled
        all_scrapers = [
            (scrape_zilean, 'Zilean'),
            (scrape_knightcrawler, 'Knightcrawler'),
            (scrape_torrentio, 'Torrentio'),
            (scrape_comet, 'Comet')
        ]

        # Filter scrapers based on their enabled status in settings
        scrapers = [
            (scraper_func, scraper_name.lower())
            for scraper_func, scraper_name in all_scrapers
            if get_setting(scraper_name, 'enabled', default=False)
        ]

        with ThreadPoolExecutor(max_workers=len(scrapers)) as executor:
            future_to_scraper = {executor.submit(run_scraper, scraper_func, scraper_name): scraper_name for scraper_func, scraper_name in scrapers}
            for future in as_completed(future_to_scraper):
                scraper_name = future_to_scraper[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    logging.error(f"Scraper {scraper_name} generated an exception: {str(e)}")

        # Filter out results based on user settings, filter_out terms, minimum size, and year
        disable_4k = get_setting('Scraper', '4k', default='False')
        disable_hdr = get_setting('Scraper', 'hdr', default='False')
        min_size_gb = 0.01  # Minimum size in GB
        filtered_results = []

        def parse_size(size):
            if isinstance(size, (int, float)):
                return float(size)  # Assume it's already in GB
            elif isinstance(size, str):
                size = size.upper()
                if 'GB' in size:
                    return float(size.replace('GB', '').strip())
                elif 'MB' in size:
                    return float(size.replace('MB', '').strip()) / 1024
                elif 'KB' in size:
                    return float(size.replace('KB', '').strip()) / (1024 * 1024)
            return 0  # Default to 0 if unable to parse

        def extract_year(title):
            match = re.search(r'\b(19\d{2}|20\d{2})\b', title)
            return int(match.group(1)) if match else None

        for result in all_results:
            torrent_title = result.get('title', '').lower()
            size_gb = parse_size(result.get('size', 0))
            result_year = extract_year(torrent_title)
            
            if disable_4k and ('4k' in torrent_title or '2160p' in torrent_title):
                continue
            if disable_hdr and 'hdr' in torrent_title:
                continue
            if any(term in torrent_title for term in filter_out_terms):
                continue
            if size_gb < min_size_gb:
                continue
            if result_year is not None and result_year != year:
                continue
            filtered_results.append(result)

        # Filter out results with low title similarity
        similarity_threshold = 0.5
        final_results = []
        for result in filtered_results:
            torrent_title = result.get('title', '')
            parsed_title, _, _ = extract_title_and_se(torrent_title)
            title_similarity = similarity(parsed_title, title)
            if title_similarity > similarity_threshold:
                final_results.append(result)

        sorted_results = sorted(final_results, key=lambda x: rank_result_key(x, title, year, season, episode, multi))

        # Assign multi-pack status to each result
        for result in sorted_results:
            season_pack = detect_season_pack(result.get('title', ''))
            result['is_multi_pack'] = season_pack != 'N/A' and season_pack != 'Unknown'

        return sorted_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return []
