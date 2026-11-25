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
    # Built from the task registry, so new tasks show up and removed ones go away.
    # The old hand-written list had drifted: it offered two tasks that no longer
    # exist and missed about 25 real ones.
    from queues.runner.task_registry import SPECS, QUEUE
    runner = get_program_runner()

    def _runnable(name):
        if runner is None:
            return True
        if name in getattr(runner, 'queue_processing_map', {}):
            return True
        return name.startswith('task_') and hasattr(runner, name)

    task_map = []
    for spec in SPECS.values():
        if not _runnable(spec.name):
            continue
        label = f"Queue: {spec.label}" if spec.kind == QUEUE else spec.label
        task_map.append({'id': spec.name, 'display_name': label})
    # Runnable by hand but not scheduled, so not in the registry.
    for name, label in (('task_generate_airtime_report', 'Generate airtime report'),):
        if name not in SPECS and _runnable(name):
            task_map.append({'id': name, 'display_name': label})

    # Get content sources from program runner for content source tasks
    program_runner = runner or ProgramRunner()
    content_sources = program_runner.get_content_sources() or {}
    
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
