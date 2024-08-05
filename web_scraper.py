import logging
from typing import Dict, Any, List, Tuple, Optional
from settings import get_setting
import requests
from scraper.scraper import scrape
from debrid.real_debrid import extract_hash_from_magnet, add_to_real_debrid
import re

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

    search_url = f"{overseerr_url}/api/v1/search?query={requests.utils.quote(search_term)}"

    try:
        response = requests.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data['results']:
            return data['results']
        else:
            logging.warning(f"No results found for search term: {search_term}")
            return []
    except requests.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return []

def parse_search_term(search_term: str) -> Tuple[str, int, int]:
    # Match patterns like "S01E01", "s01e01", "1x01", "01x01"
    match = re.search(r'(?:s|S)?(\d+)(?:e|E|x)(\d+)', search_term)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        base_title = re.sub(r'(?:s|S)?(\d+)(?:e|E|x)(\d+)', '', search_term).strip()
        return base_title, season, episode
    return search_term, None, None

def web_scrape(search_term: str) -> Dict[str, Any]:
    logging.info(f"Starting web scrape for search term: {search_term}")
    
    base_title, season, episode = parse_search_term(search_term)
    logging.info(f"Parsed search term: title='{base_title}', season={season}, episode={episode}")
    
    search_results = search_overseerr(base_title)
    if not search_results:
        logging.warning(f"No results found for search term: {base_title}")
        return {"error": "No results found"}

    logging.info(f"Found results: {search_results}")

    return {
        "results": [
            {
                "id": result['id'],
                "title": result.get('title') or result.get('name', ''),
                "year": result.get('releaseDate', '')[:4] if result.get('mediaType') == 'movie' else result.get('firstAirDate', '')[:4],
                "media_type": result.get('mediaType', ''),
                "season": season,
                "episode": episode
            }
            for result in search_results
        ]
    }

def get_media_details(media_id: str, media_type: str) -> Dict[str, Any]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    details_url = f"{overseerr_url}/api/v1/{media_type}/{media_id}"

    try:
        response = requests.get(details_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Error fetching media details: {e}")
        return {}

def parse_season_episode(search_term: str) -> Tuple[int, int, bool]:
    # Match patterns like "S01E01", "s01e01", "S01", "s01"
    match = re.search(r'[Ss](\d+)(?:[Ee](\d+))?', search_term)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else 1
        multi = not bool(match.group(2))  # If episode is not specified, set multi to True
        return season, episode, multi
    return 1, 1, True  # Default to S01E01 with multi=True if no match

def process_media_selection(media_id: str, title: str, year: str, media_type: str, season: Optional[int], episode: Optional[int]) -> List[Dict[str, Any]]:
    logging.info(f"Processing media selection: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}")

    details = get_media_details(media_id, media_type)
    imdb_id = details.get('externalIds', {}).get('imdbId', '')
    tmdb_id = str(details.get('id', ''))

    scraper_media_type = 'episode' if media_type == 'tv' else 'movie'
    multi = season is None or episode is None

    logging.info(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, "
                 f"media_type={scraper_media_type}, season={season}, episode={episode}, multi={str(multi).lower()}")

    # Call the scraper function
    scrape_results = scrape(imdb_id, tmdb_id, title, int(year), scraper_media_type, season, episode, str(multi).lower())

    logging.info(f"Scrape results: {scrape_results}")

    # Process the results
    processed_results = []
    for result in scrape_results:
        if isinstance(result, dict):
            magnet_link = result.get('magnet')
            if magnet_link:
                result['hash'] = extract_hash_from_magnet(magnet_link)
                processed_results.append(result)

    return processed_results

def process_torrent_selection(torrent_index: int, torrent_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    logging.info(f"Processing torrent selection: {torrent_index}")
    
    if 0 <= torrent_index < len(torrent_results):
        selected_torrent = torrent_results[torrent_index]
        magnet_link = selected_torrent.get('magnet')
        
        if magnet_link:
            result = add_to_real_debrid(magnet_link)
            if result:
                logging.info("Torrent added to Real-Debrid successfully")
                return {
                    "success": True,
                    "message": "Torrent added to Real-Debrid successfully",
                    "torrent_info": selected_torrent
                }
            else:
                logging.error("Failed to add torrent to Real-Debrid")
                return {
                    "success": False,
                    "error": "Failed to add torrent to Real-Debrid"
                }
        else:
            logging.error("No magnet link found for the selected torrent")
            return {
                "success": False,
                "error": "No magnet link found for the selected torrent"
            }
    else:
        logging.error(f"Invalid torrent index: {torrent_index}")
        return {
            "success": False,
            "error": "Invalid torrent index"
        }

def parse_torrent_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed_results = []
    for result in results:
        parsed_result = {
            "name": result.get("name", "Unknown"),
            "size": result.get("size", "Unknown"),
            "seeders": result.get("seeders", 0),
            "magnet": result.get("magnet", "")
        }
        parsed_results.append(parsed_result)
    return parsed_results