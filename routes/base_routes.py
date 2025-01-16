from flask import Blueprint, jsonify, current_app
import requests
import logging
from datetime import datetime
import os
import sys
import traceback
from .program_operation_routes import get_program_runner
from functools import wraps
import time

def cache_for_seconds(seconds):
    """Cache the result of a function for the specified number of seconds."""
    def decorator(func):
        cache = {}
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            
            # Create a cache key from the function name and arguments
            key = (func.__name__, args, frozenset(kwargs.items()))
            
            # Check if we have a cached value and it's still valid
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < seconds:
                    #logging.debug(f"Cache hit for {func.__name__}")
                    return result
            
            # If no valid cached value, call the function
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result
            
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

@base_bp.route('/api/current-task', methods=['GET'])
@cache_for_seconds(2)
def get_current_task():
    try:
        # Get program runner using the getter function
        program_runner = get_program_runner()
                
        if program_runner is not None and program_runner.is_running():
            # Get the last run times and intervals
            last_run_times = program_runner.last_run_times
            task_intervals = program_runner.task_intervals
            current_time = datetime.now().timestamp()
            
            tasks_info = []
            for task, last_run in last_run_times.items():
                interval = task_intervals.get(task, 0)
                time_since_last_run = current_time - last_run
                next_run = max(0, interval - time_since_last_run)
                
                # Only include tasks that are in the enabled_tasks set
                if task in program_runner.enabled_tasks:
                    tasks_info.append({
                        'name': task,
                        'last_run': last_run,
                        'next_run': next_run if task not in program_runner.currently_running_tasks else 0,
                        'interval': interval,
                        'enabled': True,
                        'running': task in program_runner.currently_running_tasks
                    })
            
            # Sort tasks by name for consistent ordering
            tasks_info.sort(key=lambda x: x['name'])
            
            # logging.debug(f"Found {len(tasks_info)} active tasks")
            return jsonify({
                'success': True,
                'running': True,
                'tasks': tasks_info,
                'paused': program_runner.queue_manager.is_paused() if hasattr(program_runner, 'queue_manager') else False,
                'pause_reason': program_runner.pause_reason
            })
        else:
            # logging.debug("Program not running or not initialized")
            return jsonify({
                'success': True,
                'running': False,
                'tasks': [],
                'paused': False,
                'pause_reason': None
            })
    except Exception as e:
        logging.error(f"Error getting current task info: {str(e)}")
        logging.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500 

@base_bp.route('/api/check-update', methods=['GET'])
@cache_for_seconds(300)  # Cache for 5 minutes
def check_for_update():
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
            'sha': current_branch
        }
        
        response = requests.get(api_url, headers=headers, params=params)
        if response.status_code == 200:
            commits = response.json()
            if not commits:
                return jsonify({
                    'success': True,
                    'update_available': False,
                    'message': 'No commits available'
                })
            
            latest_commit = commits[0]
            latest_message = latest_commit['commit']['message']
            
            # Check if we're on the latest commit
            if getattr(sys, 'frozen', False):
                application_path = sys._MEIPASS
            else:
                application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            
            version_path = os.path.join(application_path, 'version.txt')
            
            with open(version_path, 'r') as f:
                current_version = f.read().strip()
            
            # Extract version from latest commit message if it starts with a version number
            latest_version = None
            if latest_message.strip().startswith(('0.', '1.', '2.')):
                latest_version = latest_message.split(' - ')[0].strip()
            
            # Compare versions properly by splitting into components
            def parse_version(version):
                return [int(x) for x in version.split('.')]
            
            update_available = False
            if latest_version:
                current_parts = parse_version(current_version)
                latest_parts = parse_version(latest_version)
                update_available = latest_parts > current_parts
            
            return jsonify({
                'success': True,
                'update_available': update_available,
                'current_version': current_version,
                'latest_version': latest_version,
                'branch': current_branch
            })
        else:
            return jsonify({
                'success': False,
                'error': f'Failed to fetch commit history. Status code: {response.status_code}'
            }), 500
            
    except Exception as e:
        logging.error(f"Error checking for updates: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500 