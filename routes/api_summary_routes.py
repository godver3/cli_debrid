from flask import Blueprint, jsonify, request, render_template
import pickle
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
import re
from routes import admin_required

api_summary_bp = Blueprint('api_summary', __name__)

CACHE_FILE = 'db_content/api_summary_cache.pkl'

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
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache_data, f)

# Initialize the cache when the server starts
try:
    cache = load_cache()
except Exception as e:
    logging.error(f"Failed to load cache: {str(e)}. Starting with an empty cache.")
    cache = {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}

def summarize_api_calls(time_frame):
    log_path = 'logs/api_calls.log'
    summary = defaultdict(lambda: defaultdict(int))
    
    with open(log_path, 'r') as f:
        for line in f:
            match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - API Call: (\w+) (.*) - Domain: (.*)', line)
            if match:
                timestamp, method, url, domain = match.groups()
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f')
                
                if time_frame == 'hour':
                    key = dt.strftime('%Y-%m-%d %H:00')
                elif time_frame == 'day':
                    key = dt.strftime('%Y-%m-%d')
                elif time_frame == 'month':
                    key = dt.strftime('%Y-%m')
                
                summary[key][domain] += 1
    
    return dict(summary)

def get_cached_summary(time_frame):
    current_time = datetime.now()
    if time_frame in cache:
        last_update, data = cache[time_frame]
        if current_time - last_update < timedelta(hours=1):
            return data
    
    data = summarize_api_calls(time_frame)
    cache[time_frame] = (current_time, data)
    save_cache(cache)
    return data

def summarize_api_calls(time_frame):
    log_path = 'logs/api_calls.log'
    summary = defaultdict(lambda: defaultdict(int))
    
    with open(log_path, 'r') as f:
        for line in f:
            match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - API Call: (\w+) (.*) - Domain: (.*)', line)
            if match:
                timestamp, method, url, domain = match.groups()
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f')
                
                if time_frame == 'hour':
                    key = dt.strftime('%Y-%m-%d %H:00')
                elif time_frame == 'day':
                    key = dt.strftime('%Y-%m-%d')
                elif time_frame == 'month':
                    key = dt.strftime('%Y-%m')
                
                summary[key][domain] += 1
    
    return dict(summary)

def update_cache_with_new_entries():
    global cache
    log_path = 'logs/api_calls.log'
    last_processed_line = cache['last_processed_line']
    
    with open(log_path, 'r') as f:
        lines = f.readlines()[last_processed_line:]
        
    for i, line in enumerate(lines, start=last_processed_line):
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - API Call: (\w+) (.*) - Domain: (.*)', line)
        if match:
            timestamp, method, url, domain = match.groups()
            dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f')
            
            hour_key = dt.strftime('%Y-%m-%d %H:00')
            day_key = dt.strftime('%Y-%m-%d')
            month_key = dt.strftime('%Y-%m')
            
            for time_frame, key in [('hour', hour_key), ('day', day_key), ('month', month_key)]:
                if key not in cache[time_frame]:
                    cache[time_frame][key] = {}
                if domain not in cache[time_frame][key]:
                    cache[time_frame][key][domain] = 0
                cache[time_frame][key][domain] += 1
    
    cache['last_processed_line'] = last_processed_line + len(lines)
    save_cache(cache)

@api_summary_bp.route('/api_call_summary')
def api_call_summary():
    update_cache_with_new_entries()
    
    time_frame = request.args.get('time_frame', 'day')
    if time_frame not in ['hour', 'day', 'month']:
        time_frame = 'day'
    
    summary = cache[time_frame]
    
    # Get a sorted list of all domains
    all_domains = sorted(set(domain for period in summary.values() for domain in period))
    
    return render_template('api_call_summary.html', 
                           summary=summary, 
                           time_frame=time_frame,
                           all_domains=all_domains)

@api_summary_bp.route('/realtime_api_calls')
def realtime_api_calls():
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

def get_latest_api_calls(limit=100):
    calls = []
    with open('logs/api_calls.log', 'r') as log_file:
        for line in reversed(list(log_file)):
            parts = line.strip().split(' - ', 1)
            if len(parts) == 2:
                timestamp, message = parts
                call_info = message.split(': ', 1)
                if len(call_info) == 2:
                    method_and_url = call_info[1].split(' ', 1)
                    if len(method_and_url) == 2:
                        method, url = method_and_url
                        domain = url.split('/')[2] if url.startswith('http') else 'unknown'
                        calls.append({
                            'timestamp': timestamp,
                            'method': method,
                            'url': url,
                            'domain': domain,
                            'endpoint': '/'.join(url.split('/')[3:]) if url.startswith('http') else url,
                            'status_code': 'N/A'
                        })
                        if len(calls) >= limit:
                            break
    return calls