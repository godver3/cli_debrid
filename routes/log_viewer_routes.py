from flask import Blueprint, render_template, jsonify, request, Response
from .models import admin_required, onboarding_required
from datetime import datetime
import os

logs_bp = Blueprint('logs', __name__)

@logs_bp.route('/logs')
@admin_required
@onboarding_required
def logs():
    logs = get_recent_logs(500)  # Get the 500 most recent log entries
    return render_template('logs.html', logs=logs)

@logs_bp.route('/api/logs')
@admin_required
def api_logs():
    lines = request.args.get('lines', default=250, type=int)  # Default to 250 logs
    download = request.args.get('download', default='false').lower() == 'true'
    since = request.args.get('since')
      
    try:
        logs = get_recent_logs(lines, since)
        
        if download:
            log_content = ''
            for log in logs:
                log_content += f"{log['timestamp']} - {log['level']} - {log['message']}\n"
            
            return Response(
                log_content,
                mimetype="text/plain",
                headers={"Content-disposition": "attachment; filename=debug.log"}
            )
        else:
            return jsonify(logs)
    except Exception as e:
        return jsonify({'error': 'An error occurred while fetching logs'}), 500

def get_recent_logs(n, since=None):
    log_path = 'user/logs/debug.log'
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r') as f:
        logs = f.readlines()
    
    parsed_logs = [parse_log_line(log.strip()) for log in logs]
    
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            parsed_logs = [
                log for log in parsed_logs 
                if log['timestamp'] and datetime.fromisoformat(log['timestamp']) > since_dt
            ]
        except ValueError as e:
            return parsed_logs[-n:]
    
    return parsed_logs[-n:]  # Return at most n logs

def parse_log_line(line):
    parts = line.split(' - ', 3)
    if len(parts) == 4:
        timestamp, module, level, message = parts
        try:
            # Validate the timestamp
            datetime.fromisoformat(timestamp)
        except ValueError:
            timestamp = ''  # Set to empty string if invalid
        
        level = level.strip().lower()  # Normalize the level
        return {'timestamp': timestamp, 'level': level, 'message': f"{module} - {message}"}
    else:
        return {'timestamp': '', 'level': 'info', 'message': line}
    
def get_log_level(log_entry):
    if ' - DEBUG - ' in log_entry:
        return 'debug'
    elif ' - INFO - ' in log_entry:
        return 'info'
    elif ' - WARNING - ' in log_entry:
        return 'warning'
    elif ' - ERROR - ' in log_entry:
        return 'error'
    elif ' - CRITICAL - ' in log_entry:
        return 'critical'
    else:
        return 'info'  # Default to info if level can't be determined