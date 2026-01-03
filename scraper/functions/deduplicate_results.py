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

        # First check: Use magnet link
        if magnet:
            trimmed_magnet = trim_magnet(magnet)
            unique_id = trimmed_magnet
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
