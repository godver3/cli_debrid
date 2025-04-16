from flask import Blueprint, jsonify, current_app, request, make_response, Response
import requests
import logging
from datetime import datetime
import os
import sys
import traceback
from .program_operation_routes import get_program_runner
from functools import wraps
import time
import hashlib
import json
from pathlib import Path
import errno # Add errno for file locking checks
from apscheduler.triggers.interval import IntervalTrigger # Add this import
import pytz # Add pytz for timezone handling
import markdown

# Import notification functions
from .notifications import (
    get_all_notifications as get_notifications_data,
    mark_single_notification_read,
    mark_all_notifications_read as mark_all_read
)

# Global cache storage
_function_cache = {}
_function_last_modified = {}
_function_etags = {}

def generate_etag(data):
    """Generate an ETag for the given data using MD5"""
    # Use json.dumps with sort_keys=True for consistent hashing
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

def clear_cache():
    """Clear the update check cache"""
    try:
        # Check if check_for_update exists and has a clear method
        if 'check_for_update' in globals() and hasattr(check_for_update, 'clear'):
            check_for_update.clear()
        # Check if get_notifications exists and has a clear method
        if 'get_notifications' in globals() and hasattr(get_notifications, 'clear'):
            get_notifications.clear()
        #logging.info("Successfully cleared function caches")
    except NameError:
        # This might happen if the functions haven't been defined yet during import cycles
        logging.warning("Could not clear cache for functions - likely an import timing issue.")
    except Exception as e:
        logging.error(f"Error clearing cache: {str(e)}", exc_info=True)

def cache_for_seconds(seconds):
    """Enhanced caching decorator with ETag support, handles JSON data."""
    def decorator(func):
        cache_key = func.__name__
        if cache_key not in _function_cache:
            _function_cache[cache_key] = {}
            _function_last_modified[cache_key] = {}
            _function_etags[cache_key] = {}

        def clear():
            if cache_key in _function_cache:
                _function_cache[cache_key].clear()
                _function_last_modified[cache_key].clear()
                _function_etags[cache_key].clear()
                #logging.debug(f"Cache cleared for function: {cache_key}")


        @wraps(func)
        def wrapper(*args, **kwargs):
            # Skip caching for streaming responses
            if func.__name__.endswith('_stream'):
                return func(*args, **kwargs)

            now = time.time()
            # Create a hashable key from args and kwargs
            key_tuple = (args, frozenset(kwargs.items()))
            try:
                # Most args/kwargs should be hashable
                key = key_tuple
            except TypeError:
                 # Fallback for unhashable types: use stable repr
                 key = repr(key_tuple)


            # ETag check
            if_none_match = request.headers.get('If-None-Match')
            cached_etag = _function_etags[cache_key].get(key)
            if if_none_match and cached_etag and cached_etag == if_none_match:
                #logging.debug(f"ETag match for {func.__name__}, returning 304")
                return '', 304  # Not Modified

            # Cache check
            if key in _function_cache[cache_key]:
                cached_data, timestamp = _function_cache[cache_key][key]
                if now - timestamp < seconds:
                    #logging.debug(f"Cache hit for {func.__name__}. Age: {now - timestamp:.1f}s")
                    response = make_response(jsonify(cached_data)) # Assume 200 OK for cached data
                    # Use cached ETag if available, otherwise generate (should usually be cached)
                    etag_to_use = cached_etag or generate_etag(cached_data)
                    response.headers['ETag'] = etag_to_use
                    # Calculate remaining cache time for Cache-Control
                    remaining_time = max(0, int(seconds - (now - timestamp)))
                    response.headers['Cache-Control'] = f'private, max-age={remaining_time}'
                    return response
                # else: cache expired


            # Call the function if no valid cache or expired
            #logging.debug(f"Cache miss or expired for {func.__name__}. Calling function.")
            result = func(*args, **kwargs)

            # Check if the result indicates an error or is a direct Response
            # These should not be cached.
            is_response_like = hasattr(result, 'get_data') and hasattr(result, 'status_code')
            is_response_tuple = (isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int) and
                                 hasattr(result[0], 'get_data') and hasattr(result[0], 'status_code'))

            if is_response_like or is_response_tuple:
                #logging.debug(f"Not caching error/direct response for {func.__name__}")
                return result # Return the error/direct response without caching

            # --- Cache the successful JSON data result ---
            # Assume 'result' is JSON-serializable data if it wasn't an error/Response
            data_to_cache = result
            _function_cache[cache_key][key] = (data_to_cache, now)
            _function_last_modified[cache_key][key] = now

            # Generate and store ETag for the fresh data
            etag = generate_etag(data_to_cache)
            _function_etags[cache_key][key] = etag
            #logging.debug(f"Generated ETag for {func.__name__}: {etag}")


            # Create the response for the fresh data
            response = make_response(jsonify(data_to_cache)) # Assume 200 OK
            response.headers['ETag'] = etag
            response.headers['Cache-Control'] = f'private, max-age={seconds}'
            #logging.debug(f"Caching successful response for {func.__name__}")
            return response

        wrapper.clear = clear # Attach clear method
        return wrapper
    return decorator

base_bp = Blueprint('base', __name__, template_folder='../templates', static_folder='../static')

def get_current_branch():
    try:
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        branch_path = os.path.join(application_path, 'branch_id')
        
        with open(branch_path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        logging.error(f"Error reading branch_id file: {str(e)}")
        return 'main'  # Default to main if there's an error

def get_branch_suffix():
    branch = get_current_branch()
    return 'm' if branch == 'main' else 'd'

# Register the function to be available in templates
@base_bp.app_template_global()
def get_version_with_branch():
    try:
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        version_path = os.path.join(application_path, 'version.txt')
        
        with open(version_path, 'r') as f:
            version = f.read().strip()
        return f"{version}{get_branch_suffix()}"
    except Exception as e:
        logging.error(f"Error reading version: {str(e)}")
        return f"0.0.0{get_branch_suffix()}"

@base_bp.route('/api/release-notes', methods=['GET'])
def get_release_notes():
    try:
        # Get current branch from branch_id file
        current_branch = get_current_branch()
        
        # GitHub API endpoint for commits (using public API)
        api_url = f"https://api.github.com/repos/godver3/cli_debrid/commits"
        
        # Make request to GitHub API with a user agent (required by GitHub)
        headers = {
            'User-Agent': 'cli-debrid-app'
        }
        
        # Get the latest 10 commits for the current branch
        params = {
            'per_page': 10,
            'page': 1,
            'sha': current_branch  # Specify the branch to fetch commits from
        }
        
        response = requests.get(api_url, headers=headers, params=params)
        if response.status_code == 200:
            commits = response.json()
            if not commits:
                return jsonify({
                    'success': True,
                    'version': 'No Commits',
                    'name': f'No Commits Available ({current_branch} branch)',
                    'body': 'No commit history is available.',
                    'published_at': ''
                })
            
            # Format the commit messages into markdown
            commit_notes = []
            seen_versions = set()
            
            for commit in commits:
                message = commit['commit']['message']
                # Only process commits that start with version numbers (e.g., "0.5.35 -")
                if not message.strip().startswith(('0.', '1.', '2.')):
                    continue
                    
                # Extract version from message (assuming format "X.Y.Z - description")
                version = message.split(' - ')[0].strip()
                
                # Skip if we've already seen this version
                if version in seen_versions:
                    continue
                    
                seen_versions.add(version)
                date = datetime.strptime(commit['commit']['author']['date'], '%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%d %H:%M:%S')
                sha = commit['sha'][:7]  # Short SHA
                commit_notes.append(f"### {date} - {sha}\n{message}\n")
            
            body = "\n".join(commit_notes) if commit_notes else "No version commits available."
            
            return jsonify({
                'success': True,
                'version': f"Latest Commit: {commits[0]['sha'][:7]} ({current_branch} branch)",
                'name': f'Recent Changes - {current_branch} branch',
                'body': body,
                'published_at': commits[0]['commit']['author']['date']
            })
        else:
            logging.error(f"Failed to fetch commit history for branch {current_branch}. Status code: {response.status_code}")
            return jsonify({
                'success': False,
                'error': f'Failed to fetch commit history for branch {current_branch}'
            }), 500
            
    except Exception as e:
        logging.error(f"Error fetching commit history: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@base_bp.route('/api/check-update', methods=['GET'])
@cache_for_seconds(3600)  # Cache for 1 hour
def check_for_update():
    from utilities.settings import get_setting
    #logging.debug(f"get_setting('Debug', 'check_for_updates', True): {get_setting('Debug', 'check_for_updates', True)}")
    if not get_setting('Debug', 'check_for_updates', True):
        #logging.debug("Update check disabled by user setting")
        return {'success': True, 'update_available': False, 'message': 'Update check disabled by user setting'}

    try:
        # Get current branch and version
        current_branch = get_current_branch()
        current_version = get_version_with_branch()
        
        # GitHub API endpoint for commits
        api_url = f"https://api.github.com/repos/godver3/cli_debrid/commits"
        headers = {'User-Agent': 'cli-debrid-app'}
        params = {
            'per_page': 1,
            'page': 1,
            'sha': current_branch  # This ensures we only get commits from our current branch
        }
        
        #logging.debug(f"Making GitHub API request for update check on branch: {current_branch}")
        response = requests.get(api_url, headers=headers, params=params, timeout=5)
        if response.status_code == 200:
            commits = response.json()
            if not commits:
                logging.debug(f"No commits available for branch: {current_branch}")
                return {
                    'success': True,
                    'update_available': False,
                    'message': f'No commits available for branch: {current_branch}'
                }
            
            latest_commit = commits[0]
            latest_message = latest_commit['commit']['message']
            
            # Extract version from latest commit message if it starts with a version number
            latest_version = None
            if latest_message.strip().startswith(('0.', '1.', '2.')):
                latest_version = latest_message.split(' - ')[0].strip()
                # Add branch suffix to latest version for consistent comparison
                latest_version = f"{latest_version}{'d' if current_branch == 'dev' else 'm'}"
            
            # Compare versions properly by splitting into components
            def parse_version(version):
                # Remove the branch suffix before parsing version numbers
                version = version.rstrip('md')
                return [int(x) for x in version.split('.')]
            
            update_available = False
            if latest_version:
                try:
                    current_parts = parse_version(current_version)
                    latest_parts = parse_version(latest_version)
                    update_available = latest_parts > current_parts
                    #logging.debug(f"Version comparison on {current_branch} branch: current {current_parts} vs latest {latest_parts}")
                except Exception as e:
                    logging.error(f"Error parsing versions - current: {current_version}, latest: {latest_version}, error: {str(e)}")
                    return {
                        'success': False,
                        'error': f'Error parsing versions: {str(e)}'
                    }
            
            logging.debug(f"Update check complete on {current_branch} branch - current: {current_version}, latest: {latest_version}, update available: {update_available}")
            return {
                'success': True,
                'update_available': update_available,
                'current_version': current_version,
                'latest_version': latest_version,
                'branch': current_branch
            }
        else:
            logging.error(f"GitHub API request failed with status {response.status_code}")
            return {
                'success': False,
                'error': f'GitHub API request failed with status {response.status_code}'
            }
    except Exception as e:
        logging.error(f"Error checking for update: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }

@base_bp.route('/api/notifications')
@cache_for_seconds(30)  # Cache for 30 seconds
def get_notifications():
    """Get notifications via the centralized notification handler."""
    result_data, status_code = get_notifications_data()
    if status_code != 200:
        # If there was an error getting data, return the error response directly
        # This will bypass the cache mechanism in the updated decorator.
        return jsonify(result_data), status_code
    # If successful, return just the data dictionary for caching.
    return result_data

@base_bp.route('/api/notifications/mark-read', methods=['POST'])
def mark_notification_read():
    """Mark a notification as read via the centralized handler."""
    notification_id = request.json.get('id')
    if not notification_id:
        return jsonify({"error": "Notification ID required"}), 400
    
    result, status_code = mark_single_notification_read(notification_id)
    return jsonify(result), status_code

@base_bp.route('/api/notifications/mark-all-read', methods=['POST'])
def mark_all_notifications_read():
    """Mark all notifications as read via the centralized handler."""
    result, status_code = mark_all_read()
    return jsonify(result), status_code

@base_bp.route('/api/task-stream')
def task_stream():
    """Stream task updates. No caching for streaming endpoints."""
    def generate():
        while True:
            try:
                program_runner = get_program_runner()
                # Use scheduler's timezone if available, else UTC
                tz = program_runner.scheduler.timezone if program_runner and hasattr(program_runner, 'scheduler') else pytz.utc
                current_time_dt = datetime.now(tz)

                if program_runner is not None and program_runner.is_running() and hasattr(program_runner, 'scheduler'):
                    tasks_info = []
                    # Use scheduler lock for safety when iterating jobs
                    with program_runner.scheduler_lock:
                        jobs = program_runner.scheduler.get_jobs()
                        for job in jobs:
                            interval = None
                            if isinstance(job.trigger, IntervalTrigger):
                                interval = job.trigger.interval.total_seconds()

                            next_run_timestamp = None
                            if job.next_run_time:
                                # Ensure next_run_time is timezone-aware using scheduler's timezone
                                next_run_aware = job.next_run_time.astimezone(tz)
                                next_run_timestamp = next_run_aware.timestamp()

                            # Enabled = job has a next run time (is not paused indefinitely)
                            enabled = job.next_run_time is not None

                            # Running status is difficult to get accurately, omitting for now
                            # running = False # Placeholder

                            # Calculate time until next run in seconds
                            time_until_next = None
                            if enabled and next_run_timestamp:
                                time_until_next = max(0, next_run_timestamp - current_time_dt.timestamp())


                            tasks_info.append({
                                'name': job.id,
                                # 'last_run': Not easily available from APScheduler job object
                                'next_run': time_until_next, # Time until next run in seconds
                                'next_run_timestamp': next_run_timestamp, # Actual timestamp
                                'interval': interval,
                                'enabled': enabled,
                                #'running': running # Omitted for now
                            })

                    tasks_info.sort(key=lambda x: x['name'])

                    # Use the runner's pause state and reason
                    is_paused = program_runner.queue_paused
                    pause_reason = program_runner.pause_reason

                    data = {
                        'success': True,
                        'running': True, # ProgramRunner itself is running
                        'tasks': tasks_info,
                        'paused': is_paused,
                        'pause_reason': pause_reason
                    }
                else:
                    data = {
                        'success': True,
                        'running': False, # ProgramRunner is not running or scheduler missing
                        'tasks': [],
                        'paused': False,
                        'pause_reason': None
                    }

                yield f"data: {json.dumps(data, default=str)}\n\n"

            except Exception as e:
                logging.error(f"Error in task stream: {str(e)}", exc_info=True) # Log stack trace
                # Ensure we yield an error structure consistent with the success case
                yield f"data: {json.dumps({'success': False, 'running': False, 'tasks': [], 'paused': False, 'pause_reason': None, 'error': str(e)})}\n\n"

            time.sleep(1)  # Check for updates every second

    response = Response(generate(), mimetype='text/event-stream')
    response.headers.update({
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'X-Accel-Buffering': 'no'  # Disable buffering in Nginx
    })
    return response

# Helper function to safely create filename from path
def path_to_filename(page_path):
    if not page_path or page_path == '/':
        return 'root_index' # Or just 'index', 'home', etc.
    
    # Remove leading/trailing slashes and replace others with underscores
    filename = page_path.strip('/').replace('/', '_')
    
    # Basic sanitization (allow alphanumeric, underscore, hyphen)
    filename = "".join(c for c in filename if c.isalnum() or c in ('_', '-')).rstrip('_')
    
    return filename if filename else 'root_index' # Fallback if sanitization resulted in empty string

@base_bp.route('/api/help-content')
def get_help_content():
    page_path = request.args.get('page_path', '/')
    help_dir = os.path.join(current_app.root_path, '..', 'help_content') # Adjust path if needed
    
    filename_base = path_to_filename(page_path)
    specific_filepath = os.path.join(help_dir, f"{filename_base}.md")
    default_filepath = os.path.join(help_dir, 'default.md')

    filepath_to_use = default_filepath # Default to default.md
    if os.path.exists(specific_filepath):
        filepath_to_use = specific_filepath
    elif not os.path.exists(default_filepath):
         return jsonify(success=False, error="Help content directory or default file not found."), 500

    try:
        with open(filepath_to_use, 'r', encoding='utf-8') as f:
            md_content = f.read()
        
        html_content = markdown.markdown(md_content, extensions=['fenced_code', 'tables'])
        return jsonify(success=True, html=html_content)
        
    except Exception as e:
        current_app.logger.error(f"Error reading/parsing help file {filepath_to_use}: {e}")
        return jsonify(success=False, error="Could not load help content."), 500

# Clear check-update cache on startup
clear_cache()
logging.info("Cleared check-update cache on startup")