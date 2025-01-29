from flask import Blueprint, render_template, jsonify
from performance_monitor import monitor
import psutil
import time
import os

performance_bp = Blueprint('performance', __name__)

@performance_bp.route('/dashboard')
def performance_dashboard():
    """Render the performance monitoring dashboard."""
    return render_template('performance/dashboard.html')

@performance_bp.route('/api/performance/log')
def get_performance_log():
    """Get the most recent performance log entry."""
    # Get log directory from environment variable with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_file = os.path.join(log_dir, 'performance.log')
    
    try:
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8') as f:
                # Read all lines
                lines = f.readlines()
                
                # Find the start of the last report (marked by ====)
                for i in range(len(lines) - 1, -1, -1):
                    if '=' * 100 in lines[i]:
                        # Get all lines from this point to the end
                        report_lines = lines[i:]
                        # Join them back together
                        report = ''.join(report_lines)
                        
                        # Parse sections
                        sections = {}
                        current_section = None
                        section_lines = []
                        
                        for line in report.split('\n'):
                            # Skip empty lines and separators
                            if not line.strip() or all(c in '=-' for c in line):
                                if current_section and section_lines:
                                    sections[current_section] = section_lines
                                    section_lines = []
                                continue
                            
                            # Check if this is a section header (contains emoji)
                            if any(ord(c) > 127 for c in line) and not line.startswith(' '):
                                if current_section and section_lines:
                                    sections[current_section] = section_lines
                                current_section = line.strip()
                                section_lines = []
                            elif current_section and line.strip():
                                section_lines.append(line.strip())
                        
                        # Add the last section if exists
                        if current_section and section_lines:
                            sections[current_section] = section_lines
                        
                        return jsonify({
                            'raw': report,
                            'sections': sections
                        })
                
        return jsonify({'error': 'No performance data available'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
