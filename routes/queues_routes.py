from flask import Blueprint, render_template, jsonify
from .models import user_required, onboarding_required
from datetime import datetime
from queue_manager import QueueManager
import logging
from .program_operation_routes import program_is_running, program_is_initializing
from initialization import get_initialization_status

queues_bp = Blueprint('queues', __name__)

queue_manager = QueueManager()

@queues_bp.route('/')
@user_required
@onboarding_required
def index():
    queue_contents = queue_manager.get_queue_contents()
    program_running = program_is_running()
    program_initializing = program_is_initializing()
    
    for queue_name, items in queue_contents.items():
        if queue_name == 'Upgrading':
            for item in items:
                upgrade_info = queue_manager.queues['Upgrading'].upgrade_times.get(item['id'])
                if upgrade_info:
                    time_added = upgrade_info.get('time_added')
                    # logging.info(f"time_added: {time_added}")
                    if isinstance(time_added, str):
                        item['time_added'] = time_added
                    elif isinstance(time_added, datetime):
                        item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        item['time_added'] = str(time_added)
                else:
                    item['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                item['upgrades_found'] = item.get('upgrades_found', 0)
        elif queue_name == 'Checking':
            for item in items:
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')
        elif queue_name == 'Sleeping':
            for item in items:
                item['wake_count'] = queue_manager.get_wake_count(item['id'])
        elif queue_name == 'Pending Uncached':
            for item in items:
                item['time_added'] = item.get('time_added', datetime.now())
                item['filled_by_magnet'] = item.get('filled_by_magnet', 'Unknown')
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')

    for queue_name, items in queue_contents.items():
        if queue_name == 'Unreleased':
            for item in items:
                if item['release_date'] is None:
                    item['release_date'] = "Unknown"


    upgrading_queue = queue_contents.get('Upgrading', [])
    return render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue, program_running=program_running, program_initializing=program_initializing)

@queues_bp.route('/api/queue_contents')
@user_required
def api_queue_contents():
    queue_contents = queue_manager.get_queue_contents()
    program_running = program_is_running()
    program_initializing = program_is_initializing()
    initialization_status = get_initialization_status() if program_initializing else None
    
    for queue_name, items in queue_contents.items():
        if queue_name == 'Upgrading':
            for item in items:
                upgrade_info = queue_manager.queues['Upgrading'].upgrade_times.get(item['id'])
                if upgrade_info:
                    time_added = upgrade_info.get('time_added')
                    if isinstance(time_added, str):
                        item['time_added'] = time_added
                    elif isinstance(time_added, datetime):
                        item['time_added'] = time_added.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        item['time_added'] = str(time_added)
                else:
                    item['time_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                item['upgrades_found'] = queue_manager.queues['Upgrading'].upgrades_found.get(item['id'], 0)
        elif queue_name == 'Wanted':
            for item in items:
                if 'scrape_time' in item:
                    if item['scrape_time'] != "Unknown" and item['scrape_time'] != "Invalid date":
                        scrape_time = datetime.strptime(item['scrape_time'], '%Y-%m-%d %H:%M:%S')
                        item['formatted_scrape_time'] = scrape_time.strftime('%Y-%m-%d %I:%M %p')
                    else:
                        item['formatted_scrape_time'] = item['scrape_time']
        elif queue_name == 'Sleeping':
            for item in items:
                if 'wake_count' not in item or item['wake_count'] is None:
                    item['wake_count'] = queue_manager.get_wake_count(item['id'])

    return jsonify({
        "contents": queue_contents,
        "program_running": program_running,
        "program_initializing": program_initializing,
        "initialization_status": initialization_status
    })