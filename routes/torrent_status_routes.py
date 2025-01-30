from flask import Blueprint, render_template, jsonify
from debrid.common.utils import format_torrent_status
from debrid import get_debrid_provider
from settings import get_setting

# Create Blueprint
torrent_status_bp = Blueprint('torrent_status', __name__, url_prefix='/torrent_status')

@torrent_status_bp.route('/')
def torrent_status():
    """Display the torrent status page"""
    return render_template('torrent_status.html')

@torrent_status_bp.route('/api/torrent-status')
def get_torrent_status():
    """API endpoint to get current torrent status"""
    try:
       
        # Get the configured debrid provider instance
        provider = get_debrid_provider()
        
        # Get active torrents and stats using the provider
        active_torrents, download_stats = provider.get_torrent_status()
        
        # Format the status
        status_text = format_torrent_status(active_torrents, download_stats)
        
        # Split the status text into sections for better frontend rendering
        sections = {}
        current_section = None
        current_content = []
        
        for line in status_text.split('\n'):
            if not line.strip():
                continue
                
            if line.endswith(':'):  # This is a section header
                if current_section:
                    sections[current_section] = current_content
                current_section = line.strip(':')
                current_content = []
            else:
                current_content.append(line)
        
        # Add the last section
        if current_section:
            sections[current_section] = current_content
            
        return jsonify({
            'success': True,
            'sections': sections,
            'raw_status': status_text
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
