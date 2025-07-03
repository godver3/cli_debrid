from routes.api_tracker import api
import logging
from typing import List, Dict, Any, Tuple
from utilities.settings import get_setting, load_config as get_jackett_settings
from urllib.parse import quote, urlencode
import json
import re
from database.database_reading import get_episode_details

JACKETT_FILTER = "!status:failing,test:passed"

def rename_special_characters(text: str) -> str:
    replacements = [
        ("&", ""),
        ("\u00fc", "ue"),
        ("\u00e4", "ae"),
        ("\u00e2", "a"),
        ("\u00e1", "a"),
        ("\u00e0", "a"),
        ("\u00f6", "oe"),
        ("\u00f4", "o"),
        ("\u00e8", "e"),
        (":", ""),
        ("(", ""),
        (")", ""),
        ("`", ""),
        (",", ""),
        ("!", ""),
        ("?", ""),
        (" - ", " "),
        ("*", ""),
        (".", " "),
    ]
    
    for old, new in replacements:
        text = text.replace(old, new)
    
    return text

def scrape_jackett(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None, tmdb_id: str = None, is_translated_search: bool = False) -> List[Dict[str, Any]]:
    #logging.info(f"Starting Jackett scrape for: {title} ({year}) - Type: {content_type}")
    #logging.info(f"Parameters - IMDB: {imdb_id}, Season: {season}, Episode: {episode}, Multi: {multi}, Genres: {genres}, TMDB ID: {tmdb_id}, Translated: {is_translated_search}")
    
    jackett_results = []
    all_settings = get_jackett_settings()
    
    #logging.debug(f"Loaded Jackett settings: {json.dumps(all_settings.get('Scrapers', {}), indent=2)}")
    jackett_instances = all_settings.get('Scrapers', {})
    jackett_filter = "!status:failing,test:passed"
    for instance, settings in jackett_instances.items():
        if instance.startswith('Jackett'):
            if not settings.get('enabled', False):
                logging.debug(f"Jackett instance '{instance}' is disabled, skipping")
                continue
            
            try:
                # Pass the is_translated_search flag down
                instance_results = scrape_jackett_instance(instance, settings, imdb_id, title, year, content_type, season, episode, multi, genres, tmdb_id, is_translated_search)
                jackett_results.extend(instance_results)
            except Exception as e:
                logging.error(f"Error scraping Jackett instance '{instance}': {str(e)}", exc_info=True)
    return jackett_results

def scrape_jackett_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None, tmdb_id: str = None, is_translated_search: bool = False) -> List[Dict[str, Any]]:
    logging.info(f"Scraping Jackett instance: {instance} (Translated Search: {is_translated_search})")
    jackett_url = settings.get('url', '')
    jackett_api = settings.get('api', '')
    enabled_indexers = settings.get('enabled_indexers', '').lower()
    
    # Get the global setting instead
    global_seeders_only = get_setting('Scraping', 'jackett_seeders_only', True)
    #logging.debug(f"Using global 'jackett_seeders_only' setting: {global_seeders_only} for instance {instance}")

    search_queries = []

    if "UFC" in title.upper():
        ufc_number =  re.search(r'UFC[^"]*?(\d\d\d)', title.upper())
        params = f"UFC {ufc_number.group(1)}"
        search_queries = [params]
        logging.info(f"UFC event detected. Using search term: {params}")
    elif content_type.lower() == 'movie':
        params = f"{title} {year}"
        search_queries = [params]
    else:
        params = f"{title}"
        if season is not None:
            # Add standard season/episode format
            standard_query = f"{params}.s{season:02d}"
            if episode is not None and not multi:
                standard_query = f"{standard_query}e{episode:02d}"
            search_queries.append(standard_query)

            # Check if this is a news show for additional date-based search
            is_news = genres and 'news' in [g.lower() for g in genres]
            if is_news and episode is not None and imdb_id:
                # Get episode details from database
                episode_details = get_episode_details(imdb_id, season, episode)
                if episode_details and episode_details.get('release_date'):
                    # Format release date as YYYY.MM.DD
                    formatted_date = episode_details['release_date'].replace('-', '.')
                    date_query = f"{params} {formatted_date}"
                    search_queries.append(date_query)
        else:
            # For non-episode searches, just use the title with IMDB if available
            base_query = f"{params}"
            if imdb_id:
                base_query = f"{base_query} ({imdb_id})"
            search_queries.append(base_query)

    # Perform searches and combine results
    all_results = []
    for query in search_queries:
        query = rename_special_characters(query)

        search_endpoint = f"{jackett_url}/api/v2.0/indexers/all/results?apikey={jackett_api}"
        query_params = {'Query': query}

        if enabled_indexers:
            query_params['Tracker[]'] = [indexer.strip() for indexer in enabled_indexers.split(',')]

        full_url = f"{search_endpoint}&{urlencode(query_params, doseq=True)}"

        try:
            response = api.get(full_url, headers={'accept': 'application/json'})
            
            if response.status_code == 200:
                data = response.json()
                # Pass the global setting to the parser
                results = parse_jackett_results(data.get('Results', []), instance, global_seeders_only)
                all_results.extend(results)
            else:
                logging.error(f"Jackett API error for {instance}: Status code {response.status_code}")
        except Exception as e:
            logging.error(f"Error querying Jackett API for {instance}: {str(e)}")

    # Remove duplicates based on magnet links
    seen_magnets = set()
    unique_results = []
    for result in all_results:
        if result['magnet'] not in seen_magnets:
            seen_magnets.add(result['magnet'])
            unique_results.append(result)

    return unique_results

def parse_jackett_results(data: List[Dict[str, Any]], ins_name: str, seeders_only: bool) -> List[Dict[str, Any]]:
    #logging.info(f"Parsing {len(data)} results from {ins_name}")
    results = []
    filtered_no_magnet = 0
    filtered_no_seeders = 0
    
    for item in data:
        parsed_info = {} # Dictionary to hold extra details
        title = item.get('Title', 'N/A')
        
        # Determine the primary link (MagnetUri first, then Link)
        magnet = item.get('MagnetUri')
        link = item.get('Link')
        is_torrent_url = False

        if magnet:
             primary_link = magnet
        elif link:
             primary_link = link
             # Check if the Link is actually a magnet link
             if not primary_link.startswith('magnet:'):
                 is_torrent_url = True
        else:
            filtered_no_magnet += 1
            logging.debug(f"Skipping result '{title}' - No magnet or link found")
            continue

        seeders = item.get('Seeders', 0)

        if seeders_only and seeders == 0:
            filtered_no_seeders += 1
            #logging.debug(f"Filtered out '{title}' due to no seeders (global seeders_only={seeders_only})")
            continue
            
        # Extract basic info
        tracker = item.get('Tracker', 'N/A')
        size_bytes = item.get('Size', 0)
        size_gb = round(size_bytes / (1024 * 1024 * 1024), 2) if size_bytes else 0.0

        # Extract additional details into parsed_info
        parsed_info['tracker_id'] = item.get('TrackerId')
        parsed_info['tracker_type'] = item.get('TrackerType')
        parsed_info['category_desc'] = item.get('CategoryDesc')
        parsed_info['publish_date'] = item.get('PublishDate')
        parsed_info['peers'] = item.get('Peers') # Seeders + Leechers
        parsed_info['grabs'] = item.get('Grabs')
        parsed_info['imdb_id'] = item.get('Imdb')
        parsed_info['genres'] = item.get('Genres')
        parsed_info['description'] = item.get('Description')
        parsed_info['guid'] = item.get('Guid') # Link to torrent details page or .torrent file
        # Store tracker specific info if needed
        parsed_info['minimum_ratio'] = item.get('MinimumRatio')
        parsed_info['minimum_seed_time'] = item.get('MinimumSeedTime')
        parsed_info['download_volume_factor'] = item.get('DownloadVolumeFactor')
        parsed_info['upload_volume_factor'] = item.get('UploadVolumeFactor')
        
        # Initialize hash
        info_hash = item.get('InfoHash', '')

        result = {
            'title': title,
            'size': size_gb,
            'source': f"{ins_name} - {tracker}",
            'magnet': primary_link, # Use the determined primary link
            'seeders': seeders,
            'hash': info_hash, # Start with InfoHash field if present
            'parsed_info': parsed_info # Store all extra details
        }
            
        # Set the appropriate property based on link type
        if is_torrent_url:
            result['torrent_url'] = primary_link
        else:
            # If it's a magnet link, ensure hash is extracted
            result['magnet_link'] = primary_link
            if not info_hash and primary_link.startswith('magnet:'): # Only try extracting if hash wasn't in the main field
                try:
                    # Use regex for more robust hash extraction from magnet
                    hash_match = re.search(r'urn:btih:([a-fA-F0-9]{40})', primary_link)
                    if hash_match:
                         result['hash'] = hash_match.group(1).lower()
                         logging.debug(f"Extracted hash {result['hash']} from magnet link for '{title}'")
                except Exception as e:
                    logging.error(f"Error extracting hash from magnet link '{primary_link}': {e}")
        
        # Update main hash field if extracted successfully
        if result['hash'] and not info_hash:
             item['InfoHash'] = result['hash'] # Update the source item for consistency if needed later

        results.append(result)
        #logging.debug(f"Added result: {title} ({result['size']:.2f}GB, {seeders} seeders)")

    # Log summary (optional)
    # if filtered_no_magnet > 0 or filtered_no_seeders > 0:
    #     logging.debug(f"Jackett parsing summary for {ins_name}:")
    #     logging.debug(f"- Total items processed: {len(data)}")
    #     logging.debug(f"- Successfully parsed: {len(results)}")
    #     logging.debug(f"- Filtered (no link): {filtered_no_magnet}")
    #     logging.debug(f"- Filtered (no seeders): {filtered_no_seeders}")

    return results

def construct_url(settings: Dict[str, Any], title: str, year: int, content_type: str, season: int = None, episode: int = None, jackett_filter: str = "!status:failing,test:passed", multi: bool = False) -> str:
    jackett_url = settings.get('url', '')
    jackett_api = settings.get('api', '')
    enabled_indexers = settings.get('enabled_indexers', '')

    # Build the search query
    if content_type == 'movie':
        params = f"{title} {year}"
    else:
        params = f"{title}"
        if season is not None:
            params = f"{params}.s{season:02d}"
            if episode is not None and not multi:
                params = f"{params}e{episode:02d}"

    # Apply special character renaming
    params = rename_special_characters(params)

    search_endpoint = f"{jackett_url}/api/v2.0/indexers/{jackett_filter}/results?apikey={jackett_api}"
    query_params = {'Query': params}
    
    if enabled_indexers:
        query_params['Tracker[]'] = [indexer.strip() for indexer in enabled_indexers.split(',')]

    full_url = f"{search_endpoint}&{urlencode(query_params, doseq=True)}"

    return full_url

def fetch_data(url: str) -> Dict[str, Any]:
    response = api.get(url, headers={'accept': 'application/json'})
    #logging.debug(f"Jackett instance '{instance}' API status code: {response.status_code}")

    if response.status_code == 200:
        return response.json()
    else:
        logging.error(f"Jackett instance API error: Status code {response.status_code}")
        return {}
