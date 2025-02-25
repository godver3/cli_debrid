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

# Global cache storage
_function_cache = {}
_function_last_modified = {}
_function_etags = {}

def generate_etag(data):
    """Generate an ETag for the given data"""
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

def clear_cache():
    """Clear the update check cache"""
    try:
        # Get the check_for_update function and call its clear method
        if hasattr(check_for_update, 'clear'):
            check_for_update.clear()
            #logging.info("Successfully cleared update check cache")
    except Exception as e:
        logging.error(f"Error clearing cache: {str(e)}", exc_info=True)

def cache_for_seconds(seconds):
    """Enhanced caching decorator with ETag support"""
    def decorator(func):
        cache_key = func.__name__  # Use function name as the cache namespace
        if cache_key not in _function_cache:
            _function_cache[cache_key] = {}
            _function_last_modified[cache_key] = {}
            _function_etags[cache_key] = {}
        
        # Add clear method to the decorated function
        def clear():
            if cache_key in _function_cache:
                _function_cache[cache_key].clear()
                _function_last_modified[cache_key].clear()
                _function_etags[cache_key].clear()
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Skip caching for streaming responses
            if func.__name__.endswith('_stream'):
                return func(*args, **kwargs)
                
            now = time.time()
            key = (args, frozenset(kwargs.items()))
            
            #logging.debug(f"Cache request for {func.__name__} with key {key}")
            
            # Check if client sent If-None-Match header
            if_none_match = request.headers.get('If-None-Match')
            if if_none_match and key in _function_etags[cache_key] and _function_etags[cache_key][key] == if_none_match:
                #logging.debug(f"ETag match for {func.__name__}, returning 304")
                return '', 304  # Not Modified
            
            # Check if we have a valid cached value
            if key in _function_cache[cache_key]:
                result, timestamp = _function_cache[cache_key][key]
                if now - timestamp < seconds:
                    #logging.debug(f"Cache hit for {func.__name__}, returning cached value. Age: {now - timestamp:.1f}s")
                    if isinstance(result, Response):
                        return result
                    response = make_response(jsonify(result))
                    response.headers['ETag'] = _function_etags[cache_key].get(key, '')
                    response.headers['Cache-Control'] = f'private, max-age={seconds}'
                    return response
            
            # If no valid cached value, call the function
            result = func(*args, **kwargs)
            
            # Don't cache Response objects
            if isinstance(result, Response):
                #logging.debug(f"Not caching Response object for {func.__name__}")
                return result
                
            _function_cache[cache_key][key] = (result, now)
            _function_last_modified[cache_key][key] = now
            
            # Generate new ETag
            etag = str(hash((str(result), now)))
            _function_etags[cache_key][key] = etag
            
            response = make_response(jsonify(result))
            response.headers['ETag'] = etag
            response.headers['Cache-Control'] = f'private, max-age={seconds}'
            
            return response
            
        wrapper.clear = clear  # Attach clear method to the wrapper
        return wrapper
    return decorator

base_bp = Blueprint('base', __name__)

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
    from settings import get_setting
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
    """Get notifications from the notification file"""
    try:
        db_content_dir = os.getenv('USER_DB_CONTENT', '/user/db_content')
        notification_file = Path(db_content_dir) / 'notifications.json'
        
        if not notification_file.exists():
            # Create empty notification file if it doesn't exist
            notification_file.write_text(json.dumps({"notifications": []}))
            return jsonify({"notifications": []})
        
        with open(notification_file) as f:
            notifications = json.load(f)
            
        # Filter out expired notifications
        current_time = datetime.now().isoformat()
        if "notifications" in notifications:
            notifications["notifications"] = [
                n for n in notifications["notifications"]
                if "expires" not in n or n["expires"] > current_time
            ]
            
        return jsonify(notifications)
    except Exception as e:
        logging.error(f"Error reading notifications: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to read notifications", "notifications": []})

@base_bp.route('/api/notifications/mark-read', methods=['POST'])
def mark_notification_read():
    """Mark a notification as read"""
    try:
        notification_id = request.json.get('id')
        if not notification_id:
            return jsonify({"error": "Notification ID required"}), 400
            
        db_content_dir = os.getenv('USER_DB_CONTENT', '/user/db_content')
        notification_file = Path(db_content_dir) / 'notifications.json'
        
        if not notification_file.exists():
            return jsonify({"error": "No notifications found"}), 404
            
        with open(notification_file) as f:
            notifications = json.load(f)
            
        # Mark the notification as read
        found = False
        for notification in notifications.get("notifications", []):
            if notification["id"] == notification_id:
                notification["read"] = True
                found = True
                break
                
        if not found:
            return jsonify({"error": "Notification not found"}), 404
            
        # Write back to file
        with open(notification_file, 'w') as f:
            json.dump(notifications, f, indent=2)
            
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Error marking notification as read: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to mark notification as read"}), 500

@base_bp.route('/api/task-stream')
def task_stream():
    """Stream task updates. No caching for streaming endpoints."""
    def generate():
        while True:
            try:
                program_runner = get_program_runner()
                current_time = datetime.now().timestamp()
                
                if program_runner is not None and program_runner.is_running():
                    last_run_times = program_runner.last_run_times
                    task_intervals = program_runner.task_intervals
                    
                    tasks_info = []
                    for task, last_run in last_run_times.items():
                        interval = task_intervals.get(task, 0)
                        time_since_last_run = current_time - last_run
                        next_run = max(0, interval - time_since_last_run)
                        
                        if task in program_runner.enabled_tasks:
                            tasks_info.append({
                                'name': task,
                                'last_run': last_run,
                                'next_run': next_run if task not in program_runner.currently_running_tasks else 0,
                                'interval': interval,
                                'enabled': True,
                                'running': task in program_runner.currently_running_tasks
                            })
                    
                    tasks_info.sort(key=lambda x: x['name'])
                    
                    data = {
                        'success': True,
                        'running': True,
                        'tasks': tasks_info,
                        'paused': program_runner.queue_manager.is_paused() if hasattr(program_runner, 'queue_manager') else False,
                        'pause_reason': program_runner.pause_reason
                    }
                else:
                    data = {
                        'success': True,
                        'running': False,
                        'tasks': [],
                        'paused': False,
                        'pause_reason': None
                    }
                
                yield f"data: {json.dumps(data, default=str)}\n\n"
                    
            except Exception as e:
                logging.error(f"Error in task stream: {str(e)}")
                yield f"data: {json.dumps({'success': False, 'error': str(e)})}\n\n"
            
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

# Clear check-update cache on startup
clear_cache()
logging.info("Cleared check-update cache on startup")