"""ProgramRunner scheduling: task scheduling, heartbeat, pause/resume and saved schedules."""

import logging
import random
import time
import os
import ctypes
import platform
import gc
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
from apscheduler.triggers.interval import IntervalTrigger
import threading
from utilities.settings import get_setting
from datetime import datetime, time as dt_time
from queues.runner import task_registry

VIEWER_HISTORY_FIRST_RUN_DELAY = 300  # first sync soon after startup when there's no history yet


class SchedulingMixin:
    # New method to get task target
    def _get_task_target(self, task_name: str, manual: bool = False):
        """Resolves the target function and arguments for a given task name."""
        target_func = None
        args = []
        kwargs = {}
        task_type_determined = "Unknown"

        # 1. Queue Processing Tasks (using the map)
        if task_name in self.queue_processing_map:
            task_type_determined = "Queue Task (Map)"
            method_name = self.queue_processing_map[task_name]
            if hasattr(self.queue_manager, method_name):
                target_func = getattr(self.queue_manager, method_name)
                if task_name == 'Checking':
                    args = [self] # Pass ProgramRunner instance
            else:
                logging.error(f"Method '{method_name}' not found in QueueManager for task '{task_name}'")

        # 2. Content Source Tasks (task_SOURCE_wanted)
        elif task_name.startswith('task_') and task_name.endswith('_wanted'):
            task_type_determined = "Content Source Task"
            source_id = task_name[5:-7]
            if self.content_sources is None:
                self.get_content_sources(force_refresh=True)
            source_data = self.content_sources.get(source_id)
            if source_data:
                target_func = self.process_content_source
                args = [source_id, source_data]
                if manual:
                    # Explicit manual/debug-triggered runs bypass the source cache so a
                    # user flipping unblacklist_on_source_run and re-running immediately
                    # actually reaches add_wanted_items instead of being cache-skipped.
                    kwargs = {'bypass_cache': True}
            else:
                logging.warning(f"Content source data not found for source ID '{source_id}' derived from task '{task_name}'. This task will be skipped.")

        # 3. Regular task_* methods (including combined tasks and new load adjustment task)
        elif task_name.startswith('task_'):
            task_type_determined = "Regular Task (task_*)"
            if hasattr(self, task_name):
                target_func = getattr(self, task_name)
            else:
                logging.error(f"Method '{task_name}' not found in ProgramRunner")

        # Default/Error case
        else:
            task_type_determined = "ERROR - Unknown Format"
            logging.error(f"Unknown task type or name format for task resolution: '{task_name}'")

        logging.debug(f"Resolved task '{task_name}' as Type: {task_type_determined}")
        return target_func, args, kwargs

    # Use _get_task_target in _schedule_task
    def _schedule_task(self, task_name: str, interval_seconds: int, initial_run: bool = False, initial_delay_seconds: int = 0):
        """Schedules a single task in APScheduler, wrapped for duration measurement.

        Args:
            task_name: Name of the task to schedule
            interval_seconds: Interval in seconds between task runs
            initial_run: Whether this is the initial scheduling run
            initial_delay_seconds: Delay in seconds before first run (for staggering startup tasks)
        """
        # Safety check: scheduler must be initialized before scheduling tasks
        if self.scheduler is None:
            logging.warning(f"Cannot schedule task '{task_name}': scheduler is not initialized. Start the program first.")
            return False

        # When disable_initialization is enabled, delay heavy tasks by their full interval
        # This prevents release_dates and content sources from running immediately on startup
        if initial_run and initial_delay_seconds == 0:
            disable_init = get_setting('Debug', 'disable_initialization', '')
            if disable_init:
                # Heavy tasks: release_dates and content sources (_wanted tasks)
                is_heavy_task = (task_name == 'task_refresh_release_dates' or task_name.endswith('_wanted'))
                if is_heavy_task:
                    initial_delay_seconds = interval_seconds
                    logging.info(f"[disable_initialization] Delaying heavy task '{task_name}' by {interval_seconds}s (full interval)")

        current_thread_id_outer = threading.get_ident()
        logging.debug(f"Attempting to schedule task: '{task_name}' with interval {interval_seconds}s (initial_run: {initial_run}, initial_delay: {initial_delay_seconds}s) (Thread: {current_thread_id_outer})")
        lock_acquired = False # Flag to track lock acquisition
        try:
            current_thread_id = threading.get_ident()
            logging.info(f"SCHED_TASK_PRE_LOCK: Preparing to acquire scheduler_lock for task '{task_name}' (Thread: {current_thread_id})")
            with self.scheduler_lock:
                lock_acquired = True
                current_thread_id_inner = threading.get_ident() # Get ID again after lock
                logging.info(f"SCHED_TASK_POST_LOCK: Acquired scheduler_lock for task '{task_name}' (Thread: {current_thread_id_inner})")
                job_id = task_name # Use task name as job ID for regular tasks

                # Check if job already exists
                existing_job = self.scheduler.get_job(job_id)
                if existing_job:
                    if initial_run:
                        logging.debug(f"Task '{job_id}' already scheduled. Skipping initial schedule.")
                        return True
                    logging.info(f"Task '{job_id}' already exists. Removing old job before rescheduling.")
                    try:
                        self.scheduler.remove_job(job_id)
                    except Exception as e:
                        logging.error(f"Error removing existing job '{job_id}': {e}")
                        return False

                # --- Resolve target function using helper ---
                target_func, args, kwargs = self._get_task_target(task_name)
                # ------------------------------------------

                if target_func:
                    try:
                        # For regular tasks, actual_job_id and task_name_for_logging are the same (job_id).
                        # Pass the bound method + its args separately (not via functools.partial) —
                        # APScheduler's SQLAlchemyJobStore pickles jobs on start()/add_job(), and
                        # functools.partial objects can never be resolved to a serializable reference
                        # (apscheduler.util.obj_to_ref unconditionally rejects partials), which crashes
                        # scheduling with "This Job cannot be serialized...".

                        # Explicitly pass scheduler's timezone to add_job
                        # This should prevent the IntervalTrigger from calling tzlocal.get_localzone()
                        resolved_timezone = self.scheduler.timezone
                        logging.debug(f"Passing timezone '{resolved_timezone}' explicitly to add_job for task '{job_id}'")

                        # *** START STAGGER EDIT: Support initial delay for startup staggering ***
                        # Create trigger with start_date if delay is specified
                        # Add jitter to interval to prevent task alignment (±10% randomization)
                        import random
                        jitter_factor = random.uniform(0.9, 1.1)  # ±10% jitter
                        jittered_interval = int(interval_seconds * jitter_factor)

                        if initial_delay_seconds > 0:
                            # Explicit delay requested (stagger, disable_initialization, etc.) — honour it.
                            from datetime import datetime, timedelta
                            first_run_time = datetime.now(resolved_timezone) + timedelta(seconds=initial_delay_seconds)
                            trigger = IntervalTrigger(seconds=jittered_interval, start_date=first_run_time, timezone=resolved_timezone)
                            logging.info(f"Task '{job_id}' will start in {initial_delay_seconds}s (at {first_run_time.strftime('%H:%M:%S')}) for startup staggering, interval: {jittered_interval}s (jittered from {interval_seconds}s)")
                        elif initial_run:
                            # On startup, resume from the persisted next-run time so the timer continues
                            # where it left off before the restart instead of resetting to a full interval.
                            from datetime import datetime, timedelta
                            _persisted_next = getattr(self, '_task_schedules', {}).get(job_id)
                            if _persisted_next:
                                first_run_time = datetime.fromtimestamp(_persisted_next, tz=resolved_timezone)
                                trigger = IntervalTrigger(seconds=jittered_interval, start_date=first_run_time, timezone=resolved_timezone)
                                _remaining = (_persisted_next - time.time())
                                if _remaining > 0:
                                    logging.info(f"Task '{job_id}' resuming persisted schedule: next run in {_remaining/3600:.1f}h")
                                else:
                                    logging.info(f"Task '{job_id}' was overdue by {-_remaining/3600:.1f}h, will run immediately")
                            else:
                                # No persisted schedule — use exact interval for first fire so jitter
                                # doesn't cause the first run to land outside the configured interval.
                                from datetime import datetime, timedelta
                                first_run_time = datetime.now(resolved_timezone) + timedelta(seconds=interval_seconds)
                                trigger = IntervalTrigger(seconds=jittered_interval, start_date=first_run_time, timezone=resolved_timezone)
                                logging.debug(f"Task '{job_id}' no persisted schedule, first run in {interval_seconds}s, interval jittered to {jittered_interval}s")
                        else:
                            trigger = IntervalTrigger(seconds=jittered_interval, timezone=resolved_timezone)
                            if jittered_interval != interval_seconds:
                                logging.debug(f"Task '{job_id}' interval jittered: {jittered_interval}s (from {interval_seconds}s)")
                        # *** END STAGGER EDIT ***

                        # Queue-processing tasks get their own executor so they don't
                        # block scheduled maintenance tasks and vice versa.
                        _executor = task_registry.executor_for(job_id)

                        add_job_kwargs = {
                            'func': self._run_and_measure_task,
                            'args': (job_id, task_name, target_func, args, kwargs),
                            'trigger': trigger,
                            'id': job_id,
                            'name': job_id,
                            'replace_existing': True,
                            'misfire_grace_time': None,
                            'max_instances': 1,
                            'timezone': resolved_timezone,
                            'executor': _executor,
                        }

                        self.scheduler.add_job(**add_job_kwargs)

                        # Updated log message with stagger info
                        if initial_delay_seconds > 0:
                            logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds}s (staggered start: +{initial_delay_seconds}s, max_instances=1)")
                        else:
                            logging.info(f"Scheduled task '{job_id}' to run every {interval_seconds} seconds (max_instances=1, wrapped for duration measurement).")
                        return True
                    except Exception as e:
                        logging.error(f"Error scheduling task '{job_id}': {e}", exc_info=True)
                        return False
                else:
                     logging.warning(f"Failed to determine target function for task '{task_name}'. Cannot schedule. This might be an obsolete task toggle.")
                     return False
        finally:
            current_thread_id_finally = threading.get_ident()
            if lock_acquired:
                logging.info(f"SCHED_TASK_FINALLY: Releasing scheduler_lock implicitly for task '{task_name}' (Thread: {current_thread_id_finally}, Lock Acquired: True).")
            else:
                logging.info(f"SCHED_TASK_FINALLY: Lock was not acquired or error for task '{task_name}' (Thread: {current_thread_id_finally}, Lock Acquired: False).")

    def _schedule_initial_tasks(self):
        """Schedules all enabled tasks based on initial configuration and prunes obsolete content source tasks."""
        logging.info("Scheduling initial tasks and checking for obsolete toggles...")
        scheduled_count = 0
        failed_to_schedule_count = 0 # Tasks that couldn't be scheduled for various reasons (e.g. no interval)
        pruned_obsolete_task_count = 0

        # Iterate over a copy of the set to allow modification of the original self.enabled_tasks
        tasks_to_process = list(self.enabled_tasks)

        # *** START STAGGER EDIT: Calculate stagger delays for Trakt tasks ***
        # Identify all enabled Trakt tasks that need staggering on startup
        trakt_tasks_to_stagger = sorted([
            task for task in tasks_to_process
            if task.startswith('task_Trakt Lists_') or task == 'task_Trakt Collection_1'
        ])

        # Build a map of task_name -> initial_delay_seconds (always stagger, even after reinitialize)
        trakt_task_stagger_map = {}
        for idx, task_name in enumerate(trakt_tasks_to_stagger):
            delay_seconds = (idx + 1) * 240  # 240s (4 min), 480s (8 min), 720s (12 min), etc.
            trakt_task_stagger_map[task_name] = delay_seconds

        # Log stagger plan
        if trakt_task_stagger_map:
            logging.info(f"Staggering {len(trakt_task_stagger_map)} enabled Trakt tasks to prevent concurrent API calls:")
            for task_name, delay in sorted(trakt_task_stagger_map.items(), key=lambda x: x[1]):
                logging.info(f"  - {task_name}: +{delay}s delay")
        # *** END STAGGER EDIT ***

        for task_name in tasks_to_process:
            # Ensure task is still in self.enabled_tasks; it might have been removed if tasks_to_process had duplicates
            # and one was already processed and removed. However, list(set) makes duplicates unlikely.
            # This check is more of a safeguard if self.enabled_tasks was manipulated externally during this loop,
            # or if tasks_to_process could somehow have a task not currently in self.enabled_tasks.
            if task_name not in self.enabled_tasks:
                continue

            interval = self.task_intervals.get(task_name)
            if interval is not None:
                # *** START STAGGER EDIT: Apply stagger delay for Trakt tasks ***
                # Get the stagger delay for this task (0 if not a Trakt task)
                delay = trakt_task_stagger_map.get(task_name, 0)
                if task_name == 'task_sync_viewer_history' and not os.path.exists(
                        os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'viewer_history.db')):
                    delay = VIEWER_HISTORY_FIRST_RUN_DELAY

                # Attempt to schedule with the appropriate delay
                if self._schedule_task(task_name, interval, initial_run=True, initial_delay_seconds=delay):
                    scheduled_count += 1
                # *** END STAGGER EDIT ***
                else:
                    # Scheduling failed. _schedule_task already logged a warning if it was due to target_func being None.
                    # Now, specifically check if it was an obsolete content source task.
                    is_content_source_task_pattern = task_name.startswith('task_') and task_name.endswith('_wanted')
                    
                    if is_content_source_task_pattern:
                        # Confirm the failure was due to a missing content source by re-checking _get_task_target's outcome.
                        # _get_task_target logs its own warning if the source_id is not found.
                        target_func_check, _, _ = self._get_task_target(task_name)
                        if target_func_check is None:
                            logging.warning(
                                f"Obsolete task toggle found for missing content source: '{task_name}'. "
                                f"Removing it from active enabled tasks. This change will be saved if/when task toggles are persisted."
                            )
                            self.enabled_tasks.discard(task_name) # Remove from the live set
                            pruned_obsolete_task_count += 1
                        else:
                            # Task matched content source pattern, _schedule_task failed, but _get_task_target now finds a function.
                            # This is an unexpected state, possibly due to timing or a different scheduling issue.
                            logging.error(f"Task '{task_name}' (content source type) failed to schedule, but a target function was found on re-check. Investigate.")
                            failed_to_schedule_count += 1
                    else:
                        # Failed to schedule, and it's not a content source task pattern.
                        # The warning for this was already logged by _schedule_task if target_func was None.
                        failed_to_schedule_count += 1
            else:
                logging.warning(f"Task '{task_name}' is enabled but has no interval defined in task_intervals. Skipping scheduling.")
                failed_to_schedule_count += 1
        
        if pruned_obsolete_task_count > 0:
            logging.info(f"Pruned {pruned_obsolete_task_count} obsolete content source task toggle(s) from the active configuration during this startup.")
        
        logging.info(f"Initial task scheduling Tally: "
                     f"Successfully Scheduled: {scheduled_count}, "
                     f"Failed/Skipped (e.g. no interval, other errors): {failed_to_schedule_count}, "
                     f"Pruned Obsolete Content Source Tasks: {pruned_obsolete_task_count}.")

        # Seed task_schedule.json with the initial next-run times for any task that doesn't
        # already have a persisted entry. This ensures that even daily tasks that have never
        # completed survive their first restart correctly.
        try:
            _seeded = 0
            for job in self.scheduler.get_jobs():
                _jinterval = self.task_intervals.get(job.id, 0)
                if job.next_run_time and job.id not in self._task_schedules and _jinterval >= 1200:
                    self._task_schedules[job.id] = job.next_run_time.timestamp()
                    _seeded += 1
            if _seeded:
                import json as _json_seed
                with open(self._task_schedule_file, 'w') as _f_seed:
                    _json_seed.dump(self._task_schedules, _f_seed)
                logging.info(f"[TaskScheduler] Seeded {_seeded} new task schedules into task_schedule.json")
        except Exception as _e_seed:
            logging.debug(f"[TaskScheduler] Could not seed task schedules: {_e_seed}")

        # Schedule a periodic snapshot so restarts always restore the actual remaining time.
        try:
            from apscheduler.triggers.interval import IntervalTrigger as _SnapshotTrigger
            self.scheduler.add_job(
                self._snapshot_task_schedules,
                trigger=_SnapshotTrigger(minutes=5),
                id='task_snapshot_schedules',
                name='Snapshot Task Schedules',
                replace_existing=True,
                misfire_grace_time=60,
            )
            logging.info("[TaskScheduler] Snapshot job registered (every 5 min)")
        except Exception as _e_snap:
            logging.warning(f"[TaskScheduler] Could not register snapshot job: {_e_snap}")

    def _is_within_pause_schedule(self):
        """Checks if the current time is within the configured pause schedule."""
        if not get_setting('Queue', 'enable_pause_schedule', False):
            return False # Schedule not enabled

        start_time_str = get_setting('Queue', 'pause_start_time', '00:00')
        end_time_str = get_setting('Queue', 'pause_end_time', '00:00')

        try:
            start_time = dt_time.fromisoformat(start_time_str)
            end_time = dt_time.fromisoformat(end_time_str)
        except ValueError:
            logging.error(f"Invalid pause time format: start='{start_time_str}', end='{end_time_str}'. Must be HH:MM.")
            return False # Treat invalid format as schedule not active

        # Get current time in the configured timezone using the imported function
        from metadata.metadata import _get_local_timezone
        tz = _get_local_timezone() # Use the imported function directly
        now = datetime.now(tz).time()

        # Handle overnight schedules (e.g., start 22:00, end 06:00)
        if start_time <= end_time:
            # Normal schedule within the same day
            return start_time <= now <= end_time
        else:
            # Overnight schedule
            return now >= start_time or now <= end_time

    def task_heartbeat(self):
        random_number = random.randint(1, 100)
        if self._running:
            if random_number < 100:
                logging.info("Program running...")
            else:
                logging.info("Program running...is your fridge?")

        # Add psutil memory logging
        if psutil:
            try:
                # 1. Explicit full garbage collection, at most every 15 minutes.
                # A full collection pauses every thread (page requests included)
                # and Python's own collector already runs continuously; the
                # malloc_trim below still runs on every heartbeat.
                now_mono = time.monotonic()
                if now_mono - getattr(self, '_last_full_gc', 0.0) >= 900:
                    self._last_full_gc = now_mono
                    gc_start = time.perf_counter()
                    collected = gc.collect()
                    logging.debug(f"Explicitly ran gc.collect(): {collected} objects in {(time.perf_counter() - gc_start) * 1000:.0f} ms")

                # 2. Log memory before trim
                process = psutil.Process(os.getpid())
                mem_info_before = process.memory_info()
                rss_mb_before = mem_info_before.rss / (1024 * 1024)
                vms_mb_before = mem_info_before.vms / (1024 * 1024)
                
                log_message = f"[Memory Usage] Before: RSS={rss_mb_before:.2f}MB, VMS={vms_mb_before:.2f}MB."

                # 3. Attempt to release memory to OS
                system = platform.system()
                if system == "Linux":
                    try:
                        ctypes.CDLL('libc.so.6').malloc_trim(0)
                        logging.debug("malloc_trim(0) called on Linux.")
                    except Exception as e:
                        logging.warning(f"Failed to call malloc_trim(0): {e}")
                elif system == "Windows":
                    try:
                        ctypes.CDLL('msvcrt')._heapmin()
                        logging.debug("_heapmin() called on Windows.")
                    except Exception as e:
                        logging.warning(f"Failed to call _heapmin(): {e}")
                
                # 4. Log memory after trim
                mem_info_after = process.memory_info()
                rss_mb_after = mem_info_after.rss / (1024 * 1024)
                vms_mb_after = mem_info_after.vms / (1024 * 1024)
                
                log_message += f" After: RSS={rss_mb_after:.2f}MB, VMS={vms_mb_after:.2f}MB."
                logging.info(log_message)

            except Exception as e:
                logging.error(f"Error in heartbeat memory management: {e}")
        else:
            # Log less frequently if psutil is missing
            if random_number < 10: # Log warning occasionally
                 logging.warning("psutil not installed, cannot report detailed memory usage in heartbeat.")

        # Add tracemalloc snapshot comparison in heartbeat
        # Check if enabled AND available before using
        if self._tracemalloc_enabled and tracemalloc_available and tracemalloc and tracemalloc.is_tracing():
            try:
                current_snapshot = tracemalloc.take_snapshot()
                if self.previous_tracemalloc_snapshot:
                    # Compare the current snapshot to the previous one
                    stats = current_snapshot.compare_to(self.previous_tracemalloc_snapshot, 'lineno')

                    # Log the top 10 differences (lines allocating the most *new* memory)
                    logging.info("[Tracemalloc Heartbeat] Top 10 memory differences since last heartbeat:")
                    total_diff = 0
                    for i, stat in enumerate(stats[:10], 1):
                        total_diff += stat.size_diff
                        # Limit traceback line length for cleaner logs
                        trace_line = stat.traceback.format()[-1]
                        trace_line = trace_line[:150] + '...' if len(trace_line) > 150 else trace_line
                        logging.info(f"  {i}: {trace_line} | Diff: {stat.size_diff / 1024:+.1f} KiB | Count Diff: {stat.count_diff:+} | New Size: {stat.size / 1024:.1f} KiB")
                    logging.info(f"[Tracemalloc Heartbeat] Total Diff in Top 10: {total_diff / 1024:+.1f} KiB")

                # Store the current snapshot for the next comparison
                self.previous_tracemalloc_snapshot = current_snapshot

            except Exception as e_trace_hb:
                logging.error(f"[Tracemalloc Heartbeat] Error processing snapshot comparison: {e_trace_hb}")

        # Watchdog: ensure critical queue tasks are still scheduled.
        # APScheduler can silently drop jobs when misfire_grace_time=None and the thread
        # pool is saturated (e.g. task_nzb_health_check consuming 99% of runtime).
        #
        # Skipped while a reinitialize() is in progress: scheduler.shutdown(wait=True)
        # blocks the executor's shutdown on this very task finishing, while get_jobs()
        # below needs the same _jobstores_lock shutdown() already holds — calling it here
        # during a reinit deadlocks task_heartbeat against reinitialize() forever, which is
        # exactly what made adding/deleting a content source "take forever".
        if not getattr(self, '_reinitializing', False):
            try:
                _scheduled_job_ids = {job.id for job in self.scheduler.get_jobs()}
                for _task_name in ('Scraping', 'Adding', 'Checking', 'Wanted'):
                    if _task_name not in _scheduled_job_ids and _task_name in self.enabled_tasks:
                        logging.warning(f'[Heartbeat] Watchdog: {_task_name!r} missing from scheduler — rescheduling')
                        self._schedule_task(_task_name, self.task_intervals.get(_task_name, 30))
            except Exception as _wd_err:
                logging.debug(f'[Heartbeat] Watchdog error: {_wd_err}')

        # Scraping-queue stuck detector: if the in-memory Scraping queue has been at the
        # same size >= WANTED_THROTTLE_SCRAPING_SIZE (100) for more than 10 minutes without
        # any items being moved out, force-sync by moving all stale items back to Wanted
        # so the throttle releases and the system recovers without a restart.
        try:
            _scraping_q = self.queue_manager.queues.get('Scraping')
            if _scraping_q is not None:
                _scraping_size = len(_scraping_q.get_contents())
                _now = time.time()
                if not hasattr(self, '_scraping_stuck_since'):
                    self._scraping_stuck_since = None
                    self._scraping_stuck_last_size = 0
                if _scraping_size >= 100:
                    if self._scraping_stuck_last_size != _scraping_size:
                        # Size changed — reset the stuck timer
                        self._scraping_stuck_since = _now
                        self._scraping_stuck_last_size = _scraping_size
                    elif self._scraping_stuck_since is None:
                        self._scraping_stuck_since = _now
                    elif _now - self._scraping_stuck_since > 600:  # 10 minutes stuck
                        logging.warning(
                            f'[Heartbeat] Scraping queue stuck at {_scraping_size} items for '
                            f'{int(_now - self._scraping_stuck_since)}s — '
                            f'force-syncing in-memory queue from DB to release throttle'
                        )
                        _new_size = _scraping_size
                        try:
                            _scraping_q.update()
                            _new_size = len(_scraping_q.get_contents())
                            logging.info(f'[Heartbeat] Scraping queue after force-sync: {_new_size} items')
                        except Exception as _sync_err:
                            logging.warning(f'[Heartbeat] Scraping queue force-sync error: {_sync_err}')
                        self._scraping_stuck_since = _now  # Reset timer after intervention
                        self._scraping_stuck_last_size = _new_size
                else:
                    self._scraping_stuck_since = None
                    self._scraping_stuck_last_size = _scraping_size
        except Exception as _stuck_err:
            logging.debug(f'[Heartbeat] Scraping stuck detector error: {_stuck_err}')

            # The old logic to stop the program after 5 retries is already commented out, which is good.

    def pause_queue(self):
        # THROTTLING: Prevent rapid pause operations
        from datetime import datetime
        now = datetime.now()
        if self.last_pause_time:
            elapsed = (now - self.last_pause_time).total_seconds()
            if elapsed < self.pause_resume_cooldown:
                logging.debug(f"Pause throttled ({elapsed:.1f}s < {self.pause_resume_cooldown}s). Skipping pause_queue().")
                return

        self.last_pause_time = now

        # Pause ALL running jobs
        with self.scheduler_lock:
            if self.scheduler.state != 1: # 1 = STATE_RUNNING
                logging.warning("Scheduler is not running, cannot pause jobs.")
                return

            all_jobs = self.scheduler.get_jobs()
            paused_count = 0
            logging.debug(f"Pausing all running jobs. Total jobs found: {len(all_jobs)}")
            logging.debug(f"Jobs already tracked as paused by this mechanism: {sorted(list(self.paused_jobs_by_queue))}")

            # Define tasks that should never be paused (essential monitoring tasks)
            # task_check_database_health resumes a DB_HEALTH pause once the database checks out
            never_pause_tasks = {'task_check_service_connectivity', 'task_heartbeat', 'task_check_database_health'}

            for job in all_jobs:
                job_id = job.id
                
                # Skip pausing essential monitoring tasks
                if job_id in never_pause_tasks:
                    logging.debug(f"Skipping pause for essential task: {job_id}")
                    continue
                
                # Only pause if the job is scheduled to run (not already paused indefinitely)
                # and not already tracked by this mechanism
                if job.next_run_time is not None and job_id not in self.paused_jobs_by_queue:
                     try:
                         self.scheduler.pause_job(job_id)
                         self.paused_jobs_by_queue.add(job_id)
                         paused_count += 1
                         logging.debug(f"Paused scheduler job via pause_queue: {job_id}")
                     except Exception as e:
                         logging.error(f"Error pausing job '{job_id}': {e}")
                elif job.next_run_time is None:
                    # Paused by something else (e.g. disabled in the Task Manager). Don't
                    # track it, or resume_queue would switch the disabled task back on.
                    logging.debug(f"Job '{job_id}' was already paused. Leaving it untracked.")
                elif job_id in self.paused_jobs_by_queue:
                     logging.debug(f"Job '{job_id}' was already in paused_jobs_by_queue set. Skipping pause action.")


            # Use the existing QueueManager pause state if needed for UI/status
        from queues.queue_manager import QueueManager
        # Pass the reason_string to QueueManager's pause
        reason_for_qm = self.pause_info.get("reason_string") if self.pause_info else "Unknown reason"
        QueueManager().pause_queue(reason=reason_for_qm)

        self.queue_paused = True
        # Log using pause_info
        log_reason = self.pause_info.get('reason_string', 'Unknown') if self.pause_info else 'Unknown'
        logging.info(f"Queue paused. Attempted to pause all running jobs (except essential monitoring tasks)... Reason: {log_reason}")

    def resume_queue(self):
        # A failing database health check holds the pause. Connectivity coming back, a
        # library delete or a cli_mount sync finishing clear pause_info and call this
        # too; put the DB_HEALTH pause back instead of resuming.
        if self.db_health_pause_info:
            self.pause_info = dict(self.db_health_pause_info)
            if not self.queue_paused:
                self.pause_queue()
            logging.info("[Resume Queue] Not resuming - the database health check is still failing")
            return

        # THROTTLING: Prevent rapid resume operations
        from datetime import datetime
        now = datetime.now()
        if self.last_resume_time:
            elapsed = (now - self.last_resume_time).total_seconds()
            if elapsed < self.pause_resume_cooldown:
                logging.debug(f"Resume throttled ({elapsed:.1f}s < {self.pause_resume_cooldown}s). Skipping resume_queue().")
                return

        self.last_resume_time = now

        # Resume logic remains the same, but update log context
        logging.info(f"[Resume Queue] Starting resume process. Queue paused: {self.queue_paused}, Pause type: {self.pause_info.get('error_type') if self.pause_info else 'None'}")

        with self.scheduler_lock:
            if self.scheduler.state != 1: # 1 = STATE_RUNNING
                logging.warning("Scheduler is not running, cannot resume jobs.")
                return

            resumed_count = 0
            # Only resume jobs that were paused by pause_queue (or added to the set)
            jobs_to_resume = list(self.paused_jobs_by_queue) # Copy to avoid modification issues
            logging.debug(f"Attempting to resume jobs tracked during pause: {sorted(jobs_to_resume)}")

            for job_id in jobs_to_resume:
                 try:
                     job = self.scheduler.get_job(job_id)
                     if job:
                        # Check if the job is actually paused (next_run_time is None)
                        if job.next_run_time is None:
                            self.scheduler.resume_job(job_id)
                            self.paused_jobs_by_queue.remove(job_id) # Remove from tracked set
                            resumed_count += 1
                            logging.debug(f"Resumed scheduler job via resume_queue: {job_id}")
                        else:
                            logging.debug(f"Job '{job_id}' was found but already running (not paused). Removing from paused_jobs_by_queue set.")
                            # Remove from set even if not paused, as it shouldn't be tracked anymore
                            if job_id in self.paused_jobs_by_queue:
                                self.paused_jobs_by_queue.remove(job_id)
                     else:
                         # Job doesn't exist anymore, remove it from the tracking set
                         logging.warning(f"Job '{job_id}' not found while resuming, removing from paused_jobs_by_queue set.")
                         if job_id in self.paused_jobs_by_queue:
                              self.paused_jobs_by_queue.remove(job_id)

                 except Exception as e:
                     # Log specific errors during resume attempt
                     logging.error(f"Error resuming job '{job_id}': {e}")
                     # Decide if we should keep it in the set or remove on error?
                     # Removing might be safer to prevent infinite loops if resume fails consistently.
                     if job_id in self.paused_jobs_by_queue:
                           logging.warning(f"Removing job '{job_id}' from paused_jobs_by_queue set due to resume error.")
                           self.paused_jobs_by_queue.remove(job_id)


            # Use the existing QueueManager resume state if needed for UI/status
        from queues.queue_manager import QueueManager
        QueueManager().resume_queue()

        # Clear all pause-related state
        self.queue_paused = False
        self.connectivity_failure_time = None
        self.connectivity_retry_count = 0
        # Clear pause_info
        self.pause_info = {
            "reason_string": None, "error_type": None, "service_name": None,
            "status_code": None, "retry_count": 0
        }
        logging.info(f"[Resume Queue] Queue resumed successfully. Resumed {resumed_count} jobs.")  # Better log message

            # Don't re-raise - allow other content sources to continue processing

    


    # def task_generate_airtime_report(self):
    #     generate_airtime_report()

    # ---------------------------------------------------------------------------
    # Task schedule persistence helpers
    # ---------------------------------------------------------------------------

    def _load_task_schedules(self) -> dict:
        """Load persisted task next-run timestamps from disk."""
        import json
        path = getattr(self, '_task_schedule_file', None)
        if path:
            try:
                if os.path.exists(path):
                    with open(path, 'r') as f:
                        data = json.load(f)
                    logging.info(f"[TaskScheduler] Loaded {len(data)} persisted task schedules from task_schedule.json")
                    return data
            except Exception as e:
                logging.warning(f"[TaskScheduler] Could not load task_schedule.json: {e}")
        return {}

    def _save_task_schedule(self, task_name: str, next_run_time: float):
        """Persist a task's next scheduled run time to disk."""
        import json
        path = getattr(self, '_task_schedule_file', None)
        if not path:
            return
        try:
            self._task_schedules[task_name] = next_run_time
            with open(path, 'w') as f:
                json.dump(self._task_schedules, f)
        except Exception as e:
            logging.debug(f"[TaskScheduler] Could not save task_schedule.json: {e}")

    def _snapshot_task_schedules(self):
        """Snapshot every qualifying task's current next_run_time to disk.

        Runs every 5 minutes so that on restart the actual remaining time is
        restored rather than resetting to the full default interval.
        """
        import json
        path = getattr(self, '_task_schedule_file', None)
        if not path:
            return
        try:
            updated = 0
            for job in self.scheduler.get_jobs():
                if job.id == 'task_snapshot_schedules':
                    continue
                _jinterval = self.task_intervals.get(job.id, 0)
                if job.next_run_time and _jinterval >= 1200:
                    self._task_schedules[job.id] = job.next_run_time.timestamp()
                    updated += 1
            if updated:
                with open(path, 'w') as f:
                    json.dump(self._task_schedules, f)
                logging.debug(f"[TaskScheduler] Snapshot saved {updated} task schedules")
        except Exception as e:
            logging.debug(f"[TaskScheduler] Snapshot error: {e}")
