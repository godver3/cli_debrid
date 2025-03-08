from api_tracker import api
import logging
from typing import List, Dict, Any, Tuple
from settings import get_setting, load_config as get_jackett_settings
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
        ("'", ""),
        ("*", ""),
        (".", " "),
    ]
    
    for old, new in replacements:
        text = text.replace(old, new)
    
    # Remove any remaining apostrophes
    text = text.replace("'", "")
    
    return text

def scrape_jackett(imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None, tmdb_id: str = None) -> List[Dict[str, Any]]:
    #logging.info(f"Starting Jackett scrape for: {title} ({year}) - Type: {content_type}")
    #logging.info(f"Parameters - IMDB: {imdb_id}, Season: {season}, Episode: {episode}, Multi: {multi}, Genres: {genres}, TMDB ID: {tmdb_id}")
    
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
                instance_results = scrape_jackett_instance(instance, settings, imdb_id, title, year, content_type, season, episode, multi, genres, tmdb_id)
                jackett_results.extend(instance_results)
            except Exception as e:
                logging.error(f"Error scraping Jackett instance '{instance}': {str(e)}", exc_info=True)
    return jackett_results

def scrape_jackett_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None, tmdb_id: str = None) -> List[Dict[str, Any]]:
    logging.info(f"Scraping Jackett instance: {instance}")
    jackett_url = settings.get('url', '')
    jackett_api = settings.get('api', '')
    enabled_indexers = settings.get('enabled_indexers', '').lower()
    seeders_only = settings.get('seeders_only', False)

    search_queries = []

    if "UFC" in title.upper():
        ufc_number = title.upper().split("UFC")[-1].strip()
        params = f"UFC {ufc_number}"
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
            query_params['Tracker'] = [indexer.strip() for indexer in enabled_indexers.split(',')]

        full_url = f"{search_endpoint}&{urlencode(query_params, doseq=True)}"

        try:
            response = api.get(full_url, headers={'accept': 'application/json'})
            
            if response.status_code == 200:
                data = response.json()
                results = parse_jackett_results(data.get('Results', []), instance, seeders_only)
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
        title = item.get('Title', 'N/A')
        if item.get('MagnetUri'):
            magnet = item['MagnetUri']
            is_torrent_url = False
        elif item.get('Link'):
            magnet = item['Link']
            # Check if this is likely a torrent URL rather than a magnet link
            is_torrent_url = not magnet.startswith('magnet:')
        else:
            filtered_no_magnet += 1
            #logging.debug(f"Skipping result '{title}' - No magnet or link found")
            continue

        seeders = item.get('Seeders', 0)
        if seeders_only and seeders == 0:
            filtered_no_seeders += 1
            #logging.debug(f"Filtered out '{title}' due to no seeders")
            continue

        if item.get('Tracker') and item.get('Size'):
            result = {
                'title': title,
                'size': item.get('Size', 0) / (1024 * 1024 * 1024),  # Convert to GB
                'source': f"{ins_name} - {item.get('Tracker', 'N/A')}",
                'magnet': magnet,
                'seeders': seeders
            }
            
            # Set the appropriate property for cache checking
            if is_torrent_url:
                result['torrent_url'] = magnet
            else:
                result['magnet_link'] = magnet
                
                # Try to extract hash for standard magnet links
                if magnet.startswith('magnet:'):
                    try:
                        from urllib.parse import parse_qs
                        params = parse_qs(magnet.split('?', 1)[1])
                        xt_params = params.get('xt', [])
                        for xt in xt_params:
                            if xt.startswith('urn:btih:'):
                                result['hash'] = xt.split(':')[2].lower()
                                break
                    except Exception as e:
                        logging.error(f"Error extracting hash from magnet link: {e}")
                
            results.append(result)
            #logging.debug(f"Added result: {title} ({result['size']:.2f}GB, {seeders} seeders)")

    #logging.info(f"Parsing complete - {len(results)} valid results, {filtered_no_magnet} filtered for no magnet, {filtered_no_seeders} filtered for no seeders")
    return results

def construct_url(settings: Dict[str, Any], title: str, year: int, content_type: str, season: int = None, episode: int = None, jackett_filter: str = "!status:failing,test:passed", multi: bool = False) -> str:
    jackett_url = settings.get('url', '')
    jackett_api = settings.get('api_key', '')
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
        query_params.update({f'Tracker[]': {enabled_indexers}})

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
