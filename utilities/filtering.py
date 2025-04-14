# utilities/filtering.py
import logging
from typing import List, Dict, Any
from database.core import get_db_connection

def strip_version(version):
    """Strip asterisk from version for comparison, matching add_wanted_items logic."""
    return version.rstrip('*') if version else version

def prefilter_collected_items(
    raw_items_list: List[Dict[str, Any]],
    versions_dict: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Prefilters a list of raw media items, removing movies that are already
    marked as 'Collected' in the database for one of the enabled versions.

    Args:
        raw_items_list: The list of raw items (dictionaries) from a content source.
        versions_dict: The versions dictionary associated with the content source.

    Returns:
        A new list containing only the items that should proceed to metadata processing.
    """
    if not raw_items_list:
        return []

    movie_imdb_ids = set()
    movie_tmdb_ids = set()
    items_to_check = [] # Keep track of original items corresponding to IDs

    # --- Identify movies and their IDs ---
    for item in raw_items_list:
        if item.get('media_type') == 'movie' or ('season_number' not in item and 'episode_number' not in item):
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            if imdb_id or tmdb_id:
                items_to_check.append(item)
                if imdb_id:
                    movie_imdb_ids.add(str(imdb_id))
                if tmdb_id:
                    movie_tmdb_ids.add(str(tmdb_id))

    if not items_to_check:
        # No movies found in the list, return original list
        return raw_items_list

    # --- Determine enabled versions ---
    enabled_versions = {
        strip_version(v) for v, enabled in versions_dict.items() if enabled
    }
    if not enabled_versions:
        logging.warning("Prefiltering called with no enabled versions. Skipping DB check.")
        return raw_items_list # Cannot filter without enabled versions

    # --- Query Database for Collected Movies ---
    conn = get_db_connection()
    cursor = conn.cursor()
    collected_movie_identifiers = set() # Store tuples (imdb_id, tmdb_id) of collected movies

    # Build query conditions for IDs
    id_conditions = []
    params = []
    if movie_imdb_ids:
        id_conditions.append(f"imdb_id IN ({','.join(['?']*len(movie_imdb_ids))})")
        params.extend(list(movie_imdb_ids))
    if movie_tmdb_ids:
        id_conditions.append(f"tmdb_id IN ({','.join(['?']*len(movie_tmdb_ids))})")
        params.extend(list(movie_tmdb_ids))

    if not id_conditions:
        conn.close()
        return raw_items_list # No IDs to check

    id_query_part = " OR ".join(id_conditions)

    # Build query conditions for versions
    version_query_part = f"version IN ({','.join(['?']*len(enabled_versions))})"
    params.extend(list(enabled_versions))

    query = f"""
        SELECT imdb_id, tmdb_id
        FROM media_items
        WHERE type = 'movie'
          AND state = 'Collected'
          AND ({id_query_part})
          AND {version_query_part}
    """

    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        for row in rows:
            # Store both IDs if available to match against the item later
            collected_movie_identifiers.add((str(row['imdb_id']) if row['imdb_id'] else None,
                                             str(row['tmdb_id']) if row['tmdb_id'] else None))
    except Exception as e:
        logging.error(f"Error querying collected movies for prefiltering: {e}", exc_info=True)
        # Fail safe: return original list if DB query fails
        conn.close()
        return raw_items_list
    finally:
        conn.close()

    if not collected_movie_identifiers:
        # No collected movies found matching criteria, return original list
        return raw_items_list

    # --- Filter the original list ---
    filtered_list = []
    skipped_count = 0
    for item in raw_items_list:
        is_movie_to_check = False
        if item.get('media_type') == 'movie' or ('season_number' not in item and 'episode_number' not in item):
             if item.get('imdb_id') or item.get('tmdb_id'):
                  is_movie_to_check = True

        if not is_movie_to_check:
            # Not a movie we are checking, keep it
            filtered_list.append(item)
            continue

        # Check if this movie's identifiers match any collected ones
        item_imdb_id = str(item.get('imdb_id')) if item.get('imdb_id') else None
        item_tmdb_id = str(item.get('tmdb_id')) if item.get('tmdb_id') else None
        is_collected = False
        for collected_imdb, collected_tmdb in collected_movie_identifiers:
            # Match if either IMDb or TMDB ID matches the collected entry's corresponding ID
            if (item_imdb_id and item_imdb_id == collected_imdb) or \
               (item_tmdb_id and item_tmdb_id == collected_tmdb):
                is_collected = True
                break

        if is_collected:
            logging.debug(f"Prefiltering: Skipping movie '{item.get('title', 'Unknown')}' (IMDb: {item_imdb_id}, TMDb: {item_tmdb_id}) - already collected for requested version.")
            skipped_count += 1
        else:
            filtered_list.append(item)

    if skipped_count > 0:
        logging.info(f"Prefiltered {skipped_count} collected movies before metadata processing.")

    return filtered_list
