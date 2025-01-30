from flask import Blueprint, render_template, jsonify
import json
import os

performance_bp = Blueprint('performance', __name__)

@performance_bp.route('/dashboard')
def performance_dashboard():
    """Render the performance monitoring dashboard."""
    return render_template('performance/dashboard.html')

@performance_bp.route('/api/performance/log')
def get_performance_log():
    """Get the performance data from JSON file."""
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_file = os.path.join(log_dir, 'performance_log.json')
    
    try:
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                data = json.load(f)
                return jsonify(data)
        return jsonify({'error': 'No performance data available'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
