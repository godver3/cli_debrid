from flask import Blueprint, render_template, jsonify
from .models import user_required, onboarding_required
from datetime import datetime
from queue_manager import QueueManager
import logging

queues_bp = Blueprint('queues', __name__)

queue_manager = QueueManager()

@queues_bp.route('/')
@user_required
@onboarding_required
def index():
    queue_contents = queue_manager.get_queue_contents()
    for queue_name, items in queue_contents.items():
        if queue_name == 'Upgrading':
            for item in items:
                item['time_added'] = item.get('time_added', datetime.now())
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
                item['magnet_link'] = item.get('magnet_link', 'Unknown')


    upgrading_queue = queue_contents.get('Upgrading', [])
    return render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue)

@queues_bp.route('/api/queue_contents')
def api_queue_contents():
    contents = queue_manager.get_queue_contents()
    # Ensure wake counts are included for Sleeping queue items
    if 'Sleeping' in contents:
        for item in contents['Sleeping']:
            item['wake_count'] = queue_manager.get_wake_count(item['id'])

    return jsonify(contents)