"""ProgramRunner task control: trigger/enable/disable, intervals, job listener, run measurement, load and memory regulation."""

import logging
import time
import uuid
try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc = None
    tracemalloc_available = False
try:
    import psutil
except ImportError:
    psutil = None
import threading
import apscheduler.events
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
from utilities.settings import get_setting
from datetime import datetime, timezone
try:
    import resource  # Unix only
except ImportError:
    resource = None
from queues.runner import task_registry


class TaskControlMixin:
            # Updated sections log moved to after the scan loop


    def trigger_task(self, task_name):
        """Manually trigger a task to run immediately by adding it to APScheduler's queue."""
        normalized_name = self._normalize_task_name(task_name)
        job_id_base = normalized_name # This is the base task name, e.g., "task_sync_time"

        logging.info(f"Attempting to manually trigger task: {job_id_base} by adding it to APScheduler queue.")

        target_func, args, kwargs = self._get_task_target(job_id_base, manual=True)

        if target_func:
            try:
                # Prevent duplicate concurrent runs — check if this task is already executing.
                with self._running_task_lock:
                    _already_running = any(
                        # scheduled job id is the task name; manual runs are manual_<task>_<uuid4>
                        jid == job_id_base or (jid.startswith('manual_') and jid[7:-37] == job_id_base)
                        for jid in self.currently_executing_tasks
                    )
                if _already_running:
                    logging.warning(f"Task '{job_id_base}' is already running — skipping manual trigger.")
                    return {"success": False, "message": f"Task '{job_id_base}' is already running."}

                # Generate a unique ID for this manual job instance
                manual_job_instance_id = f"manual_{job_id_base}_{uuid.uuid4()}"
                
                # Pass the unique manual_job_instance_id as the first arg (actual_job_id_from_scheduler)
                # and job_id_base as the second arg (task_name_for_logging) to _run_and_measure_task.
                # Pass the bound method + its args separately (not via functools.partial) — see the
                # matching comment in _schedule_task for why partials break APScheduler's job store.
                manual_job_args = (manual_job_instance_id, job_id_base, target_func, args, kwargs)

                # Get timezone safely, with fallback if scheduler is None
                if self.scheduler and hasattr(self.scheduler, 'timezone'):
                    scheduler_timezone = self.scheduler.timezone
                else:
                    # Fallback timezone detection
                    try:
                        from metadata.metadata import _get_local_timezone
                        scheduler_timezone = _get_local_timezone()
                        logging.warning(f"Scheduler timezone not available, using fallback: {scheduler_timezone}")
                    except Exception as e:
                        logging.error(f"Failed to get fallback timezone: {e}, using UTC")
                        scheduler_timezone = timezone.utc
                
                run_now_date = datetime.now(scheduler_timezone)

                with self.scheduler_lock:
                    # Ensure scheduler exists before adding job
                    if not self.scheduler:
                        logging.error(f"Cannot trigger task '{job_id_base}': Scheduler is not initialized")
                        raise RuntimeError(f"Scheduler not initialized for manual task '{job_id_base}'")
                    
                    _manual_executor = task_registry.executor_for(job_id_base)
                    self.scheduler.add_job(
                        func=self._run_and_measure_task,
                        args=manual_job_args,
                        trigger='date',
                        run_date=run_now_date,
                        id=manual_job_instance_id,
                        name=f"Manual run of {job_id_base}",
                        replace_existing=False,
                        max_instances=1,
                        misfire_grace_time=600,
                        executor=_manual_executor,
                    )
                        
                    logging.info(f"Task '{job_id_base}' (Manual Job ID: {manual_job_instance_id}) successfully queued for immediate execution via APScheduler.")
                    return {"success": True, "message": f"Task '{job_id_base}' queued for execution.", "job_id": manual_job_instance_id}

            except Exception as e:
                logging.error(f"Error submitting manual task '{job_id_base}' to APScheduler: {e}", exc_info=True)
                raise RuntimeError(f"Failed to queue manual task '{job_id_base}': {e}")
        else:
            logging.error(f"Could not determine target function for manual trigger of '{job_id_base}'")
            if job_id_base not in self.task_intervals:
                raise ValueError(f"Task '{job_id_base}' is not defined. Cannot queue.")
            else:
                raise ValueError(f"Task function for '{job_id_base}' not found despite task being defined.")

    def enable_task(self, task_name):
        """Enable a task by adding/resuming its job in the scheduler."""
        current_thread_id_outer = threading.get_ident()
        normalized_name = self._normalize_task_name(task_name)
        job_id = normalized_name
        logging.info(f"ENABLE_TASK: Attempting to enable task '{normalized_name}' (Job ID: {job_id}) (Thread: {current_thread_id_outer}).")

        if normalized_name not in self.task_intervals:
             logging.error(f"ENABLE_TASK: Cannot enable task '{normalized_name}': No interval defined. (Thread: {current_thread_id_outer})")
             return False

        current_thread_id_before_lock = threading.get_ident()
        logging.info(f"ENABLE_TASK: Preparing to acquire scheduler_lock for '{normalized_name}'. (Thread: {current_thread_id_before_lock})")
        with self.scheduler_lock:
            current_thread_id_after_lock = threading.get_ident()
            logging.info(f"ENABLE_TASK: Acquired scheduler_lock for '{normalized_name}'. (Thread: {current_thread_id_after_lock})")
            job = self.scheduler.get_job(job_id)
            if job and not getattr(self.scheduler, 'running', True):
                # Program stopped: a pending job is already set to run once it starts.
                self.enabled_tasks.add(normalized_name)
                return True
            if job:
                 logging.debug(f"ENABLE_TASK: Job '{job_id}' exists. (Thread: {current_thread_id_after_lock})")
                 if job.next_run_time is not None: # Job exists and is scheduled (not paused indefinitely)
                     logging.info(f"ENABLE_TASK: Task '{normalized_name}' is already scheduled and enabled. (Thread: {current_thread_id_after_lock})")
                     # Ensure it's in our enabled_tasks set
                     if normalized_name not in self.enabled_tasks: self.enabled_tasks.add(normalized_name)
                     return True
                 else: # Job exists but is paused
                     logging.info(f"ENABLE_TASK: Job '{job_id}' exists but is paused. Resuming. (Thread: {current_thread_id_after_lock})")
                     try:
                         self.scheduler.resume_job(job_id)
                         self.enabled_tasks.add(normalized_name) # Add to set
                         # Remove from manual pause set if it was there
                         if job_id in self.paused_jobs_by_queue: self.paused_jobs_by_queue.remove(job_id)
                         logging.info(f"ENABLE_TASK: Resumed existing paused job for task: {normalized_name} (Thread: {current_thread_id_after_lock})")
                         return True
                     except Exception as e_resume:
                         logging.error(f"ENABLE_TASK: Error resuming job '{job_id}': {e_resume} (Thread: {current_thread_id_after_lock})", exc_info=True)
                         return False
            else: # Job doesn't exist, need to add it
                 logging.info(f"ENABLE_TASK: Job '{job_id}' does not exist. Scheduling new job. (Thread: {current_thread_id_after_lock})")
                 interval = self.task_intervals.get(normalized_name)
                 if interval:
                     logging.debug(f"ENABLE_TASK: Interval for new job '{normalized_name}' is {interval}s. Calling _schedule_task. (Thread: {current_thread_id_after_lock})")
                     if self._schedule_task(normalized_name, interval): # Use the schedule method
                         self.enabled_tasks.add(normalized_name) # Add to set
                         logging.info(f"ENABLE_TASK: Scheduled and enabled new task: {normalized_name} (Thread: {current_thread_id_after_lock})")
                         return True
                     else:
                         logging.error(f"ENABLE_TASK: Failed to schedule new job for task: {normalized_name} (Thread: {current_thread_id_after_lock})")
                         return False
                 else:
                     logging.error(f"ENABLE_TASK: Interval not found for task '{normalized_name}' during enable. (Thread: {current_thread_id_after_lock})")
                     return False
        current_thread_id_finally = threading.get_ident()
        logging.info(f"ENABLE_TASK: Finished attempt to enable task '{normalized_name}'. Lock released implicitly. (Thread: {current_thread_id_finally})")
        return False # Should have returned earlier in most cases

    def disable_task(self, task_name):
        """Disable a task by pausing its job in the scheduler."""
        normalized_name = self._normalize_task_name(task_name)
        job_id = normalized_name

        # Don't allow disabling essential tasks? Or handle via UI?
        # essential = {'task_heartbeat', 'task_check_service_connectivity', ...}
        # if job_id in essential:
        #    logging.warning(f"Cannot disable essential task: {job_id}")
        #    return False

        if normalized_name not in self.task_intervals:
             # Should not happen if task was previously enabled, but good practice
             logging.warning(f"Task '{normalized_name}' not found in intervals. Cannot disable.")
             # Still ensure it's removed from enabled_tasks set if present
             if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name)
             return True # Consider successful if not defined/already disabled

        with self.scheduler_lock:
            # Disabled while the queue is paused: resume_queue must not resume it.
            self.paused_jobs_by_queue.discard(job_id)
            job = self.scheduler.get_job(job_id)
            if job and not getattr(self.scheduler, 'running', True):
                # Program stopped: jobs are still pending and have no next_run_time yet.
                self.scheduler.remove_job(job_id)
                self.enabled_tasks.discard(normalized_name)
                logging.info(f"Removed pending job for task: {normalized_name}")
                return True
            if job:
                 if job.next_run_time is None: # Already paused
                      logging.info(f"Task '{normalized_name}' job is already paused.")
                      # Ensure it's removed from enabled_tasks set
                      if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name)
                      return True
                 else: # Job exists and is running, pause it
                      try:
                          self.scheduler.pause_job(job_id)
                          if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name) # Remove from set
                          logging.info(f"Paused job for task: {normalized_name}")
                          return True
                      except Exception as e:
                          logging.error(f"Error pausing job '{job_id}': {e}")
                          return False
            else: # Job doesn't exist
                 logging.info(f"Task '{normalized_name}' job not found (already removed or never scheduled). Considered disabled.")
                 # Ensure it's removed from enabled_tasks set
                 if normalized_name in self.enabled_tasks: self.enabled_tasks.remove(normalized_name)
                 return True

    def _apply_saved_intervals(self):
        """Apply custom intervals (seconds) from task_intervals.json to self.task_intervals."""
        import json
        from routes.program_operation_routes import _get_task_intervals_file_path
        path = _get_task_intervals_file_path()
        try:
            with open(path) as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logging.error(f"Error loading custom task intervals from {path}: {e}")
            return
        applied = 0
        for task_name, seconds in (saved or {}).items():
            name = self._normalize_task_name(task_name)
            if name not in self.task_intervals or seconds is None:
                continue
            try:
                seconds = int(seconds)
            except (TypeError, ValueError):
                logging.warning(f"Skipping invalid custom interval for '{name}': {seconds!r}")
                continue
            if seconds < 1:
                continue
            self.task_intervals[name] = seconds
            applied += 1
        if applied:
            logging.info(f"Applied {applied} custom task intervals from {path}")

    def _normalize_task_name(self, task_name):
        """Normalize task name to match how it's stored internally."""
        # Ensure queue_processing_map exists before trying to access it
        if not hasattr(self, 'queue_processing_map'):
             # This might happen if called extremely early, though unlikely now
             logging.error("_normalize_task_name called before queue_processing_map was defined!")
             # Fallback: try other normalization rules without map check
        else:
            # Handle queue tasks (which might be passed without task_ prefix)
            # Use the map keys now
            for queue_name_key in self.queue_processing_map.keys():
                if task_name.lower() == queue_name_key.lower():
                    return queue_name_key # Return the canonical name used as the key/job_id

        # Handle task_ prefix for other tasks
        if task_name.startswith('task_'):
            # Check if it's a known task (including the combined one)
            if task_name in self.task_intervals:
                 return task_name
            # Maybe it's a content source task passed with underscores?
            elif '_wanted' in task_name:
                 # Try replacing underscores with spaces (except first one) for content sources
                 parts = task_name.split('_')
                 if len(parts) > 2 and parts[0] == 'task' and parts[-1] == 'wanted':
                     # Reconstruct potential name with spaces (handle multi-word sources)
                     # Example: task_My_Overseerr_Instance_wanted -> task_My Overseerr Instance_wanted
                     content_part = "_".join(parts[1:-1]) # Get 'My_Overseerr_Instance'
                     # This simple reconstruction might fail if source names have underscores themselves
                     # A better approach would be to iterate self.task_intervals keys if needed.
                     # For now, assume simple cases or that keys match `task_..._wanted` format.
                     # Let's just check if the original task_name is in intervals again, as that's the primary key format.
                     if task_name in self.task_intervals:
                           return task_name
                 # Fallback: return original if complex content source name check failed or not found
                 # Check if original name exists before warning
                 if task_name in self.task_intervals: return task_name

            # If it starts with task_ but isn't found, return as is but maybe log?
            # Let's assume if it starts with task_ it should be in intervals if valid.

        # Try adding task_ prefix if not present
        potential_task_name = f'task_{task_name}'
        if potential_task_name in self.task_intervals:
            return potential_task_name

        # Handle potential content source task passed without prefix/suffix
        potential_content_source_task = f'task_{task_name}_wanted'
        if potential_content_source_task in self.task_intervals:
             return potential_content_source_task
        # Handle content source task passed with spaces needing underscores
        potential_content_source_task_underscores = f'task_{task_name.replace(" ", "_")}_wanted'
        if potential_content_source_task_underscores in self.task_intervals:
             return potential_content_source_task_underscores


        # If no match found after all checks, return the original input
        # logging.warning(f"Could not normalize task name '{task_name}' to a known task key.")
        return task_name # Return original if no normalization rule matched

    # def task_update_statistics_summary(self):
    #     """Update the statistics summary table for faster statistics page loading"""
    #     try:
    #         # Use the directly imported function with force=True
    #         from database.statistics import update_statistics_summary
    #         update_statistics_summary(force=True)
    #         logging.debug("Scheduled statistics summary update complete")
    #     except Exception as e:
    #         logging.error(f"Error updating statistics summary: {str(e)}")


    # --- END: New Task Implementation ---


    # Add Listener Method
    def _job_listener(self, event: apscheduler.events.JobEvent):
        """Listener called for various job events to track executing tasks."""
        task_id_for_log = getattr(event, 'job_id', 'unknown_job')
        event_code = event.code 
        is_manual_task = task_id_for_log.startswith('manual_')
        log_prefix = f"APScheduler event for task '{task_id_for_log}':"

        if event_code == EVENT_JOB_SUBMITTED:
            if task_id_for_log != 'Wanted' and task_id_for_log != 'Scraping' and task_id_for_log != 'Adding':
                logging.info(f"{log_prefix} Job Submitted.")
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.add(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' added to manual_tasks set.")
            return # Submitted jobs don't proceed to interval adjustment logic

        elif event_code == EVENT_JOB_EXECUTED: # Successful completion
            if task_id_for_log != 'Wanted' and task_id_for_log != 'Scraping' and task_id_for_log != 'Adding':
                logging.info(f"{log_prefix} Job Executed Successfully.")
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set after successful execution.")
                    # DO NOT return here; successful executions proceed to interval adjustment logic below.

        elif event_code == EVENT_JOB_ERROR:
            exception_info = getattr(event, 'exception', 'N/A')
            logging.error(f"{log_prefix} Job Errored. Exception: {exception_info}", exc_info=True if event.exception else False)
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set due to error.")
            return # Errors skip interval adjustment logic

        elif event_code == EVENT_JOB_MISSED:
            logging.warning(f"{log_prefix} Job Missed.")
            if is_manual_task:
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set due to being missed.")
            return # Missed jobs skip interval adjustment logic

        elif event_code == EVENT_JOB_MAX_INSTANCES:
            logging.warning(f"{log_prefix} Job Max Instances Reached (skipped).")
            if is_manual_task: # If a manual task was somehow submitted then skipped for max_instances
                with self._running_task_lock: # Lock for modifying manual_tasks
                    self.manual_tasks.discard(task_id_for_log)
                    logging.debug(f"Manual task '{task_id_for_log}' removed from manual_tasks set due to max instances.")
            return # Max_instances skips interval adjustment logic
        
        else: # Other unknown event codes
            logging.debug(f"{log_prefix} Unhandled event code {event_code}.")
            return # Unhandled events skip interval adjustment logic

        # Original comment: Handle Interval Adjustment (Only for successful execution)
        # The logic here ensures that only EVENT_JOB_EXECUTED reaches this point.
        # ... (rest of interval adjustment logic remains the same) ...

    # Modify _run_and_measure_task for tracemalloc sampling AND heavy task locking
    def _run_and_measure_task(self, actual_job_id_from_scheduler, task_name_for_logging, func, args, kwargs): # Added task_name_for_log
        """Wraps a task function to measure execution duration, track memory usage with tracemalloc, and handle locking for heavy DB tasks."""
        start_time = time.monotonic()
        
        log_display_name = task_name_for_logging
        if actual_job_id_from_scheduler != task_name_for_logging: # True for manual tasks
            log_display_name = f"{task_name_for_logging} (Job ID: {actual_job_id_from_scheduler})"

        mem_before = 0
        mem_after = 0
        run_tracemalloc_sample = False # Flag to indicate if we run tracemalloc this time
        # lock_acquired = False # Flag to track if heavy task lock was acquired # REVERTED

        # --- Heavy Task Lock Handling ---
        # is_heavy_task = task_name_for_log in self.HEAVY_DB_TASKS # REVERTED - Scheduler handles global queue
        # if is_heavy_task: # REVERTED
            # logging.debug(f"Task '{task_name_for_logging}' requires heavy task lock. Attempting acquisition...") # REVERTED
            # lock_acquired = self.heavy_task_lock.acquire(blocking=False) # REVERTED
            # if not lock_acquired: # REVERTED
                 # logging.info(f"Skipping heavy task '{task_name_for_logging}' execution: Another heavy task is currently running.") # REVERTED
                 # return # Skip execution if lock not acquired # REVERTED
            # else: # REVERTED
                 # logging.info(f"Heavy task lock acquired for '{task_name_for_logging}'. Proceeding with execution.") # REVERTED
        # --- End Heavy Task Lock Handling ---

        # Manage currently_executing_tasks
        with self._running_task_lock:
            self.currently_executing_tasks.add(actual_job_id_from_scheduler)
            if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
                logging.info(f"Task '{log_display_name}' started execution, added to currently_executing_tasks.")

        # Record start time for UI stifling
        # 'start_time' is already time.monotonic() from the beginning of this function
        with self._executing_task_start_times_lock:
            self.executing_task_start_times[actual_job_id_from_scheduler] = start_time 
        if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
            logging.debug(f"Task '{log_display_name}' start time {start_time:.3f} recorded for UI stifling.")

        # Determine if we should sample this execution
        # Check if enabled AND available AND actually tracing
        if self._tracemalloc_enabled and tracemalloc_available and tracemalloc and tracemalloc.is_tracing():
            self.task_execution_count += 1
            # Check if the current count is a multiple of the sample rate
            if self.task_execution_count % self.tracemalloc_sample_rate == 0:
                run_tracemalloc_sample = True
                # Log when a sample is being taken for visibility
                logging.info(f"[Tracemalloc] Sampling task '{log_display_name}' (Execution #{self.task_execution_count})")

        # Get memory usage before if sampling this execution
        # Check available again just before use
        if run_tracemalloc_sample and tracemalloc_available and tracemalloc:
            try:
                mem_before, _ = tracemalloc.get_traced_memory()
            except Exception as e_mem:
                logging.error(f"[Tracemalloc] Error getting memory before task '{log_display_name}': {e_mem}")
                run_tracemalloc_sample = False # Don't try 'after' if 'before' failed
        elif run_tracemalloc_sample:
             # Should not happen if checks above are correct, but log defensively
             logging.warning(f"[Tracemalloc] Attempted sample for '{log_display_name}', but tracemalloc not available/tracing at point of memory check.")
             run_tracemalloc_sample = False

        # Capture CPU time before task execution
        cpu_start = self._get_current_thread_cpu_seconds()

        try:
            # Execute the original task function
            func(*args, **kwargs)
            duration = time.monotonic() - start_time # Measure duration regardless
            # Record runtime on successful completion
            self._record_task_runtime(task_name_for_logging, duration)

            # Persist next-run time for tasks with interval >= 20 min so the schedule
            # survives restarts. Only for regular scheduled tasks (not manual runs).
            _task_interval = self.task_intervals.get(task_name_for_logging, 0)
            if _task_interval >= 1200 and actual_job_id_from_scheduler == task_name_for_logging:
                self._save_task_schedule(task_name_for_logging, time.time() + _task_interval)

            # Get memory usage after and log delta if sampling this execution
            # Check available again just before use
            if run_tracemalloc_sample and tracemalloc_available and tracemalloc:
                try:
                    mem_after, _ = tracemalloc.get_traced_memory()
                    mem_delta = mem_after - mem_before
                    mem_delta_mb = mem_delta / (1024 * 1024)
                    mem_before_mb = mem_before / (1024 * 1024)
                    mem_after_mb = mem_after / (1024 * 1024)

                    log_level = logging.INFO if abs(mem_delta_mb) < 1 else logging.WARNING # Log higher if delta > 1MB
                    # Added [Tracemalloc Sample] prefix for clarity
                    logging.log(log_level, f"Task '{log_display_name}' completed in {duration:.3f}s. [Tracemalloc Sample] Mem Before: {mem_before_mb:.2f}MB, Mem After: {mem_after_mb:.2f}MB, Delta: {mem_delta_mb:+.2f}MB")

                    # If memory increased significantly during the sample, log top allocations
                    if mem_delta > 1024 * 1024: # Log top allocations if increase > 1MB (adjust threshold if needed)
                        snapshot = tracemalloc.take_snapshot()
                        # Log top allocations from the end snapshot. Comparing snapshots adds complexity.
                        top_stats = snapshot.statistics('lineno')
                        logging.warning(f"[Tracemalloc] Task '{log_display_name}' sample showed positive memory delta > 1MB. Top 5 allocations at end:")
                        for i, stat in enumerate(top_stats[:5], 1):
                            # Limit traceback line length for cleaner logs
                            trace_line = stat.traceback.format()[-1]
                            trace_line = trace_line[:200] + '...' if len(trace_line) > 200 else trace_line
                            logging.warning(f"  {i}: {trace_line} - Size: {stat.size / 1024:.1f} KiB, Count: {stat.count}")

                except Exception as e_mem:
                    logging.error(f"[Tracemalloc] Error getting memory after task '{log_display_name}': {e_mem}")
            elif run_tracemalloc_sample:
                # Log if we intended to sample but tracemalloc became unavailable
                 logging.warning(f"[Tracemalloc] Attempted sample for '{log_display_name}', but tracemalloc not available at point of 'after' memory check.")

            # Capture CPU time after task execution
            cpu_end = self._get_current_thread_cpu_seconds()
            cpu_seconds = cpu_end - cpu_start
            cpu_util = (cpu_seconds / duration) * 100 if duration > 0 else 0
            self._record_task_cpu(task_name_for_logging, cpu_seconds)
            if self._log_per_run_cpu:
                logging.debug(f"[CPU] Task '{log_display_name}' cpu={cpu_seconds:.3f}s, wall={duration:.3f}s, util={cpu_util:.1f}%")

            # Optional: Log normal duration if not sampling (can be noisy)
            # else:
            #    logging.debug(f"Task '{log_display_name}' completed successfully in {duration:.3f}s (No tracemalloc sample this time)")

            return duration # Return duration for the listener

        except Exception as e:
            duration = time.monotonic() - start_time
            logging.error(f"Error during execution of job '{log_display_name}': {e}", exc_info=True)
            # Record runtime even on error
            self._record_task_runtime(task_name_for_logging, duration)

            # Log memory even on error if sampling this execution
            # Check available again just before use
            if run_tracemalloc_sample and tracemalloc_available and tracemalloc:
                 try:
                     mem_after, _ = tracemalloc.get_traced_memory()
                     # Note: mem_before might be 0 if the 'before' call failed
                     mem_delta = mem_after - mem_before
                     mem_delta_mb = mem_delta / (1024 * 1024)
                     mem_before_mb = mem_before / (1024 * 1024)
                     mem_after_mb = mem_after / (1024 * 1024)
                     logging.error(f"[Tracemalloc] Memory state after error in '{log_display_name}'. Mem Before: {mem_before_mb:.2f}MB, Mem After: {mem_after_mb:.2f}MB, Delta: {mem_delta_mb:+.2f}MB")
                 except Exception as e_mem_err:
                     logging.error(f"[Tracemalloc] Error getting memory after task error in '{log_display_name}': {e_mem_err}")
            elif run_tracemalloc_sample:
                 # Log if we intended to sample but tracemalloc became unavailable
                 logging.warning(f"[Tracemalloc] Attempted sample for '{log_display_name}', but tracemalloc not available at point of error memory check.")

            # Capture CPU time after task execution on error
            cpu_end = self._get_current_thread_cpu_seconds()
            cpu_seconds = cpu_end - cpu_start
            cpu_util = (cpu_seconds / duration) * 100 if duration > 0 else 0
            self._record_task_cpu(task_name_for_logging, cpu_seconds)
            if self._log_per_run_cpu:
                logging.debug(f"[CPU] Task '{log_display_name}' cpu={cpu_seconds:.3f}s, wall={duration:.3f}s, util={cpu_util:.1f}%")

            raise # Re-raise the exception
        finally:
            # Manage currently_executing_tasks
            with self._running_task_lock:
                self.currently_executing_tasks.discard(actual_job_id_from_scheduler)
                if log_display_name != 'Wanted' and log_display_name != 'Scraping' and log_display_name != 'Adding':
                    logging.info(f"Task '{log_display_name}' finished execution, removed from currently_executing_tasks.")
                    
            # Add inter-task sleep for low power mode
            try:
                inter_task_sleep_seconds = self.current_inter_task_sleep
                if inter_task_sleep_seconds > 0:
                    logging.debug(f"Sleeping for {inter_task_sleep_seconds:.2f}s after task '{log_display_name}' due to current inter-task sleep setting.")
                    time.sleep(inter_task_sleep_seconds)
            except (ValueError, TypeError):
                # If setting is invalid, don't sleep
                pass

            # Clear start time for UI stifling
            with self._executing_task_start_times_lock:
                removed_start_time = self.executing_task_start_times.pop(actual_job_id_from_scheduler, None)
            if removed_start_time is None:
                logging.warning(f"Task '{log_display_name}' was not found in executing_task_start_times upon completion/error for UI stifling.")
            elif log_display_name not in ['Wanted', 'Scraping', 'Adding']:
                logging.debug(f"Task '{log_display_name}' start time removed for UI stifling.")

            # --- Release heavy task lock if acquired ---
            # if lock_acquired: # REVERTED
                # try: # REVERTED
                    # self.heavy_task_lock.release() # REVERTED
                    # logging.info(f"Heavy task lock released for '{task_name_for_logging}'.") # REVERTED
                # except Exception as e_release: # REVERTED
                    # Should not happen if lock_acquired is True, but log defensively # REVERTED
                    # logging.error(f"Error releasing heavy task lock for '{task_name_for_logging}': {e_release}") # REVERTED
            # --- End Release heavy task lock --- # REVERTED

    def task_regulate_system_load(self):
        """Monitors CPU and RAM usage and dynamically adjusts inter-task sleep time to regulate system load."""
        if not psutil:
            logging.warning("Cannot regulate system load: psutil library is not installed.")
            self.disable_task('task_regulate_system_load')
            return

        base_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))

        # Get regulation parameters
        cpu_threshold = int(get_setting('System Load Regulation', 'cpu_threshold_percent', 75))
        ram_threshold = int(get_setting('System Load Regulation', 'ram_threshold_percent', 75))
        increase_step = float(get_setting('System Load Regulation', 'regulation_increase_step_seconds', 1.0))
        decrease_step = float(get_setting('System Load Regulation', 'regulation_decrease_step_seconds', 1.0))
        max_sleep = float(get_setting('System Load Regulation', 'regulation_max_sleep_seconds', 60.0))

        # Get system usage - non-blocking
        try:
            cpu_usage = psutil.cpu_percent(interval=None)
            ram_usage = psutil.virtual_memory().percent
        except Exception as e:
            logging.error(f"Error getting system usage: {e}")
            return

        high_load = False
        if cpu_usage > cpu_threshold:
            logging.warning(f"CPU usage ({cpu_usage:.1f}%) exceeds threshold ({cpu_threshold}%). Increasing inter-task sleep.")
            high_load = True
        
        if ram_usage > ram_threshold:
            logging.warning(f"RAM usage ({ram_usage:.1f}%) exceeds threshold ({ram_threshold}%). Increasing inter-task sleep.")
            high_load = True

        if high_load:
            new_sleep = self.current_inter_task_sleep + increase_step
            self.current_inter_task_sleep = min(new_sleep, max_sleep)
            logging.info(f"System load high. Inter-task sleep increased to {self.current_inter_task_sleep:.2f}s")
        else:
            if self.current_inter_task_sleep > base_sleep:
                new_sleep = self.current_inter_task_sleep - decrease_step
                self.current_inter_task_sleep = max(new_sleep, base_sleep)
                logging.debug(f"System load normal. Inter-task sleep adjusted to {self.current_inter_task_sleep:.2f}s")

    # Add method for live interval updates
    def update_task_interval(self, task_name: str, interval_seconds: int | None):
        """
        Updates the interval (in seconds) for a specific task live.
        If interval_seconds is None, resets to the default interval.
        """
        normalized_name = self._normalize_task_name(task_name)

        if normalized_name not in self.original_task_intervals:
            logging.error(f"Cannot update interval for '{normalized_name}': Task not defined.")
            return False

        # Define minimum seconds
        MIN_INTERVAL_SECONDS = 1 # Must match validation

        target_interval_seconds = 0
        is_resetting = interval_seconds is None

        if is_resetting:
            target_interval_seconds = self.original_task_intervals.get(normalized_name)
            logging.info(f"Resetting task '{normalized_name}' interval to default: {target_interval_seconds}s")
            if normalized_name in self.task_intervals:
                 self.task_intervals[normalized_name] = target_interval_seconds
        else:
            # Validate the provided seconds
            try:
                interval_sec_int = int(interval_seconds)
                 # Validate seconds
                if interval_sec_int >= MIN_INTERVAL_SECONDS:
                    target_interval_seconds = interval_sec_int
                else:
                    logging.error(f"Invalid interval for '{normalized_name}': {interval_seconds} (must be >= {MIN_INTERVAL_SECONDS} seconds). Cannot apply live update.")
                    return False
            except (ValueError, TypeError):
                logging.error(f"Invalid interval format for '{normalized_name}': {interval_seconds}. Cannot apply live update.")
                return False

            logging.info(f"Updating task '{normalized_name}' interval live to: {target_interval_seconds}s")
            self.task_intervals[normalized_name] = target_interval_seconds # Update internal map

        # Apply the change to the scheduler. _schedule_task replaces the job with the same
        # jitter and timezone as a normal schedule; a paused (disabled) job stays paused.
        with self.scheduler_lock:
            job = self.scheduler.get_job(normalized_name)
            if not job:
                logging.info(f"Task '{normalized_name}' is not currently scheduled. Interval preference updated internally.")
                return True
            was_paused = job.next_run_time is None
            if not self._schedule_task(normalized_name, target_interval_seconds):
                logging.error(f"Error rescheduling job '{normalized_name}' with new interval.")
                return False
            if was_paused:
                self.scheduler.pause_job(normalized_name)
            logging.info(f"Successfully rescheduled task '{normalized_name}' with new interval {target_interval_seconds}s.")
            return True

    def task_trim_memory(self):
        """Run gc.collect() + malloc_trim(0) to return glibc arena pages to the OS.

        Heavy operations (upgrade hub scan, debrid manager audit, rclone file count, etc.)
        allocate large temporary objects that Python frees via GC but glibc holds in its
        malloc arenas indefinitely. This task forces both layers to release those pages,
        keeping the baseline RSS stable over long uptimes.
        """
        import gc

        def _read_current_rss_kb():
            """Read current RSS from /proc/self/status (VmRSS), not the peak high-water mark."""
            try:
                with open('/proc/self/status') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            return int(line.split()[1])
            except Exception:
                pass
            return None

        import time as _time
        before_rss = _read_current_rss_kb()

        # Evict RD library stable cache if idle for 2+ hours
        lib_torrents_freed = 0
        try:
            from routes.debrid_manager_routes import _lib, _lib_last_accessed
            idle_secs = _time.time() - _lib_last_accessed
            if _lib['stable'] is not None and idle_secs > 7200:
                with _lib['lock']:
                    if _lib['stable'] is not None:
                        lib_torrents_freed = len(_lib['stable'].get('torrents', []))
                        _lib['stable'] = None
                logging.info(f"[TRIM_MEMORY] Evicted idle RD library cache ({lib_torrents_freed} torrents, idle {idle_secs/3600:.1f}h)")
        except Exception as e:
            logging.debug(f"[TRIM_MEMORY] Could not evict RD library cache: {e}")

        collected = gc.collect()

        trim_ok = False
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
            trim_ok = True
        except Exception as e:
            logging.debug(f"[TRIM_MEMORY] malloc_trim unavailable: {e}")

        after_rss = _read_current_rss_kb()

        if before_rss and after_rss:
            freed_kb = before_rss - after_rss
            logging.info(f"[TRIM_MEMORY] gc collected {collected} objects; malloc_trim={'ok' if trim_ok else 'unavailable'}; RSS delta: {freed_kb/1024:.1f} MB (before={before_rss//1024} MB, after={after_rss//1024} MB)")
        else:
            logging.info(f"[TRIM_MEMORY] gc collected {collected} objects; malloc_trim={'ok' if trim_ok else 'unavailable'}")

    def _fail_safe_resume_if_stuck(self):
        """Force-resume the queue if it has been paused due to connectivity issues for too long.

        The normal resume flow relies on periodic connectivity checks.  If, for any
        reason, those checks fail to un-pause the queue even after connectivity is
        restored (for example because the check itself is failing), this method
        will act as a watchdog.  Once the pause has lasted longer than
        `Queue -> connectivity_fail_safe_minutes` (defaults to 3 minutes) it
        will clear the pause state and invoke `resume_queue()` unconditionally.
        """
        try:
            # Only act when the queue is actually paused
            if not self.queue_paused:
                return
                
            # Check all pause types, not just CONNECTION_ERROR
            current_pause_type = self.pause_info.get("error_type") if self.pause_info else None
            
            # For scheduled pauses, don't use fail-safe. A DB_HEALTH pause is held until
            # task_check_database_health sees a healthy database again.
            if current_pause_type in ("SYSTEM_SCHEDULED", "DB_HEALTH"):
                return

            # How long has it been since the connectivity failure was first detected?
            if not self.connectivity_failure_time:
                # If we're paused but don't have a failure time, something's wrong
                if self.queue_paused and current_pause_type in ["CONNECTION_ERROR", "UNAUTHORIZED", "FORBIDDEN", "DB_HEALTH"]:
                    logging.warning(f"Queue is paused ({current_pause_type}) but no failure time tracked. Setting failure time now.")
                    self.connectivity_failure_time = time.time()
                return

            elapsed = time.time() - self.connectivity_failure_time
            from utilities.settings import get_setting  # Local import to avoid cycles
            try:
                threshold_minutes = float(get_setting('Queue', 'connectivity_fail_safe_minutes', 3))
            except (ValueError, TypeError):
                threshold_minutes = 3.0
            threshold_seconds = threshold_minutes * 60

            # Log periodically that we're still stuck
            if not hasattr(self, '_last_failsafe_log_time'):
                self._last_failsafe_log_time = 0
                
            if elapsed > 60 and time.time() - self._last_failsafe_log_time > 60:
                logging.warning(
                    f"[Fail-safe] Queue has been paused for {elapsed/60:.1f} minutes "
                    f"(threshold: {threshold_minutes} minutes). Type: {current_pause_type}"
                )
                self._last_failsafe_log_time = time.time()

            if elapsed < threshold_seconds:
                return  # Still within grace period – keep waiting

            logging.warning(
                f"[Fail-safe] WATCHDOG TRIGGERED: Queue has been paused for "
                f"{elapsed/60:.1f} minutes (>{threshold_minutes} minute threshold). "
                f"Pause type: {current_pause_type}. Forcibly resuming!"
            )

            # Clear connectivity tracking and pause info before resuming
            self.connectivity_failure_time = None
            self.connectivity_retry_count = 0
            self.pause_info = {
                "reason_string": None,
                "error_type": None,
                "service_name": None,
                "status_code": None,
                "retry_count": 0,
            }
            self._last_failsafe_log_time = 0
            
            # Attempt to resume irrespective of the current connectivity check result
            self.resume_queue()
            
            logging.info("[Fail-safe] Queue forcibly resumed by watchdog. Services may still be unavailable.")
        except Exception as e:
            logging.error(f"Error in fail-safe resume logic: {e}", exc_info=True)

    def _record_task_runtime(self, task_name: str, duration_seconds: float):
        """Accumulate runtime and periodically log per-task percentage."""
        now = time.monotonic()
        with self.task_runtime_lock:
            self.task_runtime_totals[task_name] += duration_seconds
            if now - self._last_runtime_log_time >= self._runtime_log_interval_sec:
                self._emit_task_runtime_report_locked(now)

    def _emit_task_runtime_report_locked(self, now: float):
        """Assumes task_runtime_lock held. Emits report and resets counters."""
        if not self.task_runtime_totals:
            self._last_runtime_log_time = now
            return
        total = sum(self.task_runtime_totals.values())
        if total <= 0:
            self.task_runtime_totals.clear()
            self._last_runtime_log_time = now
            return
        parts = [f"{t}={v / total * 100:.1f} %" for t, v in sorted(self.task_runtime_totals.items(), key=lambda x: x[1], reverse=True)]
        logging.info(f"[RUNTIME] Last {self._runtime_log_interval_sec} s: "+", ".join(parts)+f"  (total={total:.1f} s)")
        self.task_runtime_totals.clear()
        self._last_runtime_log_time = now

    # Add method to get current thread CPU seconds
    def _get_current_thread_cpu_seconds(self):
        """Get the current thread's CPU time in seconds."""
        try:
            if resource is not None and hasattr(resource, 'getrusage'):
                if hasattr(resource, 'RUSAGE_THREAD'):
                    return resource.getrusage(resource.RUSAGE_THREAD).ru_utime
                else:
                    return resource.getrusage(resource.RUSAGE_SELF).ru_utime
            else:
                return time.process_time()
        except Exception as e:
            logging.error(f"Error getting current thread CPU time: {e}", exc_info=True)
            return 0

    # Add method to record task CPU seconds
    def _record_task_cpu(self, task_name: str, cpu_seconds: float):
        """Accumulate task CPU seconds and periodically log per-task CPU share."""
        now = time.monotonic()
        with self.task_cpu_lock:
            self.task_cpu_totals[task_name] += cpu_seconds
            if now - self._last_cpu_log_time >= self._cpu_log_interval_sec:
                self._emit_task_cpu_report_locked(now)

    def _emit_task_cpu_report_locked(self, now: float):
        """Assumes task_cpu_lock held. Emits report and resets counters."""
        if not self.task_cpu_totals:
            self._last_cpu_log_time = now
            return
        total = sum(self.task_cpu_totals.values())
        if total <= 0:
            self.task_cpu_totals.clear()
            self._last_cpu_log_time = now
            return
        parts = [f"{t}={v / total * 100:.1f} % ({v:.2f}s)" for t, v in sorted(self.task_cpu_totals.items(), key=lambda x: x[1], reverse=True)]
        logging.info(f"[CPU] Last {self._cpu_log_interval_sec} s: "+", ".join(parts)+f"  (total_cpu={total:.2f} s)")
        self.task_cpu_totals.clear()
        self._last_cpu_log_time = now
