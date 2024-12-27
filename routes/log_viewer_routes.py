from flask import Blueprint, render_template, jsonify, request, Response
from .models import admin_required, onboarding_required
from datetime import datetime
import os
from collections import deque

logs_bp = Blueprint('logs', __name__)

LOG_LEVELS = {
    'debug': 10,
    'info': 20,
    'warning': 30,
    'error': 40,
    'critical': 50
}

@logs_bp.route('/logs')
@admin_required
@onboarding_required
def logs():
    logs = get_recent_logs(100, level='all')  # Reduced from 500 to 100
    return render_template('logs.html', logs=logs)

@logs_bp.route('/api/logs')
@admin_required
def api_logs():
    lines = request.args.get('lines', default=250, type=int)  # Number of logs to retrieve
    download = request.args.get('download', default='false').lower() == 'true'
    since = request.args.get('since', default='')
    level = request.args.get('level', default='all').lower()  # New parameter for log level
    
    if level not in LOG_LEVELS and level != 'all':
        return jsonify({'error': 'Invalid log level provided'}), 400

    try:
        logs = get_recent_logs(lines, since, level)
        
        if download:
            log_content = '\n'.join([f"{log['timestamp']} - {log['level'].upper()} - {log['message']}" for log in logs])
            return Response(
                log_content,
                mimetype="text/plain",
                headers={"Content-disposition": "attachment; filename=debug.log"}
            )
        else:
            return jsonify(logs)
    except Exception as e:
        return jsonify({'error': 'An error occurred while fetching logs'}), 500

def get_recent_logs(n, since='', level='all'):
    # Get logs directory from environment variable with fallback
    logs_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_path = os.path.join(logs_dir, 'debug.log')
    
    if not os.path.exists(log_path):
        return []
    
    parsed_logs = []
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = deque(f, maxlen=n)
        
        for line in lines:
            parsed_line = parse_log_line(line.strip())
            if parsed_line and should_include_log(parsed_line, since, level):
                parsed_logs.append(parsed_line)
    except Exception as e:
        # Handle exception (e.g., log it)
        return []
    
    # Return logs in chronological order (oldest first)
    return parsed_logs

def should_include_log(parsed_line, since='', level='all'):
    if not parsed_line:
        return False

    if since:
        try:
            log_time = datetime.fromisoformat(parsed_line['timestamp'])
            since_dt = datetime.fromisoformat(since)
            if log_time <= since_dt:
                return False
        except ValueError:
            pass  # If we can't parse the timestamp, we'll include the log

    if level != 'all':
        log_level = LOG_LEVELS.get(parsed_line['level'], 0)
        filter_level = LOG_LEVELS.get(level, 0)
        if log_level < filter_level:
            return False

    return True

def parse_log_line(line):
    parts = line.split(' - ', 3)
    if len(parts) >= 4:
        timestamp, module, level, message = parts[:4]
        try:
            # Validate the timestamp
            datetime.fromisoformat(timestamp)
        except ValueError:
            return None  # Invalid timestamp
        level = level.strip().lower()
        return {'timestamp': timestamp, 'level': level, 'message': f"{module} - {message}"}
    else:
        return None  # Invalid log line format