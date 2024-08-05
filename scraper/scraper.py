import PTN
import logging
import re
import requests
from typing import List, Dict, Any, Tuple, Optional
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from .zilean import scrape_zilean
from .torrentio import scrape_torrentio
from .knightcrawler import scrape_knightcrawler
from .comet import scrape_comet
from settings import get_setting
import time
from metadata.metadata import get_overseerr_movie_details, get_overseerr_cookies, imdb_to_tmdb, get_overseerr_show_details, get_overseerr_show_episodes, get_episode_count_for_seasons, get_all_season_episode_counts

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def calculate_bitrate(size_gb, runtime_minutes):
    if not size_gb or not runtime_minutes:
        return 0
    size_bits = size_gb * 8 * 1024 * 1024 * 1024 * 100  # Convert GB to bits
    runtime_seconds = runtime_minutes * 60
    bitrate_mbps = (size_bits / runtime_seconds) / 1000000  # Convert to Mbps
    return round(bitrate_mbps, 2)

def get_tmdb_season_info(tmdb_id: int, season_number: int, api_key: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Error fetching TMDB season info: {e}")
        return None

def get_media_info_for_bitrate(media_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Retrieve episode count and runtime information for given media items.
    
    Args:
    media_items (List[Dict[str, Any]]): List of media items to process.
    
    Returns:
    List[Dict[str, Any]]: List of media items with additional 'episode_count' and 'runtime' fields.
    """
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    cookies = get_overseerr_cookies(overseerr_url)
    processed_items = []

    for item in media_items:
        try:
            if item['media_type'] == 'movie':
                details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                if details:
                    item['episode_count'] = 1
                    item['runtime'] = details.get('runtime', 100)  # Default to 100 minutes if not available
                else:
                    logging.warning(f"Could not fetch details for movie: {item['title']}")
                    item['episode_count'] = 1
                    item['runtime'] = 100  # Default value
            
            elif item['media_type'] == 'episode':
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                if show_details:
                    seasons = show_details.get('seasons', [])
                    item['episode_count'] = sum(season.get('episodeCount', 0) for season in seasons if season.get('seasonNumber', 0) != 0)
                    
                    # Try to get runtime from TMDB API first
                    tmdb_api_key = get_setting('TMDB', 'api_key')
                    if tmdb_api_key:
                        first_season = next((s for s in seasons if s.get('seasonNumber', 0) != 0), None)
                        if first_season:
                            season_info = get_tmdb_season_info(item['tmdb_id'], first_season['seasonNumber'], tmdb_api_key)
                            if season_info and season_info.get('episodes'):
                                item['runtime'] = season_info['episodes'][0].get('runtime', 30)
                            else:
                                item['runtime'] = 30
                        else:
                            item['runtime'] = 30
                    else:
                        # Fallback to Overseerr data if TMDB API key is not available
                        if seasons:
                            first_season = next((s for s in seasons if s.get('seasonNumber', 0) != 0), None)
                            if first_season:
                                season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], first_season['seasonNumber'], cookies)
                                first_episode = season_details.get('episodes', [{}])[0]
                                item['runtime'] = first_episode.get('runtime', 30)
                            else:
                                item['runtime'] = 30
                        else:
                            item['runtime'] = 30
                else:
                    logging.warning(f"Could not fetch details for TV show: {item['title']}")
                    item['episode_count'] = 1
                    item['runtime'] = 30  # Default value
            
            logging.debug(f"Processed {item['title']}: {item['episode_count']} episodes, {item['runtime']} minutes per episode/movie")
            processed_items.append(item)

        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}")
            # Add item with default values in case of error
            item['episode_count'] = 1
            item['runtime'] = 30 if item['media_type'] == 'episode' else 100
            processed_items.append(item)

    return processed_items

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

def preprocess_title(title):
    # Remove common resolution and quality terms
    terms_to_remove = ['1080p', '720p', '2160p', '4k', 'uhd', 'hdr', 'web-dl', 'webrip', 'bluray', 'dvdrip']
    for term in terms_to_remove:
        title = re.sub(r'\b' + re.escape(term) + r'\b', '', title, flags=re.IGNORECASE)
    return title.strip()

def detect_season_pack(title: str) -> str:
    # Regular expression to match season patterns
    season_patterns = [
        r'\bS(\d{1,2})(?:\s*-?\s*S?(\d{1,2}))?\b',  # Matches S01, S01-S03, S01 03, S01-03
        r'\bSeason\s+(\d{1,2})(?:\s*-?\s*(\d{1,2}))?\b',  # Matches Season 1, Season 1-3, Season 1 3
        r'\bSaison\s+(\d{1,2})(?:\s*-?\s*(\d{1,2}))?\b',  # Matches French "Saison 1", "Saison 1-3", "Saison 1 3"
        r'\bSeason(?:\s+\d{1,2}){2,}\b',  # Matches "Season 1 2 3 4 5" (at least two numbers)
        r'\b(?:Seasons|Season)\s+(\d{1,2})\s*-?\s*(\d{1,2})\b',  # Matches "Seasons 1-6", "Season 1-6"
    ]
    
    # Check for single episode pattern first
    episode_pattern = r'\bS(\d{1,2})E(\d{1,2})(?:-E?(\d{1,2}))?\b'
    episode_match = re.search(episode_pattern, title, re.IGNORECASE)
    if episode_match:
        season = int(episode_match.group(1))
        start_ep = int(episode_match.group(2))
        end_ep = int(episode_match.group(3)) if episode_match.group(3) else start_ep
        if end_ep - start_ep > 5:  # Assume it's a season pack if more than 5 episodes
            return str(season)
        return 'N/A'
    
    # Check for the specific case mentioned
    if re.search(r'\b(?:Seasons|Season)\s+1-6\b', title, re.IGNORECASE):
        return '1,2,3,4,5,6'
    
    for pattern in season_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            if 'Season' in pattern and '{2,}' in pattern:  # This is our pattern for "Season 1 2 3 4 5" format
                seasons = [int(s) for s in re.findall(r'\d+', match.group(0))]
                if len(seasons) > 1:  # Ensure we have at least two season numbers
                    return ','.join(str(s) for s in range(min(seasons), max(seasons) + 1))
            else:
                start_season = int(match.group(1))
                end_season = int(match.group(2)) if match.group(2) else start_season
                if end_season < start_season:
                    end_season, start_season = start_season, end_season
                # Sanity check: limit the maximum number of seasons
                if end_season > 50 or start_season > 50:
                    return 'Unknown'
                if start_season != end_season:
                    return ','.join(str(s) for s in range(start_season, end_season + 1))
                else:
                    return str(start_season)
    
    # Check for complete series or multiple seasons
    if re.search(r'\b(complete series|all seasons)\b', title, re.IGNORECASE):
        return 'Complete'
    
    # Check for specific ranges like "1-6" in the title
    range_match = re.search(r'\b(\d{1,2})\s*-\s*(\d{1,2})\b', title)
    if range_match:
        start, end = map(int, range_match.groups())
        if 1 <= start < end <= 50:  # Sanity check
            return ','.join(str(s) for s in range(start, end + 1))
    
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
            return -50
    return -100  # For unknown resolutions, assign the lowest rank

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

def rank_result_key(result: Dict[str, Any], all_results: List[Dict[str, Any]], query: str, query_year: int, query_season: int, query_episode: int, multi: bool, content_type: str) -> Tuple:
    torrent_title = result.get('title', '')
    parsed = result.get('parsed_info', {})
    extracted_title = parsed.get('title', torrent_title)
    torrent_year = parsed.get('year')
    torrent_season, torrent_episode = parsed.get('season'), parsed.get('episode')

    # Add debug logging for content type
    #logging.debug(f"Processing result: {torrent_title}")
    #logging.debug(f"Content type: {content_type}")

    # Get user-defined weights
    resolution_weight = int(get_setting('Scraping', 'resolution_weight', default=3))
    hdr_weight = int(get_setting('Scraping', 'hdr_weight', default=3))
    similarity_weight = int(get_setting('Scraping', 'similarity_weight', default=3))
    size_weight = int(get_setting('Scraping', 'size_weight', default=3))
    bitrate_weight = int(get_setting('Scraping', 'bitrate_weight', default=3))

    # Calculate base scores
    title_similarity = similarity(extracted_title, query)
    resolution_score = get_resolution_rank(torrent_title)
    hdr_score = 1 if 'HDR' in torrent_title.upper() else 0
    size = parse_size(result.get('size', 0))
    runtime = result.get('runtime', 0)
    bitrate = result.get('bitrate', 0)  # Use pre-calculated bitrate

    # Calculate percentile ranks for size and bitrate
    all_sizes = [parse_size(r.get('size', 0)) for r in all_results]
    all_bitrates = [r.get('bitrate', 0) for r in all_results]  # Use pre-calculated bitrates

    def percentile_rank(value, all_values):
        return sum(1 for v in all_values if v <= value) / len(all_values) if all_values else 0

    size_percentile = percentile_rank(size, all_sizes)
    bitrate_percentile = percentile_rank(bitrate, all_bitrates)

    # Normalize scores to a 0-10 range
    normalized_similarity = title_similarity * 10
    normalized_resolution = min(resolution_score * 2.5, 10)  # Assuming max resolution score is 4
    normalized_hdr = hdr_score * 10
    normalized_size = size_percentile * 10
    normalized_bitrate = bitrate_percentile * 10

    # Apply weights
    weighted_similarity = normalized_similarity * similarity_weight
    weighted_resolution = normalized_resolution * resolution_weight
    weighted_hdr = normalized_hdr * hdr_weight
    weighted_size = normalized_size * size_weight
    weighted_bitrate = normalized_bitrate * bitrate_weight

    # Existing logic for year, season, and episode matching
    year_match = 5 if query_year == torrent_year else (1 if abs(query_year - (torrent_year or 0)) <= 1 else 0)
    season_match = 5 if query_season == torrent_season else 0
    episode_match = 5 if query_episode == torrent_episode else 0

    # Multi-pack handling
    season_pack = detect_season_pack(torrent_title)
    is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
    is_queried_season_pack = is_multi_pack and str(query_season) in season_pack.split(',')

    # Calculate the number of seasons in the pack
    if is_multi_pack and season_pack != 'Complete':
        num_seasons = len(season_pack.split(','))
    elif season_pack == 'Complete':
        num_seasons = 100  # Assign a high value for complete series
    else:
        num_seasons = 0

    # Apply a bonus for multi-packs when requested, scaled by the number of seasons
    MULTI_PACK_BONUS = 10  # Base bonus
    multi_pack_score = 50+(MULTI_PACK_BONUS * num_seasons) if multi and is_queried_season_pack else 0
    
    # Penalize multi-packs when looking for single episodes
    SINGLE_EPISODE_PENALTY = -25
    single_episode_score = SINGLE_EPISODE_PENALTY if not multi and is_multi_pack and query_episode is not None else 0

    filter_score = result.get('filter_score', 0)

    # Combine scores
    total_score = (
        weighted_similarity +
        weighted_resolution +
        weighted_hdr +
        weighted_size +
        weighted_bitrate +
        (year_match * 5) +
        (season_match * 5) +
        (episode_match * 5) +
        multi_pack_score +
        single_episode_score +
        filter_score
    )

    # Content type matching score
    content_type_score = 0
    if content_type.lower() == 'movie':
        #logging.debug(f"Applying movie logic for: {torrent_title}")
        if re.search(r'(s\d{2}|e\d{2})', torrent_title, re.IGNORECASE):
            content_type_score = -500
            #logging.debug(f"Penalized TV pattern in movie: {torrent_title}, score: {content_type_score}")
    elif content_type.lower() == 'episode':
        #logging.debug(f"Applying TV show logic for: {torrent_title}")
        if not re.search(r'(s\d{2}|e\d{2})', torrent_title, re.IGNORECASE):
            content_type_score = -500
            #logging.debug(f"Penalized non-TV pattern: {torrent_title}, score: {content_type_score}")
        if re.search(r'\b(19|20)\d{2}\b', torrent_title) and not re.search(r'(season|episode|s\d{2}|e\d{2})', torrent_title, re.IGNORECASE):
            content_type_score -= 250
            #logging.debug(f"Additional movie-like penalty: {torrent_title}, total score: {content_type_score}")
    else:
        logging.warning(f"Unknown content type: {content_type} for result: {torrent_title}")

    # Add content_type_score to the total score
    total_score += content_type_score

    # Create a score breakdown
    score_breakdown = {
        'similarity_score': weighted_similarity,
        'resolution_score': weighted_resolution,
        'hdr_score': weighted_hdr,
        'size_score': weighted_size,
        'bitrate_score': weighted_bitrate,
        'year_match': year_match * 5,
        'season_match': season_match * 5,
        'episode_match': episode_match * 5,
        'multi_pack_score': multi_pack_score,
        'single_episode_score': single_episode_score,
        'filter_score': filter_score,
        'total_score': total_score
    }

    # Add content_type_score to the score breakdown
    score_breakdown['content_type_score'] = content_type_score

    # Add the score breakdown to the result
    result['score_breakdown'] = score_breakdown

    # Return negative total_score to sort in descending order
    return (-total_score, -year_match, -season_match, -episode_match)
    
def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    try:
        start_time = time.time()
        all_results = []

        logging.debug(f"Starting scraping for: {title} ({year})")
        #logging.debug(f"Input content_type: {content_type}")
        
        # Ensure content_type is correctly set
        if content_type.lower() not in ['movie', 'episode']:
            logging.warning(f"Invalid content_type: {content_type}. Defaulting to 'movie'.")
            content_type = 'movie'

        
        
        # Get TMDB ID and runtime once for all results
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)

        # Get media info for bitrate calculation
        media_item = {
            'title': title,
            'media_type': 'movie' if content_type.lower() == 'movie' else 'episode',
            'tmdb_id': tmdb_id
        }
        enhanced_media_items = get_media_info_for_bitrate([media_item])
        if enhanced_media_items:
            episode_count = enhanced_media_items[0]['episode_count']
            runtime = enhanced_media_items[0]['runtime']
        else:
            episode_count = 1
            runtime = 100 if content_type.lower() == 'movie' else 30

        # Pre-calculate episode counts for TV shows
        season_episode_counts = {}
        if content_type.lower() == 'episode':
            season_episode_counts = get_all_season_episode_counts(overseerr_url, overseerr_api_key, tmdb_id, cookies)


        logging.debug(f"Retrieved runtime for {title}: {runtime} minutes, Episode count: {episode_count}")

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

        # Get Overseerr details
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)

        # Filter results
        filtering_start = time.time()
        filtered_results = []
        for result in all_results:
            result['tmdb_id'] = tmdb_id
            result['runtime'] = runtime
            result['episode_count'] = episode_count

            torrent_title = result.get('title', '').lower()
            size_gb = parse_size(result.get('size', 0))
            parsed_info = PTN.parse(torrent_title)
            resolution = parsed_info.get('resolution', '').lower()
            is_hdr = parsed_info.get('hdr', False)
            result_year = parsed_info.get('year')

            # Title similarity filter
            parsed_title = parsed_info.get('title', '')
            title_similarity = similarity(parsed_title, title)

            # Movie-specific filtering
            if content_type.lower() == 'movie':
                if title_similarity < 0.8 or (result_year and result_year != year):
                    logging.debug(f"Filtered out movie: {torrent_title}, similarity: {title_similarity:.2f}, year: {result_year}")
                    continue

            # TV show filtering
            elif content_type.lower() == 'episode' and season is not None:
                season_pack = detect_season_pack(torrent_title)

                if season_pack == 'N/A':  # Single episode
                    episode_season = re.search(r'S(\d{2})E\d{2}', torrent_title, re.IGNORECASE)
                    if episode_season and int(episode_season.group(1)) != season:
                        logging.debug(f"Filtered out wrong season: {torrent_title}, season: {episode_season.group(1)}, expected: {season}")
                        continue

                elif season_pack != 'Unknown' and season_pack != 'Complete':
                    seasons = [int(s) for s in season_pack.split(',')]
                    if season not in seasons:
                        logging.debug(f"Filtered out wrong season pack: {torrent_title}, seasons: {season_pack}, expected: {season}")
                        continue

                elif season_pack == 'Unknown':
                    logging.debug(f"Filtered out unknown season pack: {torrent_title}")
                    continue

                if multi:
                    # Multi-episode search
                    if season_pack == 'N/A' or (season_pack != 'Complete' and str(season) not in season_pack.split(',')):
                        logging.debug(f"Filtered out for multi-episode search: {torrent_title}, season pack: {season_pack}")
                        continue
                else:
                    # Single episode search
                    if season_pack != 'N/A':
                        logging.debug(f"Filtered out season pack for single episode search: {torrent_title}")
                        continue
                    
                    expected_pattern = f'S{season:02d}E{episode:02d}'
                    if expected_pattern not in torrent_title.upper():
                        logging.debug(f"Filtered out non-matching episode: {torrent_title}, expected: {expected_pattern}")
                        continue

            # Apply common filters
            if not any(regex.search(torrent_title) for regex in filter_in_regex) and filter_in_regex:
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

            # Apply preferred filters (these don't exclude results, but affect ranking)
            preferred_in_bonus = sum(preferred_filter_bonus for term in preferred_filter_in if term in torrent_title)
            preferred_out_penalty = sum(preferred_filter_bonus for term in preferred_filter_out if term in torrent_title)

            result['filter_score'] = preferred_in_bonus - preferred_out_penalty
            result['parsed_info'] = parsed_info
            filtered_results.append(result)
            
            original_size_gb = parse_size(result.get('size', 0))
            
            if content_type.lower() == 'episode':
                season_pack = detect_season_pack(result.get('title', ''))

                if season_pack == 'N/A':  # Single episode
                    normalized_episode_count = 1
                    size_gb = original_size_gb
                elif season_pack == 'Complete':  # Complete series
                    normalized_episode_count = sum(season_episode_counts.values())
                    size_gb = original_size_gb / normalized_episode_count
                elif season_pack != 'Unknown':  # Specific season or range of seasons
                    seasons = [int(s) for s in season_pack.split(',')]
                    normalized_episode_count = sum(season_episode_counts.get(s, 0) for s in seasons)
                    size_gb = original_size_gb / normalized_episode_count
                else:  # Unknown - default to single season
                    normalized_episode_count = season_episode_counts.get(season, 1)
                    size_gb = original_size_gb / normalized_episode_count
            else:
                # Movie logic
                size_gb = original_size_gb
                normalized_episode_count = 1
                season_pack = 'N/A'  # Set to N/A for movies

            bitrate = calculate_bitrate(size_gb, runtime)
            result['bitrate'] = bitrate

            # Add the one-liner logging here
            if content_type.lower() == 'episode':
                logging.debug(f"Result: {result.get('title', 'Unknown')} | "
                             f"Type: TV Show | "
                             f"Season Pack: {season_pack} | "
                             f"Episodes: {normalized_episode_count} | "
                             f"Total Size: {original_size_gb:.2f} GB | "
                             f"Size per Episode: {size_gb:.2f} GB | "
                             f"Estimated Bitrate: {bitrate:.2f} Mbps")
            else:
                logging.debug(f"Result: {result.get('title', 'Unknown')} | "
                             f"Type: Movie | "
                             f"Total Size: {original_size_gb:.2f} GB | "
                             f"Estimated Bitrate: {bitrate:.2f} Mbps")

            result['parsed_info'] = parsed_info
            filtered_results.append(result)

        logging.debug(f"Filtering took {time.time() - filtering_start:.2f} seconds")
        logging.debug(f"Total results after filtering: {len(filtered_results)}")

        # Add is_multi_pack information to each result
        for result in filtered_results:
            torrent_title = result.get('title', '')
            preprocessed_title = preprocess_title(torrent_title)
            season_pack = detect_season_pack(preprocessed_title)
            is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
            result['is_multi_pack'] = is_multi_pack
            result['season_pack'] = season_pack

            # Debug logging for multi-episode results
            if is_multi_pack:
                if season_pack == 'Complete':
                    logging.debug(f"Multi-episode result detected: {torrent_title} (Complete series)")
                else:
                    seasons = season_pack.split(',')
                    num_seasons = len(seasons)
                    logging.debug(f"Multi-episode result detected: {torrent_title} (Seasons: {season_pack}, Count: {num_seasons})")
            else:
                logging.debug(f"Single episode or movie result: {torrent_title}")

        # Sort results
        sorting_start = time.time()
                    
        def stable_rank_key(x):
            # First, use the rank_result_key function with content_type
            primary_key = rank_result_key(x, filtered_results, title, year, season, episode, multi, content_type)
            
            # Then, use a tuple of stable secondary keys
            secondary_keys = (
                x.get('scraper', ''),  # Scraper name
                x.get('title', ''),    # Torrent title
                x.get('size', 0),      # Size
                x.get('seeders', 0)    # Seeders
            )
            
            return (primary_key, secondary_keys)

        sorted_results = sorted(filtered_results, key=stable_rank_key)
        logging.debug(f"Sorting took {time.time() - sorting_start:.2f} seconds")
        
        logging.debug(f"Total scraping process took {time.time() - start_time:.2f} seconds")
        return sorted_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return []
