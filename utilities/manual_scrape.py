import logging
import requests
import re
from typing import Tuple
from typing import Dict, Any, Optional, Tuple, List
from scraper.scraper import scrape
from utilities.result_viewer import display_results
from debrid.real_debrid import add_to_real_debrid, extract_hash_from_magnet
from settings import get_setting
from metadata.metadata import imdb_to_tmdb
import os
from collections import Counter

logger = logging.getLogger(__name__)

def search_overseerr(search_term: str) -> List[Dict[str, Any]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    # Extract year from search term if present
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', search_term)
    year = year_match.group(1) if year_match else None
    
    # Remove year, season, and episode info from search term for better search results
    search_term_clean = re.sub(r'\b(19\d{2}|20\d{2})\b|\b[Ss]\d+(?:[Ee]\d+)?\b', '', search_term).strip()

    search_url = f"{overseerr_url}/api/v1/search?query={requests.utils.quote(search_term_clean)}"

    try:
        response = requests.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data['results']:
            # Filter results by year if provided
            if year:
                matching_results = [
                    result for result in data['results']
                    if (result['mediaType'] == 'movie' and result.get('releaseDate', '').startswith(year)) or
                       (result['mediaType'] == 'tv' and result.get('firstAirDate', '').startswith(year))
                ]
                return matching_results if matching_results else data['results']
            
            return data['results']
        else:
            logging.warning(f"No results found for search term: {search_term}")
            return []
    except requests.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return []

def run_manual_scrape(search_term=None, return_details=False):
    if search_term is None:
        search_term = input("Enter search term (you can include year, season, and/or episode): ")
    search_results = search_overseerr(search_term)

    if not search_results:
        print("No results found. Please try a different search term.")
        return None

    season, episode = parse_season_episode(search_term)

    for result in search_results:
        details = get_details(result)

        if not details:
            print("Could not fetch details for the selected item. Moving to next result.")
            continue

        imdb_id = details.get('externalIds', {}).get('imdbId', '')
        tmdb_id = str(details.get('id', ''))
        title = details.get('title') if result['mediaType'] == 'movie' else details.get('name', '')
        year = details.get('releaseDate', '')[:4] if result['mediaType'] == 'movie' else details.get('firstAirDate', '')[:4]

        if result['mediaType'] == 'movie':
            movie_or_episode = 'movie'
        else:
            movie_or_episode = 'episode'

        if movie_or_episode == 'movie':
            season = None
            episode = None
            multi = 'false'
        elif movie_or_episode == 'episode':
            if not season:
                season = input("Enter season number: ") or '1'
            if season and not episode:
                episode = input("Enter episode number: ") or '1'
            multi_choice = input("Multi-episode wanted? (y/n): ").lower()
            multi = 'true' if multi_choice in ['y', 'm'] else 'false'

        print(f"\nSelected: {title} ({year})")
        print(f"Media Type: {movie_or_episode}")
        if movie_or_episode == 'episode':
            if season:
                print(f"Season: {season}")
            if episode:
                print(f"Episode: {episode}")
            print(f"Multi-episode: {multi}")

        confirm = input("Is this correct? (y/n): ").lower()
        if confirm == 'y':
            if return_details:
                return {
                    'imdb_id': imdb_id,
                    'tmdb_id': tmdb_id,
                    'title': title,
                    'year': year,
                    'movie_or_episode': movie_or_episode,
                    'season': season if movie_or_episode == 'episode' else None,
                    'episode': episode if movie_or_episode == 'episode' else None,
                    'multi': multi
                }
            else:
                # Pass the version parameter
                manual_scrape(imdb_id, tmdb_id, title, year, movie_or_episode, season, episode, multi)
                return
        else:
            print("Moving to next result...")

    print("No more results available. Please try a different search term.")
    return None

def get_details(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return None

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    media_type = item['mediaType']
    tmdb_id = item['id']

    if media_type == 'movie':
        details_url = f"{overseerr_url}/api/v1/movie/{tmdb_id}"
    elif media_type == 'tv':
        details_url = f"{overseerr_url}/api/v1/tv/{tmdb_id}"
    else:
        logging.error(f"Unknown media type: {media_type}")
        return None

    try:
        response = requests.get(details_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Error fetching details from Overseerr: {e}")
        return None

def imdb_id_to_title_and_year(imdb_id: str, movie_or_episode: str) -> Tuple[str, int]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if movie_or_episode == "movie":
        media_type = "movie"
    else:
        media_type = "tv"

    tmdb_id = imdb_to_tmdb(overseerr_url, overseerr_api_key, imdb_id, media_type)
    if not tmdb_id:
        return "", 0

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    if movie_or_episode == "movie":
        movie_url = f"{overseerr_url}/api/v1/movie/{tmdb_id}"
        try:
            response = requests.get(movie_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                return data['title'], int(data['releaseDate'][:4])
        except requests.RequestException as e:
            logging.error(f"Error fetching movie data: {e}")
    else:
        tv_url = f"{overseerr_url}/api/v1/tv/{tmdb_id}"
        try:
            response = requests.get(tv_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                return data['name'], int(data['firstAirDate'][:4])
        except requests.RequestException as e:
            logging.error(f"Error fetching TV show data: {e}")
    os.system('clear')

    return "", 0

def scrape_sync(imdb_id, tmdb_id, title, year, movie_or_episode, season, episode, multi, version):
    logger = logging.getLogger(__name__)

    # Convert input parameters
    season = int(season) if season is not None else None
    episode = int(episode) if episode is not None else None
    year = int(year) if year is not None else None
    multi = multi if isinstance(multi, bool) else (multi.lower() in ['true', '1', 'yes', 'on'] if isinstance(multi, str) else False)

    logger.debug(f"Scrape parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, "
                 f"movie_or_episode={movie_or_episode}, season={season}, episode={episode}, multi={multi}, "
                 f"version={version}")

    # Call the scrape function with the version
    scrape_result, filtered_out_results = scrape(imdb_id, tmdb_id, title, year, movie_or_episode, version, season, episode, multi)

    # Log the type and structure of scrape_result
    #logger.debug(f"Type of scrape_result: {type(scrape_result)}")
    #if isinstance(scrape_result, tuple):
        #logger.debug(f"Length of scrape_result tuple: {len(scrape_result)}")

    # Ensure we're using the deduplicated results
    if isinstance(scrape_result, tuple) and len(scrape_result) > 1:
        results = scrape_result[1]  # Use the second element which should be the deduplicated results
        #logger.debug("Using the second element of the tuple as deduplicated results")
    elif isinstance(scrape_result, list):
        results = scrape_result
        #logger.debug("scrape_result is a list, using it directly")
    else:
        logger.error(f"Unexpected scrape_result format: {type(scrape_result)}")
        return

    logger.debug(f"Number of results received from scrape: {len(results)}")

    # Log details about potential duplicates
    title_counter = Counter(result.get('title') for result in results if isinstance(result, dict))
    #logger.debug("Potential duplicates based on title:")
    #for title, count in title_counter.items():
        #if count > 1:
            #logger.debug(f"  '{title}': {count} occurrences")

    magnet_counter = Counter(result.get('magnet') for result in results if isinstance(result, dict) and result.get('magnet'))
    #logger.debug("Potential duplicates based on magnet link:")
    #for magnet, count in magnet_counter.items():
        #if count > 1:
            #logger.debug(f"  '{magnet[:50]}...': {count} occurrences")

    processed_results = []
    for index, result in enumerate(results):
        if isinstance(result, dict):
            magnet_link = result.get('magnet')
            if magnet_link:
                result['hash'] = extract_hash_from_magnet(magnet_link)
                processed_results.append(result)
                #logger.debug(f"Processed result {index}: title='{result.get('title')}', "
                             #f"size='{result.get('size')}', magnet='{magnet_link[:50]}...'")
            #else:
                #logger.debug(f"Skipped result {index} due to missing magnet link")
        elif isinstance(result, str):
            logger.warning(f"Result {index} is a string: {result}")
        else:
            logger.warning(f"Unexpected result format for index {index}: {type(result)}")

    #logger.debug(f"Number of processed results: {len(processed_results)}")

    if not processed_results:
        logger.error("No valid results found after processing.")
        return

    # Log details about the results being passed to display_results
    #logger.debug("Results being passed to display_results:")
    #for index, result in enumerate(processed_results):
        #logger.debug(f"  Result {index}: title='{result.get('title')}', "
                     #f"size='{result.get('size')}', magnet='{result.get('magnet')[:50]}...'")

    selected_item = display_results(processed_results, filtered_out_results)
    if selected_item:
        #logger.debug(f"Selected item: title='{selected_item.get('title')}', "
                     #f"size='{selected_item.get('size')}', magnet='{selected_item.get('magnet')[:50]}...'")
        magnet_link = selected_item.get('magnet')
        if magnet_link:
            add_to_real_debrid(magnet_link)
            os.system('clear')
        else:
            logger.error("No magnet link found for the selected item.")
    else:
        logger.error("No item selected.")
        os.system('clear')
        
def parse_season_episode(search_term: str) -> Tuple[str, str]:
    season_episode_match = re.search(r'\b[Ss](\d+)(?:[Ee](\d+))?\b', search_term)
    if season_episode_match:
        season = season_episode_match.group(1)
        episode = season_episode_match.group(2) if season_episode_match.group(2) else '1'  # Default to episode 1
        return season, episode
    return '', ''

def manual_scrape(imdb_id, tmdb_id, title, year, movie_or_episode, season, episode, multi):
    # Get the scraping versions from settings
    scraping_versions = get_setting('Scraping', 'versions', default={})

    # Prompt the user to choose a version or use the default
    version = input(f"Enter the scraping version to use (available versions: {', '.join(scraping_versions.keys())}) [default: '1080p']: ") or '1080p'

    # Log the version being used
    logging.debug(f"Using scraping version: {version}")

    # Call scrape_sync with the version
    scrape_sync(imdb_id, tmdb_id, title, year, movie_or_episode, season, episode, multi, version)
