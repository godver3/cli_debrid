from flask import Blueprint, render_template, jsonify, request, Response
from .models import user_required, onboarding_required
from datetime import datetime
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
    consolidated = {}
    original_count = len(items)  # Keep track of original count
    
    # If limit is specified, only process that many items
    items_to_process = items[:limit] if limit else items
    
    for item in items_to_process:
        key = f"{item['title']}_{item.get('year', 'Unknown')}"
        if key not in consolidated:
            # Handle null or empty release dates
            release_date = item.get('release_date')
            if release_date is None or release_date == '' or release_date == 'null':
                release_date = 'Unknown'

            consolidated[key] = {
                'title': item['title'],
                'year': item.get('year', 'Unknown'),
                'versions': set(),
                'seasons': set(),
                'release_date': release_date,
                'physical_release_date': item.get('physical_release_date'),
                'scraping_versions': item.get('scraping_versions', {}),
                'version': item.get('version')  # Store the version for checking requirements
            }
        consolidated[key]['versions'].add(item.get('version', 'Unknown'))
        if item.get('type') == 'episode' and 'season_number' in item:
            consolidated[key]['seasons'].add(item['season_number'])
    
    # Convert sets to lists for JSON serialization
    result = []
    for key, data in consolidated.items():
        result.append({
            'title': data['title'],
            'year': data['year'],
            'versions': list(data['versions']),
            'seasons': list(data['seasons']),
            'release_date': data['release_date'],
            'physical_release_date': data['physical_release_date'],
            'scraping_versions': data['scraping_versions'],
            'version': data['version']  # Keep the version for checking requirements
        })
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

def process_item_for_response(item, queue_name):
    try:
        # Add scraping version settings to each item
        scraping_versions = get_setting('Scraping', 'versions', {})
        # Handle Infinity values in scraping_versions
        for version in scraping_versions.values():
            if isinstance(version, dict):
                for key, value in version.items():
                    if value == float('inf'):
                        version[key] = "Infinity"
                    elif value == float('-inf'):
                        version[key] = "-Infinity"
        
        item['scraping_versions'] = scraping_versions
        
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
            item['upgrades_found'] = queue_manager.queues['Upgrading'].upgrades_found.get(item['id'], 0)
        elif queue_name == 'Wanted':
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
    # --- START EDIT: Define queues that need DB fetching ---
    DB_FETCH_QUEUES = {"Wanted", "Blacklisted", "Unreleased", "Final_Check"}
    # --- END EDIT ---

    def generate():
        while True:
            try:
                # Get queue contents (only fetches in-memory queues now)
                in_memory_queue_contents = queue_manager.get_queue_contents()
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
                        processed_items = [process_item_for_response(item, queue_name) for item in limited_items]
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

                        processed_items = [process_item_for_response(item, queue_name) for item in limited_items]

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
                    # --- START EDIT: Use final_contents ---
                    "contents": final_contents,
                    # --- END EDIT ---
                    "queue_counts": queue_counts,
                    "hidden_counts": hidden_counts,
                    "program_running": program_running,
                    "program_initializing": program_initializing,
                    "initialization_status": initialization_status # Include initialization status here
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
