"""Collected-from-Plex fetch and direct Plex scan with progress."""

from flask import jsonify, request, Response, stream_with_context
from queues.run_program import get_and_add_recent_collected_from_plex, get_and_add_all_collected_from_plex
from utilities.settings import get_setting
import logging
from routes import admin_required
import time
import json
import threading
import asyncio
from ._common import analysis_progress, debug_bp, scan_progress

def async_get_collected_from_plex(collection_type):
    try:
        if collection_type == 'all':
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                return {'success': False, 'error': 'All/Recent are not available in Symlinked/Local mode; use Backfill.'}
            else:
                get_and_add_all_collected_from_plex()
            message = 'Successfully retrieved and added all collected items from Library'
        elif collection_type == 'recent':
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                return {'success': False, 'error': 'All/Recent are not available in Symlinked/Local mode; use Backfill.'}
            else:
                get_and_add_recent_collected_from_plex()
            message = 'Successfully retrieved and added recent collected items from Library'
        elif collection_type == 'backfill':
            # Backfill works in all modes - it updates existing records with current metadata
            # Symlink mode: Updates file sizes from filesystem (fast, no API calls)
            # Plex mode: Updates metadata from Plex API (location_on_disk, resolution, size, imdb_id, tmdb_id, collected_at)
            logging.info("Running backfill to update existing records with current metadata")
            get_and_add_all_collected_from_plex(backfill=True)
            message = 'Successfully backfilled metadata for already-Collected items'
        else:
            raise ValueError('Invalid collection type')

        return {'success': True, 'message': message}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@debug_bp.route('/api/get_collected_from_plex', methods=['POST'])
@admin_required
def get_collected_from_plex():
    collection_type = request.json.get('collection_type', 'recent')

    if collection_type not in ['all', 'recent', 'backfill']:
        return jsonify({'success': False, 'error': 'Invalid collection type'}), 400

    from routes.extensions import task_queue

    task_id = task_queue.add_task(async_get_collected_from_plex, collection_type)
    return jsonify({'task_id': task_id}), 202

@debug_bp.route('/api/direct_plex_scan', methods=['POST'])
@admin_required
def direct_plex_scan():
    """Direct route to scan Plex library with progress tracking."""
    try:
        import uuid
        from utilities.plex_functions import get_collected_from_plex
        
        # Generate unique scan ID
        scan_id = str(uuid.uuid4())
        scan_progress[scan_id] = {
            'status': 'starting',
            'message': 'Initializing scan...',
            'movies_count': 0,
            'episodes_count': 0,
            'complete': False,
            'shows_processed': 0,
            'total_shows': 0,
            'movies_processed': 0,
            'total_movies': 0,
            'episodes_found': 0,
            'errors': []
        }
        
        def progress_callback(status_type, message, counts=None):
            """Callback function to update progress"""
            logging.debug(f"Scan {scan_id}: progress_callback called with status={status_type}, message='{message}', counts={counts}") # Add logging
            update = {
                'status': status_type,
                'message': message,
                'complete': status_type in ['complete', 'error']
            }
            if counts:
                update.update(counts)
            scan_progress[scan_id].update(update)
            logging.debug(f"Scan {scan_id}: scan_progress updated to: {scan_progress[scan_id]}") # Add logging

            if status_type == 'error':
                scan_progress[scan_id]['errors'].append(message)
        
        def run_scan():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Update status to show collection starting
                scan_progress[scan_id].update({
                    'status': 'collecting',
                    'message': 'Starting Plex library scan...',
                    'phase': 'collection'
                })
                
                # Run the scan with progress callback
                collected_content = loop.run_until_complete(
                    get_collected_from_plex(
                        request='all',
                        progress_callback=progress_callback,
                        bypass=True
                    )
                )
                
                loop.close()
                
                if collected_content:
                    # Extract movies and episodes from collected content
                    movies = collected_content.get('movies', [])
                    episodes = collected_content.get('episodes', [])
                    
                    total_items = len(movies) + len(episodes)
                    logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes")
                    
                    # Update status to show database addition starting
                    scan_progress[scan_id].update({
                        'status': 'adding',
                        'message': f'Adding {total_items} items to database...',
                        'phase': 'database',
                        'total_items': total_items,
                        'processed_items': 0
                    })
                    
                    # Add the collected items to the database
                    from database.collected_items import add_collected_items
                    try:
                        if total_items > 0:
                            # Update progress before starting database addition
                            scan_progress[scan_id].update({
                                'status': 'adding',
                                'message': f'Adding {len(movies)} movies and {len(episodes)} episodes to database...',
                                'phase': 'database',
                                'complete': False  # Ensure we're not marked as complete during database phase
                            })
                            
                            add_collected_items(movies + episodes)
                            
                            # Only now mark as complete after database addition is done
                            scan_progress[scan_id].update({
                                'status': 'complete',
                                'message': f'Successfully scanned Plex library and added {len(movies)} movies and {len(episodes)} episodes to database',
                                'success': True,
                                'complete': True,
                                'phase': 'complete'
                            })
                        else:
                            scan_progress[scan_id].update({
                                'status': 'complete',
                                'message': 'Scanned Plex library but found no items to add',
                                'success': True,
                                'complete': True,
                                'phase': 'complete'
                            })
                    except Exception as e:
                        error_msg = f"Error adding collected items to database: {str(e)}"
                        logging.error(error_msg, exc_info=True)
                        scan_progress[scan_id].update({
                            'status': 'error',
                            'message': error_msg,
                            'success': False,
                            'complete': True,
                            'phase': 'error',
                            'errors': [error_msg]
                        })
                else:
                    scan_progress[scan_id].update({
                        'status': 'error',
                        'message': 'No content retrieved from Plex scan',
                        'success': False,
                        'complete': True,
                        'phase': 'error'
                    })
            except Exception as e:
                error_msg = str(e)
                logging.error(f"Error during Plex scan: {error_msg}", exc_info=True)
                # Ensure final counts are added even in case of error, if available
                final_counts = {
                   'shows_processed': scan_progress[scan_id].get('shows_processed', 0),
                   'total_shows': scan_progress[scan_id].get('total_shows', 0),
                   'movies_processed': scan_progress[scan_id].get('movies_processed', 0),
                   'total_movies': scan_progress[scan_id].get('total_movies', 0),
                   'episodes_found': scan_progress[scan_id].get('episodes_found', 0)
                }
                scan_progress[scan_id].update({
                    'status': 'error',
                    'message': f"Error during scan: {error_msg}",
                    'success': False,
                    'complete': True,
                    'phase': 'error',
                    'errors': [error_msg],
                    **final_counts # Also add counts on error
                })
            finally:
                # Ensure final counts are included in the final completion message if status wasn't error
                if scan_progress.get(scan_id) and scan_progress[scan_id].get('status') not in ['error', 'starting']:
                    final_counts = {
                        'shows_processed': scan_progress[scan_id].get('shows_processed', 0),
                        'total_shows': scan_progress[scan_id].get('total_shows', 0),
                        'movies_processed': scan_progress[scan_id].get('movies_processed', 0),
                        'total_movies': scan_progress[scan_id].get('total_movies', 0),
                        'episodes_found': scan_progress[scan_id].get('episodes_found', 0)
                    }
                    # Update the existing final status with the counts
                    scan_progress[scan_id].update(final_counts)
                    logging.info(f"Ensured final counts ({final_counts}) are in completion status for scan {scan_id}")

                # Clean up after 5 minutes
                threading.Timer(300, lambda: scan_progress.pop(scan_id, None)).start()
        
        # Start scan in background thread
        thread = threading.Thread(target=run_scan)
        thread.daemon = True
        thread.start()
        
        return jsonify({'success': True, 'scan_id': scan_id})
            
    except Exception as e:
        logging.error(f"Error initiating Plex scan: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)})

@debug_bp.route('/api/plex_scan_progress/<scan_id>')
@admin_required
def plex_scan_progress(scan_id):
    """SSE endpoint for tracking Plex scan progress."""
    def generate():
        logging.info(f"[Server SSE {scan_id}] Connection established.") # Added log
        while True:
            try: # Added try block for better error handling inside loop
                if scan_id not in scan_progress:
                    # Check if it's in analysis_progress as a fallback (might be analysis task)
                    if scan_id in analysis_progress:
                         progress = analysis_progress[scan_id]
                         yield f"data: {json.dumps(progress)}\n\n"
                         if progress.get('complete', False):
                             logging.info(f"[Server SSE {scan_id}] Analysis task complete=true detected.") # Added log
                             # Add a small delay AFTER sending the final message
                             logging.info(f"[Server SSE {scan_id}] Sleeping for 0.5s before break.") # Added log
                             time.sleep(0.5) # Sleep for 500ms 
                             logging.info(f"[Server SSE {scan_id}] Breaking loop after sleep.") # Added log
                             break # THEN break the loop
                    else:
                        logging.info(f"[Server SSE {scan_id}] Scan/Task ID not found.") # Added log
                        yield f"data: {json.dumps({'status': 'error', 'message': 'Scan or task not found'})}\n\n"
                        break
                    
                else: # Scan progress found
                    progress = scan_progress[scan_id]
                    logging.debug(f"[Server SSE {scan_id}] Current progress: {progress}") # Added debug log
                    
                    # Add error details to progress if any exist
                    if progress.get('errors'):
                        progress['error_details'] = progress['errors']
                    
                    data_to_send = json.dumps(progress)
                    logging.info(f"[Server SSE {scan_id}] Yielding data: {data_to_send[:200]}...") # Added log (truncated)
                    yield f"data: {data_to_send}\n\n"
                    
                    logging.debug(f"[Server SSE {scan_id}] Checking complete flag...") # Added debug log
                    if progress.get('complete', False): # Use .get() for safety
                        logging.info(f"[Server SSE {scan_id}] Scan task complete=true detected.") # Added log
                        # Add a small delay AFTER sending the final message
                        # to give the client time to process it before the connection closes.
                        logging.info(f"[Server SSE {scan_id}] Sleeping for 0.5s before break.") # Added log
                        time.sleep(0.5) # Sleep for 500ms 
                        logging.info(f"[Server SSE {scan_id}] Breaking loop after sleep.") # Added log
                        break # THEN break the loop
                    
                    logging.debug(f"[Server SSE {scan_id}] Sleeping for 1s before next iteration.") # Added debug log
                    time.sleep(1)
                    
                # except Exception as e: # Removed specific except block here to simplify
                #     logging.error(f"[Server SSE {scan_id}] Error inside generate loop: {e}", exc_info=True)
                #     try:
                #         yield f"data: {json.dumps({'status': 'error', 'message': f'Server error: {e}', 'complete': True})}\n\n"
                #     except Exception as yield_err:
                #         logging.error(f"[Server SSE {scan_id}] Failed to yield error message: {yield_err}")
                #     break # Exit loop on error
                
            except Exception as e:
                logging.error(f"[Server SSE {scan_id}] Error inside generate loop: {e}", exc_info=True)
                try:
                    yield f"data: {json.dumps({'status': 'error', 'message': f'Server error: {e}', 'complete': True})}\n\n"
                except Exception as yield_err:
                    logging.error(f"[Server SSE {scan_id}] Failed to yield error message: {yield_err}")
                break # Exit loop on error
        
        logging.info(f"[Server SSE {scan_id}] Generate loop finished.") # Added log
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/api/task_status/<task_id>')
@admin_required
def task_status(task_id):
    from routes.extensions import task_queue

    task_info = task_queue.get_task_status(task_id)
    return jsonify(task_info)

def update_trakt_settings(content_sources):
    trakt_watchlist_enabled = any(
        source_data['enabled'] 
        for source_id, source_data in content_sources.items() 
        if source_id.startswith('Trakt Watchlist')
    )
    trakt_lists = ','.join([
        source_data.get('trakt_lists', '') 
        for source_id, source_data in content_sources.items()
        if source_id.startswith('Trakt Lists') and source_data['enabled']
    ])
