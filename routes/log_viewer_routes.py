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
import threading
import uuid

logs_bp = Blueprint('logs', __name__)

LOG_LEVELS = {
    'debug': 10,
    'info': 20,
    'warning': 30,
    'error': 40,
    'critical': 50
}

# Global dictionary to store upload status
upload_tasks = {}

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
        
        # Generate a unique task ID
        task_id = str(uuid.uuid4())
        
        # Initialize the task status
        upload_tasks[task_id] = {
            'status': 'collecting',
            'progress': 0,
            'url': None,
            'error': None,
            'timestamp': time.time()
        }
        
        # Start the background upload task
        threading.Thread(
            target=process_log_upload,
            args=(task_id,),
            daemon=True
        ).start()
        
        # Return immediately with the task ID
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Log upload started in background'
        })
        
    except Exception as e:
        logging.error(f"Error starting log sharing: {str(e)}")
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500

@logs_bp.route('/api/logs/share/status/<task_id>', methods=['GET'])
@admin_required
def check_share_status(task_id):
    # Check if the task ID exists
    if task_id not in upload_tasks:
        return jsonify({'error': 'Task ID not found'}), 404
    
    # Get the current status
    task_info = upload_tasks[task_id]
    
    # Clean up completed tasks older than 1 hour
    current_time = time.time()
    for old_task_id in list(upload_tasks.keys()):
        if (old_task_id != task_id and 
            upload_tasks[old_task_id]['status'] in ['completed', 'failed'] and
            current_time - upload_tasks[old_task_id]['timestamp'] > 3600):
            del upload_tasks[old_task_id]
    
    # Return the status
    return jsonify({
        'status': task_info['status'],
        'progress': task_info['progress'],
        'url': task_info['url'],
        'error': task_info['error'],
        'timestamp': task_info['timestamp']
    })

def process_log_upload(task_id):
    """Background task to process log upload"""
    try:
        # Update status
        upload_tasks[task_id]['status'] = 'collecting'
        upload_tasks[task_id]['progress'] = 10
        
        # Get all logs without any filtering
        logs = get_all_logs_for_upload(level='all')
        if not logs:
            upload_tasks[task_id]['status'] = 'failed'
            upload_tasks[task_id]['error'] = 'No logs found'
            return
        
        upload_tasks[task_id]['progress'] = 20
        logging.info(f"Task {task_id}: Collected {len(logs)} log entries, preparing for compression")
        
        # Format logs for sharing
        log_content = '\n'.join([f"{log['timestamp']} - {log['level'].upper()} - {log['message']}" for log in logs])
        
        # Compress the logs with maximum compression
        upload_tasks[task_id]['status'] = 'compressing'
        upload_tasks[task_id]['progress'] = 30
        logging.info(f"Task {task_id}: Compressing logs")
        
        compressed_buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed_buffer, mode='wb', compresslevel=9) as gz:
            gz.write(log_content.encode('utf-8'))
        
        compressed_data = compressed_buffer.getvalue()
        compressed_size_kb = len(compressed_data) / 1024
        original_size_kb = len(log_content) / 1024
        logging.info(f"Task {task_id}: Compressed size: {compressed_size_kb:.2f}KB (Original: {original_size_kb:.2f}KB)")
        
        upload_tasks[task_id]['progress'] = 40
        
        # Check if the compressed size exceeds typical limits
        if compressed_size_kb > 10000:  # 10MB file size limit for oshi.at
            logging.warning(f"Task {task_id}: Compressed log size ({compressed_size_kb:.2f}KB) may exceed oshi.at's limits")
            
            # Try with a reduced set of logs (only errors and warnings)
            upload_tasks[task_id]['status'] = 'reducing'
            try:
                logging.info(f"Task {task_id}: Attempting to create a reduced log set due to size constraints")
                reduced_logs = get_all_logs_for_upload(level='warning', max_lines=100000)
                if reduced_logs:
                    reduced_content = '\n'.join([f"{log['timestamp']} - {log['level'].upper()} - {log['message']}" for log in reduced_logs])
                    
                    compressed_buffer = io.BytesIO()
                    with gzip.GzipFile(fileobj=compressed_buffer, mode='wb', compresslevel=9) as gz:
                        gz.write(reduced_content.encode('utf-8'))
                    
                    compressed_data = compressed_buffer.getvalue()
                    compressed_size_kb = len(compressed_data) / 1024
                    logging.info(f"Task {task_id}: Reduced log set: {len(reduced_logs)} entries, size: {compressed_size_kb:.2f}KB")
                    
                    # Still too large
                    if compressed_size_kb > 10000:
                        upload_tasks[task_id]['status'] = 'failed'
                        upload_tasks[task_id]['error'] = 'Log file is too large even after reduction'
                        return
            except Exception as e:
                logging.error(f"Task {task_id}: Error creating reduced logs: {str(e)}")
                upload_tasks[task_id]['status'] = 'failed'
                upload_tasks[task_id]['error'] = f'Error creating reduced logs: {str(e)}'
                return
        
        # Update status to uploading
        upload_tasks[task_id]['status'] = 'uploading'
        upload_tasks[task_id]['progress'] = 50
        
        try:
            logging.info(f"Task {task_id}: Attempting upload to oshi.at")
            file_url = upload_to_oshi(compressed_data, task_id)
            
            if file_url:
                logging.info(f"Task {task_id}: Upload completed successfully to oshi.at")
                upload_tasks[task_id]['status'] = 'completed'
                upload_tasks[task_id]['progress'] = 100
                upload_tasks[task_id]['url'] = file_url
            else:
                upload_tasks[task_id]['status'] = 'failed'
                upload_tasks[task_id]['error'] = 'Upload completed but no URL returned'
        except Exception as e:
            logging.error(f"Task {task_id}: Failed to upload to oshi.at: {str(e)}")
            upload_tasks[task_id]['status'] = 'failed'
            upload_tasks[task_id]['error'] = f'Upload failed: {str(e)}'
            
    except Exception as e:
        logging.error(f"Task {task_id}: Error during log sharing: {str(e)}")
        upload_tasks[task_id]['status'] = 'failed'
        upload_tasks[task_id]['error'] = f'An error occurred: {str(e)}'
    
    # Update timestamp regardless of outcome
    upload_tasks[task_id]['timestamp'] = time.time()

def upload_to_oshi(data, task_id=None):
    """Upload to oshi.at with 24 hour expiration"""
    url = 'https://oshi.at'
    
    files = {
        'f': ('debug.log.gz', data, 'application/gzip')
    }
    
    # Set expiry to 24 hours in minutes (1440 minutes)
    form_data = {
        'expire': '1440'  # 24 hours in minutes
    }
    
    logging.info(f"Uploading to oshi.at: file size {len(data)/1024:.2f}KB, mime: application/gzip")
    
    try:
        # Update progress if task_id is provided
        if task_id and task_id in upload_tasks:
            upload_tasks[task_id]['progress'] = 60
        
        response = requests.post(
            url,
            files=files,
            data=form_data,
            timeout=240  # 4 minute timeout to handle larger uploads
        )
        
        # Update progress if task_id is provided
        if task_id and task_id in upload_tasks:
            upload_tasks[task_id]['progress'] = 90
        
        logging.info(f"Response status: {response.status_code}")
        
        if response.status_code != 200:
            logging.error(f"Upload error response from oshi.at: {response.text[:500]}")
            response.raise_for_status()
        
        # oshi.at returns a text response with multiple lines:
        # Line 1: Management URL (https://oshi.at/filename/random.extension)
        # Line 2: Direct download URL (https://oshi.at/DL/filename/random.extension)
        # Line 3: Deletion URL
        # Line 4: Expiration info
        lines = response.text.strip().split('\n')
        
        logging.info(f"oshi.at response lines: {len(lines)}")
        
        # Check if the response has the expected format and extract direct download URL from line 2
        if len(lines) >= 2 and 'DL:' in lines[1]:
            direct_download_url = lines[1].strip().replace('DL: ', '')
            logging.info(f"Extracted direct download URL: {direct_download_url}")
            return direct_download_url
        elif len(lines) >= 1 and 'http' in lines[0]:
            # Fallback to the management URL if we can't find a direct download URL
            management_url = lines[0].strip()
            logging.info(f"Using management URL as fallback: {management_url}")
            return management_url
        else:
            logging.error(f"Unexpected response format from oshi.at: {response.text[:500]}")
            raise Exception("Failed to parse oshi.at response")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Request error during upload to oshi.at: {str(e)}")
        raise Exception(f"Upload failed: {str(e)}")

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
                lines = deque(f, maxlen=max_lines)
                current_log = None
                for line in lines:
                    line = line.strip()
                    parsed_line = parse_log_line(line)
                    
                    if parsed_line:
                        # If we have a current log, append it before starting new one
                        if current_log and should_include_log(current_log, since='', level=level):
                            current_file_logs.append(current_log)
                        current_log = parsed_line
                    elif current_log:
                        # This is a continuation line - append to current message
                        current_log['message'] += '\n' + line
                
                # Don't forget to append the last log if it exists
                if current_log and should_include_log(current_log, since='', level=level):
                    current_file_logs.append(current_log)
            
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
        
        current_log = None
        for line in lines:
            line = line.strip()
            parsed_line = parse_log_line(line)
            
            if parsed_line:
                # If we have a current log, append it before starting new one
                if current_log and should_include_log(current_log, since, level):
                    parsed_logs.append(current_log)
                current_log = parsed_line
            elif current_log:
                # This is a continuation line - append to current message
                current_log['message'] += '\n' + line
        
        # Don't forget to append the last log if it exists
        if current_log and should_include_log(current_log, since, level):
            parsed_logs.append(current_log)
            
        # Trim to the desired number of logs
        if len(parsed_logs) > n:
            parsed_logs = parsed_logs[-n:]
            
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
    # First split to get timestamp
    parts = line.split(' - ', 1)
    if len(parts) < 2:
        return None  # Invalid log line format
        
    timestamp, remainder = parts
    
    try:
        # Validate the timestamp
        datetime.fromisoformat(timestamp)
    except ValueError:
        return None  # Invalid timestamp
        
    # Split the remainder to get module and level
    parts = remainder.split(' - ', 2)
    if len(parts) < 3:
        return None  # Invalid log line format
        
    module, level, message = parts
    level = level.strip().lower()
    
    return {
        'timestamp': timestamp,
        'level': level,
        'message': f"{module} - {message}"
    }

@logs_bp.route('/api/logs/stream')
@admin_required
def stream_logs():
    def generate():
        last_timestamp = request.args.get('since', '')
        level = request.args.get('level', 'all').lower()
        # Get client requested interval with bounds, default to 100ms instead of 200ms
        # interval = max(0.05, min(2.0, float(request.args.get('interval', '0.1'))))
        # Hard code to 50ms
        interval = 0.05
        first_batch = True
        #logging.debug(f"Starting log stream with interval {interval}s, level {level}")

        # Pre-initialize json encoder for performance
        json_encoder = json.JSONEncoder()
        
        while True:
            try:
                start_time = time.time()
                
                # On first connection, get all logs up to MAX_LOGS
                if first_batch:
                    logs = get_recent_logs(1000, level=level)
                    first_batch = False
                    #logging.debug(f"First batch: Found {len(logs)} logs")
                else:
                    # After first batch, only get new logs since last timestamp
                    logs = get_recent_logs(1000, since=last_timestamp, level=level)

                # Always send data, even if empty, to keep connection alive
                data = json_encoder.encode({
                    'logs': logs if logs else [],
                    'serverTime': start_time  # Use start time for more accurate latency
                })
                yield f"data: {data}\n\n"
                
                if logs:
                    last_timestamp = logs[-1]['timestamp']
                
                # Calculate how long we should sleep
                elapsed = time.time() - start_time
                sleep_time = max(0, interval - elapsed)  # Don't sleep if we've already taken longer than interval
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    
            except Exception as e:
                logging.error(f"Error in stream_logs: {str(e)}")
                # Send an empty array on error to keep connection alive
                yield f"data: {json_encoder.encode({'logs': [], 'serverTime': time.time()})}\n\n"
                time.sleep(interval)

    return Response(
        stream_with_context(generate()), 
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable proxy buffering
        }
    )