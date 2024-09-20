from flask import Blueprint, render_template, jsonify, request, Response
from .models import admin_required, onboarding_required
from datetime import datetime
import os

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
    since = request.args.get('since')
    level = request.args.get('level', default='all').lower()  # New parameter for log level
    
    if level not in LOG_LEVELS and level != 'all':
        return jsonify({'error': 'Invalid log level provided'}), 400
    
    try:
        logs = get_recent_logs(lines, since, level)
        
        if download:
            log_content = ''.join([f"{log['timestamp']} - {log['level']} - {log['message']}\n" for log in logs])
            return Response(
                log_content,
                mimetype="text/plain",
                headers={"Content-disposition": "attachment; filename=debug.log"}
            )
        else:
            return jsonify(logs)
    except Exception as e:
        return jsonify({'error': 'An error occurred while fetching logs'}), 500

def get_recent_logs(n, since=None, level='all'):
    log_path = '/user/logs/debug.log'
    if not os.path.exists(log_path):
        return []
    
    parsed_logs = []
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            buffer = bytearray()
            pointer = f.tell()
            while pointer >= 0 and len(parsed_logs) < n:
                f.seek(pointer)
                byte = f.read(1)
                if byte == b'\n':
                    if buffer:
                        line = buffer.decode('utf-8')[::-1]
                        parsed_line = parse_log_line(line)
                        if since:
                            try:
                                log_time = datetime.fromisoformat(parsed_line['timestamp'])
                                since_dt = datetime.fromisoformat(since)
                                if log_time > since_dt:
                                    if level == 'all' or LOG_LEVELS.get(parsed_line['level'], 0) >= LOG_LEVELS.get(level, 0):
                                        parsed_logs.append(parsed_line)
                            except ValueError:
                                if level == 'all' or LOG_LEVELS.get(parsed_line['level'], 0) >= LOG_LEVELS.get(level, 0):
                                    parsed_logs.append(parsed_line)
                        else:
                            if level == 'all' or LOG_LEVELS.get(parsed_line['level'], 0) >= LOG_LEVELS.get(level, 0):
                                parsed_logs.append(parsed_line)
                        buffer = bytearray()
                else:
                    buffer.extend(byte)
                pointer -= 1
        if buffer and len(parsed_logs) < n:
            line = buffer.decode('utf-8')[::-1]
            parsed_line = parse_log_line(line)
            if since:
                try:
                    log_time = datetime.fromisoformat(parsed_line['timestamp'])
                    since_dt = datetime.fromisoformat(since)
                    if log_time > since_dt:
                        if level == 'all' or LOG_LEVELS.get(parsed_line['level'], 0) >= LOG_LEVELS.get(level, 0):
                            parsed_logs.append(parsed_line)
                except ValueError:
                    if level == 'all' or LOG_LEVELS.get(parsed_line['level'], 0) >= LOG_LEVELS.get(level, 0):
                        parsed_logs.append(parsed_line)
            else:
                if level == 'all' or LOG_LEVELS.get(parsed_line['level'], 0) >= LOG_LEVELS.get(level, 0):
                    parsed_logs.append(parsed_line)
    except Exception as e:
        # Handle exception (e.g., log it)
        return []
    
    # **Reverse the list to have oldest logs first**
    return parsed_logs[:n][::-1]

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