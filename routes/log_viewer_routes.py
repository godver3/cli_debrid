from flask import Blueprint, render_template, jsonify, request, Response, stream_with_context
from .models import admin_required, onboarding_required
from datetime import datetime
import os
from collections import deque
import requests
import gzip
import io
import logging
import re
import time
import json

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
    logs = get_recent_logs(1000, level='all')  # Reduced from 500 to 100
    return render_template('logs.html', logs=logs)

@logs_bp.route('/api/logs')
@admin_required
def api_logs():
    lines = request.args.get('lines', default=1000, type=int)  # Number of logs to retrieve
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

@logs_bp.route('/api/logs/share', methods=['POST'])
@admin_required
def share_logs():
    try:
        logging.info("Starting log collection for sharing")
        # Get all logs without any filtering
        logs = get_all_logs_for_upload(level='all')
        if not logs:
            return jsonify({'error': 'No logs found'}), 404

        logging.info(f"Collected {len(logs)} log entries, preparing for compression")
        
        # Format logs for sharing
        log_content = '\n'.join([f"{log['timestamp']} - {log['level'].upper()} - {log['message']}" for log in logs])
        
        # Compress the logs with maximum compression
        logging.info("Compressing logs")
        compressed_buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed_buffer, mode='wb', compresslevel=9) as gz:
            gz.write(log_content.encode('utf-8'))
        
        compressed_data = compressed_buffer.getvalue()
        logging.info(f"Compressed size: {len(compressed_data) / 1024:.2f}KB (Original: {len(log_content) / 1024:.2f}KB)")
        
        # Try multiple file sharing services in case one fails
        services = [
            ('https://0x0.st', upload_to_0x0),  # Primary service
            ('https://transfer.sh/', upload_to_transfer_sh)  # Fallback
        ]
        
        last_error = None
        for service_url, upload_func in services:
            try:
                logging.info(f"Attempting upload to {service_url}")
                file_url = upload_func(compressed_data)
                if file_url:
                    logging.info("Upload completed successfully")
                    return jsonify({
                        'success': True,
                        'url': file_url,
                        'service': service_url.replace('https://', '').rstrip('/'),
                        'originalSize': len(log_content),
                        'compressedSize': len(compressed_data)
                    })
            except Exception as e:
                last_error = str(e)
                logging.error(f"Failed to upload to {service_url}: {str(e)}")
                continue
        
        # If we get here, all services failed
        raise Exception(f"All upload services failed. Last error: {last_error}")
        
    except Exception as e:
        logging.error(f"Error during log sharing: {str(e)}")
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500

def upload_to_transfer_sh(data):
    """Upload to transfer.sh with timeout and proper headers"""
    files = {
        'file': ('debug.log.gz', data, 'application/gzip')
    }
    
    response = requests.post(
        'https://transfer.sh/',
        files=files,
        timeout=30,  # 30 second timeout
        headers={
            'Max-Days': '14',  # Keep for 14 days
            'User-Agent': 'CLI-Debrid Log Uploader'
        }
    )
    
    if response.status_code != 200:
        raise Exception(f'transfer.sh returned status code {response.status_code}')
    
    return response.text.strip()

def upload_to_0x0(data):
    """Upload to 0x0.st as fallback"""
    files = {
        'file': ('debug.log.gz', data, 'application/gzip')
    }
    
    response = requests.post(
        'https://0x0.st',
        files=files,
        timeout=30  # 30 second timeout
    )
    
    if response.status_code != 200:
        raise Exception(f'0x0.st returned status code {response.status_code}')
    
    return response.text.strip()

def get_all_logs_for_upload(level='all', max_lines=500000):
    """Get all logs from all rotated files in chronological order for upload"""
    # Get logs directory from environment variable with fallback
    logs_dir = os.environ.get('USER_LOGS', '/user/logs')
    base_log_path = os.path.join(logs_dir, 'debug.log')
    
    if not os.path.exists(base_log_path):
        return []
    
    # Get all log files and sort them correctly
    log_files = []
    for file in os.listdir(logs_dir):
        if file.startswith('debug.log'):
            log_files.append(file)
    
    def sort_key(filename):
        # Extract the number from the filename (e.g., 'debug.log.1' -> 1)
        match = re.search(r'debug\.log(?:\.(\d+))?$', filename)
        if not match or not match.group(1):
            return -1  # Current log file (no number) should be processed last
        return int(match.group(1))
    
    # Sort files in order: highest number (oldest) to debug.log (newest)
    log_files.sort(key=sort_key, reverse=True)  # Process oldest files first
    logging.info(f"Found {len(log_files)} log files to process in order: {', '.join(log_files)}")
    
    all_logs = []
    
    # Process each file and append its logs (oldest to newest)
    for log_file in log_files:
        file_path = os.path.join(logs_dir, log_file)
        try:
            file_size = os.path.getsize(file_path) / 1024  # Size in KB
            logging.info(f"Processing {log_file} (Size: {file_size:.2f}KB)")
            
            current_file_logs = []
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    parsed_line = parse_log_line(line.strip())
                    if parsed_line and should_include_log(parsed_line, since='', level=level):
                        current_file_logs.append(parsed_line)
            
            # Add this file's logs to the end of all_logs
            all_logs.extend(current_file_logs)
            logging.info(f"Processed {log_file}: added {len(current_file_logs)} entries. Total: {len(all_logs)}")
            
        except Exception as e:
            logging.error(f"Error reading log file {file_path}: {str(e)}")
            continue
    
    # Take the last max_lines entries
    if max_lines and len(all_logs) > max_lines:
        all_logs = all_logs[-max_lines:]
        logging.info(f"Trimmed to last {max_lines} entries")
    
    logging.info(f"Final log count: {len(all_logs)} entries")
    # Log first and last timestamps to verify order
    if all_logs:
        logging.info(f"First log timestamp: {all_logs[0]['timestamp']}")
        logging.info(f"Last log timestamp: {all_logs[-1]['timestamp']}")
    return all_logs

def get_recent_logs(n, since='', level='all'):
    """Get recent logs for the live viewer"""
    # Get logs directory from environment variable with fallback
    logs_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_path = os.path.join(logs_dir, 'debug.log')
    
    if not os.path.exists(log_path):
        return []
    
    parsed_logs = []
    try:
        # If filtering by level, read more lines to ensure we get enough of the desired level
        buffer_multiplier = 5 if level != 'all' else 1
        max_lines = n * buffer_multiplier

        with open(log_path, 'r', encoding='utf-8') as f:
            lines = deque(f, maxlen=max_lines)
        
        for line in lines:
            parsed_line = parse_log_line(line.strip())
            if parsed_line and should_include_log(parsed_line, since, level):
                parsed_logs.append(parsed_line)
                # Once we have enough logs of the desired level, we can stop
                if len(parsed_logs) >= n:
                    break
    except Exception as e:
        logging.error(f"Error reading current log file: {str(e)}")
        return []
    
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

@logs_bp.route('/api/logs/stream')
@admin_required
def stream_logs():
    def generate():
        last_timestamp = request.args.get('since', '')
        level = request.args.get('level', 'all').lower()
        first_batch = True

        while True:
            try:
                # On first connection, get all logs up to MAX_LOGS
                if first_batch:
                    logs = get_recent_logs(1000, level=level)
                    first_batch = False
                else:
                    # After first batch, only get new logs since last timestamp
                    logs = get_recent_logs(1000, since=last_timestamp, level=level)

                # Always send data, even if empty, to keep connection alive
                data = json.dumps(logs if logs else [])
                yield f"data: {data}\n\n"
                
                if logs:
                    last_timestamp = logs[-1]['timestamp']
                
                time.sleep(0.2)  # Check every 200ms
            except Exception as e:
                logging.error(f"Error in stream_logs: {str(e)}")
                # Send an empty array on error to keep connection alive
                yield "data: []\n\n"
                time.sleep(0.2)

    return Response(stream_with_context(generate()), 
                   mimetype='text/event-stream',
                   headers={
                       'Cache-Control': 'no-cache',
                       'Connection': 'keep-alive',
                       'X-Accel-Buffering': 'no'  # Disable proxy buffering
                   })