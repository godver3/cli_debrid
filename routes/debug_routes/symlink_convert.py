"""Convert library to symlinks with progress."""

from flask import jsonify, Response, stream_with_context
from utilities.settings import get_setting
import logging
from routes import admin_required
import time
import json
import threading
from utilities.local_library_scan import convert_item_to_symlink
from database.database_writing import RESET_COLLECTION_STATE_SQL
from ._common import debug_bp, scan_progress
from utilities.paths import safe_realpath

def _has_same_item_copy(cursor, item):
    """True when another Wanted/Collected row is the same movie or the same
    episode (same season and episode) in the same version.

    Matching on title alone treated every other episode of a show as a copy,
    so an episode whose source file was missing got its row deleted instead of
    going back to Wanted.
    """
    where = ["type = ?", "TRIM(version, '*') = TRIM(?, '*')",
             "state IN ('Wanted', 'Collected')", "id != ?",
             "COALESCE(season_number, -1) = COALESCE(?, -1)",
             "COALESCE(episode_number, -1) = COALESCE(?, -1)"]
    params = [item.get('type'), item.get('version') or '', item.get('id'),
              item.get('season_number'), item.get('episode_number')]
    imdb_id, tmdb_id = item.get('imdb_id'), item.get('tmdb_id')
    if imdb_id:
        where.append("imdb_id = ?")
        params.append(imdb_id)
    elif tmdb_id:
        where.append("tmdb_id = ?")
        params.append(str(tmdb_id))
    else:
        where.append("title = ?")
        params.append(item.get('title'))
    cursor.execute(f"SELECT COUNT(*) FROM media_items WHERE {' AND '.join(where)}", params)
    return cursor.fetchone()[0] > 0


@debug_bp.route('/convert_to_symlinks', methods=['POST'])
@admin_required
def convert_to_symlinks():
    """Convert existing library items to use symlinks."""
    try:
        import uuid
        
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        def run_conversion():
            try:
                import os
                # Get database connection
                from database import get_db_connection
                conn = get_db_connection()
                cursor = conn.cursor()

                # Get symlinked files path from settings
                symlinked_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
                if not symlinked_path:
                    scan_progress[task_id].update({
                        'status': 'error',
                        'message': 'Symlinked files path not configured',
                        'complete': True
                    })
                    return

                # Get all items with location_on_disk set
                cursor.execute("""
                    SELECT *
                    FROM media_items 
                    WHERE location_on_disk IS NOT NULL 
                    AND location_on_disk != ''
                    AND state = 'Collected'
                """)
                items = cursor.fetchall()

                if not items:
                    scan_progress[task_id].update({
                        'status': 'error',
                        'message': 'No items found with location_on_disk set',
                        'complete': True
                    })
                    return

                total_items = len(items)
                logging.info(f"Found {total_items} items to convert to symlinks")

                # Initialize progress tracking
                scan_progress[task_id].update({
                    'status': 'running',
                    'message': f'Converting {total_items} items to symlinks...',
                    'total_items': total_items,
                    'processed_items': 0,
                    'symlinks_created': 0,
                    'items_to_wanted': 0,
                    'items_deleted': 0,
                    'items_skipped': 0,
                    'complete': False
                })

                # Convert items to symlinks
                processed = 0
                wanted_count = 0
                deleted_count = 0
                skipped_count = 0
                symlinks_created = 0
                found_symlinks = False
                check_for_symlinks = True

                for item in items:
                    item_dict = dict(item)
                    
                    # Check if item is already in symlink folder
                    current_location = item_dict.get('location_on_disk', '')
                    
                    # If the current location is in the symlink folder, skip it
                    _sym_root = symlinked_path.rstrip('/\\')
                    if current_location == _sym_root or current_location.startswith(_sym_root + os.sep):
                        logging.info(f"Skipping {item_dict['title']} ({item_dict['version']}) - already in symlink folder")
                        skipped_count += 1
                        scan_progress[task_id].update({
                            'items_skipped': skipped_count,
                            'message': f'Skipping {item_dict["title"]} - already in symlink folder'
                        })
                        continue

                    # Only check for symlinks in first 100 items unless we've found one
                    if check_for_symlinks:
                        try:
                            if os.path.islink(current_location):
                                real_path = safe_realpath(current_location)
                                logging.info(f"Found symlink for {item_dict['title']}, using original path: {real_path}")
                                # Store both the real path and the original filename
                                item_dict['filename_real_path'] = os.path.basename(real_path)
                                # Keep the original location_on_disk as is - don't resolve it yet
                                found_symlinks = True
                                logging.debug(f"Set filename_real_path to: {item_dict['filename_real_path']}")
                        except Exception as e:
                            logging.warning(f"Error checking symlink for {current_location}: {str(e)}")

                        if processed >= 100 and not found_symlinks:
                            logging.info("No symlinks found in first 100 items, disabling symlink check")
                            check_for_symlinks = False

                    result = convert_item_to_symlink(item_dict, skip_verification=True) # Pass skip_verification=True here

                    if result['success']:
                        symlinks_created += 1
                        # Update database with new location and original path
                        cursor.execute("""
                            UPDATE media_items 
                            SET location_on_disk = ?,
                                original_path_for_symlink = ?
                            WHERE id = ?
                        """, (result['new_location'], result['old_location'], result['item_id']))
                    else:
                        # If error is "Source file not found", handle specially
                        if "Source file not found" in result['error']:
                            has_duplicate = _has_same_item_copy(cursor, item_dict)
                            
                            if has_duplicate:
                                # Delete this item as we already have a copy
                                cursor.execute("DELETE FROM media_items WHERE id = ?", (result['item_id'],))
                                deleted_count += 1
                                logging.info(f"Deleted item {item_dict['title']} as duplicate exists")
                            else:
                                # Update the item to Wanted state
                                cursor.execute(f"""
                                    UPDATE media_items
                                    SET state = 'Wanted',
                                        filled_by_file = NULL,
                                        filled_by_title = NULL,
                                        filled_by_magnet = NULL,
                                        filled_by_torrent_id = NULL,
                                        {RESET_COLLECTION_STATE_SQL},
                                        location_on_disk = NULL,
                                        last_updated = CURRENT_TIMESTAMP,
                                        version = TRIM(version, '*')
                                    WHERE id = ?
                                """, (result['item_id'],))
                                wanted_count += 1
                                logging.info(f"Moved item {item_dict['title']} to Wanted state")
                    
                    processed += 1
                    
                    # Update progress
                    scan_progress[task_id].update({
                        'processed_items': processed,
                        'symlinks_created': symlinks_created,
                        'items_to_wanted': wanted_count,
                        'items_deleted': deleted_count,
                        'items_skipped': skipped_count,
                        'message': f'Processing: {item_dict["title"]}'
                    })
                    
                    # Commit every 50 items
                    if processed % 50 == 0:
                        conn.commit()

                conn.commit()
                conn.close()

                # Final status update
                scan_progress[task_id].update({
                    'status': 'complete',
                    'message': 'Library conversion completed successfully',
                    'complete': True,
                    'success': True
                })

            except Exception as e:
                logging.error(f"Error during library conversion: {str(e)}", exc_info=True)
                scan_progress[task_id].update({
                    'status': 'error',
                    'message': f'Error during conversion: {str(e)}',
                    'complete': True,
                    'success': False
                })
            finally:
                # Clean up progress tracking after 5 minutes
                threading.Timer(300, lambda: scan_progress.pop(task_id, None)).start()

        # Initialize progress tracking
        scan_progress[task_id] = {
            'status': 'starting',
            'message': 'Initializing library conversion...',
            'complete': False
        }

        # Start conversion in background thread
        thread = threading.Thread(target=run_conversion)
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'task_id': task_id})

    except Exception as e:
        logging.error(f"Error initiating library conversion: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)})

@debug_bp.route('/api/conversion_progress/<task_id>')
@admin_required
def conversion_progress(task_id):
    """SSE endpoint for tracking library conversion progress."""
    def generate():
        while True:
            if task_id not in scan_progress:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Conversion task not found'})}\n\n"
                break
                
            progress = scan_progress[task_id]
            yield f"data: {json.dumps(progress)}\n\n"
            
            if progress['complete']:
                break
                
            time.sleep(1)
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')
