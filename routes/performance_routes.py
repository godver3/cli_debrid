from flask import Blueprint, render_template, jsonify, request
import json
import os
from datetime import datetime, timedelta

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
    
    # Get optional time range parameters
    hours = request.args.get('hours', type=int, default=24)
    limit = request.args.get('limit', type=int, default=1000)
    entry_type = request.args.get('type', type=str)  # Optional type filter
    metric_type = request.args.get('metric', type=str)  # Optional metric filter
    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    
    try:
        entries = []
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        # Skip if doesn't have timestamp
                        if 'timestamp' not in entry:
                            continue
                            
                        # Apply type filter if specified
                        if entry_type and entry.get('type') != entry_type:
                            continue
                            
                        # Apply metric filter if specified
                        if metric_type and ('metrics' not in entry or metric_type not in entry['metrics']):
                            continue
                            
                        entry_time = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00'))
                        if entry_time >= cutoff_time:
                            entries.append(entry)
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        continue
        
        # Sort entries by timestamp in descending order (newest first)
        entries.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        # Take only the most recent entries up to the limit
        entries = entries[:limit]
        
        # Re-sort in ascending order for display
        entries.sort(key=lambda x: x.get('timestamp', ''))
        
        # Get system info for metadata
        metadata = {
            'log_start_time': entries[0]['timestamp'] if entries else None,
            'log_end_time': entries[-1]['timestamp'] if entries else None,
            'total_entries': len(entries),
            'entry_types': list(set(e.get('type') for e in entries if 'type' in e))
        }
        
        return jsonify({
            'metadata': metadata,
            'entries': entries
        })
                
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@performance_bp.route('/api/performance/cpu')
def get_cpu_metrics():
    """Get CPU performance metrics from the log file."""
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_file = os.path.join(log_dir, 'performance_log.json')
    
    # Get optional time range parameters
    hours = request.args.get('hours', type=int, default=1)  # Default to last hour
    limit = request.args.get('limit', type=int, default=60)  # Default to 60 entries (1 per minute)
    include_threads = request.args.get('threads', type=bool, default=False)  # Option to include thread data
    cutoff_time = datetime.now() - timedelta(hours=hours)
    
    try:
        entries = []
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        # Only process CPU metric entries
                        if entry.get('type') != 'cpu_metrics':
                            continue
                            
                        entry_time = datetime.fromisoformat(entry['timestamp'])
                        if entry_time >= cutoff_time:
                            # Optionally exclude thread data to reduce payload size
                            if not include_threads and 'metrics' in entry:
                                entry['metrics'].pop('thread_times', None)
                            entries.append(entry)
                            if len(entries) >= limit:
                                break
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        continue
        
        # Sort entries by timestamp
        entries.sort(key=lambda x: x.get('timestamp', ''))
        
        # Calculate summary statistics
        summary = {}
        if entries:
            cpu_percentages = [e['metrics']['process_cpu_percent'] for e in entries if 'metrics' in e]
            if cpu_percentages:
                summary = {
                    'avg_cpu_percent': sum(cpu_percentages) / len(cpu_percentages),
                    'max_cpu_percent': max(cpu_percentages),
                    'min_cpu_percent': min(cpu_percentages),
                    'samples': len(cpu_percentages)
                }
        
        return jsonify({
            'summary': summary,
            'entries': entries
        })
                
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@performance_bp.route('/api/performance/memory')
def get_memory_metrics():
    """Get memory performance metrics from the log file."""
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_file = os.path.join(log_dir, 'performance_log.json')
    
    # Get optional time range parameters
    hours = request.args.get('hours', type=int, default=1)  # Default to last hour
    limit = request.args.get('limit', type=int, default=60)  # Default to 60 entries
    cutoff_time = datetime.now() - timedelta(hours=hours)
    
    try:
        entries = []
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        # Only process memory metric entries
                        if entry.get('type') not in ['basic_metrics', 'detailed_memory']:
                            continue
                            
                        entry_time = datetime.fromisoformat(entry['timestamp'])
                        if entry_time >= cutoff_time:
                            entries.append(entry)
                            if len(entries) >= limit:
                                break
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        continue
        
        # Sort entries by timestamp
        entries.sort(key=lambda x: x.get('timestamp', ''))
        
        # Calculate summary statistics
        summary = {}
        if entries:
            memory_metrics = [e['metrics'] for e in entries if 'metrics' in e]
            if memory_metrics:
                rss_values = [m.get('memory_rss', 0) for m in memory_metrics]
                vms_values = [m.get('memory_vms', 0) for m in memory_metrics]
                system_memory_used = [m.get('system_memory_used', 0) for m in memory_metrics]
                
                summary = {
                    'avg_rss_mb': sum(rss_values) / len(rss_values),
                    'max_rss_mb': max(rss_values),
                    'min_rss_mb': min(rss_values),
                    'avg_vms_mb': sum(vms_values) / len(vms_values),
                    'max_vms_mb': max(vms_values),
                    'min_vms_mb': min(vms_values),
                    'avg_system_memory_used': sum(system_memory_used) / len(system_memory_used),
                    'samples': len(memory_metrics)
                }
        
        return jsonify({
            'summary': summary,
            'entries': entries
        })
                
    except Exception as e:
        return jsonify({'error': str(e)}), 500
