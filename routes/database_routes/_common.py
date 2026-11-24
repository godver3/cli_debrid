"""Database blueprint plus path, source-filter and queue helpers shared by the database route modules."""

from flask import Blueprint
import logging
from utilities.settings import get_setting
from utilities.plex_removal_cache import cache_plex_removal
import os

database_bp = Blueprint('database', __name__)

def translate_plex_path_to_local(plex_path: str) -> str:
    """
    Translate a Plex path to the local filesystem path.

    Plex stores paths as it sees them (e.g., /movies/Title/file.mkv),
    but the local filesystem may have these at a different location
    (e.g., /mnt/symlinked/movies/Title/file.mkv).

    This function attempts to find the local path by checking if the
    plex_path exists, and if not, prepending the symlinked_files_path.

    Args:
        plex_path: The path as stored in location_on_disk (Plex's view)

    Returns:
        The local filesystem path where the symlink actually exists
    """
    if not plex_path:
        return plex_path

    # First, check if the path already exists (it might already be a local path)
    if os.path.exists(plex_path) or os.path.islink(plex_path):
        return plex_path

    # Get the symlinked files path setting
    symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')
    if not symlinked_files_path:
        logging.debug(f"No symlinked_files_path setting found, returning original path: {plex_path}")
        return plex_path

    # Try to construct the local path by prepending symlinked_files_path
    # The plex_path might be something like /movies/Title/file.mkv
    # and we need /mnt/symlinked/movies/Title/file.mkv

    # Remove leading slash from plex_path if present for joining
    relative_path = plex_path.lstrip('/')
    local_path = os.path.join(symlinked_files_path, relative_path)

    if os.path.exists(local_path) or os.path.islink(local_path):
        logging.debug(f"Translated Plex path '{plex_path}' to local path '{local_path}'")
        return local_path

    # If that didn't work, try matching by filename in the symlinked directory
    # This handles cases where folder structure differs between Plex and local
    filename = os.path.basename(plex_path)

    # Try to find the file recursively in symlinked_files_path (limited depth)
    try:
        for root, dirs, files in os.walk(symlinked_files_path):
            # Limit depth to avoid searching too deep
            depth = root.replace(symlinked_files_path, '').count(os.sep)
            if depth > 4:  # Max 4 levels deep
                dirs[:] = []  # Don't recurse further
                continue
            if filename in files:
                found_path = os.path.join(root, filename)
                if os.path.islink(found_path):
                    logging.debug(f"Found symlink by filename search: '{found_path}' for Plex path '{plex_path}'")
                    return found_path
    except Exception as e:
        logging.debug(f"Error searching for file in symlinked_files_path: {e}")

    # Return original path if no translation found
    logging.debug(f"Could not translate Plex path '{plex_path}', returning as-is")
    return plex_path

# Configuration Constants
BATCH_SIZE = 450  # Number of items to process in each batch

PER_PAGE = 250  # Number of items per page for pagination

STATS_CACHE_DURATION_SECONDS = 60  # Cache statistics for 60 seconds

def queue_plex_removal_for_item(item_db_data, file_management, item_id):
    """Helper function to queue Plex removal for an item based on file management mode.

    Args:
        item_db_data: Dictionary containing item data from database
        file_management: String indicating file management mode ('Plex' or 'Symlinked/Local')
        item_id: ID of the item being processed
    """
    if file_management == 'Plex' and item_db_data.get('filled_by_file'):
        if item_db_data['type'] == 'movie':
            cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'])
        elif item_db_data['type'] == 'episode':
            cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'], item_db_data.get('episode_title'))
        logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Direct Mount).")
    elif file_management == 'Symlinked/Local' and item_db_data.get('location_on_disk'):
        # Path for symlinked items should be location_on_disk, which is the symlink path
        path_to_remove = item_db_data['location_on_disk']
        if item_db_data['type'] == 'movie':
            cache_plex_removal(item_db_data['title'], path_to_remove)
        elif item_db_data['type'] == 'episode':
            cache_plex_removal(item_db_data['title'], path_to_remove, item_db_data.get('episode_title'))
        logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Symlinked/Local mode with Plex URL). Path: {path_to_remove}")

def sync_queues_after_rescrape(item_ids):
    """Drop rescrapped items from in-memory queues immediately after the DB reset.

    Bulk rescrape only updates SQLite; without this, Checking/Adding can keep
    processing stale filled_by_* fields until task_update_queue_views runs (~30s),
    and sibling pack reuse can re-bind the old torrent on the very next tick.
    """
    if not item_ids:
        return
    id_set = {int(i) for i in item_ids}
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        if not runner or not getattr(runner, 'queue_manager', None):
            return
        qm = runner.queue_manager
        for queue_name in ('Checking', 'Adding', 'Scraping'):
            queue = qm.queues.get(queue_name)
            if not queue or not hasattr(queue, 'items'):
                continue
            for mem_item in list(queue.items):
                if mem_item.get('id') in id_set:
                    if hasattr(queue, 'remove_item'):
                        queue.remove_item(mem_item)
                    else:
                        queue.items = [i for i in queue.items if i.get('id') not in id_set]
        adding_queue = qm.queues.get('Adding')
        if adding_queue and hasattr(adding_queue, '_nzb_submitted_ids'):
            adding_queue._nzb_submitted_ids -= id_set
        if hasattr(runner, 'task_update_queue_views'):
            runner.task_update_queue_views()
        logging.info(f"Rescrape: Synced in-memory queues after reset for item IDs: {sorted(id_set)}")
    except Exception as exc:
        logging.warning(f"Rescrape: Post-rescrape queue sync failed: {exc}")

# ---------------------------------------------------------------------------
# Lightweight statistics helper – counts collected movies / shows / episodes
# ---------------------------------------------------------------------------

def get_basic_collection_counts():
    """Return basic collection statistics.

    Mirrors the logic in `database.statistics.get_collected_counts` but stripped
    down to the three numbers we need, avoiding the summary table checks and
    extra overhead. This still honours the business rules of counting only
    collected / upgrading items, deduplicating movies by `imdb_id`, shows by
    episode `imdb_id`, and episodes by the (imdb_id, season, episode) tuple.
    """

    from database import get_db_connection

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Unique collected movies
        cursor.execute(
            """
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items
            WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
            """
        )
        total_movies = cursor.fetchone()[0]

        # Unique shows (distinct imdb_id among collected episodes)
        cursor.execute(
            """
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items
            WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            """
        )
        total_shows = cursor.fetchone()[0]

        # Unique episodes (distinct imdb_id + season + episode)
        cursor.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT imdb_id, season_number, episode_number
                FROM media_items
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            )
            """
        )
        total_episodes = cursor.fetchone()[0]

        return {
            'total_movies': total_movies,
            'total_shows': total_shows,
            'total_episodes': total_episodes,
        }
    finally:
        if conn:
            conn.close()

def item_source_label(item):
    """Where an item's file came from: 'Usenet', 'Debrid (<provider>)', 'Debrid', or ''."""
    tid = str(item.get('filled_by_torrent_id') or '')
    if not tid:
        return ''
    if tid.startswith('nzb:'):
        return 'Usenet'
    provider = item.get('debrid_provider')
    return f'Debrid ({provider})' if provider else 'Debrid'

def source_sql(db_columns):
    """SQL for item_source_label(): the Source column's value, for filtering."""
    if 'filled_by_torrent_id' not in db_columns:
        return None
    debrid = ("CASE WHEN COALESCE(debrid_provider, '') != '' THEN 'Debrid (' || debrid_provider || ')' "
              "ELSE 'Debrid' END") if 'debrid_provider' in db_columns else "'Debrid'"
    return ("(CASE WHEN COALESCE(filled_by_torrent_id, '') = '' THEN '' "
            "WHEN filled_by_torrent_id LIKE 'nzb:%' THEN 'Usenet' "
            f"ELSE {debrid} END)")

def source_filter_clause(db_columns, operator, value):
    """(where clause, params) for a filter on the computed Source column, or None.

    'Debrid' matches every debrid row; 'Debrid (<provider>)' one provider; 'None' no file."""
    expr = source_sql(db_columns)
    if expr is None:
        return None
    value = '' if value in (None, 'None') else str(value)
    if operator == 'is_null':
        return f"{expr} = ''", []
    if operator == 'is_not_null':
        return f"{expr} != ''", []
    if operator in ('equals', 'not_equals'):
        clause = f"{expr} LIKE 'Debrid%'" if value == 'Debrid' else f"{expr} = ?"
        params = [] if value == 'Debrid' else [value]
        return (clause if operator == 'equals' else f"NOT ({clause})"), params
    if operator in ('contains', 'not_contains'):
        if not value:
            return None
        clause = f"{expr} LIKE ?"
        return (clause if operator == 'contains' else f"NOT ({clause})"), [f'%{value}%']
    if operator == 'starts_with' and value:
        return f"{expr} LIKE ?", [f'{value}%']
    if operator == 'ends_with' and value:
        return f"{expr} LIKE ?", [f'%{value}']
    return None
