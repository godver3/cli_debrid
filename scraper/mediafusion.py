from api_tracker import api
import logging
import re
from typing import Dict, Any, List
from database.database_reading import get_imdb_aliases

def scrape_mediafusion_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    mediafusion_base_url = settings.get('url', '').rstrip('manifest.json')
    
    try:
        # Get all IMDB aliases for this ID
        imdb_ids = get_imdb_aliases(imdb_id)
        all_results = []
        
        # Scrape for each IMDB ID (original + aliases)
        for current_imdb_id in imdb_ids:
            url = construct_url(mediafusion_base_url, current_imdb_id, content_type, season, episode)
            response = fetch_data(url)
            if not response:
                logging.warning(f"No response received for IMDb ID: {current_imdb_id} from {instance}")
                continue
            
            if 'streams' not in response:
                logging.warning(f"No 'streams' key in response for IMDb ID: {current_imdb_id} from {instance}")
                continue
                
            parsed_results = parse_results(response['streams'], instance)
            all_results.extend(parsed_results)
            
        # Remove duplicates based on info_hash
        seen_hashes = set()
        unique_results = []
        for result in all_results:
            if result['info_hash'] not in seen_hashes:
                seen_hashes.add(result['info_hash'])
                unique_results.append(result)
                
        logging.debug(f"Found {len(unique_results)} unique results after checking {len(imdb_ids)} IMDB IDs")
        return unique_results
    except Exception as e:
        logging.error(f"Error in scrape_mediafusion_instance for {instance}: {str(e)}", exc_info=True)
        return []

def construct_url(base_url: str, imdb_id: str, content_type: str, season: int = None, episode: int = None) -> str:
    if content_type == "movie":
        return f"{base_url}stream/movie/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{base_url}stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{base_url}stream/series/{imdb_id}.json"
    else:
        logging.error("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

def fetch_data(url: str) -> Dict:
    try:
        response = api.get(url)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Failed to fetch data from URL: {url} with status {response.status_code}")
    except api.exceptions.RequestException as e:
        logging.error(f"Request failed: {str(e)}")
    return {}

def parse_size(size_info: str) -> float:
    # Look for common size patterns with various emoji and formats
    size_patterns = [
        r'💾\s*([\d.]+)\s*(\w+)',  # Original emoji format
        r'📦\s*([\d.]+)\s*(\w+)',  # Alternative emoji
        r'(?:Size[: ]*|^)([\d.]+)\s*(\w+)',  # Text format
        r'\[([\d.]+)\s*(\w+)\]',  # Bracket format
        r'\(([\d.]+)\s*(\w+)\)',  # Parentheses format
    ]
    
    for pattern in size_patterns:
        size_match = re.search(pattern, size_info, re.IGNORECASE)
        if size_match:
            try:
                size, unit = size_match.groups()
                size = float(size)
                unit = unit.lower()
                if unit.startswith('g'):  # GB, GiB
                    return size
                elif unit.startswith('m'):  # MB, MiB
                    return size / 1024
                elif unit.startswith('t'):  # TB, TiB
                    return size * 1024
                elif unit.startswith('k'):  # KB, KiB
                    return size / (1024 * 1024)
            except (ValueError, TypeError) as e:
                logging.debug(f"Failed to convert size value: {str(e)}")
                continue
    
    logging.debug(f"Could not parse size from: {size_info}")
    return 0.0

def parse_seeder(seeder_info: str) -> int:
    # Look for seeder patterns with various formats
    seeder_patterns = [
        r'👤\s*(\d+)',  # Emoji format
        r'(?:Seeders?[: ]*|^)(\d+)',  # Text format
        r'\[(\d+)\s*seeders?\]',  # Bracket format
        r'\((\d+)\s*seeders?\)',  # Parentheses format
    ]
    
    for pattern in seeder_patterns:
        seeder_match = re.search(pattern, seeder_info, re.IGNORECASE)
        if seeder_match:
            try:
                return int(seeder_match.group(1))
            except (ValueError, TypeError):
                continue
    
    return 0

def parse_results(streams: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    stats = {
        'skipped_count': 0,
        'no_title_count': 0,
        'no_info_hash_count': 0,
        'parse_error_count': 0,
        'total_processed': 0
    }
    
    for stream in streams:
        try:
            stats['total_processed'] += 1
            description = stream.get('description', '')
            name = stream.get('name', '')
            behavior_hints = stream.get('behaviorHints', {})
            
            if not description and not name:
                stats['no_title_count'] += 1
                continue
            
            # Split description into parts for metadata parsing
            description_parts = description.split('\n') if description else []
            
            # Get title from the first line of description or filename
            raw_title = description_parts[0].strip() if description_parts else behavior_hints.get('filename', name)
            
            # Clean up the title
            title = raw_title
            if title.startswith('📂'):
                title = title[1:].strip()
            if title.startswith('[ ') and title.endswith(' ]'):
                title = title[2:-2].strip()
            
            # Initialize metadata values
            size = 0.0
            seeders = 0
            
            # Try to get size from behaviorHints first
            if 'videoSize' in behavior_hints:
                try:
                    size = float(behavior_hints['videoSize']) / (1024 * 1024 * 1024)  # Convert bytes to GB
                except (ValueError, TypeError):
                    pass
            
            # Parse metadata from description parts
            for part in description_parts:
                part = part.strip()
                if size == 0:
                    size_info = parse_size(part)
                    if size_info > 0:
                        size = size_info
                
                seeder_info = parse_seeder(part)
                if seeder_info > 0:
                    seeders = seeder_info
            
            # Extract info hash from URL
            url = stream.get('url', '')
            info_hash_match = re.search(r'/stream/([a-f0-9]{40})(?:/|$)', url)
            
            if not info_hash_match:
                stats['no_info_hash_count'] += 1
                continue
            
            info_hash = info_hash_match.group(1)
            magnet_link = f'magnet:?xt=urn:btih:{info_hash}'
            
            results.append({
                'title': title,
                'size': size,
                'seeders': seeders,
                'source': instance,
                'magnet': magnet_link,
                'info_hash': info_hash
            })
            
        except Exception as e:
            stats['parse_error_count'] += 1
            logging.error(f"Error parsing result: {str(e)}", exc_info=True)
            continue
    
    #logging.debug(f"MediaFusion parsing stats for {instance}: processed={stats['total_processed']}, success={len(results)}, errors={stats['parse_error_count']}")
    return results
