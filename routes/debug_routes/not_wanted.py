"""Not-wanted magnets and URLs."""

from flask import jsonify, render_template, request
from queues.config_manager import load_config
import logging
from routes import admin_required
from database.not_wanted_magnets import get_not_wanted_magnets, get_not_wanted_urls, save_not_wanted_magnets, save_not_wanted_urls
from ._common import debug_bp

@debug_bp.route('/not_wanted')
@admin_required
def not_wanted():
    config = load_config()
    not_wanted_magnets = get_not_wanted_magnets()
    urls = get_not_wanted_urls()
    return render_template('debug_not_wanted.html', magnets=not_wanted_magnets, urls=urls)

@debug_bp.route('/not_wanted/magnet/remove', methods=['POST'])
@admin_required
def remove_not_wanted_magnet():
    magnet_hash = request.form.get('hash')
    if not magnet_hash:
        return jsonify({'success': False, 'error': 'Magnet hash is required'}), 400

    try:
        magnets = get_not_wanted_magnets()
        if magnet_hash in magnets:
            magnets.remove(magnet_hash)
            save_not_wanted_magnets(magnets)
            return jsonify({'success': True, 'message': 'Magnet removed from not wanted list.'}), 200
        else:
            return jsonify({'success': False, 'error': 'Magnet not found in not wanted list.'}), 404
    except Exception as e:
        logging.error(f"Error removing not wanted magnet: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/not_wanted/url/remove', methods=['POST'])
@admin_required
def remove_not_wanted_url():
    url_to_remove = request.form.get('url')
    if not url_to_remove:
        return jsonify({'success': False, 'error': 'URL is required'}), 400

    try:
        urls = get_not_wanted_urls()
        if url_to_remove in urls:
            urls.remove(url_to_remove)
            save_not_wanted_urls(urls)
            return jsonify({'success': True, 'message': 'URL removed from not wanted list.'}), 200
        else:
            return jsonify({'success': False, 'error': 'URL not found in not wanted list.'}), 404
    except Exception as e:
        logging.error(f"Error removing not wanted URL: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/not_wanted/purge', methods=['POST'])
@admin_required
def purge_not_wanted():
    purge_type = request.form.get('purge_type')
    
    if purge_type == 'magnets':
        try:
            save_not_wanted_magnets(set())
            return jsonify({'success': True, 'message': 'All not wanted magnets have been purged.'}), 200
        except Exception as e:
            logging.error(f"Error purging not wanted magnets: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    elif purge_type == 'urls':
        try:
            save_not_wanted_urls(set())
            return jsonify({'success': True, 'message': 'All not wanted URLs have been purged.'}), 200
        except Exception as e:
            logging.error(f"Error purging not wanted URLs: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    else:
        return jsonify({'success': False, 'error': 'Invalid purge type'}), 400
