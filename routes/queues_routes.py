from flask import Blueprint, render_template, jsonify, request, Response
from .models import user_required, onboarding_required
from datetime import datetime, timedelta
from queues.queue_manager import QueueManager
import logging
from .program_operation_routes import program_is_running, program_is_initializing
from queues.initialization import get_initialization_status
from cli_battery.app.limiter import limiter
from utilities.settings import get_setting
import json
import time
from database.database_reading import get_all_media_items, get_item_count_by_state

queues_bp = Blueprint('queues', __name__)
queue_manager = QueueManager()

def init_limiter(app):
    """Initialize the rate limiter with the Flask app"""
    limiter.init_app(app)

def consolidate_items(items, limit=None):
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
            
    return result, original_count

@queues_bp.route('/')
@user_required
@onboarding_required
def index():
    queue_contents = queue_manager.get_queue_contents()
    program_running = program_is_running()
    program_initializing = program_is_initializing()
    
    for queue_name, items in queue_contents.items():
        if queue_name == 'Upgrading':
            for item in items:
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
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
                item['filled_by_torrent_id'] = item.get('filled_by_torrent_id', 'Unknown')
                item['progress'] = item.get('progress', 0)
                item['state'] = item.get('state', 'unknown')
                # Use the cached progress information instead of making direct API calls
                if item.get('filled_by_torrent_id') and item['filled_by_torrent_id'] != 'Unknown':
                    checking_queue = queue_manager.queues['Checking']
                    item['progress'] = checking_queue.get_torrent_progress(item['filled_by_torrent_id'])
                    item['state'] = checking_queue.get_torrent_state(item['filled_by_torrent_id'])
        elif queue_name == 'Sleeping':
            for item in items:
                item['wake_count'] = queue_manager.get_wake_count(item['id'])
        elif queue_name == 'Pending Uncached':
            for item in items:
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_magnet'] = item.get('filled_by_magnet', 'Unknown')
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')

    for queue_name, items in queue_contents.items():
        if queue_name == 'Unreleased':
            for item in items:
                if item['release_date'] is None:
                    item['release_date'] = "Unknown"


    upgrading_queue = queue_contents.get('Upgrading', [])
    return render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue, program_running=program_running, program_initializing=program_initializing)

@queues_bp.route('/api/queue_contents')
@user_required
@limiter.limit("1 per 5 seconds")
def api_queue_contents():
    queue_name = request.args.get('queue', None)
    queue_contents = queue_manager.get_queue_contents()
    program_running = program_is_running()
    program_initializing = program_is_initializing()
    
    # Get initialization status
    initialization_status = None
    if program_initializing:
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
    
    # If a specific queue is requested, only process that queue
    if queue_name and queue_name in queue_contents:
        items = queue_contents[queue_name]
        
        if queue_name == 'Blacklisted':
            items, total_count = consolidate_items(items)  # Remove limit
            return jsonify({
                "contents": {queue_name: items},
                "total_items": total_count,
                "original_count": total_count,  # Add original count
                "program_running": program_running,
                "program_initializing": program_initializing,
                "initialization_status": initialization_status
            })
        elif queue_name == 'Unreleased':
            items, total_count = consolidate_items(items)
            return jsonify({
                "contents": {queue_name: items},
                "total_items": total_count,
                "original_count": total_count,  # Add original count
                "program_running": program_running,
                "program_initializing": program_initializing,
                "initialization_status": initialization_status
            })
    
    # Process all queues with their specific logic
    queue_counts = {}  # Store original counts
    for queue_name, items in queue_contents.items():
        queue_counts[queue_name] = len(items)  # Store original count before any processing
        
        if queue_name == 'Upgrading':
            for item in items:
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
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
                item['filled_by_torrent_id'] = item.get('filled_by_torrent_id', 'Unknown')
                item['progress'] = item.get('progress', 0)
                item['state'] = item.get('state', 'unknown')
                if item.get('filled_by_torrent_id') and item['filled_by_torrent_id'] != 'Unknown':
                    checking_queue = queue_manager.queues['Checking']
                    item['progress'] = checking_queue.get_torrent_progress(item['filled_by_torrent_id'])
                    item['state'] = checking_queue.get_torrent_state(item['filled_by_torrent_id'])
        elif queue_name == 'Sleeping':
            for item in items:
                if 'wake_count' not in item or item['wake_count'] is None:
                    item['wake_count'] = queue_manager.get_wake_count(item['id'])

        # Pre-consolidate data for specific queues
        if queue_name == 'Blacklisted':
            items, _ = consolidate_items(items)  # Remove limit, we already have the count
            queue_contents[queue_name] = items
        elif queue_name == 'Unreleased':
            items, _ = consolidate_items(items)
            queue_contents[queue_name] = items

    return jsonify({
        "contents": queue_contents,
        "queue_counts": queue_counts,  # Add original counts
        "program_running": program_running,
        "program_initializing": program_initializing,
        "initialization_status": initialization_status
    })

def process_item_for_response(item, queue_name, currently_processing_upgrade_id=None):
    from utilities.settings import get_setting
    try:
        # Add scraping version settings to each item
        scraping_versions = get_setting('Scraping', 'versions', {})
        # Handle Infinity values in scraping_versions
        for version_key, version_data in scraping_versions.items():
            if isinstance(version_data, dict):
                for key, value in version_data.items():
                    if value == float('inf'):
                        version_data[key] = "Infinity"
                    elif value == float('-inf'):
                        version_data[key] = "-Infinity"

        item['scraping_versions'] = scraping_versions
        
        # --- START EDIT: Add processing flag ---
        item['is_processing'] = (queue_name == 'Upgrading' and item['id'] == currently_processing_upgrade_id)
        # --- END EDIT ---
        
        # --- START EDIT: Add force_priority flag ---
        item['is_force_priority'] = item.get('force_priority', False)
        # --- END EDIT ---
        
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
            # --- START EDIT: Calculate and format scrape time ---
            item['formatted_scrape_time'] = "Unknown" # Default value
            try:
                use_alt = get_setting('Debug', 'use_alternate_scrape_time_strategy', False)
                anchor_str = get_setting('Debug', 'alternate_scrape_time_24h', '00:00')
                now = datetime.now()
                release_date_str = item.get('release_date')
                airtime_str = item.get('airtime')
                version = item.get('version')
                item_type = item.get('type')

                scraping_versions = get_setting('Scraping', 'versions', {})
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
                    # Calculate the anchor datetime for today and tomorrow
                    today_anchor = now.replace(hour=anchor_time.hour, minute=anchor_time.minute, second=0, microsecond=0)
                    if now < today_anchor:
                        prev_anchor = today_anchor - timedelta(days=1)
                        next_anchor = today_anchor
                    else:
                        prev_anchor = today_anchor
                        next_anchor = today_anchor + timedelta(days=1)
                    # Item's anchor datetime
                    item_release_date = datetime.strptime(effective_release_date_str, '%Y-%m-%d').date()
                    item_anchor_dt = datetime.combine(item_release_date, anchor_time)
                    # If item's anchor is in the future, show that
                    if item_anchor_dt > now:
                        item['formatted_scrape_time'] = item_anchor_dt.strftime('%Y-%m-%d %I:%M %p') + ' (Alt Scrape Time)'
                    else:
                        # Show the next anchor after now
                        item['formatted_scrape_time'] = next_anchor.strftime('%Y-%m-%d %I:%M %p') + ' (Alt Scrape Time)'
                elif effective_release_date_str:
                    # Parse effective date
                    release_date = datetime.strptime(effective_release_date_str, '%Y-%m-%d').date()
                    if airtime_str:
                        try: airtime = datetime.strptime(airtime_str, '%H:%M:%S').time()
                        except ValueError:
                            try: airtime = datetime.strptime(airtime_str, '%H:%M').time()
                            except ValueError: airtime = datetime.strptime("00:00", '%H:%M').time()
                    else: airtime = datetime.strptime("00:00", '%H:%M').time()
                    release_datetime = datetime.combine(release_date, airtime)
                    offset_hours = 0.0
                    if item_type == 'movie':
                        movie_offset_setting = get_setting("Queue", "movie_airtime_offset", "0")
                        try: offset_hours = float(movie_offset_setting)
                        except (ValueError, TypeError): pass
                    elif item_type == 'episode':
                        episode_offset_setting = get_setting("Queue", "episode_airtime_offset", "0")
                        try: offset_hours = float(episode_offset_setting)
                        except (ValueError, TypeError): pass
                    effective_scrape_time = release_datetime + timedelta(hours=offset_hours)
                    item['formatted_scrape_time'] = effective_scrape_time.strftime('%Y-%m-%d %I:%M %p')
            except Exception as e:
                 logging.warning(f"Could not calculate scrape time for Wanted item {item.get('id')}: {e}")
                 item['formatted_scrape_time'] = "Error Calculating"
            # --- END EDIT ---
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
            if item.get('filled_by_torrent_id') and item['filled_by_torrent_id'] != 'Unknown':
                checking_queue = queue_manager.queues['Checking']
                item['progress'] = checking_queue.get_torrent_progress(item['filled_by_torrent_id'])
                item['state'] = checking_queue.get_torrent_state(item['filled_by_torrent_id'])
        elif queue_name == 'Sleeping':
            if 'wake_count' not in item or item['wake_count'] is None:
                item['wake_count'] = queue_manager.get_wake_count(item['id'])
        elif queue_name == 'Final_Check':
            # --- START EDIT: Use final_check_add_timestamp or fallback ---
            # Determine the timestamp to display
            display_timestamp = item.get('final_check_add_timestamp') or item.get('last_updated')
            
            # Add it to the item under a specific key for the frontend
            item['final_check_display_time'] = display_timestamp
            # --- END EDIT ---
            pass # No other specific action needed here other than ensuring it flows through
        
        # Ensure all values are JSON serializable
        for key, value in item.items():
            if isinstance(value, datetime):
                # Format the specific timestamp if it's a datetime object
                if key == 'final_check_display_time':
                    item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                # Format other datetime objects as before
                elif key not in ['scraping_versions']: # Avoid trying to format complex objects
                    item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
            elif value == float('inf'):
                item[key] = "Infinity"
            elif value == float('-inf'):
                item[key] = "-Infinity"
            elif not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                # Convert sets to lists if necessary
                if isinstance(value, set):
                     item[key] = list(value)
                else:
                     item[key] = str(value) # Fallback to string conversion
                
        return item
    except Exception as e:
        logging.error(f"Error processing item in queue {queue_name}: {str(e)}", exc_info=True) # Added exc_info
        # Return a safe version of the item
        return {
            'id': item.get('id', 'unknown'),
            'title': item.get('title', 'Error processing item'),
            'error': str(e)
        }

@queues_bp.route('/api/queue-stream')
@user_required
def queue_stream():
    """Stream queue updates, fetching limited DB data for large queues."""
    ITEMS_LIMIT = 500  # Fixed limit of 500 items per queue
    DB_FETCH_QUEUES = {"Wanted", "Blacklisted", "Unreleased", "Final_Check"}

    def generate():
        while True:
            try:
                # Get queue contents (only fetches in-memory queues now)
                in_memory_queue_contents = queue_manager.get_queue_contents()
                program_running = program_is_running()
                program_initializing = program_is_initializing()
                
                # --- START EDIT: Get currently processing upgrade ID ---
                currently_processing_upgrade_id = None
                if 'Upgrading' in queue_manager.queues:
                    currently_processing_upgrade_id = queue_manager.queues['Upgrading'].get_currently_processing_item_id()
                # --- END EDIT ---
                
                # Get initialization status
                initialization_status = None
                if program_initializing:
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
                
                # --- START EDIT: Fetch DB queues and counts ---
                final_contents = {}
                queue_counts = {}
                hidden_counts = {}

                # Process in-memory queues first
                for queue_name, items in in_memory_queue_contents.items():
                    if queue_name not in DB_FETCH_QUEUES:
                        total_count = len(items)
                        queue_counts[queue_name] = total_count
                        hidden_count = max(0, total_count - ITEMS_LIMIT)
                        if hidden_count > 0:
                            hidden_counts[queue_name] = hidden_count

                        limited_items = items[:ITEMS_LIMIT]
                        # --- START EDIT: Pass ID to processing function ---
                        processed_items = [process_item_for_response(item, queue_name, currently_processing_upgrade_id) for item in limited_items]
                        # --- END EDIT ---
                        final_contents[queue_name] = processed_items

                # Process database-backed queues
                for queue_name in DB_FETCH_QUEUES:
                    try:
                        # Get total count from DB
                        total_count = get_item_count_by_state(queue_name)
                        queue_counts[queue_name] = total_count
                        hidden_count = max(0, total_count - ITEMS_LIMIT)
                        if hidden_count > 0:
                            hidden_counts[queue_name] = hidden_count

                        # Fetch limited items from DB
                        limited_items_raw = get_all_media_items(state=queue_name, limit=ITEMS_LIMIT)
                        limited_items = [dict(item) for item in limited_items_raw] # Convert rows

                        # --- START EDIT: Pass ID to processing function ---
                        processed_items = [process_item_for_response(item, queue_name, currently_processing_upgrade_id) for item in limited_items]
                        # --- END EDIT ---

                        # Apply consolidation if needed
                        if queue_name in ['Blacklisted', 'Unreleased']:
                            processed_items, _ = consolidate_items(processed_items) # Use original consolidate function

                        final_contents[queue_name] = processed_items

                    except Exception as db_err:
                         logging.error(f"Error fetching data for DB queue '{queue_name}': {db_err}")
                         final_contents[queue_name] = [] # Provide empty list on error
                         queue_counts[queue_name] = 0
                         hidden_counts[queue_name] = 0
                # --- END EDIT ---

                data = {
                    "contents": final_contents,
                    "queue_counts": queue_counts,
                    "hidden_counts": hidden_counts,
                    "program_running": program_running,
                    "program_initializing": program_initializing,
                    "initialization_status": initialization_status,
                    # --- START EDIT: Include the ID in the stream data (optional, but could be useful for global indicators) ---
                    "currently_processing_upgrade_id": currently_processing_upgrade_id
                    # --- END EDIT ---
                }

                yield f"data: {json.dumps(data, default=str)}\n\n"

            except Exception as e:
                logging.error(f"Error in queue stream: {str(e)}")
                try:
                    yield f"data: {json.dumps({'success': False, 'error': str(e)})}\n\n"
                except Exception as inner_e:
                    # Failsafe: yield a hardcoded valid JSON string
                    yield 'data: {"success": false, "error": "Unknown streaming error"}\n\n'
            
            time.sleep(2.5)  # Check for updates every 2.5 seconds
    
    response = Response(generate(), mimetype='text/event-stream')
    response.headers.update({
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'X-Accel-Buffering': 'no'  # Disable buffering in Nginx
    })
    return response
