from flask import Blueprint, render_template, jsonify, request, Response, current_app
from .models import user_required, onboarding_required
from datetime import datetime, timedelta
from queues.queue_manager import QueueManager
import logging
from .program_operation_routes import get_program_status
from queues.initialization import get_initialization_status
from utilities.settings import get_setting
import json
import time
from database.database_reading import get_all_media_items, get_item_count_by_state, get_item_counts_by_states

# Add rate limiting and caching improvements to prevent bombarding Real-Debrid API
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
import json
import threading
from collections import defaultdict

# Global rate limiter for torrent status checks
_torrent_status_rate_limiter = {
    'last_check': defaultdict(float),
    'min_interval': 30,  # Minimum 30 seconds between checks for the same torrent
    'lock': threading.Lock()
}

def _get_provider_display(item: dict) -> str:
    """Return a short provider label for Adding/Checking queue items.
    NZB items: scraper name (NZBGeek, althub, etc.)
    Debrid items: provider name (Real-Debrid, Torbox, Debrid-Link, etc.)
    """
    torrent_id = item.get('filled_by_torrent_id', '') or ''
    if str(torrent_id).startswith('nzb:'):
        # NZB — extract indexer name from scrape_results
        try:
            import re as _re
            raw = item.get('scrape_results') or '[]'
            results = json.loads(raw) if isinstance(raw, str) else (raw or [])
            title = item.get('original_scraped_torrent_title', '') or item.get('filled_by_file', '')

            def _extract_indexer(r):
                # 'source' field is like "NZBGeek_1" or "althub_1"
                source = r.get('source', '')
                if source:
                    cleaned = _re.sub(r'_\d+$', '', source)
                    if cleaned and not cleaned.isdigit():
                        return cleaned
                # scraper_type is the indexer name e.g. "althub", "NZBGeek"
                scraper_type = r.get('scraper_type', '')
                if scraper_type and scraper_type not in ('Newznab', 'newznab') and not scraper_type.isdigit():
                    return scraper_type
                return ''

            # Use first NZB result's indexer (all NZB results in a pack come from same indexer search)
            for r in results:
                if r.get('protocol') == 'nzb':
                    name = _extract_indexer(r)
                    if name:
                        return name
        except Exception:
            pass
        return 'Usenet'
    elif torrent_id and torrent_id != 'Unknown':
        # Debrid — return configured provider name
        try:
            from debrid import get_debrid_provider
            provider = get_debrid_provider()
            name = type(provider).__name__.replace('Provider', '').replace('Client', '')
            # Map class name to friendly name
            _names = {
                'RealDebrid': 'Real-Debrid',
                'AllDebrid': 'AllDebrid',
                'Torbox': 'Torbox',
                'Premiumize': 'Premiumize',
                'DebridLink': 'Debrid-Link',
            }
            return _names.get(name, name)
        except Exception:
            pass
    return ''


def get_torrent_status_check_interval():
    """Get the minimum interval between torrent status checks from settings"""
    try:
        from utilities.settings import get_setting
        return get_setting('Debug', 'torrent_status_check_interval_seconds', default=30)
    except:
        return 30

# Global cache for torrent status to reduce API calls
_torrent_status_cache = {}
_torrent_status_cache_lock = threading.Lock()
TORRENT_STATUS_CACHE_DURATION = 60  # Cache for 60 seconds

def get_queue_contents_with_progressive_fallback(queue_manager, max_time=20):
    """
    Get queue contents with progressive fallback based on time taken.
    Returns minimal data if taking too long, allowing the UI to still show something useful.
    """
    import time

    start_time = time.time()
    result_queue = {}
    errors = {}

    # First, try to get quick summary data for all queues
    summary_start = time.time()
    quick_summary = {}

    for queue_name, queue in queue_manager.queues.items():
        try:
            # Get just the count first - this should be fast for most queues
            if hasattr(queue, 'items'):
                count = len(queue.items)
            elif hasattr(queue, '__len__'):
                count = len(queue)
            else:
                # For database-backed queues, use a non-blocking read so a locked DB
                # (e.g. add_collected_items holding BEGIN IMMEDIATE) never stalls the page load
                if queue_name in ['Wanted', 'Blacklisted', 'Unreleased']:
                    try:
                        import sqlite3 as _sqlite3, os as _os
                        _db_path = _os.path.join(_os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'media_items.db')
                        _rc = _sqlite3.connect(_db_path, timeout=0)
                        _rc.row_factory = _sqlite3.Row
                        count = _rc.execute("SELECT COUNT(*) FROM media_items WHERE state=?", (queue_name,)).fetchone()[0]
                        _rc.close()
                    except Exception:
                        count = 0
                else:
                    count = 0

            quick_summary[queue_name] = {
                'count': count,
                'loaded': False,  # Will be set to True when full data is loaded
                'items': []  # Placeholder for items
            }
        except Exception as e:
            logging.warning(f"Error getting summary for queue {queue_name}: {e}")
            quick_summary[queue_name] = {'count': 0, 'loaded': False, 'items': []}

    summary_time = time.time() - summary_start
    logging.debug(f"[QUEUE_ROUTES] Queue summary took {summary_time:.3f}s")

    # If summary took too long, return minimal data immediately
    if summary_time > max_time / 3:  # Reduced threshold - if summary takes >6.67s, bail
        logging.warning(f"[QUEUE_ROUTES] Summary took {summary_time:.3f}s, returning minimal data")
        # Convert to expected format - empty lists for all queues
        minimal_result = {}
        for queue_name in quick_summary.keys():
            minimal_result[queue_name] = []
        return minimal_result, "minimal"

    # Now try to get full contents, but with time tracking
    full_load_start = time.time()
    for queue_name, queue in queue_manager.queues.items():
        queue_start = time.time()

        try:
            # Skip database-backed queues that are slow to load fully
            if queue_name in ['Wanted', 'Blacklisted', 'Unreleased']:
                # For these queues, we'll load them separately or use counts only
                quick_summary[queue_name]['loaded'] = 'partial'  # Mark as partial
                continue

            # Get full contents for in-memory queues
            # Checking queue: skip live API calls on page render — SSE stream handles live progress
            if queue_name == 'Checking' and hasattr(queue, 'get_contents'):
                items = queue.get_contents(raw=True)
            else:
                items = queue.get_contents()
            quick_summary[queue_name]['items'] = items
            quick_summary[queue_name]['loaded'] = True

            queue_time = time.time() - queue_start
            if queue_time > 0.5:
                logging.info(f"[QUEUE_ROUTES] Queue {queue_name} took {queue_time:.3f}s ({len(items)} items)")

            # Check if we're approaching the time limit
            elapsed = time.time() - start_time
            if elapsed > max_time:
                logging.warning(f"[QUEUE_ROUTES] Approaching time limit ({elapsed:.1f}s), stopping full load")
                break

        except Exception as e:
            logging.error(f"Error loading queue {queue_name}: {e}")
            errors[queue_name] = str(e)
            quick_summary[queue_name]['error'] = str(e)

    full_load_time = time.time() - full_load_start
    total_time = time.time() - start_time

    logging.info(f"[QUEUE_ROUTES] Full queue loading took {full_load_time:.3f}s (total: {total_time:.3f}s)")

    # Convert to the expected format for compatibility
    final_result = {}
    for queue_name, data in quick_summary.items():
        if data['loaded'] == True:
            # Full data loaded
            final_result[queue_name] = data['items']
        elif data['loaded'] == 'partial':
            # Partial data - return empty list but mark for lazy loading
            final_result[queue_name] = []
        else:
            # No data loaded - return empty list
            final_result[queue_name] = []

    # If we have any errors or timed out, include that info
    error_info = None
    if errors:
        error_info = f"Some queues had errors: {', '.join(errors.keys())}"
    elif total_time > max_time:
        error_info = "timeout"

    return final_result, error_info

def get_cached_torrent_status(torrent_id: str, queue_manager) -> Optional[Dict]:
    """Get cached torrent status or return None if cache miss/expired"""
    with _torrent_status_cache_lock:
        if torrent_id in _torrent_status_cache:
            cache_entry = _torrent_status_cache[torrent_id]
            if time.time() - cache_entry['timestamp'] < TORRENT_STATUS_CACHE_DURATION:
                return cache_entry['data']
            else:
                # Remove expired entry
                del _torrent_status_cache[torrent_id]
    return None

def set_cached_torrent_status(torrent_id: str, status_data: Dict):
    """Cache torrent status data"""
    with _torrent_status_cache_lock:
        _torrent_status_cache[torrent_id] = {
            'data': status_data,
            'timestamp': time.time()
        }

def can_check_torrent_status(torrent_id: str) -> bool:
    """Check if enough time has passed to make another API call for this torrent"""
    with _torrent_status_rate_limiter['lock']:
        last_check = _torrent_status_rate_limiter['last_check'][torrent_id]
        min_interval = get_torrent_status_check_interval()
        current_time = time.time()

        if last_check == 0.0:
            # Never checked — register now but skip this cycle so the first SSE
            # cycle doesn't make N simultaneous API calls and stall for 30s.
            _torrent_status_rate_limiter['last_check'][torrent_id] = current_time
            return False

        if current_time - last_check >= min_interval:
            _torrent_status_rate_limiter['last_check'][torrent_id] = current_time
            return True
        return False

def get_torrent_status_with_rate_limiting(torrent_id: str, queue_manager) -> Dict:
    """Get torrent status with rate limiting and caching"""
    # Check cache first
    cached_status = get_cached_torrent_status(torrent_id, queue_manager)
    if cached_status:
        return cached_status

    # Check rate limiting — on cache miss with no rate limit token, return defaults
    # immediately rather than making a live API call. This prevents the first SSE
    # cycle from making N simultaneous API calls and stalling for 30s.
    if not can_check_torrent_status(torrent_id):
        return {'progress': 0, 'state': 'unknown'}
    
    try:
        # Make the actual API call
        checking_queue = queue_manager.queues['Checking']
        progress = checking_queue.get_torrent_progress(torrent_id)
        state = checking_queue.get_torrent_state(torrent_id, increment_strikes=False)
        
        status_data = {
            'progress': progress if progress is not None else 0,
            'state': state if state else 'unknown'
        }
        
        # Cache the result
        set_cached_torrent_status(torrent_id, status_data)
        return status_data
        
    except Exception as e:
        logging.warning(f"Error getting torrent status for {torrent_id}: {str(e)}")
        # Return cached data if available, otherwise defaults
        if cached_status:
            return cached_status
        return {'progress': 0, 'state': 'unknown'}

def get_rate_limiting_stats() -> Dict:
    """Get statistics about rate limiting for monitoring"""
    with _torrent_status_rate_limiter['lock']:
        active_limits = len(_torrent_status_rate_limiter['last_check'])
        min_interval = get_torrent_status_check_interval()
    
    with _torrent_status_cache_lock:
        cache_size = len(_torrent_status_cache)
    
    return {
        'active_torrent_limits': active_limits,
        'min_check_interval_seconds': min_interval,
        'cache_size': cache_size,
        'cache_duration_seconds': TORRENT_STATUS_CACHE_DURATION
    }


queues_bp = Blueprint('queues', __name__)
queue_manager = QueueManager()


@queues_bp.route('/api/queue_workers', methods=['POST'])
@onboarding_required
def save_queue_workers():
    """Save queue_pool_workers setting and reinitialize only the APScheduler — no full restart."""
    try:
        data = request.get_json(silent=True) or {}
        workers = int(data.get('workers', 2))
        workers = max(1, min(3, workers))

        from utilities.settings import set_setting
        set_setting('Queue', 'queue_pool_workers', workers)
        logging.info(f'[QueueWorkers] queue_pool_workers saved as {workers} — takes effect on next restart')
        return jsonify(success=True, workers=workers, message=f'Queue workers set to {workers}. Takes effect on next restart.')
    except Exception as e:
        logging.error(f'[QueueWorkers] Error: {e}')
        return jsonify(success=False, error=str(e))

# Cache settings to avoid repeated database/file reads
_settings_cache = {}
_cache_timestamp = 0
CACHE_DURATION = 30  # Cache settings for 30 seconds

# === Processing rate statistics cache ===
_items_per_hour_cache = {
    'value': None,
    'timestamp': 0
}
ITEMS_PER_HOUR_CACHE_DURATION = 300  # 5 minutes

# Columns fetched from DB for queue display — avoids SELECT * on a wide table
QUEUE_DISPLAY_COLUMNS = [
    'id', 'imdb_id', 'tmdb_id', 'title', 'year', 'type', 'state', 'version',
    'season_number', 'episode_number', 'release_date', 'physical_release_date',
    'airtime', 'content_source', 'content_source_detail', 'filled_by_file', 'filled_by_torrent_id',
    'wake_count', 'collected_at', 'last_updated', 'final_check_add_timestamp',
    'force_priority', 'ghostlisted'
]

def consolidate_items(items, limit=None):
    # Add timing for performance monitoring
    start_time = time.time()
    
    # If no items, return immediately
    if not items:
        return [], 0
        
    # If limit is specified, only process that many items
    items_to_process = items[:limit] if limit else items
    original_count = len(items_to_process)
    
    # Use a dictionary comprehension for faster initial grouping
    consolidated = {}
    
    # Pre-allocate sets for versions and seasons to avoid repeated set creation
    for item in items_to_process:
        # Create a unique key that includes season/episode for TV shows
        if item.get('type') == 'episode':
            key = f"{item['title']}_{item.get('year', 'Unknown')}_S{item.get('season_number', 'Unknown')}E{item.get('episode_number', 'Unknown')}"
        else:
            key = f"{item['title']}_{item.get('year', 'Unknown')}"
            
        if key not in consolidated:
            # Handle null or empty release dates
            release_date = item.get('release_date')
            if release_date is None or release_date == '' or release_date == 'null':
                release_date = 'Unknown'

            # Create the base item with all fields at once
            consolidated[key] = {
                'title': item['title'],
                'year': item.get('year', 'Unknown'),
                'type': item.get('type', 'movie'),
                'versions': {item.get('version', 'Unknown')},  # Initialize with first version
                'seasons': set(),
                'release_date': release_date,
                'physical_release_date': item.get('physical_release_date'),
                'scraping_versions': item.get('scraping_versions', {}),
                'version': item.get('version')
            }
            
            # Add season/episode info for episodes
            if item.get('type') == 'episode':
                consolidated[key]['season_number'] = item.get('season_number')
                consolidated[key]['episode_number'] = item.get('episode_number')
                consolidated[key]['seasons'].add(item.get('season_number'))
        else:
            # Just add the version to existing item
            consolidated[key]['versions'].add(item.get('version', 'Unknown'))
            if item.get('type') == 'episode' and 'season_number' in item:
                consolidated[key]['seasons'].add(item['season_number'])
    
    # Convert to list and convert sets to lists in one pass
    result = []
    for key, data in consolidated.items():
        item_data = {
            'title': data['title'],
            'year': data['year'],
            'type': data['type'],
            'versions': list(data['versions']),
            'seasons': list(data['seasons']),
            'release_date': data['release_date'],
            'physical_release_date': data['physical_release_date'],
            'scraping_versions': data['scraping_versions'],
            'version': data['version']
        }
        if data['type'] == 'episode':
            item_data['season_number'] = data.get('season_number')
            item_data['episode_number'] = data.get('episode_number')
        result.append(item_data)
    
    # Log timing if consolidation took significant time
    total_time = time.time() - start_time
    if total_time > 0.1:  # Log if consolidation took more than 100ms
        logging.debug(f"[QUEUE_ROUTES] consolidate_items took {total_time:.3f}s for {original_count} items, consolidated to {len(result)} items")
            
    return result, original_count

@queues_bp.route('/')
@user_required
@onboarding_required
def index():
    # --- Performance logging ---
    start_time = time.time()
    logging.debug(f"[QUEUE_ROUTES] Starting index route at {start_time}")

    queue_manager_start = time.time()
    # Use progressive fallback to handle large queues gracefully
    queue_contents, error_info = get_queue_contents_with_progressive_fallback(queue_manager, max_time=20)
    queue_manager_time = time.time() - queue_manager_start

    if error_info == "minimal":
        logging.info(f"[QUEUE_ROUTES] Queue loading was slow, showing minimal view with counts only")
        # Continue with minimal data - the template will handle showing counts vs full data
    elif error_info == "timeout":
        logging.warning(f"[QUEUE_ROUTES] Queue loading timed out, showing partial data")
        # Continue with partial data
    elif error_info:
        logging.warning(f"[QUEUE_ROUTES] Some queues had errors: {error_info}")
        # Continue with partial data, errors will be shown in UI

    logging.debug(f"[QUEUE_ROUTES] Queue loading completed in {queue_manager_time:.3f}s")
    
    program_status_start = time.time()
    program_status = get_program_status()
    program_status_time = time.time() - program_status_start
    logging.debug(f"[QUEUE_ROUTES] get_program_status() took {program_status_time:.3f}s")
    
    # Log queue sizes for debugging
    for queue_name, items in queue_contents.items():
        logging.debug(f"[QUEUE_ROUTES] Queue '{queue_name}' has {len(items)} items")
    
    queue_processing_start = time.time()
    for queue_name, items in queue_contents.items():
        queue_item_start = time.time()

        # Skip processing if no items or if items is not a list
        if not items or not isinstance(items, list):
            queue_item_time = time.time() - queue_item_start
            if queue_item_time > 0.1:  # Log if processing took more than 100ms
                logging.debug(f"[QUEUE_ROUTES] Processing queue '{queue_name}' took {queue_item_time:.3f}s (skipped - no items)")
            continue

        if queue_name == 'Upgrading':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue

                upgrade_info = queue_manager.queues['Upgrading'].upgrade_times.get(item['id'])
                if upgrade_info:
                    time_added = upgrade_info.get('time_added')
                    # logging.info(f"time_added: {time_added}")
                    if isinstance(time_added, str):
                        item['time_added'] = time_added
                    elif isinstance(time_added, datetime):
                        item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        item['time_added'] = str(time_added)
                else:
                    item['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                item['upgrades_found'] = item.get('upgrades_found', 0)
        elif queue_name == 'Checking':
            for item in items:
                if not isinstance(item, dict):
                    continue
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
                item['filled_by_torrent_id'] = item.get('filled_by_torrent_id', 'Unknown')
                # Don't make live API calls on page render — SSE stream populates progress
                item['progress'] = item.get('progress', 0)
                item['state'] = item.get('state', 'unknown')
        elif queue_name == 'Sleeping':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                if 'wake_count' not in item or item['wake_count'] is None:
                    item['wake_count'] = queue_manager.get_wake_count(item['id'])
        elif queue_name == 'Pending Uncached':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_magnet'] = item.get('filled_by_magnet', 'Unknown')
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
        elif queue_name == 'Pre_release':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                # Add pre-release specific data
                item['time_added'] = item.get('time_added', datetime.now())
                # Get pre-release data if available
                pre_release_queue = queue_manager.queues.get('Pre_release')
                if pre_release_queue and hasattr(pre_release_queue, 'pre_release_data'):
                    pre_release_info = pre_release_queue.pre_release_data.get(item['id'], {})
                    item['scrape_count'] = pre_release_info.get('scrape_count', 0)
                    item['last_scrape'] = pre_release_info.get('last_scrape', 'Never')
                else:
                    item['scrape_count'] = 0
                    item['last_scrape'] = 'Never'
        
        queue_item_time = time.time() - queue_item_start
        if queue_item_time > 0.1:  # Log if processing took more than 100ms
            logging.debug(f"[QUEUE_ROUTES] Processing queue '{queue_name}' took {queue_item_time:.3f}s")
    
    queue_processing_time = time.time() - queue_processing_start
    logging.debug(f"[QUEUE_ROUTES] Total queue processing took {queue_processing_time:.3f}s")

    unreleased_start = time.time()
    for queue_name, items in queue_contents.items():
        if queue_name == 'Unreleased':
            for item in items:
                if item['release_date'] is None:
                    item['release_date'] = "Unknown"
    unreleased_time = time.time() - unreleased_start
    logging.debug(f"[QUEUE_ROUTES] Unreleased queue processing took {unreleased_time:.3f}s")

    # Add display names for content sources
    display_names_start = time.time()
    from utilities.settings import get_all_settings
    content_sources = get_all_settings().get('Content Sources', {})
    for queue_name, items in queue_contents.items():
        if queue_name in ['Wanted', 'Scraping', 'Adding', 'Pre_release', 'Checking']:
            for item in items:
                if item.get('content_source'):
                    source_config = content_sources.get(item['content_source'], {})
                    display_name = source_config.get('display_name', item['content_source'])
                    item['content_source_display'] = item.get('content_source_detail') or display_name
                if queue_name in ['Adding', 'Checking']:
                    item['provider_display'] = _get_provider_display(item)
                if queue_name == 'Adding':
                    _tid = str(item.get('filled_by_torrent_id', ''))
                    if _tid.startswith('nzb:'):
                        _lu = item.get('last_updated')
                        _is_dl = False
                        if _lu:
                            try:
                                from datetime import datetime as _dt_ui
                                _lu_dt = _dt_ui.fromisoformat(str(_lu).replace('Z', '+00:00').split('+')[0])
                                if (_dt_ui.now() - _lu_dt).total_seconds() >= 60:
                                    _is_dl = True
                            except Exception:
                                pass
                        if _is_dl:
                            item['display_state'] = 'Downloading'
    display_names_time = time.time() - display_names_start
    logging.debug(f"[QUEUE_ROUTES] Display names processing took {display_names_time:.3f}s")

    template_start = time.time()
    upgrading_queue = queue_contents.get('Upgrading', [])
    from utilities.settings import get_setting as _qs
    _queue_pool_workers = int(_qs('Queue', 'queue_pool_workers', 2))
    response = render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue, program_status=program_status, queue_pool_workers=_queue_pool_workers)
    template_time = time.time() - template_start
    logging.debug(f"[QUEUE_ROUTES] Template rendering took {template_time:.3f}s")
    
    total_time = time.time() - start_time
    logging.debug(f"[QUEUE_ROUTES] Total index route took {total_time:.3f}s")
    
    return response

@queues_bp.route('/api/queue_contents')
@user_required
def api_queue_contents():
    # --- Performance logging ---
    start_time = time.time()
    logging.debug(f"[QUEUE_ROUTES] Starting api_queue_contents route at {start_time}")

    queue_name = request.args.get('queue', None)
    queue_manager_start = time.time()
    # Use progressive fallback to handle large queues gracefully
    queue_contents, error_info = get_queue_contents_with_progressive_fallback(queue_manager, max_time=20)
    queue_manager_time = time.time() - queue_manager_start

    if error_info == "minimal":
        logging.info(f"[QUEUE_ROUTES] API queue loading was slow, showing minimal view")
    elif error_info == "timeout":
        logging.warning(f"[QUEUE_ROUTES] API queue loading timed out, showing partial data")
    elif error_info:
        logging.warning(f"[QUEUE_ROUTES] API queue loading had errors: {error_info}")

    logging.debug(f"[QUEUE_ROUTES] API queue loading completed in {queue_manager_time:.3f}s")
    
    program_status_start = time.time()
    program_status = get_program_status()
    program_status_time = time.time() - program_status_start
    logging.debug(f"[QUEUE_ROUTES] get_program_status() took {program_status_time:.3f}s")
    
    # Get initialization status
    initialization_status = None
    if program_status == 'Starting':
        status_start = time.time()
        status = get_initialization_status()
        if status:
            initialization_status = {
                'current_step': status.get('current_step', ''),
                'total_steps': status.get('total_steps', 4),
                'current_step_number': status.get('current_step_number', 0),
                'progress_value': status.get('progress_value', 0),
                'substep_details': status.get('substep_details', ''),
                'error_details': status.get('error_details', None),
                'is_substep': status.get('is_substep', False),
                'current_phase': status.get('current_phase', None)
            }
        status_time = time.time() - status_start
        logging.debug(f"[QUEUE_ROUTES] get_initialization_status() took {status_time:.3f}s")
    
    # If a specific queue is requested, only process that queue
    if queue_name and queue_name in queue_contents:
        items = queue_contents[queue_name]
        
        if queue_name == 'Blacklisted':
            consolidate_start = time.time()
            items, total_count = consolidate_items(items)  # Remove limit
            consolidate_time = time.time() - consolidate_start
            logging.debug(f"[QUEUE_ROUTES] Consolidate_items for Blacklisted took {consolidate_time:.3f}s")
            return jsonify({
                "contents": {queue_name: items},
                "total_items": total_count,
                "original_count": total_count,  # Add original count
                "program_status": program_status,
                "initialization_status": initialization_status
            })
        elif queue_name == 'Unreleased':
            consolidate_start = time.time()
            items, total_count = consolidate_items(items)
            consolidate_time = time.time() - consolidate_start
            logging.debug(f"[QUEUE_ROUTES] Consolidate_items for Unreleased took {consolidate_time:.3f}s")
            return jsonify({
                "contents": {queue_name: items},
                "total_items": total_count,
                "original_count": total_count,  # Add original count
                "program_status": program_status,
                "initialization_status": initialization_status
            })
    
    # Process all queues with their specific logic
    queue_counts = {}  # Store original counts
    for queue_name, items in queue_contents.items():
        queue_counts[queue_name] = len(items)  # Store original count before any processing

        # Skip processing if no items or if items is not a list
        if not items or not isinstance(items, list):
            continue

        if queue_name == 'Upgrading':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                upgrade_info = queue_manager.queues['Upgrading'].upgrade_times.get(item['id'])
                if upgrade_info:
                    time_added = upgrade_info.get('time_added')
                    if isinstance(time_added, str):
                        item['time_added'] = time_added
                    elif isinstance(time_added, datetime):
                        item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        item['time_added'] = str(time_added)
                else:
                    item['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                item['upgrades_found'] = queue_manager.queues['Upgrading'].upgrades_found.get(item['id'], 0)
        elif queue_name == 'Wanted':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                if 'scrape_time' in item:
                    if item['scrape_time'] not in ["Unknown", "Invalid date or time"]:
                        try:
                            scrape_time = datetime.strptime(item['scrape_time'], '%Y-%m-%d %H:%M:%S')
                            item['formatted_scrape_time'] = scrape_time.strftime('%Y-%m-%d %I:%M %p')
                        except ValueError:
                            item['formatted_scrape_time'] = item['scrape_time']
                    else:
                        item['formatted_scrape_time'] = item['scrape_time']
        elif queue_name == 'Checking':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
                item['filled_by_torrent_id'] = item.get('filled_by_torrent_id', 'Unknown')
                item['progress'] = item.get('progress', 0)
                item['state'] = item.get('state', 'unknown')
                if item.get('filled_by_torrent_id') and item['filled_by_torrent_id'] != 'Unknown':
                    # Use rate-limited function to prevent API bombardment
                    status_data = get_torrent_status_with_rate_limiting(item['filled_by_torrent_id'], queue_manager)
                    item['progress'] = status_data['progress']
                    item['state'] = status_data['state']
        elif queue_name == 'Sleeping':
            for item in items:
                # Skip if item is not a dictionary
                if not isinstance(item, dict):
                    logging.warning(f"[QUEUE_ROUTES] Skipping non-dict item in {queue_name}: {type(item)}")
                    continue
                if 'wake_count' not in item or item['wake_count'] is None:
                    item['wake_count'] = queue_manager.get_wake_count(item['id'])

        # Pre-consolidate data for specific queues
        if queue_name == 'Blacklisted':
            consolidate_start = time.time()
            items, _ = consolidate_items(items)  # Remove limit, we already have the count
            consolidate_time = time.time() - consolidate_start
            logging.debug(f"[QUEUE_ROUTES] Consolidate_items for Blacklisted (pre-consolidate) took {consolidate_time:.3f}s")
            queue_contents[queue_name] = items
        elif queue_name == 'Unreleased':
            consolidate_start = time.time()
            items, _ = consolidate_items(items)
            consolidate_time = time.time() - consolidate_start
            logging.debug(f"[QUEUE_ROUTES] Consolidate_items for Unreleased (pre-consolidate) took {consolidate_time:.3f}s")
            queue_contents[queue_name] = items

    # Add display names for content sources in API response
    display_names_start = time.time()
    from utilities.settings import get_all_settings
    content_sources = get_all_settings().get('Content Sources', {})
    for queue_name, items in queue_contents.items():
        if queue_name in ['Wanted', 'Scraping', 'Adding', 'Pre_release', 'Checking']:
            for item in items:
                if item.get('content_source'):
                    source_config = content_sources.get(item['content_source'], {})
                    display_name = source_config.get('display_name', item['content_source'])
                    item['content_source_display'] = item.get('content_source_detail') or display_name
                if queue_name in ['Adding', 'Checking']:
                    item['provider_display'] = _get_provider_display(item)
                if queue_name == 'Adding':
                    _tid = str(item.get('filled_by_torrent_id', ''))
                    if _tid.startswith('nzb:'):
                        _lu = item.get('last_updated')
                        _is_dl = False
                        if _lu:
                            try:
                                from datetime import datetime as _dt_ui2
                                _lu_dt2 = _dt_ui2.fromisoformat(str(_lu).replace('Z', '+00:00').split('+')[0])
                                if (_dt_ui2.now() - _lu_dt2).total_seconds() >= 60:
                                    _is_dl = True
                            except Exception:
                                pass
                        if _is_dl:
                            item['display_state'] = 'Downloading'
    display_names_time = time.time() - display_names_start
    logging.debug(f"[QUEUE_ROUTES] API display names processing took {display_names_time:.3f}s")

    response_start = time.time()
    response = jsonify({
        "contents": queue_contents,
        "queue_counts": queue_counts,  # Add original counts
        "program_status": program_status,
        "initialization_status": initialization_status
    })
    response_time = time.time() - response_start
    logging.debug(f"[QUEUE_ROUTES] jsonify response took {response_time:.3f}s")
    
    total_time = time.time() - start_time
    logging.debug(f"[QUEUE_ROUTES] Total api_queue_contents route took {total_time:.3f}s")
    
    return response

def get_cached_setting(category, key, default=None):
    """Get settings with caching to reduce I/O operations"""
    global _settings_cache, _cache_timestamp
    import time
    
    current_time = time.time()
    cache_key = f"{category}.{key}"
    
    # Check if cache is still valid (30 seconds)
    if current_time - _cache_timestamp > CACHE_DURATION:
        _settings_cache.clear()
        _cache_timestamp = current_time
    
    if cache_key not in _settings_cache:
        from utilities.settings import get_setting
        _settings_cache[cache_key] = get_setting(category, key, default)
    
    return _settings_cache[cache_key]

def process_item_for_response(item, queue_name, currently_processing_upgrade_id=None):
    # Cache common settings that are accessed frequently
    global _settings_cache
    
    # Add timing for performance monitoring
    start_time = time.time()
    
    try:
        # Efficiently add only the necessary 'require_physical_release' flag.
        if queue_name in ['Wanted', 'Scraping', 'Adding', 'Upgrading', 'Blacklisted', 'Unreleased']:
            version = item.get('version')
            if version:
                scraping_versions = get_cached_setting('Scraping', 'versions', {})
                version_settings = scraping_versions.get(version, {})
                item['require_physical_release'] = version_settings.get('require_physical_release', False)
            else:
                item['require_physical_release'] = False
        
        # Add processing and priority flags efficiently
        item['is_processing'] = (queue_name == 'Upgrading' and item['id'] == currently_processing_upgrade_id)
        item['is_force_priority'] = item.get('force_priority', False)
        
        if queue_name == 'Upgrading':
            upgrade_info = queue_manager.queues['Upgrading'].upgrade_times.get(item['id'])
            if upgrade_info:
                time_added = upgrade_info.get('time_added')
                if isinstance(time_added, str):
                    item['time_added'] = time_added
                elif isinstance(time_added, datetime):
                    item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    item['time_added'] = str(time_added)
            else:
                item['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            item['upgrades_found'] = queue_manager.queues['Upgrading'].upgrades_data.get(item['id'], {}).get('count', 0)
        elif queue_name == 'Wanted':
            # Optimize scrape time calculation - only compute when necessary
            scrape_start = time.time()
            item['formatted_scrape_time'] = compute_scrape_time_cached(item)
            scrape_time = time.time() - scrape_start
            if scrape_time > 0.01:  # Log if scrape time calculation took more than 10ms
                logging.debug(f"[QUEUE_ROUTES] compute_scrape_time_cached for item {item.get('id')} took {scrape_time:.3f}s")
        elif queue_name == 'Checking':
            # Convert datetime to string for time_added
            time_added = item.get('time_added', datetime.now())
            if isinstance(time_added, datetime):
                item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
            else:
                item['time_added'] = str(time_added)
                
            item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
            item['filled_by_torrent_id'] = item.get('filled_by_torrent_id', 'Unknown')
            item['progress'] = item.get('progress', 0)
            item['state'] = item.get('state', 'unknown')
            # Time in queue from CheckingQueue's in-memory tracking
            checking_q = queue_manager.queues.get('Checking')
            if checking_q:
                entered = checking_q.checking_queue_times.get(item['id'])
                item['time_in_queue'] = round(time.time() - entered) if entered else None
            else:
                item['time_in_queue'] = None
            # Plex scan tick count
            try:
                import queues.run_program as _rp
                runner = _rp.program_runner
                if runner and hasattr(runner, 'plex_scan_tick_counts'):
                    cache_key = f"{item.get('filled_by_title', '')}:{item.get('filled_by_file', '')}"
                    item['plex_tick'] = runner.plex_scan_tick_counts.get(cache_key, 0)
                else:
                    item['plex_tick'] = 0
            except Exception:
                item['plex_tick'] = 0
            if item.get('filled_by_torrent_id') and item['filled_by_torrent_id'] != 'Unknown':
                # Use rate-limited function to prevent API bombardment
                status_data = get_torrent_status_with_rate_limiting(item['filled_by_torrent_id'], queue_manager)
                item['progress'] = status_data['progress']
                item['state'] = status_data['state']
        elif queue_name == 'Sleeping':
            if 'wake_count' not in item or item['wake_count'] is None:
                item['wake_count'] = queue_manager.get_wake_count(item['id'])
        elif queue_name == 'Final_Check':
            display_timestamp = item.get('final_check_add_timestamp') or item.get('last_updated')
            item['final_check_display_time'] = display_timestamp
        elif queue_name == 'Pre_release':
            # Add pre-release specific data
            time_added = item.get('time_added', datetime.now())
            if isinstance(time_added, datetime):
                item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
            else:
                item['time_added'] = str(time_added)
            
            # Get pre-release data if available
            pre_release_queue = queue_manager.queues.get('Pre_release')
            if pre_release_queue and hasattr(pre_release_queue, 'pre_release_data'):
                pre_release_info = pre_release_queue.pre_release_data.get(item['id'], {})
                item['scrape_count'] = pre_release_info.get('scrape_count', 0)
                item['last_scrape'] = pre_release_info.get('last_scrape', 'Never')
            else:
                item['scrape_count'] = 0
                item['last_scrape'] = 'Never'
        
        # Add display names for content sources
        if queue_name in ['Wanted', 'Scraping', 'Adding', 'Pre_release', 'Checking'] and item.get('content_source'):
            # Cache content sources settings to avoid repeated lookups
            if not hasattr(process_item_for_response, '_content_sources_cache'):
                from utilities.settings import get_all_settings
                process_item_for_response._content_sources_cache = get_all_settings().get('Content Sources', {})
                process_item_for_response._content_sources_timestamp = time.time()
            elif time.time() - process_item_for_response._content_sources_timestamp > 30:  # Cache for 30 seconds
                from utilities.settings import get_all_settings
                process_item_for_response._content_sources_cache = get_all_settings().get('Content Sources', {})
                process_item_for_response._content_sources_timestamp = time.time()
            
            content_sources = process_item_for_response._content_sources_cache
            source_config = content_sources.get(item['content_source'], {})
            display_name = source_config.get('display_name', item['content_source'])
            item['content_source_display'] = item.get('content_source_detail') or display_name

        if queue_name in ['Adding', 'Checking']:
            item['provider_display'] = _get_provider_display(item)

        # Optimize JSON serialization - only process problematic fields
        datetime_fields = ['final_check_display_time', 'time_added', 'last_updated']
        for key, value in item.items():
            if isinstance(value, datetime):
                if key == 'final_check_display_time':
                    item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                elif key in datetime_fields:
                    item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
            elif value == float('inf'):
                item[key] = "Infinity"
            elif value == float('-inf'):
                item[key] = "-Infinity"
            elif isinstance(value, set):
                item[key] = list(value)
            elif not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                item[key] = str(value)
        
        # Log timing if processing took significant time
        total_time = time.time() - start_time
        if total_time > 0.05:  # Log if processing took more than 50ms
            logging.debug(f"[QUEUE_ROUTES] process_item_for_response for {queue_name} item {item.get('id')} took {total_time:.3f}s")
                
        return item
    except Exception as e:
        logging.error(f"Error processing item in queue {queue_name}: {str(e)}", exc_info=True)
        return {
            'id': item.get('id', 'unknown'),
            'title': item.get('title', 'Error processing item'),
            'error': str(e)
        }

# Module-level cache for scrape time computation results.
# Invalidated as a whole every SCRAPE_TIME_CACHE_DURATION seconds so that
# setting changes (offsets, alt strategy, etc.) are picked up promptly.
_scrape_time_cache: dict = {}
_scrape_time_cache_ts: float = 0.0
SCRAPE_TIME_CACHE_DURATION = 30  # seconds


def _compute_scrape_time(item):
    """Core scrape time computation (no caching). Called by compute_scrape_time_cached."""
    try:
        use_alt = get_cached_setting('Debug', 'use_alternate_scrape_time_strategy', False)
        anchor_str = get_cached_setting('Debug', 'alternate_scrape_time_24h', '00:00')

        now = datetime.now()
        release_date_str = item.get('release_date')
        airtime_str = item.get('airtime')
        version = item.get('version')
        item_type = item.get('type')

        scraping_versions = get_cached_setting('Scraping', 'versions', {})
        version_settings = scraping_versions.get(version, {})
        require_physical = version_settings.get('require_physical_release', False)
        physical_release_date_str = item.get('physical_release_date')

        effective_release_date_str = None
        if require_physical and physical_release_date_str:
            effective_release_date_str = physical_release_date_str
        elif not require_physical and release_date_str and str(release_date_str).lower() not in ['unknown', 'none', 'null', '']:
            effective_release_date_str = str(release_date_str)

        if use_alt and effective_release_date_str:
            try:
                anchor_time = datetime.strptime(anchor_str, '%H:%M').time()
            except Exception:
                anchor_time = datetime.strptime('00:00', '%H:%M').time()

            today_anchor = now.replace(hour=anchor_time.hour, minute=anchor_time.minute, second=0, microsecond=0)
            if now < today_anchor:
                next_anchor = today_anchor
            else:
                next_anchor = today_anchor + timedelta(days=1)

            item_release_date = datetime.strptime(effective_release_date_str, '%Y-%m-%d').date()
            item_anchor_dt = datetime.combine(item_release_date, anchor_time)

            if item_anchor_dt > now:
                return item_anchor_dt.strftime('%Y-%m-%d %I:%M %p') + ' (Alt Scrape Time)'
            else:
                return next_anchor.strftime('%Y-%m-%d %I:%M %p') + ' (Alt Scrape Time)'

        elif effective_release_date_str:
            release_date = datetime.strptime(effective_release_date_str, '%Y-%m-%d').date()
            if airtime_str:
                try:
                    airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                except ValueError:
                    try:
                        airtime = datetime.strptime(airtime_str, '%H:%M').time()
                    except ValueError:
                        airtime = datetime.strptime("00:00", '%H:%M').time()
            else:
                airtime = datetime.strptime("00:00", '%H:%M').time()

            release_datetime = datetime.combine(release_date, airtime)

            if item_type == 'movie' and not airtime_str:
                release_datetime += timedelta(days=1)

            item['release_date'] = release_datetime.strftime('%Y-%m-%d')

            offset_hours = 0.0
            if item_type == 'movie':
                movie_offset_setting = get_cached_setting("Queue", "movie_airtime_offset", "0")
                try:
                    offset_hours = float(movie_offset_setting)
                except (ValueError, TypeError):
                    pass
            elif item_type == 'episode':
                episode_offset_setting = get_cached_setting("Queue", "episode_airtime_offset", "0")
                try:
                    offset_hours = float(episode_offset_setting)
                except (ValueError, TypeError):
                    pass

            effective_scrape_time = release_datetime + timedelta(hours=offset_hours)
            return effective_scrape_time.strftime('%Y-%m-%d %I:%M %p')

    except Exception as e:
        logging.warning(f"Could not calculate scrape time for Wanted item {item.get('id')}: {e}")
        return "Error Calculating"

    return "Unknown"


def compute_scrape_time_cached(item):
    """Optimized scrape time calculation with per-key caching.

    Cache is keyed by the 5-tuple of fields that affect the result and is
    fully invalidated every SCRAPE_TIME_CACHE_DURATION seconds so that
    settings changes are reflected without a restart.
    """
    global _scrape_time_cache, _scrape_time_cache_ts

    current_ts = time.time()
    if current_ts - _scrape_time_cache_ts > SCRAPE_TIME_CACHE_DURATION:
        _scrape_time_cache.clear()
        _scrape_time_cache_ts = current_ts

    cache_key = (
        item.get('release_date'),
        item.get('airtime'),
        item.get('version'),
        item.get('type'),
        item.get('physical_release_date'),
    )

    if cache_key in _scrape_time_cache:
        return _scrape_time_cache[cache_key]

    result = _compute_scrape_time(item)
    _scrape_time_cache[cache_key] = result
    return result

@queues_bp.route('/api/rate-limiting-stats')
@user_required
def rate_limiting_stats():
    """Get rate limiting statistics for monitoring"""
    return jsonify(get_rate_limiting_stats())

@queues_bp.route('/api/queue-stream')
@user_required
def queue_stream():
    """Stream queue updates, optimized for high item counts."""
    app = current_app._get_current_object()

    # Determine client type from User-Agent first, as it's needed in the generator.
    ua_string = request.headers.get('User-Agent', '')
    mobile_indicators = ['Mobile', 'Android', 'iPhone', 'iPad', 'iPod', 'Opera Mini', 'IEMobile']
    is_mobile_client = any(indicator in ua_string for indicator in mobile_indicators)

    # Use the 'limit' from the request to control the number of items sent.
    try:
        limit = int(request.args.get('limit'))
    except (TypeError, ValueError):
        # Fallback to User-Agent detection if limit is not provided or invalid.
        limit = 50 if is_mobile_client else 100

    # Apply a hard maximum limit to prevent abuse.
    ITEMS_LIMIT = min(limit, 500)
    # Special limit for Checking queue to improve performance
    CHECKING_QUEUE_LIMIT = 20
    DB_FETCH_QUEUES = {"Wanted", "Final_Check"}  # Removed Blacklisted and Unreleased
    COUNT_ONLY_QUEUES = {"Blacklisted", "Unreleased", "Collected"}  # New set for count-only queues
    
    # Performance optimization: Track last sent data to avoid redundant updates
    last_sent_hash = None
    consecutive_identical_sends = 0
    MAX_IDENTICAL_SENDS = 3  # Stop sending if data unchanged for 3 cycles

    def generate():
        nonlocal last_sent_hash, consecutive_identical_sends
        last_heartbeat = time.time()
        HEARTBEAT_INTERVAL = 30  # Send heartbeat every 30 seconds
        cycle_count = 0
        
        while True:
            cycle_start = time.time()
            cycle_count += 1
            logging.debug(f"[QUEUE_STREAM] Starting cycle {cycle_count} at {cycle_start}")
            
            with app.app_context():
                try:
                    section_start = time.time()
                    current_time = time.time()
                    
                    # Send heartbeat to keep connection alive
                    if current_time - last_heartbeat > HEARTBEAT_INTERVAL:
                        yield f"data: {json.dumps({'heartbeat': True, 'timestamp': current_time})}\n\n"
                        last_heartbeat = current_time
                    
                    program_status_start = time.time()
                    program_status = get_program_status()
                    program_status_time = time.time() - program_status_start
                    if program_status_time > 0.1:
                        logging.debug(f"[QUEUE_STREAM] get_program_status() took {program_status_time:.3f}s")

                    if program_status in ["Stopped", "Stopping"]:
                        yield f"data: {json.dumps({'program_status': program_status})}\n\n"
                        time.sleep(2)
                        continue

                    initialization_status = None
                    if program_status == "Starting":
                        init_status_start = time.time()
                        status = get_initialization_status()
                        if status:
                            initialization_status = {
                                'current_step': status.get('current_step', ''),
                                'total_steps': status.get('total_steps', 4),
                                'current_step_number': status.get('current_step_number', 0),
                                'progress_value': status.get('progress_value', 0),
                                'substep_details': status.get('substep_details', ''),
                                'error_details': status.get('error_details', None),
                                'is_substep': status.get('is_substep', False),
                                'current_phase': status.get('current_phase', None)
                            }
                        init_status_time = time.time() - init_status_start
                        logging.debug(f"[QUEUE_STREAM] get_initialization_status() took {init_status_time:.3f}s")
                        yield f"data: {json.dumps({'program_status': 'Starting', 'initialization_status': initialization_status})}\n\n"
                        time.sleep(0.5)
                        continue

                    # If program is running, proceed to send queue data
                    fetch_start = time.time()
                    queue_manager_start = time.time()
                    queue_manager = QueueManager()
                    queue_manager_time = time.time() - queue_manager_start
                    if queue_manager_time > 0.1:
                        logging.debug(f"[QUEUE_STREAM] QueueManager() instantiation took {queue_manager_time:.3f}s")
                    
                    currently_processing_upgrade_id = None
                    if 'Upgrading' in queue_manager.queues:
                        upgrade_id_start = time.time()
                        currently_processing_upgrade_id = queue_manager.queues['Upgrading'].get_currently_processing_item_id()
                        upgrade_id_time = time.time() - upgrade_id_start
                        if upgrade_id_time > 0.1:
                            logging.debug(f"[QUEUE_STREAM] get_currently_processing_item_id() took {upgrade_id_time:.3f}s")

                    mem_start = time.time()
                    in_memory_queue_contents = queue_manager.get_queue_contents()
                    mem_time = time.time() - mem_start
                    logging.debug(f"[QUEUE_STREAM] get_queue_contents() took {mem_time:.3f}s")

                    final_contents = {}
                    queue_counts = {}
                    hidden_counts = {}

                    # Process in-memory queues first
                    in_memory_start = time.time()
                    for queue_name, items in in_memory_queue_contents.items():
                        if queue_name not in DB_FETCH_QUEUES and queue_name not in COUNT_ONLY_QUEUES:
                            queue_process_start = time.time()
                            total_count = len(items)
                            queue_counts[queue_name] = total_count
                            
                            # Use special limit for Checking queue to improve performance
                            # Checking queue is limited to 20 items regardless of general page size
                            if queue_name == 'Checking':
                                limit_for_queue = CHECKING_QUEUE_LIMIT
                                # Don't show hidden counts for Checking queue
                                hidden_count = 0
                            else:
                                limit_for_queue = ITEMS_LIMIT
                                hidden_count = max(0, total_count - ITEMS_LIMIT)
                                if hidden_count > 0:
                                    hidden_counts[queue_name] = hidden_count

                            limited_items = items[:limit_for_queue]

                            # Process items in batches for better performance
                            processed_items = []
                            batch_start = time.time()
                            for i in range(0, len(limited_items), 25):  # Process 25 items at a time
                                batch = limited_items[i:i+25]
                                batch_processed = [process_item_for_response(item, queue_name, currently_processing_upgrade_id) for item in batch]
                                processed_items.extend(batch_processed)
                            
                            batch_time = time.time() - batch_start
                            if batch_time > 0.1:
                                logging.debug(f"[QUEUE_STREAM] Processing {len(limited_items)} items for queue '{queue_name}' took {batch_time:.3f}s")
                            
                            final_contents[queue_name] = processed_items
                    
                    in_memory_time = time.time() - in_memory_start
                    logging.debug(f"[QUEUE_STREAM] In-memory queue processing took {in_memory_time:.3f}s")

                    # Batch all state COUNT queries into a single DB connection
                    count_batch_start = time.time()
                    all_db_states = list(COUNT_ONLY_QUEUES) + list(DB_FETCH_QUEUES)
                    try:
                        db_counts = get_item_counts_by_states(all_db_states)
                    except Exception as count_err:
                        logging.error(f"[QUEUE_STREAM] Batch count query failed: {count_err}")
                        db_counts = {s: 0 for s in all_db_states}
                    count_batch_time = time.time() - count_batch_start
                    logging.debug(f"[QUEUE_STREAM] Batch count query took {count_batch_time:.3f}s")

                    # Process count-only queues (Blacklisted, Unreleased, Collected)
                    count_only_start = time.time()
                    for queue_name in COUNT_ONLY_QUEUES:
                        queue_counts[queue_name] = db_counts.get(queue_name, 0)
                        final_contents[queue_name] = []

                    count_only_time = time.time() - count_only_start
                    logging.debug(f"[QUEUE_STREAM] Count-only queue processing took {count_only_time:.3f}s")

                    # Process database-backed queues (only Wanted and Final_Check now)
                    db_start = time.time()
                    for queue_name in DB_FETCH_QUEUES:
                        qp_start = time.time()
                        try:
                            total_count = db_counts.get(queue_name, 0)
                            queue_counts[queue_name] = total_count
                            hidden_count = max(0, total_count - ITEMS_LIMIT)
                            if hidden_count > 0:
                                hidden_counts[queue_name] = hidden_count

                            # We use page=1 because the stream always shows the top of the queue.
                            query_start = time.time()
                            limited_items_raw = get_all_media_items(state=queue_name, limit=ITEMS_LIMIT, columns=QUEUE_DISPLAY_COLUMNS)
                            limited_items = [dict(item) for item in limited_items_raw]
                            query_time = time.time() - query_start
                            if query_time > 0.1:
                                logging.debug(f"[QUEUE_STREAM] get_all_media_items for '{queue_name}' took {query_time:.3f}s")

                            # Process items in batches for better performance
                            processed_items = []
                            batch_start = time.time()
                            for i in range(0, len(limited_items), 25):  # Process 25 items at a time
                                batch = limited_items[i:i+25]
                                batch_processed = [process_item_for_response(item, queue_name, currently_processing_upgrade_id) for item in batch]
                                processed_items.extend(batch_processed)

                            batch_time = time.time() - batch_start
                            if batch_time > 0.1:
                                logging.debug(f"[QUEUE_STREAM] Processing {len(limited_items)} items for DB queue '{queue_name}' took {batch_time:.3f}s")

                            final_contents[queue_name] = processed_items

                            qp_time = time.time() - qp_start
                            if qp_time > 0.5:
                                logging.debug(f"[QUEUE_STREAM] Total processing for DB queue '{queue_name}' took {qp_time:.3f}s")

                        except Exception as db_err:
                             logging.error(f"Error fetching data for DB queue '{queue_name}': {db_err}")
                             final_contents[queue_name] = []
                             queue_counts[queue_name] = 0
                             hidden_counts[queue_name] = 0
                    
                    db_time = time.time() - db_start
                    logging.debug(f"[QUEUE_STREAM] Database queue processing took {db_time:.3f}s")
                    
                    # Calculate processing rate statistics
                    stats_start = time.time()
                    items_per_hour = get_items_processed_per_hour()
                    # Only include Wanted items that have reached their scheduled scrape_time.
                    ready_wanted_count = get_ready_wanted_items_count()
                    items_remaining = queue_counts.get('Scraping', 0) + ready_wanted_count
                    remaining_hours = (items_remaining / items_per_hour) if items_per_hour else None
                    remaining_scrape_time = _format_remaining_time(remaining_hours) if remaining_hours is not None else "Unknown"
                    stats_time = time.time() - stats_start
                    if stats_time > 0.1:
                        logging.debug(f"[QUEUE_STREAM] Statistics calculation took {stats_time:.3f}s")

                    data_to_send = {
                        "program_status": "Running",
                        "contents": final_contents,
                        "queue_counts": queue_counts,
                        "hidden_counts": hidden_counts,
                        "currently_processing_upgrade_id": currently_processing_upgrade_id,
                        "items_per_hour": items_per_hour,
                        "remaining_scrape_time": remaining_scrape_time,
                        "items_remaining": items_remaining
                    }

                    # Performance optimization: Only send if data has changed
                    ser_start = time.time()
                    current_hash = hash(json.dumps(data_to_send, sort_keys=True, default=str))
                    if current_hash == last_sent_hash:
                        consecutive_identical_sends += 1
                        if consecutive_identical_sends >= MAX_IDENTICAL_SENDS:
                            # Skip sending identical data, but send occasionally to keep connection alive
                            time.sleep(5)  # Wait longer when no changes
                            consecutive_identical_sends = 0  # Reset counter
                            continue
                    else:
                        last_sent_hash = current_hash
                        consecutive_identical_sends = 0

                    payload_str = json.dumps(data_to_send, default=str)
                    payload_size = len(payload_str)
                    ser_time = time.time() - ser_start
                    if ser_time > 0.1:
                        logging.debug(f"[QUEUE_STREAM] JSON serialization took {ser_time:.3f}s (payload size: {payload_size} bytes)")
                    
                    yield f"data: {payload_str}\n\n"

                    cycle_time = time.time() - cycle_start
                    if cycle_time > 1.0:  # Log if cycle took more than 1 second
                        logging.debug(f"[QUEUE_STREAM] Cycle {cycle_count} took {cycle_time:.3f}s")

                except Exception as e:
                    logging.error(f"Error in queue stream: {e}", exc_info=True)
                    yield f"data: {json.dumps({'error': str(e), 'program_status': 'Error'})}\n\n"
                
                # Dynamic refresh interval based on queue sizes
                total_items = sum(queue_counts.values()) if 'queue_counts' in locals() else 0
                if total_items > 500:
                    refresh_interval = 5.0  # Slower updates for very large queues
                elif total_items > 200:
                    refresh_interval = 3.5  # Moderate updates for large queues
                else:
                    refresh_interval = get_cached_setting('queue_refresh_interval', 2.5)

                # Further slow down updates for mobile clients
                if is_mobile_client:
                    refresh_interval = max(refresh_interval * 1.5, 4.0)  # Ensure at least ~4s interval
                
                time.sleep(refresh_interval if refresh_interval is not None else 2.5)

    return Response(generate(), mimetype='text/event-stream')

def get_paginated_items(queue_name, page, per_page):
    # Implementation of get_paginated_items function
    pass

def get_items_processed_per_hour():
    """Return the number of items collected in the last hour. Cached for 15 minutes to reduce DB load."""
    global _items_per_hour_cache
    current_ts = time.time()
    
    # Check cache first - this should prevent the slow query from running frequently
    if (_items_per_hour_cache['value'] is not None and
            current_ts - _items_per_hour_cache['timestamp'] < ITEMS_PER_HOUR_CACHE_DURATION):
        return _items_per_hour_cache['value']

    try:
        query_start = time.time()
        from database.core import get_db_connection  # Local import to avoid circular imports
        conn = get_db_connection()
        
        # Try the optimized query first
        query = (
            "SELECT COUNT(*) AS items_collected_last_hour "
            "FROM media_items "
            "WHERE collected_at >= datetime('now', 'localtime', '-1 hour');"
        )
        
        cursor = conn.execute(query)
        row = cursor.fetchone()
        items_per_hour = row['items_collected_last_hour'] if row else 0
        query_time = time.time() - query_start
        
        # If the query is still too slow, log a warning and suggest index
        if query_time > 2.0:
            logging.warning(f"[QUEUE_ROUTES] get_items_processed_per_hour database query took {query_time:.3f}s - SLOW! Consider adding index: CREATE INDEX idx_media_items_collected_at ON media_items(collected_at);")
            
            # Fallback: Use a simpler query that might be faster
            try:
                fallback_start = time.time()
                fallback_query = (
                    "SELECT COUNT(*) AS items_collected_last_hour "
                    "FROM media_items "
                    "WHERE collected_at > datetime('now', '-1 hour');"
                )
                cursor = conn.execute(fallback_query)
                row = cursor.fetchone()
                items_per_hour = row['items_collected_last_hour'] if row else 0
                fallback_time = time.time() - fallback_start
                logging.info(f"[QUEUE_ROUTES] Fallback query took {fallback_time:.3f}s (original: {query_time:.3f}s)")
            except Exception as fallback_e:
                logging.error(f"[QUEUE_ROUTES] Fallback query also failed: {fallback_e}")
                # Use a default value if both queries fail
                items_per_hour = 0
        elif query_time > 0.5:
            logging.warning(f"[QUEUE_ROUTES] get_items_processed_per_hour database query took {query_time:.3f}s - consider adding index on collected_at")
        else:
            logging.debug(f"[QUEUE_ROUTES] get_items_processed_per_hour database query took {query_time:.3f}s")
            
    except Exception as e:
        logging.error(f"Error calculating items processed per hour: {e}")
        items_per_hour = 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Update cache with the result
    _items_per_hour_cache.update({'value': items_per_hour, 'timestamp': current_ts})
    
    # Log cache update for debugging
    logging.debug(f"[QUEUE_ROUTES] Updated items_per_hour cache: {items_per_hour} items, cache valid for {ITEMS_PER_HOUR_CACHE_DURATION}s")
    
    return items_per_hour

def _format_remaining_time(hours_float: float) -> str:
    """Convert fractional hours to H:MM string format."""
    if hours_float is None or hours_float <= 0 or hours_float == float('inf'):
        return "Unknown"
    total_minutes = int(round(hours_float * 60))
    hrs, mins = divmod(total_minutes, 60)
    return f"{hrs}:{mins:02d}"

# ---------------------------------------------------------------------------
# Helper: count Wanted items whose *computed* scrape time has passed
# ---------------------------------------------------------------------------
_ready_wanted_cache: dict = {'value': 0, 'timestamp': 0}
READY_WANTED_CACHE_DURATION = 60  # seconds


def get_ready_wanted_items_count() -> int:
    """Return number of Wanted-queue entries that are ready to be scraped.

    Result is cached for READY_WANTED_CACHE_DURATION seconds to avoid
    iterating all Wanted items on every SSE cycle (every 2.5–5 s).
    """
    global _ready_wanted_cache
    current_ts = time.time()

    if (_ready_wanted_cache['timestamp'] > 0 and
            current_ts - _ready_wanted_cache['timestamp'] < READY_WANTED_CACHE_DURATION):
        return _ready_wanted_cache['value']

    try:
        count_start = time.time()
        from database.database_reading import get_all_media_items  # Lazy import
        now = datetime.now()
        ready_count = 0

        # Wanted queue tends to be small; fetching all rows is acceptable here.
        query_start = time.time()
        for raw in get_all_media_items(state="Wanted", limit=None):
            item = dict(raw)

            scrape_str = compute_scrape_time_cached(item)

            # Skip if scrape time is unknown or invalid
            if not scrape_str or scrape_str.startswith(("Unknown", "Error")):
                continue

            # Remove optional annotation like " (Alt Scrape Time)"
            cleaned = scrape_str.split(" (")[0]

            # Try common datetime formats that the helper may return
            parsed_dt = None
            for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed_dt = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue

            if parsed_dt and parsed_dt <= now:
                ready_count += 1

        query_time = time.time() - query_start
        total_time = time.time() - count_start
        if query_time > 0.1 or total_time > 0.2:
            logging.debug(f"[QUEUE_ROUTES] get_ready_wanted_items_count took {total_time:.3f}s (query: {query_time:.3f}s, found {ready_count} ready items)")

        _ready_wanted_cache.update({'value': ready_count, 'timestamp': current_ts})
        return ready_count
    except Exception as e:
        logging.error(f"Error counting ready Wanted items: {e}")
        return 0
