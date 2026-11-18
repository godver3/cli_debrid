"""Rescrape, move to Wanted/Upgrading, test notifications."""

from flask import jsonify, request, current_app
from utilities.settings import get_all_settings, get_setting
import logging
from routes import admin_required
import os
import time
from datetime import datetime
from routes.notifications import send_notifications
from datetime import timedelta
from database.database_writing import RESET_COLLECTION_STATE_SQL
from utilities.plex_removal_cache import cache_plex_removal
from ._common import debug_bp

@debug_bp.route('/rescrape_item', methods=['POST'])
@admin_required
def rescrape_item():
    data = request.get_json()
    item_id = data.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Item ID is required'}), 400

    try:
        from database.database_reading import get_media_item_by_id
        # remove_file_from_plex is still needed if there are other direct calls,
        # but for this specific logic, we'll use the cache.

        # Get the item details first
        item = get_media_item_by_id(item_id)
        if not item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404

        # Get file management settings
        file_management = get_setting('File Management', 'file_collection_management', 'Plex')
        mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
        # original_files_path = get_setting('File Management', 'original_files_path', '') # Not directly used in this logic block
        # symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '') # Not directly used

        # Check if we're in limited environment mode
        from utilities.set_supervisor_env import is_limited_environment
        limited_env = is_limited_environment()
        
        # Handle file deletion based on management type
        if file_management == 'Plex' and (item['state'] == 'Collected' or item['state'] == 'Upgrading'):
            if mounted_location and item.get('location_on_disk'):
                if not limited_env:
                    try:
                        if os.path.exists(item['location_on_disk']):
                            os.remove(item['location_on_disk'])
                            logging.info(f"Rescrape: Deleted file {item['location_on_disk']} for item {item_id} (Plex mode).")
                    except Exception as e:
                        logging.error(f"Error deleting file at {item['location_on_disk']}: {str(e)}")
                else:
                    logging.info(f"Rescrape: Skipped file deletion for {item['location_on_disk']} due to limited environment mode (Plex mode).")

            time.sleep(1) # Allow time for filesystem operations

            if item.get('filled_by_file'): # Ensure filled_by_file exists
                if item['type'] == 'movie':
                    cache_plex_removal(item['title'], item['filled_by_file'])
                    logging.info(f"Rescrape: Queued Plex removal via cache for movie {item['title']} (item {item_id}), path: {item['filled_by_file']}.")
                elif item['type'] == 'episode':
                    cache_plex_removal(item['title'], item['filled_by_file'], item.get('episode_title'))
                    logging.info(f"Rescrape: Queued Plex removal via cache for episode {item.get('episode_title')} of {item['title']} (item {item_id}), path: {item['filled_by_file']}.")
            else:
                logging.warning(f"Rescrape: Missing 'filled_by_file' for item {item_id} (Plex mode), cannot queue Plex removal.")

        elif file_management == 'Symlinked/Local' and (item['state'] == 'Collected' or item['state'] == 'Upgrading'):
            symlink_path_for_plex = None
            # Handle symlink removal - always remove symlinks (they're just pointers)
            if item.get('location_on_disk'):
                symlink_path_for_plex = item['location_on_disk'] # Store for potential Plex removal path
                try:
                    if os.path.exists(item['location_on_disk']) and os.path.islink(item['location_on_disk']):
                        os.unlink(item['location_on_disk'])
                        logging.info(f"Rescrape: Removed symlink {item['location_on_disk']} for item {item_id} (Symlinked/Local mode).")
                except Exception as e:
                    logging.error(f"Error removing symlink at {item['location_on_disk']}: {str(e)}")

            # Handle original file removal - only delete original files if not in limited environment mode
            if item.get('original_path_for_symlink'):
                if not limited_env:
                    try:
                        if os.path.exists(item['original_path_for_symlink']):
                            os.remove(item['original_path_for_symlink'])
                            logging.info(f"Rescrape: Deleted original file {item['original_path_for_symlink']} for item {item_id} (Symlinked/Local mode).")
                    except Exception as e:
                        logging.error(f"Error deleting original file at {item['original_path_for_symlink']}: {str(e)}")
                else:
                    logging.info(f"Rescrape: Skipped original file deletion for {item['original_path_for_symlink']} due to limited environment mode (Symlinked/Local mode).")

            time.sleep(1) # Allow time for filesystem operations

            # Queue for Plex removal if configured
            plex_url = get_setting('File Management', 'plex_url_for_symlink', '')
            if plex_url:
                # For Symlinked/Local, Plex usually sees the symlink.
                # The path given to cache_plex_removal should be what Plex uses to identify the file.
                # remove_file_from_plex matches basenames.
                path_to_tell_plex = None
                if symlink_path_for_plex: # Prefer the symlink path's basename if it existed
                    path_to_tell_plex = os.path.basename(symlink_path_for_plex)
                elif item.get('original_path_for_symlink'): # Fallback to original file's basename
                     path_to_tell_plex = os.path.basename(item['original_path_for_symlink'])

                if path_to_tell_plex:
                    if item['type'] == 'movie':
                        cache_plex_removal(item['title'], path_to_tell_plex)
                        logging.info(f"Rescrape: Queued Plex removal via cache for movie {item['title']} (item {item_id}), path: {path_to_tell_plex} (Symlinked/Local mode).")
                    elif item['type'] == 'episode':
                        cache_plex_removal(item['title'], path_to_tell_plex, item.get('episode_title'))
                        logging.info(f"Rescrape: Queued Plex removal via cache for episode {item.get('episode_title')} of {item['title']} (item {item_id}), path: {path_to_tell_plex} (Symlinked/Local mode).")
                else:
                    logging.warning(f"Rescrape: No valid path (symlink or original) found for Plex removal for item {item_id} (Symlinked/Local mode).")
            else:
                logging.info(f"Rescrape: Plex URL for symlink not configured, skipping Plex removal for item {item_id} (Symlinked/Local mode).")


        # Move the item to Wanted queue
        move_item_to_wanted(item_id, item.get('original_scraped_torrent_title')) # Pass the original_scraped_torrent_title
        logging.info(f"Rescrape: Moved item {item_id} to Wanted queue.")
        return jsonify({'success': True, 'message': 'Item files processed, Plex removal cached (if applicable), and item moved to Wanted queue for rescraping'}), 200
    except Exception as e:
        logging.error(f"Error rescraping item {data.get('item_id', 'N/A')}: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def move_item_to_wanted(item_id, current_original_scraped_title=None):
    from database import get_db_connection
    conn = get_db_connection()
    try:
        # If no current_original_scraped_title was supplied, fetch it from the database so that we
        # can preserve it in the rescrape_original_torrent_title field. This helps later scraping
        # logic to avoid re-adding the same bad release.
        if current_original_scraped_title is None:
            # Prefer the title of the release we actually collected (filled_by_title) and fall back
            # to the stored original_scraped_torrent_title if that is not present. This gives the
            # scraping queue the best chance of recognising and skipping the bad release next time.
            try:
                cur_lookup = conn.cursor()
                cur_lookup.execute(
                    "SELECT filled_by_title, original_scraped_torrent_title FROM media_items WHERE id = ?",
                    (item_id,)
                )
                row = cur_lookup.fetchone()
                if row:
                    filled_title, scraped_title = row
                    current_original_scraped_title = filled_title or scraped_title
            except Exception as _fetch_err:
                logging.warning(
                    f"move_item_to_wanted: failed to fetch fallback titles for item {item_id}: {_fetch_err}"
                )
            finally:
                if 'cur_lookup' in locals():
                    cur_lookup.close()

        cursor = conn.cursor()
        cursor.execute(f'''
            UPDATE media_items
            SET state = 'Wanted',
                filled_by_file = NULL,
                filled_by_title = NULL,
                filled_by_magnet = NULL,
                filled_by_torrent_id = NULL,
                {RESET_COLLECTION_STATE_SQL},
                last_updated = ?,
                location_on_disk = NULL,
                original_path_for_symlink = NULL,
                rescrape_original_torrent_title = ?,
                original_scraped_torrent_title = NULL,
                upgrading_from = NULL,
                version = TRIM(version, '*'),
                upgrading = NULL,
                fall_back_to_single_scraper = 0,
                upgraded = NULL
            WHERE id = ?
        ''', (datetime.now(), current_original_scraped_title, item_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

@debug_bp.route('/send_test_notification', methods=['POST'])
@admin_required
def send_test_notification():
    current_app.logger.info("Entering send_test_notification function")
    try:
        # Create test notification items
        now = datetime.now()
        
        # Collection test notifications
        collection_notifications = [
            {
                'type': 'movie',
                'title': 'Test Movie 1',
                'year': 2023,
                'tmdb_id': '123456',
                'original_collected_at': now.isoformat(),
                'version': '1080p',
                'is_upgrade': False,
                'media_type': 'movie',
                'new_state': 'Collected',  # Adding new_state for NEW indicator
                'content_source': 'My Plex Watchlist',
                'content_source_detail': 'user1'
            },
            {
                'type': 'movie',
                'title': 'Test Movie 2',
                'year': 2023,
                'tmdb_id': '234567',
                'original_collected_at': (now + timedelta(hours=1)).isoformat(),
                'version': '2160p',
                'is_upgrade': True,
                'upgrading_from': 'Test Movie 2 1080p.mkv',
                'media_type': 'movie',
                'new_state': 'Collected',  # Adding new_state for upgrade indicator
                'content_source': 'Trakt Watchlist',
                'content_source_detail': 'user2'
            },
            {
                'type': 'episode',
                'title': 'Test TV Show 1',
                'year': 2023,
                'tmdb_id': '345678',
                'season_number': 1,
                'episode_number': 1,
                'original_collected_at': (now + timedelta(hours=2)).isoformat(),
                'version': 'Default',
                'is_upgrade': False,
                'media_type': 'tv',
                'new_state': 'Collected',
                'content_source': 'Overseerr',
                'content_source_detail': 'user3'
            },
            {
                'type': 'episode',
                'title': 'Test TV Show 3',
                'year': 2023,
                'tmdb_id': '789012',
                'season_number': 1,
                'episode_number': 3,
                'original_collected_at': (now + timedelta(hours=3)).isoformat(),
                'version': '2160p',
                'is_upgrade': True,
                'upgrading_from': 'Test TV Show 3 S01E03 1080p.mkv',
                'media_type': 'tv',
                'new_state': 'Collected',  # This indicates it's a completed upgrade
                'content_source': 'Trakt Lists',
                'content_source_detail': 'user4'
            }
        ]

        # State change test notifications
        state_change_notifications = [
            {
                'type': 'movie',
                'title': 'Test Movie 3',
                'year': 2023,
                'tmdb_id': '456789',
                'version': '1080p',
                'new_state': 'Checking',
                'is_upgrade': False,
                'upgrading_from': None,
                'media_type': 'movie',
                'content_source': 'MDBList',
                'content_source_detail': 'user5'
            },
            {
                'type': 'movie',
                'title': 'Test Movie 4',
                'year': 2023,
                'tmdb_id': '567890',
                'version': '2160p',
                'new_state': 'Sleeping',
                'is_upgrade': False,
                'upgrading_from': None,
                'media_type': 'movie',
                'content_source': 'My Plex RSS Watchlist',
                'content_source_detail': 'user6'
            },
            {
                'type': 'episode',
                'title': 'Test TV Show 2',
                'year': 2023,
                'tmdb_id': '678901',
                'season_number': 1,
                'episode_number': 2,
                'version': '1080p',
                'new_state': 'Upgrading',
                'is_upgrade': True,
                'upgrading_from': 'Test TV Show 2 S01E02 720p.mkv',
                'media_type': 'tv',
                'content_source': 'Other Plex Watchlist',
                'content_source_detail': 'user7'
            }
        ]

        # Fetch enabled notifications
        enabled_notifications = get_all_settings().get('Notifications', {})
        
        # Send collection notifications
        send_notifications(collection_notifications, enabled_notifications, notification_category='collected')
        
        # Send state change notifications
        send_notifications(state_change_notifications, enabled_notifications, notification_category='state_change')
        
        return jsonify({'success': True, 'message': 'Test notifications sent successfully'}), 200

    except Exception as e:
        current_app.logger.error(f"Error sending test notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': 'An error occurred while sending the test notification. Please check the server logs for more details.'}), 500

@debug_bp.route('/move_to_upgrading', methods=['POST'])
@admin_required
def move_to_upgrading():
    item_id = request.form.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Item ID is required'}), 400

    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE media_items 
            SET state = 'Upgrading',
                last_updated = ?
            WHERE id = ? AND state = 'Collected'
        ''', (datetime.now(), item_id))
        conn.commit()
        
        if cursor.rowcount > 0:
            return jsonify({'success': True, 'message': f'Item {item_id} moved to Upgrading state'}), 200
        else:
            return jsonify({'success': False, 'error': f'Item {item_id} not found or not in Collected state'}), 404
    except Exception as e:
        logging.error(f"Error moving item to Upgrading state: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()
