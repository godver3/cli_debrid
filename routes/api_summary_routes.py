from flask import Blueprint, jsonify, request, render_template
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
import re
from routes import admin_required

api_summary_bp = Blueprint('api_summary', __name__)
real_time_api_bp = Blueprint('real_time_api', __name__)

# Get logs directory from environment variable with fallback
LOGS_DIR = os.environ.get('USER_LOGS', '/user/logs')
API_LOG_FILE = os.path.join(LOGS_DIR, 'api_calls.log')

def process_log_entries(time_frame='day'):
    current_time = datetime.now()
    summary = defaultdict(lambda: defaultdict(int))
    
    with open(API_LOG_FILE, 'r') as f:
        for line in f:
            match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - INFO - (\w+) ([^\s]+).*', line)
            if match:
                timestamp, method, url = match.groups()
                domain_match = re.match(r'([^/]+).*', url)
                domain = domain_match.group(1) if domain_match else "unknown"
                
                try:
                    dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                    
                    # Skip entries older than the retention period
                    if time_frame == 'hour' and dt < current_time - timedelta(days=2):
                        continue
                    elif time_frame == 'day' and dt < current_time - timedelta(days=32):
                        continue
                    elif time_frame == 'month' and dt < current_time - timedelta(days=366):
                        continue
                    
                    if time_frame == 'hour':
                        key = dt.strftime('%Y-%m-%d %H:00')
                    elif time_frame == 'day':
                        key = dt.strftime('%Y-%m-%d')
                    else:  # month
                        key = dt.strftime('%Y-%m')
                    
                    summary[key][domain] += 1
                    
                except ValueError as e:
                    logging.warning(f"Error parsing timestamp '{timestamp}': {str(e)}")
    
    return dict(summary)

@api_summary_bp.route('/')
def index():
    logging.debug("API summary request received")
    
    time_frame = request.args.get('time_frame', 'day')
    if time_frame not in ['hour', 'day', 'month']:
        time_frame = 'day'
    
    summary = process_log_entries(time_frame)
    
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

@api_summary_bp.route('/api/summary')
def api_summary():
    time_frame = request.args.get('time_frame', 'day')
    if time_frame not in ['hour', 'day', 'month']:
        time_frame = 'day'
    
    summary = process_log_entries(time_frame)
    
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