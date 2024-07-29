from flask import Flask, render_template, jsonify, redirect, url_for, request
import threading
import time
from queue_manager import QueueManager
import logging
import os
from settings import get_all_settings, set_setting, get_setting

app = Flask(__name__)
queue_manager = QueueManager()

# Disable Werkzeug request logging
log = logging.getLogger('werkzeug')
log.disabled = True

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Global variables for statistics
start_time = time.time()
total_processed = 0
successful_additions = 0
failed_additions = 0

@app.route('/')
def index():
    return redirect(url_for('statistics'))

@app.route('/statistics')
def statistics():
    uptime = time.time() - start_time
    stats = {
        'total_processed': total_processed,
        'successful_additions': successful_additions,
        'failed_additions': failed_additions,
        'uptime': uptime
    }
    return render_template('statistics.html', stats=stats)

@app.route('/queues')
def queues():
    queue_contents = queue_manager.get_queue_contents()
    #logging.debug(f"Queue contents fetched for web display: {queue_contents}")
    return render_template('queues.html', queue_contents=queue_contents)

@app.route('/logs')
def logs():
    logs = get_recent_logs(100)  # Get the last 100 log entries
    return render_template('logs.html', logs=logs)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        for key, value in request.form.items():
            section, option = key.split('.')
            set_setting(section, option, value)
        return redirect(url_for('settings'))

    settings = {}
    for section in ['Required', 'Additional', 'Scraping', 'Debug']:
        try:
            settings[f'{section} Settings'] = get_all_settings(section)
        except KeyError:
            logging.warning(f"Settings section '{section}' not found in config file.")
            settings[f'{section} Settings'] = {}

    return render_template('settings.html', settings=settings)

@app.route('/api/queue_contents')
def api_queue_contents():
    contents = queue_manager.get_queue_contents()
    #logging.debug(f"API queue contents: {contents}")
    return jsonify(contents)

@app.route('/api/stats')
def api_stats():
    uptime = time.time() - start_time
    return jsonify({
        'total_processed': total_processed,
        'successful_additions': successful_additions,
        'failed_additions': failed_additions,
        'uptime': uptime
    })

@app.route('/api/logs')
def api_logs():
    logs = get_recent_logs(100)
    return jsonify(logs)

def get_recent_logs(n):
    log_path = 'logs/info.log'
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r') as f:
        logs = f.readlines()
    return logs[-n:]

def run_server():
    app.run(debug=True, use_reloader=False, host='0.0.0.0')

def start_server():
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

# Function to update statistics
def update_stats(processed=0, successful=0, failed=0):
    global total_processed, successful_additions, failed_additions
    total_processed += processed
    successful_additions += successful
    failed_additions += failed
    #logging.debug(f"Stats updated - Total: {total_processed}, Successful: {successful_additions}, Failed: {failed_additions}")

if __name__ == '__main__':
    app.run(debug=True)