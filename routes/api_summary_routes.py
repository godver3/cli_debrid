from flask import Blueprint, jsonify, request, render_template
import pickle
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
import re
from routes import admin_required
import logging

api_summary_bp = Blueprint('api_summary', __name__)
real_time_api_bp = Blueprint('real_time_api', __name__)

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'api_summary_cache.pkl')

# Get logs directory from environment variable with fallback
LOGS_DIR = os.environ.get('USER_LOGS', '/user/logs')
API_LOG_FILE = os.path.join(LOGS_DIR, 'api_calls.log')

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                loaded_cache = pickle.load(f)
                # Ensure the cache has the correct structure
                if not isinstance(loaded_cache, dict):
                    raise ValueError("Loaded cache is not a dictionary")
                for time_frame in ['hour', 'day', 'month']:
                    if time_frame not in loaded_cache or not isinstance(loaded_cache[time_frame], dict):
                        loaded_cache[time_frame] = {}
                if 'last_processed_line' not in loaded_cache:
                    loaded_cache['last_processed_line'] = 0
                return loaded_cache
        except (EOFError, ValueError, pickle.UnpicklingError) as e:
            logging.warning(f"Error loading cache file: {str(e)}. Creating a new cache.")
    return {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}

def save_cache(cache_data):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache_data, f)

# Initialize the cache when the server starts
try:
    cache = load_cache()
except Exception as e:
    logging.error(f"Failed to load cache: {str(e)}. Starting with an empty cache.")
    cache = {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}

def update_cache_with_new_entries():
    global cache
    last_processed_line = cache['last_processed_line']
    
    logging.debug(f"Updating cache from line {last_processed_line}")
    
    with open(API_LOG_FILE, 'r') as f:
        lines = f.readlines()[last_processed_line:]
    
    if not lines:
        logging.debug("No new entries to process")
        return

    current_time = datetime.now()
    new_entries_count = 0
    for i, line in enumerate(lines, start=last_processed_line):
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - INFO - (\w+) ([^\s]+).*', line)
        if match:
            timestamp, method, url = match.groups()
            domain_match = re.match(r'([^/]+).*', url)
            domain = domain_match.group(1) if domain_match else "unknown"
            
            try:
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                
                hour_key = dt.strftime('%Y-%m-%d %H:00')
                day_key = dt.strftime('%Y-%m-%d')
                month_key = dt.strftime('%Y-%m')
                
                for time_frame, key in [('hour', hour_key), ('day', day_key), ('month', month_key)]:
                    if key not in cache[time_frame]:
                        cache[time_frame][key] = {}
                    if domain not in cache[time_frame][key]:
                        cache[time_frame][key][domain] = 0
                    cache[time_frame][key][domain] += 1
                new_entries_count += 1
            except ValueError as e:
                logging.warning(f"Error parsing timestamp '{timestamp}': {str(e)}")
    
    logging.debug(f"Processed {new_entries_count} new entries")

    # Remove old entries from cache
    for time_frame, delta in [('hour', timedelta(days=2)), ('day', timedelta(days=32)), ('month', timedelta(days=366))]:
        old_count = len(cache[time_frame])
        cache[time_frame] = {k: v for k, v in cache[time_frame].items() if parse_cache_key(k, time_frame) > current_time - delta}
        new_count = len(cache[time_frame])
        logging.debug(f"Removed {old_count - new_count} old entries from {time_frame} cache")
    
    cache['last_processed_line'] = last_processed_line + len(lines)
    save_cache(cache)
    logging.debug("Cache updated and saved")

def parse_cache_key(key, time_frame):
    if time_frame == 'hour':
        return datetime.strptime(key, '%Y-%m-%d %H:00')
    elif time_frame == 'day':
        return datetime.strptime(key, '%Y-%m-%d')
    elif time_frame == 'month':
        return datetime.strptime(key, '%Y-%m')
    else:
        raise ValueError(f"Unknown time frame: {time_frame}")

@api_summary_bp.route('/')
def index():
    logging.debug("API summary request received")
    update_cache_with_new_entries()
    
    time_frame = request.args.get('time_frame', 'day')
    if time_frame not in ['hour', 'day', 'month']:
        time_frame = 'day'
    
    logging.debug(f"Retrieving {time_frame} summary from cache")
    summary = cache[time_frame]
    
    # Get a sorted list of all domains
    all_domains = sorted(set(domain for period in summary.values() for domain in period))
    
    logging.debug(f"Returning summary for {len(summary)} periods and {len(all_domains)} domains")
    return render_template('api_call_summary.html', 
                           summary=summary, 
                           time_frame=time_frame,
                           all_domains=all_domains)

@real_time_api_bp.route('/')
def index():
    filter_domain = request.args.get('filter', '')
    latest_calls = get_latest_api_calls()
    
    if filter_domain:
        filtered_calls = [call for call in latest_calls if call['domain'] == filter_domain]
    else:
        filtered_calls = latest_calls
    
    all_domains = sorted(set(call['domain'] for call in latest_calls))
    
    return render_template('realtime_api_calls.html', 
                           calls=filtered_calls, 
                           filter=filter_domain,
                           all_domains=all_domains)

@api_summary_bp.route('/api/latest_calls')
def api_latest_calls():
    filter_domain = request.args.get('filter', '')
    latest_calls = get_latest_api_calls()
    
    if filter_domain:
        filtered_calls = [call for call in latest_calls if call['domain'] == filter_domain]
    else:
        filtered_calls = latest_calls
    
    return jsonify(filtered_calls)

@api_summary_bp.route('/clear_api_summary_cache', methods=['POST'])
@admin_required
def clear_api_summary_cache():
    global cache
    cache = {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}
    save_cache(cache)
    return jsonify({"status": "success", "message": "API summary cache cleared"})

@api_summary_bp.route('/api/summary')
def api_summary():
    update_cache_with_new_entries()
    
    time_frame = request.args.get('time_frame', 'day')
    if time_frame not in ['hour', 'day', 'month']:
        time_frame = 'day'
    
    summary = cache[time_frame]
    
    # Get a sorted list of all domains
    all_domains = sorted(set(domain for period in summary.values() for domain in period))
    
    # Transform the data for JSON response
    json_data = {
        'time_frame': time_frame,
        'domains': all_domains,
        'periods': {}
    }
    
    # Add data for each period
    for period, domains in summary.items():
        json_data['periods'][period] = {
            'by_domain': domains,
            'total': sum(domains.values())
        }
    
    return jsonify(json_data)

def get_latest_api_calls(limit=100):
    calls = []
    with open(API_LOG_FILE, 'r') as log_file:
        for line in reversed(list(log_file)):
            # Update parsing logic to match the actual log format
            parts = line.strip().split(' - ', 2)
            if len(parts) == 3:
                timestamp, level, message = parts
                if level.strip() == 'INFO':
                    # Message format: "METHOD domain/path"
                    method_and_url = message.split(' ', 1)
                    if len(method_and_url) == 2:
                        method, url = method_and_url
                        # Extract domain from the URL
                        domain = url.split('/')[0] if '/' in url else url
                        endpoint = '/'.join(url.split('/')[1:]) if '/' in url else ''
                        calls.append({
                            'timestamp': timestamp,
                            'method': method,
                            'url': url,
                            'domain': domain,
                            'endpoint': endpoint,
                            'status_code': 'N/A'
                        })
                        if len(calls) >= limit:
                            break
    return calls