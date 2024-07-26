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
import time
from content_checkers.overseerr import get_overseerr_movie_details, get_overseerr_show_details, get_overseerr_cookies, imdb_to_tmdb

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def calculate_bitrate(size_gb, runtime_minutes):
    if not size_gb or not runtime_minutes:
        return 0
    size_bits = size_gb * 8 * 1024 * 1024 * 1024 * 100  # Convert GB to bits
    runtime_seconds = runtime_minutes * 60
    bitrate_mbps = (size_bits / runtime_seconds) / 1000000  # Convert to Mbps
    return round(bitrate_mbps, 2)

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
    parsed = result.get('parsed_info', {})
    extracted_title = parsed.get('title', torrent_title)
    torrent_year = parsed.get('year')
    torrent_season, torrent_episode = parsed.get('season'), parsed.get('episode')

    # Get user-defined weights
    resolution_bonus = int(get_setting('Scraping', 'resolution_bonus', default=3))
    hdr_bonus = int(get_setting('Scraping', 'hdr_bonus', default=3))
    similarity_threshold_bonus = int(get_setting('Scraping', 'similarity_threshold_bonus', default=3))
    file_size_bonus = int(get_setting('Scraping', 'file_size_bonus', default=3))
    bitrate_bonus = int(get_setting('Scraping', 'bitrate_bonus', default=3))

    # Calculate scores
    title_similarity = similarity(extracted_title, query)
    similarity_score = title_similarity * similarity_threshold_bonus

    resolution_score = get_resolution_rank(torrent_title) * resolution_bonus
    hdr_score = hdr_bonus if 'HDR' in torrent_title.upper() else 0
    
    size = result.get('size', 0)
    size_score = size * file_size_bonus

    tmdb_id = result.get('tmdb_id')
    runtime = result.get('runtime')
    
    if not tmdb_id or runtime is None:
        logging.warning(f"Missing TMDB ID or runtime for result: {result.get('title', 'Unknown Title')}")
        bitrate = 0
    else:
        bitrate = calculate_bitrate(size, runtime)
        
        logging.debug(f"Bitrate calculation for {result.get('title', 'Unknown Title')}:")
        logging.debug(f"  TMDB ID: {tmdb_id}")
        logging.debug(f"  Size: {size} GB")
        logging.debug(f"  Runtime: {runtime} minutes")
        logging.debug(f"  Calculated Bitrate: {bitrate} Mbps")
    bitrate_score = bitrate * bitrate_bonus

    result['bitrate'] = bitrate

    # Existing logic for year, season, and episode matching
    year_match = 2 if query_year == torrent_year else (1 if abs(query_year - (torrent_year or 0)) <= 1 else 0)
    season_match = 1 if query_season == torrent_season else 0
    episode_match = 1 if query_episode == torrent_episode else 0

    # Multi-pack handling
    season_pack = detect_season_pack(torrent_title)
    is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
    is_queried_season_pack = is_multi_pack and str(query_season) in season_pack.split(',')

    # Apply a flat large bonus for multi-packs when requested
    MULTI_PACK_BONUS = 1000  # This is a large flat bonus
    multi_pack_score = MULTI_PACK_BONUS if multi and is_queried_season_pack else 0

    # Penalize multi-packs when looking for single episodes
    SINGLE_EPISODE_PENALTY = -500  # This is a large flat penalty
    single_episode_score = SINGLE_EPISODE_PENALTY if not multi and is_multi_pack and query_episode is not None else 0

    filter_score = result.get('filter_score', 0)

    # Combine scores
    total_score = (
        similarity_score +
        resolution_score +
        (hdr_score * 2) +
        (size_score / 8) +
        (bitrate_score / 200) +
        (year_match * 5) +  # Giving more weight to year match
        (season_match * 5) +
        (episode_match * 5) +
        multi_pack_score +
        single_episode_score +
        filter_score  # This now includes the preferred filter bonus/penalty
    )

    # Create a score breakdown
    score_breakdown = {
        'similarity_score': similarity_score,
        'resolution_score': resolution_score,
        'hdr_score': hdr_score * 2,
        'size_score': size_score,
        'bitrate_score': bitrate_score,
        'year_match': year_match * 10,
        'season_match': season_match * 5,
        'episode_match': episode_match * 5,
        'multi_pack_score': multi_pack_score,
        'single_episode_score': single_episode_score,
        'filter_score': filter_score,
        'total_score': total_score
    }

    # Add the score breakdown to the result
    result['score_breakdown'] = score_breakdown

    # Return negative total_score to sort in descending order
    return (-total_score, -year_match, -season_match, -episode_match)

import re

def scrape(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    try:
        start_time = time.time()
        all_results = []

        logging.debug(f"Starting scraping for: {title} ({year})")

        # Get TMDB ID and runtime once for all results
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)

        tmdb_id = imdb_to_tmdb(overseerr_url, overseerr_api_key, f"{imdb_id}")

        if not tmdb_id:
            logging.warning(f"No TMDB ID found for IMDB ID: {imdb_id}")
            return []

        # Fetch runtime based on content type
        if content_type.lower() == 'movie':
            details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
            runtime = details.get('runtime') if details else None
        else:  # Assume TV show
            details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
            runtime = details.get('episodeRuntime', [None])[0] if details else None

        logging.debug(f"Retrieved runtime for {title}: {runtime} minutes")

        # Get filter terms and minimum size
        filter_in = get_setting('Scraping', 'filter_in', default='').split(',')
        filter_out = get_setting('Scraping', 'filter_out', default='').split(',')
        preferred_filter_in = get_setting('Scraping', 'preferred_filter_in', default='').split(',')
        preferred_filter_out = get_setting('Scraping', 'preferred_filter_out', default='').split(',')
        min_size_gb = float(get_setting('Scraping', 'min_size_gb', default=0.01))
        enable_4k = get_setting('Scraping', 'enable_4k', default='True')
        enable_hdr = get_setting('Scraping', 'enable_hdr', default='True')
        preferred_filter_bonus = 50
        filter_in = [term.strip().lower() for term in filter_in if term.strip()]
        filter_out = [term.strip().lower() for term in filter_out if term.strip()]

        preferred_filter_in = [term.strip().lower() for term in preferred_filter_in if term.strip()]
        preferred_filter_out = [term.strip().lower() for term in preferred_filter_out if term.strip()]

        # Compile regular expressions for filter out terms
        filter_out_regex = [re.compile(r'\b{}\b'.format(re.escape(term)), re.IGNORECASE) for term in filter_out]
        filter_in_regex = [re.compile(r'\b{}\b'.format(re.escape(term)), re.IGNORECASE) for term in filter_in]

        # Run scrapers concurrently using ThreadPoolExecutor
        def run_scraper(scraper_func, scraper_name):
            scraper_start = time.time()
            try:
                logging.debug(f"Starting {scraper_name} scraper")
                scraper_results = scraper_func(imdb_id, content_type, season, episode)
                if isinstance(scraper_results, tuple):
                    _, scraper_results = scraper_results
                for item in scraper_results:
                    item['scraper'] = scraper_name
                logging.debug(f"{scraper_name} scraper found {len(scraper_results)} results")
                logging.debug(f"{scraper_name} scraper took {time.time() - scraper_start:.2f} seconds")
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

        scraping_start = time.time()
        with ThreadPoolExecutor(max_workers=len(scrapers)) as executor:
            future_to_scraper = {executor.submit(run_scraper, scraper_func, scraper_name): scraper_name for scraper_func, scraper_name in scrapers}
            for future in as_completed(future_to_scraper):
                scraper_name = future_to_scraper[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    logging.error(f"Scraper {scraper_name} generated an exception: {str(e)}")

        logging.debug(f"Total scraping time: {time.time() - scraping_start:.2f} seconds")
        logging.debug(f"Total results before filtering: {len(all_results)}")

        # Filter results
        filtering_start = time.time()
        filtered_results = []
        for result in all_results:
            result['tmdb_id'] = tmdb_id
            result['runtime'] = runtime

            torrent_title = result.get('title', '').lower()
            size_gb = parse_size(result.get('size', 0))
            parsed_info = PTN.parse(torrent_title)
            resolution = parsed_info.get('resolution', '').lower()
            is_hdr = parsed_info.get('hdr', False)
            result_year = parsed_info.get('year')

            # Apply hard filters
            if any(regex.search(torrent_title) for regex in filter_in_regex):
                logging.debug(f"Filtered out by filter_in: {torrent_title}")
                continue
            if any(regex.search(torrent_title) for regex in filter_out_regex):
                logging.debug(f"Filtered out by filter_out: {torrent_title}")
                continue
            if size_gb < min_size_gb:
                logging.debug(f"Filtered out by size: {torrent_title}, size: {size_gb} GB")
                continue
            if not enable_4k and ('4k' in resolution or '2160p' in resolution):
                logging.debug(f"Filtered out by 4K: {torrent_title}")
                continue
            if not enable_hdr and is_hdr:
                logging.debug(f"Filtered out by HDR: {torrent_title}")
                continue
            if year and result_year and year != result_year:
                logging.debug(f"Filtered out by year: {torrent_title}, year: {result_year}")
                continue

            # Apply preferred filters (these don't exclude results, but affect ranking)
            preferred_in_bonus = sum(preferred_filter_bonus for term in preferred_filter_in if term in torrent_title)
            preferred_out_penalty = sum(preferred_filter_bonus for term in preferred_filter_out if term in torrent_title)

            result['filter_score'] = preferred_in_bonus - preferred_out_penalty
            result['parsed_info'] = parsed_info
            filtered_results.append(result)

        logging.debug(f"Filtering took {time.time() - filtering_start:.2f} seconds")
        logging.debug(f"Total results after filtering: {len(filtered_results)}")

        # Add is_multi_pack information to each result
        for result in filtered_results:
            torrent_title = result.get('title', '')
            season_pack = detect_season_pack(torrent_title)
            is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
            result['is_multi_pack'] = is_multi_pack

        # Sort results
        sorting_start = time.time()
        sorted_results = sorted(filtered_results, key=lambda x: rank_result_key(x, title, year, season, episode, multi))
        logging.debug(f"Sorting took {time.time() - sorting_start:.2f} seconds")

        logging.debug(f"Total scraping process took {time.time() - start_time:.2f} seconds")
        return sorted_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return []
