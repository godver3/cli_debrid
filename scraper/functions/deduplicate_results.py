from typing import List, Dict, Any
from scraper.functions.common import round_size, trim_magnet

def deduplicate_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique_results = {}
    title_size_map = {}

    for index, result in enumerate(results):
        magnet = result.get('magnet', '')
        title = result.get('title', '').lower()  # Convert to lowercase for case-insensitive comparison
        size = result.get('size', '')
        rounded_size = round_size(size)

        # First check: Use magnet link, NZB URL, or title+size
        if magnet:
            trimmed_magnet = trim_magnet(magnet)
            unique_id = trimmed_magnet
        elif result.get('nzb_url'):
            unique_id = result['nzb_url']
        else:
            unique_id = f"{title}_{rounded_size}"

        is_duplicate = False

        # Check for duplicates using magnet or title_size
        if unique_id in unique_results:
            is_duplicate = True
            existing_result = unique_results[unique_id]
        elif f"{title}_{rounded_size}" in title_size_map:
            is_duplicate = True
            existing_result = title_size_map[f"{title}_{rounded_size}"]

        if is_duplicate:
            #logging.debug(f"Existing: '{existing_result.get('title')}', New: '{title}'")
            if len(result) > len(existing_result):
                unique_results[unique_id] = result
                title_size_map[f"{title}_{rounded_size}"] = result
            elif len(result) == len(existing_result):
                # Handle None seeders by treating them as 0
                result_seeders = result.get('seeders', 0) or 0
                existing_seeders = existing_result.get('seeders', 0) or 0
                if result_seeders > existing_seeders:
                    unique_results[unique_id] = result
                    title_size_map[f"{title}_{rounded_size}"] = result
        else:
            unique_results[unique_id] = result
            title_size_map[f"{title}_{rounded_size}"] = result

    return list(unique_results.values())


def normalize_episode_format_for_dedup(episode_format: str) -> str:
    """
    Normalize episode format for deduplication purposes.
    Considers E123 and 123 as the same format.
    """
    if not episode_format:
        return ""
    
    # Remove leading 'E' and leading zeros
    normalized = episode_format.strip()
    if normalized.startswith('E'):
        normalized = normalized[1:]
    
    # Remove leading zeros
    normalized = normalized.lstrip('0')
    
    return normalized


def dedupe_passed_results(results, is_anime=False, episode_formats=None):
    """Final dedup of passed results across searches, keeping order.

    Torrents (and debrid results) merge on their title, as before. NZB
    results merge only when they are the same indexer item (same NZB URL):
    two indexers often list the same release name for different uploads,
    and merging on title alone kept whichever came back first and dropped
    the other upload unseen, sometimes keeping the one already on the
    not-wanted list, which left nothing to try.
    """
    seen = set()
    out = []
    for result in results:
        if result.get('protocol') == 'nzb' and (result.get('nzb_url') or (result.get('parsed_info') or {}).get('guid')):
            result_key = ('nzb', result.get('nzb_url') or result['parsed_info']['guid'])
        elif is_anime and episode_formats:
            # For anime, use normalized episode format for deduplication
            title = result.get('parsed_title', result.get('original_title', ''))
            episode_num = (result.get('parsed_info') or {}).get('episode')
            if episode_num:
                result_key = f"{title.lower()}_{normalize_episode_format_for_dedup(str(episode_num))}"
            else:
                result_key = result.get('original_title', '')
        else:
            result_key = result.get('original_title', '')
        if result_key not in seen:
            seen.add(result_key)
            out.append(result)
    return out
