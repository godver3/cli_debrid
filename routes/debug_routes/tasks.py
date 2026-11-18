"""cli_mount sync, manual task runs, Plex label sync, task list."""

from flask import jsonify, request
from queues.run_program import ProgramRunner
import logging
from routes import admin_required
from routes.program_operation_routes import get_program_runner
from ._common import debug_bp

@debug_bp.route('/run_full_climount_sync', methods=['POST'])
@admin_required
def run_full_climount_sync():
    """Run a full cli_mount sync (since=0) in a background thread, pausing the queue during sync."""
    try:
        import threading
        from usenet.climount_sync import sync_changes_from_climount
        from routes.program_operation_routes import get_program_runner

        runner = get_program_runner()

        def _run():
            paused = False
            try:
                if runner:
                    runner.pause_info = {
                        'reason_string': 'cli_mount full sync in progress — queue resumes automatically when complete',
                        'error_type': 'SYSTEM_MAINTENANCE',
                        'service_name': 'cli_mount sync',
                        'status_code': None,
                        'retry_count': 0,
                    }
                    runner.pause_queue()
                    paused = True
                    logging.info('[FullCMSync] Queue paused during full sync')
                result = sync_changes_from_climount(force_full=True)
                logging.info(f'[FullCMSync] Complete: {result}')
            except Exception as e:
                logging.error(f'[FullCMSync] Error: {e}', exc_info=True)
            finally:
                if paused and runner:
                    runner.last_resume_time = None  # bypass 30s throttle
                    runner.pause_info = {'reason_string': None, 'error_type': None,
                                         'service_name': None, 'status_code': None, 'retry_count': 0}
                    runner.resume_queue()
                    logging.info('[FullCMSync] Queue resumed after full sync')

        t = threading.Thread(target=_run, daemon=True, name='full-cm-sync')
        t.start()
        return jsonify({'success': True, 'message': 'Full cli_mount sync started — queue will pause automatically'}), 200
    except Exception as e:
        logging.error(f"Error in run_full_climount_sync: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/run_task', methods=['POST'])
@admin_required
def run_task():
    """Manually trigger a task by adding it to the APScheduler queue."""
    try:
        data = request.get_json()
        task_name = data.get('task_name')
        if not task_name:
            return jsonify({'success': False, 'error': 'Task name not provided'}), 400

        runner = get_program_runner() # This should now work
        if not runner:
            return jsonify({'success': False, 'error': 'ProgramRunner not initialized'}), 500

        # trigger_task now returns a dict or raises an exception
        result = runner.trigger_task(task_name) 
        
        # result will be like {"success": True, "message": "Task 'X' queued...", "job_id": "manual_X_uuid"}
        return jsonify(result), 200

    except ValueError as ve: # Catch specific errors from trigger_task (e.g., task not defined)
        logging.error(f"ValueError in run_task: {ve}")
        return jsonify({'success': False, 'error': str(ve)}), 400
    except RuntimeError as re: # Catch specific errors from trigger_task (e.g., queueing failed)
        logging.error(f"RuntimeError in run_task: {re}")
        return jsonify({'success': False, 'error': str(re)}), 500
    except Exception as e:
        logging.error(f"Error in run_task: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An unexpected error occurred: {str(e)}'}), 500

@debug_bp.route('/sync_plex_labels', methods=['POST'])
@admin_required
def sync_plex_labels():
    """
    Sync Plex labels from content sources with optional incremental mode.

    Request JSON:
        {
            "incremental": true/false,  # Optional, default false
            "days_back": 7              # Optional, default 7 (only used if incremental=true)
        }
    """
    try:
        data = request.get_json() or {}
        incremental = data.get('incremental', False)
        days_back = data.get('days_back', 7)

        # Validate days_back
        if not isinstance(days_back, int) or days_back < 1 or days_back > 365:
            return jsonify({
                'success': False,
                'error': 'days_back must be an integer between 1 and 365'
            }), 400

        logging.info(f"Manual sync_plex_labels triggered: incremental={incremental}, days_back={days_back}")

        # Get the program runner and run the task directly
        runner = get_program_runner()
        if not runner:
            return jsonify({
                'success': False,
                'error': 'ProgramRunner not initialized'
            }), 500

        # Run the task directly (not queued, runs immediately)
        runner._rebuild_plex_labels(incremental=incremental, days_back=days_back)

        mode_desc = f"incremental (last {days_back} days)" if incremental else "full"
        return jsonify({
            'success': True,
            'message': f'Label sync ({mode_desc}) completed successfully'
        }), 200

    except Exception as e:
        logging.error(f"Error in sync_plex_labels: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@debug_bp.route('/get_available_tasks', methods=['GET'])
@admin_required
def get_available_tasks():
    # Define the task list with display names
    task_map = [
        {'id': 'wanted', 'display_name': 'Wanted'},
        {'id': 'scraping', 'display_name': 'Scraping'},
        {'id': 'adding', 'display_name': 'Adding'},
        {'id': 'checking', 'display_name': 'Checking'},
        {'id': 'sleeping', 'display_name': 'Sleeping'},
        {'id': 'unreleased', 'display_name': 'Unreleased'},
        {'id': 'blacklisted', 'display_name': 'Blacklisted'},
        {'id': 'pending_uncached', 'display_name': 'Pending Uncached'},
        {'id': 'upgrading', 'display_name': 'Upgrading'},
        {'id': 'task_plex_full_scan', 'display_name': 'Plex Full Scan'},
        {'id': 'task_sync_plex_labels', 'display_name': 'Sync Plex Labels'},
        {'id': 'task_regenerate_labels_full', 'display_name': 'Rebuild Plex Labels (All Items)'},
        {'id': 'task_regenerate_labels_incremental', 'display_name': 'Rebuild Plex Labels (Never Synced + Last 7 Days)'},
        {'id': 'task_refresh_release_dates', 'display_name': 'Refresh Release Dates'},
        {'id': 'task_purge_not_wanted_magnets_file', 'display_name': 'Purge Not Wanted Magnets File'},
        {'id': 'task_purge_not_wanted_urls_file', 'display_name': 'Purge Not Wanted URLs File'},
        {'id': 'task_purge_not_wanted_nzbs_files', 'display_name': 'Purge Not Wanted NZBs (segments + GUIDs)'},
        {'id': 'task_purge_all_not_wanted_files', 'display_name': 'Purge All Not Wanted Lists (magnets, URLs, NZBs)'},
        {'id': 'task_reset_trakt_auth', 'display_name': 'Reset Trakt Authorization (clears tokens, keeps client ID/secret)'},
        {'id': 'task_generate_airtime_report', 'display_name': 'Generate Airtime Report'},
        {'id': 'task_check_service_connectivity', 'display_name': 'Check Service Connectivity'},
        {'id': 'task_send_notifications', 'display_name': 'Send Notifications'},
        {'id': 'task_check_trakt_early_releases', 'display_name': 'Check Trakt Early Releases'},
        {'id': 'task_reconcile_queues', 'display_name': 'Reconcile Queues'},
        {'id': 'task_check_plex_files', 'display_name': 'Check Plex Files'},
        {'id': 'task_update_show_ids', 'display_name': 'Update Show IDs'},
        {'id': 'task_update_show_titles', 'display_name': 'Update Show Titles'},
        {'id': 'task_get_plex_watch_history', 'display_name': 'Get Plex Watch History'},
        {'id': 'task_check_database_health', 'display_name': 'Check Database Health'},
        {'id': 'task_run_library_maintenance', 'display_name': 'Run Library Maintenance'},
        {'id': 'task_update_movie_ids', 'display_name': 'Update Movie IDs'},
        {'id': 'task_update_movie_titles', 'display_name': 'Update Movie Titles'},
        {'id': 'task_verify_symlinked_files', 'display_name': 'Verify Symlinked Files'},
        {'id': 'task_verify_plex_removals', 'display_name': 'Verify Plex Removals'},
        {'id': 'task_process_pending_rclone_paths', 'display_name': 'Process Pending Rclone Paths'},
        {'id': 'task_update_tv_show_status', 'display_name': 'Update TV Show Status'},
        {'id': 'task_heartbeat', 'display_name': 'Heartbeat'},
        {'id': 'final_check_queue', 'display_name': 'Final Check Queue'},
        {'id': 'task_analyze_library', 'display_name': 'Analyze Library'},
        {'id': 'task_overlay_sync', 'display_name': 'Overlay Sync'},
        {'id': 'task_overlay_cleanup', 'display_name': 'Overlay State Maintenance (cleanup orphaned DB records)'},
        {'id': 'task_backfill_nzb_torrent_ids', 'display_name': 'Backfill NZB Torrent IDs'},
    ]
    
    # Get content sources from program runner for content source tasks
    program_runner = ProgramRunner()
    content_sources = program_runner.get_content_sources()
    
    # Add content source tasks with display names from config
    for source_name, source_config in content_sources.items():
        if isinstance(source_config, dict) and source_config.get('enabled', False):
            task_id = f"task_{source_name}_wanted"
            
            # Use custom display name if available, otherwise format the source name
            if source_config.get('display_name'):
                display_name = f"Process Content Source: {source_config['display_name']}"
            else:
                formatted_name = ' '.join(word.capitalize() for word in source_name.split('_'))
                display_name = f"Process Content Source: {formatted_name}"
                
            task_map.append({'id': task_id, 'display_name': display_name})
    
    # For backward compatibility, also include the flat list of task IDs
    task_ids = [task['id'] for task in task_map]
    
    return jsonify({
        'tasks': task_ids,  # For backward compatibility
        'task_map': task_map  # New structured format with display names
    }), 200
