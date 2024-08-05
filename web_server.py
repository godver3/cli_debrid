from flask import Flask, render_template, jsonify, redirect, url_for, request, session
from flask_session import Session
import threading
import time
from queue_manager import QueueManager
import logging
import os
from settings import get_all_settings, set_setting, get_setting
from collections import OrderedDict
from web_scraper import web_scrape, process_media_selection, process_torrent_selection
from debrid.real_debrid import add_to_real_debrid
import re

app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
app.secret_key = '9683650475'
queue_manager = QueueManager()

# Disable Werkzeug request logging
#log = logging.getLogger('werkzeug')
#log.disabled = True

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

@app.route('/add_to_real_debrid', methods=['POST'])
def add_torrent_to_real_debrid():
    try:
        magnet_link = request.form.get('magnet_link')
        if not magnet_link:
            return jsonify({'error': 'No magnet link provided'}), 400

        result = add_to_real_debrid(magnet_link)
        if result:
            return jsonify({'message': 'Torrent added to Real-Debrid successfully'})
        else:
            return jsonify({'error': 'Failed to add torrent to Real-Debrid'}), 500

    except Exception as e:
        logging.error(f"Error adding torrent to Real-Debrid: {str(e)}")
        return jsonify({'error': 'An error occurred while adding to Real-Debrid'}), 500

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

@app.route('/select_media', methods=['POST'])
def select_media():
    try:
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        media_type = request.form.get('media_type')
        season = request.form.get('season')
        episode = request.form.get('episode')

        season = int(season) if season and season.isdigit() else None
        episode = int(episode) if episode and episode.isdigit() else None

        logging.info(f"Selecting media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}")

        torrent_results = process_media_selection(media_id, title, year, media_type, season, episode)
        
        return jsonify({'torrent_results': torrent_results})
    except Exception as e:
        logging.error(f"Error in select_media: {str(e)}")
        return jsonify({'error': 'An error occurred while selecting media'}), 500

@app.route('/scraper', methods=['GET', 'POST'])
def scraper():
    if request.method == 'POST':
        search_term = request.form.get('search_term')
        if search_term:
            session['search_term'] = search_term  # Store the search term in the session
            results = web_scrape(search_term)
            return jsonify(results)
        else:
            return jsonify({'error': 'No search term provided'})
    
    return render_template('scraper.html')

@app.route('/add_torrent', methods=['POST'])
def add_torrent():
    torrent_index = int(request.form.get('torrent_index'))
    torrent_results = session.get('torrent_results', [])
    
    if 0 <= torrent_index < len(torrent_results):
        result = process_torrent_selection(torrent_index, torrent_results)
        if result['success']:
            return render_template('scraper.html', success_message=result['message'])
        else:
            return render_template('scraper.html', error=result['error'])
    else:
        return render_template('scraper.html', error="Invalid torrent selection")


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

@app.route('/queues')
def queues():
    queue_contents = queue_manager.get_queue_contents()
    upgrading_queue = queue_contents['Upgrading']
    logging.info(f"Rendering queues page. UpgradingQueue size: {len(upgrading_queue)}")
    return render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue)

@app.route('/api/queue_contents')
def api_queue_contents():
    contents = queue_manager.get_queue_contents()
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