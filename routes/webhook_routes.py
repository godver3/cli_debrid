from flask import jsonify, request, Blueprint
import logging
from run_program import process_overseerr_webhook
from extensions import app
from queue_manager import QueueManager
from utilities.local_library_scan import check_local_file_for_item
from utilities.plex_functions import plex_update_item
from settings import get_setting
from urllib.parse import unquote
import os.path
from content_checkers.overseerr import get_overseerr_details, get_overseerr_headers
from api_tracker import api

webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/', methods=['POST'])
def webhook():
    data = request.json
    logging.debug(f"Received webhook: {data}")
    try:
        # Handle test notifications separately
        if data.get('notification_type') == 'TEST_NOTIFICATION':
            logging.info("Received test notification from Overseerr")
            return jsonify({"status": "success", "message": "Test notification received"}), 200

        # If this is a TV show request, look for season information
        if data.get('media', {}).get('media_type') == 'tv':
            # Look for season information in the extra field
            extra_items = data.get('extra', [])
            for item in extra_items:
                if item.get('name') == 'Requested Seasons':
                    try:
                        # The value could be a single season or a comma-separated list
                        seasons_str = item.get('value', '')
                        requested_seasons = [int(s.strip()) for s in seasons_str.split(',')]
                        if requested_seasons:
                            # Add to media section
                            data['media']['requested_seasons'] = requested_seasons
                            logging.info(f"Added season information to webhook data: {requested_seasons}")
                    except ValueError as e:
                        logging.error(f"Error parsing season information from webhook: {str(e)}")

            # Only process if partial requests are allowed
            if get_setting('debug', 'allow_partial_overseerr_requests', False):
                logging.info("Partial requests are not allowed, clearing requested seasons")
                if 'requested_seasons' in data['media']:
                    del data['media']['requested_seasons']

        # Mark this request as coming from Overseerr
        if data.get('media'):
            data['media']['from_overseerr'] = True

        logging.debug(f"Final webhook data before processing: {data}")
        process_overseerr_webhook(data)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@webhook_bp.route('/rclone', methods=['POST', 'GET'])
def rclone_webhook():
    try:
        file_path = request.args.get('file')
        if not file_path:
            return jsonify({"status": "error", "message": "No file path provided"}), 400

        # URL decode the file path
        file_path = unquote(file_path)
        logging.info(f"Received rclone webhook for file: {file_path}")

        # Get just the filename, removing any directory path
        file_path = os.path.basename(file_path)
        logging.info(f"Extracted filename: {file_path}")

        # Get the checking queue items
        queue_manager = QueueManager()
        checking_queue = queue_manager.queues["Checking"]

        # Find matching items
        matched_items = [item for item in checking_queue.items if item.get('filled_by_title') == file_path]

        if not matched_items:
            logging.info(f"No matching items found for file: {file_path}")
            return jsonify({"status": "success", "message": "No matching items found"}), 200

        for item in matched_items:
            logging.info(f"Found matching item {item['id']} for file {file_path}")
            if check_local_file_for_item(item, is_webhook=True):
                logging.info(f"Local file found and symlinked for item {item['id']}")

                # Check for Plex or Emby configuration and update accordingly
                if get_setting('Debug', 'emby_url', default=False):
                    # Call Emby update for the item if we have an Emby URL
                    emby_update_item(item)
                elif get_setting('File Management', 'plex_url_for_symlink', default=False):
                    # Call Plex update for the item if we have a Plex URL
                    plex_update_item(item)

                # Check if the item was marked for upgrading by check_local_file_for_item
                from database.core import get_db_connection
                conn = get_db_connection()
                cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
                current_state = cursor.fetchone()['state']
                conn.close()

                if current_state == 'Upgrading':
                    logging.info(f"Item {item['id']} is marked for upgrading, keeping in Upgrading state")
                else:
                    # Move to collected without creating another notification
                    queue_manager.move_to_collected(item, "Checking", skip_notification=True)

        return jsonify({
            "status": "success",
            "message": f"Processed {len(matched_items)} matching items"
        }), 200

    except Exception as e:
        logging.error(f"Error processing rclone webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500