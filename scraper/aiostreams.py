import logging
from routes.api_tracker import api
import re
from typing import List, Dict, Any
from database.database_reading import get_imdb_aliases
import time
import random
from http.client import RemoteDisconnected

def scrape_aiostreams_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int, content_type: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
    """
    Scrape AIOStreams instance for pre-resolved stream URLs.
    AIOStreams is a Stremio addon that returns direct streaming URLs, not torrent magnets.
    """
    base_url = settings.get('url', '').rstrip('/')
    # Remove /manifest.json if present (user might paste the full Stremio addon URL)
    if base_url.endswith('/manifest.json'):
        base_url = base_url[:-14]  # Remove the last 14 characters ('/manifest.json')

    try:
        # Get all IMDB aliases for this ID
        imdb_ids = get_imdb_aliases(imdb_id)
        all_results = []

        # Scrape for each IMDB ID (original + aliases)
        for current_imdb_id in imdb_ids:
            # Ensure IMDB ID has 'tt' prefix
            if not current_imdb_id.startswith('tt'):
                current_imdb_id = f'tt{current_imdb_id}'

            url = construct_url(base_url, current_imdb_id, content_type, season, episode)
            response = fetch_data(url)

            if not response:
                continue

            if 'streams' not in response:
                continue

            parsed_results = parse_results(response['streams'], instance)
            all_results.extend(parsed_results)

        # Remove duplicates based on info_hash
        seen_hashes = set()
        unique_results = []
        for result in all_results:
            info_hash = result.get('info_hash', '')
            if info_hash and info_hash not in seen_hashes:
                seen_hashes.add(info_hash)
                unique_results.append(result)

        return unique_results
    except Exception as e:
        logging.error(f"Error in scrape_aiostreams_instance for {instance}: {str(e)}", exc_info=True)
        return []

def construct_url(base_url: str, imdb_id: str, content_type: str, season: int = None, episode: int = None) -> str:
    """
    Construct AIOStreams API URL following Stremio addon format.
    Format: /stream/{type}/{id}.json
    For series: /stream/series/{imdb_id}:{season}:{episode}.json
    For movies: /stream/movie/{imdb_id}.json
    """
    if season is not None and episode is None:
        episode = 1

    if content_type == "movie":
        return f"{base_url}/stream/movie/{imdb_id}.json"
    elif content_type == "episode" and season is not None and episode is not None:
        return f"{base_url}/stream/series/{imdb_id}:{season}:{episode}.json"
    elif content_type == "episode":
        return f"{base_url}/stream/series/{imdb_id}.json"
    else:
        logging.error("Invalid content type provided. Must be 'movie' or 'episode'.")
        return ""

def fetch_data(url: str) -> Dict:
    """
    Fetch data from AIOStreams API with retry logic and exponential backoff.
    """
    max_retries = 4
    base_backoff_seconds = 0.5

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        for attempt in range(max_retries):
            try:
                response = api.get(url, headers=headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    return data

                # Retry on server errors (5xx)
                if 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        sleep_seconds = base_backoff_seconds * (2 ** attempt) + random.uniform(0, 0.25)
                        logging.warning(
                            f"Server error {response.status_code} while fetching {url} (attempt {attempt + 1}/{max_retries}). "
                            f"Retrying in {sleep_seconds:.2f}s"
                        )
                        time.sleep(sleep_seconds)
                        continue

                # Non-retriable status codes
                logging.warning(f"Non-200 status ({response.status_code}) for URL {url}")
                return {}

            except (api.exceptions.RequestException, RemoteDisconnected) as e:
                if attempt < max_retries - 1:
                    sleep_seconds = base_backoff_seconds * (2 ** attempt) + random.uniform(0, 0.25)
                    logging.warning(
                        f"Error fetching data: {e} (attempt {attempt + 1}/{max_retries}) for {url}. "
                        f"Retrying in {sleep_seconds:.2f}s"
                    )
                    time.sleep(sleep_seconds)
                    continue
                logging.error(f"Error fetching data: {str(e)}")
                return {}

    except Exception as e:
        logging.error(f"Unexpected error in fetch_data: {str(e)}", exc_info=True)

    return {}

def parse_size(size_info: str) -> float:
    """
    Parse file size from emoji-decorated description.
    Looks for patterns like: 📦 2.5 GB
    Returns size in GB.
    """
    size_patterns = [
        r'📦\s*([\d.]+)\s*(\w+)',  # Package emoji format
        r'💾\s*([\d.]+)\s*(\w+)',  # Disk emoji format
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
    """
    Parse seeder count from description.
    Looks for patterns like: 👤 50 or Seeders: 50
    """
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
    """
    Parse AIOStreams response into standardized format.
    Returns magnet links like other scrapers.
    """
    results = []
    stats = {
        'skipped_count': 0,
        'no_title_count': 0,
        'no_info_hash_count': 0,
        'parse_error_count': 0,
        'total_processed': 0
    }

    for stream in streams:
        parsed_info = {}
        try:
            stats['total_processed'] += 1

            # Skip error streams (timeout, failures, etc.)
            if 'streamData' in stream and stream.get('streamData', {}).get('type') == 'error':
                stats['skipped_count'] += 1
                continue

            # Extract basic stream info
            description = stream.get('description', '')
            name = stream.get('name', '')
            behavior_hints = stream.get('behaviorHints', {})

            # Extract addon name and indexer from description field
            # Description format has lines like:
            # ℹ️ TorrentsDB (addon)
            # ℹ️ ThePirateBay (indexer)
            addon_name = ''
            indexer_name = ''

            if description:
                # Split description into lines
                desc_lines = description.split('\n')
                info_lines = [line.strip() for line in desc_lines if line.strip().startswith('ℹ️')]

                # Look for addon and indexer in info lines
                # Skip first two info lines (usually filename and hash)
                # Third info line is typically the addon name
                # Fourth info line is typically the indexer name
                if len(info_lines) > 2:
                    # Extract addon name (3rd info line)
                    addon_line = info_lines[2].replace('ℹ️', '').strip()
                    if addon_line and not re.match(r'^[a-f0-9]{40}$', addon_line):  # Not a hash
                        addon_name = addon_line

                if len(info_lines) > 3:
                    # Extract indexer name (4th info line)
                    indexer_line = info_lines[3].replace('ℹ️', '').strip()
                    if indexer_line:
                        indexer_name = indexer_line

            # Store addon and indexer info
            parsed_info['addon_name'] = addon_name
            parsed_info['indexer'] = indexer_name

            # Get filename from behaviorHints (this is the actual filename)
            filename = behavior_hints.get('filename', '')

            # Use filename as title, fallback to name if no filename
            title = filename if filename else name

            if not title:
                stats['no_title_count'] += 1
                continue

            # Split description into parts for metadata parsing
            description_parts = description.split('\n') if description else []

            # Initialize metadata values
            size = 0.0
            seeders = 0
            languages = []
            source_link = None

            # Try to get size from behaviorHints first (more reliable as it's in bytes)
            if 'videoSize' in behavior_hints:
                try:
                    size_bytes = float(behavior_hints['videoSize'])
                    if size_bytes > 0:
                         size = size_bytes / (1024 * 1024 * 1024)  # Convert bytes to GB
                except (ValueError, TypeError):
                    pass  # Will try parsing from description later

            # Parse metadata from description parts
            for part in description_parts:
                part = part.strip()

                # Parse size if not already found from videoSize
                if size == 0:
                    size_info = parse_size(part)
                    if size_info > 0:
                        size = size_info

                # Parse seeders
                seeder_info = parse_seeder(part)
                if seeder_info > 0:
                    seeders = seeder_info

                # Extract Languages
                lang_match = re.search(r'🌐\s*(.+)', part)
                if lang_match:
                    lang_text = lang_match.group(1).strip()
                    languages = [lang.strip() for lang in re.split(r'[+,]', lang_text)]
                    parsed_info['languages'] = languages

                # Extract Source Link
                source_match = re.search(r'🔗\s*(.+)', part)
                if source_match:
                    source_link = source_match.group(1).strip()
                    # Remove contributor part if present
                    source_link = re.sub(r'🧑.*$', '', source_link).strip()
                    parsed_info['source_link'] = source_link

            # Check for info hash or magnet link
            info_hash = ''
            magnet_link = ''

            # Try to get infoHash from stream
            if 'infoHash' in stream:
                info_hash = stream['infoHash']
                logging.debug(f"Extracted infoHash: {info_hash}")

            # Try to get magnet from externalUrl
            if 'externalUrl' in stream and stream['externalUrl'].startswith('magnet:'):
                magnet_link = stream['externalUrl']
                logging.debug(f"Extracted magnet from externalUrl: {magnet_link[:100]}")
                # Extract hash from magnet if we don't have info_hash
                if not info_hash and 'xt=urn:btih:' in magnet_link:
                    hash_match = re.search(r'xt=urn:btih:([a-fA-F0-9]{40})', magnet_link)
                    if hash_match:
                        info_hash = hash_match.group(1).lower()

            # If we have neither info_hash nor magnet, skip this stream
            # AIOStreams in direct stream mode won't work with this app
            if not info_hash and not magnet_link:
                stats['no_info_hash_count'] += 1
                logging.debug(f"Skipping stream - no info_hash or magnet. Stream keys: {stream.keys()}")
                continue

            # Use magnet link as URL if available, otherwise construct from hash
            if magnet_link:
                final_url = magnet_link
            elif info_hash:
                final_url = f"magnet:?xt=urn:btih:{info_hash}"
            else:
                continue

            logging.info(f"Created magnet URL: {final_url[:100]} for title: {title[:50]}")

            result = {
                'title': title,
                'size': round(size, 2),
                'seeders': seeders,
                'source': f'{instance}{f" - {addon_name}" if addon_name else ""}{f" - {indexer_name}" if indexer_name else ""}',
                'magnet': final_url,
                'info_hash': info_hash,
                'parsed_info': parsed_info
            }

            if languages:
                 result['languages'] = languages

            results.append(result)

        except Exception as e:
            stats['parse_error_count'] += 1
            logging.error(f"Error parsing AIOStreams result: {str(e)}", exc_info=True)
            if 'title' in stream:
                logging.error(f"Failed stream title: {stream['title']}")
            if 'description' in stream:
                logging.error(f"Failed stream description: {stream['description']}")
            continue

    return results
