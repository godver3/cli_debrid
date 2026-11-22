import logging
import time
import os
# Import tracemalloc
try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc = None # Define as None if import fails
    tracemalloc_available = False
# Add psutil import
try:
    import psutil
except ImportError:
    psutil = None # Handle missing import gracefully
from apscheduler.schedulers.background import BackgroundScheduler
# Add ThreadPoolExecutor for explicit configuration
from apscheduler.executors.pool import ThreadPoolExecutor
# Add threading.Lock
import threading # For scheduler lock, concurrent queue processing, AND heavy task lock
import apscheduler.events # Added for listener events
from queues.initialization import initialize
from utilities.settings import get_setting
import traceback
from datetime import datetime
import asyncio
from utilities.plex_functions import run_get_collected_from_plex, run_get_recent_from_plex
from utilities.zurg_utilities import run_get_collected_from_zurg, run_get_recent_from_zurg
from database.symlink_verification import (
    migrate_plex_removal_database,
)
import pytz # Added import
# Add Debrid imports for library size task
from collections import defaultdict  # Added alongside deque above for runtime tracking
# Try to import resource module (Unix only), fallback to time.process_time()
try:
    import resource
except ImportError:
    resource = None

queue_logger = logging.getLogger('queue_logger')
program_runner = None

# Database migration check at startup
migrate_plex_removal_database()


def clear_nzb_job_health_cache(job_id: str) -> None:
    """Drop cached NZB health-check state for a cancelled or superseded cli_mount job."""
    bare = job_id[4:] if str(job_id).startswith('nzb:') else str(job_id or '')
    if not bare:
        return
    ProgramRunner._nzb_confirmed_complete.pop(bare, None)
    ProgramRunner._nzb_folder_wait_counts.pop(bare, None)
    ProgramRunner._nzb_folder_wait_since.pop(bare, None)


# Set while the startup sync (queues/initialization.py) runs in its background
# thread. Module-level rather than on ProgramRunner: reinitialize() re-runs
# __init__, which must not forget a sync that is still going.
_initial_sync_active = threading.Event()
_initial_sync_guard = threading.Lock()


def is_initial_sync_running():
    return _initial_sync_active.is_set()


def _build_scheduler_config():
    """Executors and job defaults shared by every BackgroundScheduler we build."""
    queue_workers = max(1, min(3, int(get_setting('Queue', 'queue_pool_workers', 2))))
    executors = {
        'default': ThreadPoolExecutor(max_workers=1),  # scheduled/maintenance tasks — sequential
        'queue': ThreadPoolExecutor(max_workers=queue_workers),
    }
    job_defaults = {
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': None,
    }
    return queue_workers, executors, job_defaults


def _parse_last_updated(value):
    """A media_items last_updated value as a naive datetime, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00').split('+')[0])
    except ValueError:
        return None


from queues.runner import task_registry

# Tasks whose on/off switch is a setting. ProgramRunner schedules them from the setting
# alone (saved overrides are ignored), and flipping one on the Automation page writes
# the setting (see _sync_setting_backed_tasks in program_operation_routes).
SETTING_BACKED_TASKS = {s.name: s.setting for s in task_registry.SPECS.values() if s.setting}


def viewer_history_task_wanted():
    """task_sync_viewer_history is on by default when a media server and a TMDB key are set."""
    return task_registry._viewer_history_default(get_setting)




# ProgramRunner's task methods live in queues/runner/, one mixin per area.
from queues.runner.content_sources import ContentSourceTasks
from queues.runner.connectivity import ConnectivityTasks
from queues.runner.plex_tasks import PlexTasks
from queues.runner.library_tasks import LibraryTasks
from queues.runner.usenet_tasks import UsenetTasks
from queues.runner.metadata_tasks import MetadataTasks
from queues.runner.maintenance_tasks import MaintenanceTasks
from queues.runner.scheduling import SchedulingMixin
from queues.runner.task_control import TaskControlMixin


class ProgramRunner(SchedulingMixin, TaskControlMixin, ContentSourceTasks, ConnectivityTasks, PlexTasks, LibraryTasks, UsenetTasks, MetadataTasks, MaintenanceTasks):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ProgramRunner, cls).__new__(cls)
            # Initialize _initialized here before __init__ is called
            cls._instance._initialized_runner_attributes = False # Ensure this is named uniquely
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized_runner_attributes') and self._initialized_runner_attributes:
            return
        
        self._running = False
        self._initializing = False
        self._stopping = False # ADDED: New state for stopping phase
        # Tracks whether self.scheduler.start() has ever actually been called on
        # this scheduler instance - distinct from scheduler.running, which is
        # False both before the very first start AND after a real crash/zombie.
        # _execute_start_program's "scheduler exists but isn't running" check
        # needs this to tell those two states apart; without it, the freshly
        # __init__-created scheduler on every process boot looked identical to a
        # zombie and got discarded/rebuilt (with the wrong timezone, since the
        # rebuild path reads a different setting) before it ever got to run once.
        self._scheduler_started_once = False
        
        # Use pause_info instead of pause_reason
        self.pause_info = {
            "reason_string": None,
            "error_type": None,  # e.g., "CONNECTION_ERROR", "UNAUTHORIZED", "SYSTEM_SCHEDULED", "RATE_LIMIT", "DB_HEALTH"
            "service_name": None, # e.g., "Real-Debrid API", "Plex", "System"
            "status_code": None,  # HTTP status code if applicable
            "retry_count": 0
        }
        self.connectivity_failure_time = None
        self.connectivity_retry_count = 0 # This will now primarily be for logging/timing, actual count in pause_info
        self.queue_paused = False
        # Set while task_check_database_health finds corruption: holds the queue paused
        # until a check passes, whatever other pause ends in the meantime
        self.db_health_pause_info = None

        # PAUSE/RESUME THROTTLING: Prevent rapid pause/resume cycles
        self.last_pause_time = None
        self.last_resume_time = None
        self.pause_resume_cooldown = 30  # Minimum seconds between pause/resume operations
        
        # Task schedule persistence — load saved next-run times so timers survive restarts.
        import os as _os
        _db_dir = _os.environ.get('USER_DB_CONTENT', '/user/db_content')
        _os.makedirs(_db_dir, exist_ok=True)
        self._task_schedule_file = _os.path.join(_db_dir, 'task_schedule.json')
        self._task_schedules = self._load_task_schedules()

        # In-memory job store. Task next-run times are NOT restored from this store —
        # that's handled separately by _load_task_schedules()/task_schedule.json above,
        # which is the mechanism _schedule_task's initial_run branch actually reads from.
        # A persistent (pickling) store like SQLAlchemyJobStore cannot be used here: every
        # task's target is a bound method on this ProgramRunner instance, which itself
        # holds self.scheduler, and APScheduler refuses to pickle a job whose bound-method
        # owner carries a scheduler reference ("Schedulers cannot be serialized").
        from apscheduler.jobstores.memory import MemoryJobStore
        _jobstores = {'default': MemoryJobStore()}

        # Configure scheduler timezone using the local timezone helper
        try:
            from metadata.metadata import _get_local_timezone # Added import
            tz = _get_local_timezone()
            logging.info(f"Initializing APScheduler with timezone: {tz.key}")
            _queue_workers, executors, job_defaults = _build_scheduler_config()
            self.scheduler = BackgroundScheduler(jobstores=_jobstores, executors=executors, job_defaults=job_defaults, timezone=tz)
            logging.info(f"APScheduler configured with separate thread pools: queue({_queue_workers}) and default(1).")
        except Exception as e:
            logging.error(f"Failed to get local timezone for scheduler, using system default: {e}")
            _queue_workers, executors, job_defaults = _build_scheduler_config()
            self.scheduler = BackgroundScheduler(jobstores=_jobstores, executors=executors, job_defaults=job_defaults)
            logging.info(f"APScheduler configured with separate thread pools: queue({_queue_workers}) and default(1) (system default timezone).")

        # self.scheduler_lock = threading.Lock() # Previous version
        self.scheduler_lock = threading.RLock() # MODIFIED: Ensure RLock for reentrancy
        self.paused_jobs_by_queue = set() # Keep track of jobs paused by pause_queue
        
        self.executing_task_start_times = {}
        self._executing_task_start_times_lock = threading.Lock()
        
        from queues.queue_manager import QueueManager
        
        # Initialize queue manager with logging
        logging.info("Initializing QueueManager")
        self.queue_manager = QueueManager()
        
        # Verify queue initialization
        expected_queues = ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading', 'Final_Check', 'Pre_release']
        missing_queues = [q for q in expected_queues if q not in self.queue_manager.queues]
        if missing_queues:
            logging.error(f"Missing queues during initialization: {missing_queues}")
            raise RuntimeError(f"Queue initialization failed. Missing queues: {missing_queues}")
        
        # Always resume queue on startup to ensure we're not stuck in paused state
        self.queue_manager.resume_queue()
        
        logging.info("Successfully initialized QueueManager with queues: " + ", ".join(self.queue_manager.queues.keys()))
        
        # Define queue_processing_map FIRST
        # Define queue processing map needed early by _normalize_task_name
        # This map connects queue names (often used in toggles/settings)
        # to the corresponding processing methods in QueueManager.
        self.queue_processing_map = {
            'Wanted': 'process_wanted',
            'Scraping': 'process_scraping',
            'Adding': 'process_adding',
            'Checking': 'process_checking',
            'Sleeping': 'process_sleeping',
            'Unreleased': 'process_unreleased',
            'Blacklisted': 'process_blacklisted',
            'Pending Uncached': 'process_pending_uncached',
            'Upgrading': 'process_upgrading',
            'final_check_queue': 'process_final_check', # Use lowercase key matching the task ID
            'Pre_release': 'process_pre_release'
        }

        # Tasks, intervals and default on/off come from queues/runner/task_registry.py.
        # Content source tasks are added by get_content_sources(); saved intervals and the
        # user's on/off overrides (task_toggles.json) are applied on top.
        self.content_sources = None
        self.start_time = time.time()
        # The runner can be built before main.py's startup migrations run; migrate first
        # so an old full-state task_toggles.json is never read as overrides.
        try:
            from utilities.settings import get_all_settings
            task_registry.migrate(get_setting, get_all_settings)
        except Exception as e:
            logging.error(f"Task settings migration failed: {e}", exc_info=True)
        self.original_task_intervals = task_registry.default_intervals()
        self.task_intervals = dict(self.original_task_intervals)
        self.default_enabled_tasks = task_registry.default_enabled(get_setting)
        self.enabled_tasks = set(self.default_enabled_tasks)
        self._task_overrides = task_registry.load_overrides()
        self.get_content_sources(force_refresh=True)
        self._apply_saved_intervals()
        self.enabled_tasks = task_registry.apply_overrides(
            self.default_enabled_tasks, self._task_overrides, self.task_intervals)

        # Log the final set of enabled tasks right before starting the scheduling process
        logging.info(f"Final enabled tasks before initial scheduling: {sorted(list(self.enabled_tasks))}")

        # Schedule initial tasks
        self._schedule_initial_tasks()

        # Modify tracemalloc conditional start in __init__
        # Track if tracemalloc is enabled via setting AND if it was imported
        self._tracemalloc_enabled = tracemalloc_available and get_setting('Debug', 'enable_tracemalloc', False)
        if self._tracemalloc_enabled:
            # This check is redundant now due to the above, but safe
            if tracemalloc_available and tracemalloc:
                 logging.warning("Tracemalloc memory tracking is enabled. This adds overhead.")
                 try:
                     tracemalloc.start(10) # Start tracking with a stack depth of 10
                 except Exception as e_start:
                     logging.error(f"Failed to start tracemalloc: {e_start}")
                     self._tracemalloc_enabled = False # Disable if start fails
            else:
                 # Should not happen if _tracemalloc_enabled is True, but log just in case
                 logging.warning("Tracemalloc setting enabled but module not available. Disabling.")
                 self._tracemalloc_enabled = False

        # Add task execution counter and sample rate for tracemalloc
        self.task_execution_count = 0
        # Read sample rate from settings, default to 100 (sample 1 in every 100 tasks)
        self.tracemalloc_sample_rate = int(get_setting('Debug', 'tracemalloc_sample_rate', 100))
        # Ensure sample rate is at least 1 to avoid division by zero or weird behavior
        if self.tracemalloc_sample_rate < 1:
            logging.warning(f"Invalid tracemalloc_sample_rate ({self.tracemalloc_sample_rate}), defaulting to 1.")
            self.tracemalloc_sample_rate = 1

        # Add variable to store previous snapshot
        self.previous_tracemalloc_snapshot = None
        # Log tracemalloc status with sample rate
        if self._tracemalloc_enabled:
            logging.warning(f"Tracemalloc memory tracking is ENABLED (Sample Rate: 1/{self.tracemalloc_sample_rate}). This adds overhead.")
            # Check again if it's actually tracing (might have failed to start)
            if not (tracemalloc_available and tracemalloc and tracemalloc.is_tracing()):
                 logging.error("Tracemalloc was enabled but is not tracing. Check for startup errors.")
                 self._tracemalloc_enabled = False # Ensure flag reflects reality
        # ... rest of __init__ ...

        self.current_running_task = None
        self._running_task_lock = threading.Lock() # Lock for thread-safe access

        # Add inter-task sleep variables
        self.base_inter_task_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))
        self.current_inter_task_sleep = self.base_inter_task_sleep
        logging.info(f"Initialized inter-task sleep to {self.current_inter_task_sleep}s based on settings.")

        # Add currently_executing_tasks set
        self.currently_executing_tasks = set()

        # Remove single current_running_task
        # self.current_running_task = None # Removed
        self._running_task_lock = threading.Lock() # Lock for thread-safe access to the set


        # Add task execution counter and sample rate for tracemalloc
        self.task_execution_count = 0
        # ... (rest of __init__)

        # In __init__, add:
        self.manual_tasks = set()  # Track manually triggered tasks
        self._initialized_runner_attributes = True # Mark as initialized at the end of actual init logic
        # Runtime tracking attributes
        self.task_runtime_totals = defaultdict(float)  # Accumulated runtime per task in current window
        self.task_runtime_lock = threading.Lock()  # Protect access to task_runtime_totals
        self._runtime_log_interval_sec = 300  # How often to emit runtime percentage report (seconds)
        self._last_runtime_log_time = time.monotonic()
        # CPU usage tracking attributes
        # Accumulate per-task CPU seconds in current window
        self.task_cpu_totals = defaultdict(float)
        self.task_cpu_lock = threading.Lock()
        # Share the same interval as runtime logging to avoid log noise
        self._cpu_log_interval_sec = self._runtime_log_interval_sec
        self._last_cpu_log_time = time.monotonic()
        # Control per-run CPU logging via environment variable
        self._log_per_run_cpu = os.environ.get('LOG_PER_RUN_CPU', 'false').lower() == 'true'


    # ---------------------------------------------------------------------------


    def run_initialization(self, is_restart=False):
        """Run initialization sequence.
        
        Args:
            is_restart (bool): If True, skip initialization as this is a mid-run restart.
                              This prevents unnecessary processing and potential data loss
                              when the program is restarted after settings changes.

        Runs in the background (see _start_initial_sync): the scheduler is already
        processing the queues, so the program reports Running throughout.
        """
        logging.info("Running initialization...")

        # Skip initialization if this is a restart (mid-run) or if explicitly disabled
        if is_restart:
            logging.info("Skipping initialization as this is a restart (mid-run)")
            return
            
        disable_initialization = get_setting('Debug', 'disable_initialization', '')
        if not disable_initialization:
            initialize()
            logging.info("Initialization complete")
        else:
            logging.info("Initialization disabled, skipping...")
        
        # Sync config file with runtime state after initialization is complete
        try:
            from utilities.settings import get_all_settings, save_config
            config = get_all_settings()
            content_sources = config.get('Content Sources', {})
            config_updated = False
            
            for source, data in content_sources.items():
                task_name = f'task_{source}_wanted'
                # A source added after this runner was built has no task here yet.
                # Writing enabled=False for it would switch it off in the config.
                if task_name not in self.task_intervals:
                    continue
                current_enabled = task_name in self.enabled_tasks

                if data.get('enabled') != current_enabled:
                    data['enabled'] = current_enabled
                    config_updated = True
                    logging.info(f"Updated config for {source}: enabled = {current_enabled} (to match runtime state)")
            
            if config_updated:
                save_config(config)
                logging.info("Updated content source enabled states in config file to match runtime state after initialization")
                
        except Exception as e:
            logging.error(f"Failed to update config file with runtime enabled states after initialization: {e}")

    def _start_initial_sync(self, is_restart=False):
        """Run run_initialization in a background thread.

        It used to run inline in run(), holding the program at "Starting" (the
        Queues page hidden, Stop refused) for the minutes it takes to pull every
        content source and refresh release dates, although the scheduler was
        already processing the queues. A sync still going from an earlier start
        isn't started twice.
        """
        with _initial_sync_guard:
            if _initial_sync_active.is_set():
                logging.info("Initial sync from an earlier start is still running; not starting another.")
                return
            _initial_sync_active.set()

        def _sync():
            try:
                self.run_initialization(is_restart=is_restart)
            except Exception as e:
                logging.error(f"Initial sync failed: {e}", exc_info=True)
            finally:
                _initial_sync_active.clear()

        threading.Thread(target=_sync, name='initial-sync', daemon=True).start()

    def is_initial_sync_running(self):
        return is_initial_sync_running()

    def start(self, is_restart=False):
        if self._running and self.scheduler and self.scheduler.running:
            logging.info("ProgramRunner.start called, but program is already running.")
            return
        if self._initializing:
            logging.info("ProgramRunner.start called, but program is already initializing.")
            return
        if self._stopping: 
            logging.warning("ProgramRunner.start called, but program is currently stopping.")
            return

        # On each start, reset the inter-task sleep to the configured default
        # to override any changes made by the dynamic load regulator in a previous run.
        self.base_inter_task_sleep = float(get_setting('Queue', 'main_loop_sleep_seconds', 0.0))
        self.current_inter_task_sleep = self.base_inter_task_sleep
        logging.info(f"Inter-task sleep reset to {self.current_inter_task_sleep}s on program start.")

        self._initializing = True
        self._stopping = False 
        # Store the is_restart parameter for use in run_initialization
        self._is_restart = is_restart
        logging.info("ProgramRunner: Initializing...")
        
        try:
            logging.info("Starting APScheduler...")
            if self.scheduler and not self.scheduler.running:
                start_paused = self._is_within_pause_schedule()
                self.scheduler.start(paused=start_paused)
                self._scheduler_started_once = True
                logging.info(f"APScheduler started. Paused: {start_paused}")
            elif not self.scheduler:
                logging.error("ProgramRunner.start: CRITICAL - Scheduler not initialized. Cannot start.")
                self._initializing = False # Ensure initializing is reset
                return

            # _running will now be set inside run(), after it passes its own guard.

            # Clear the initializing flag *before* entering the main run loop so run() doesn't
            # mistakenly believe initialization is still underway.
            self._initializing = False
            self.update_heartbeat() 
            logging.info("ProgramRunner: Started successfully.")
            # self.run() # The run loop should be started by the external caller if this is a thread target.
                       # If ProgramRunner.start() is the entry point for its own thread, then self.run() is appropriate here.
                       # Based on program_operation_routes, a new thread is created for runner_instance.start,
                       # so this start method itself becomes the thread's target.
                       # However, the `run_program` function at the end of the file calls program_runner.start() and then returns,
                       # implying start() might be expected to block or manage its own loop if it's the main program thread.
                       # The `run` method contains the main while loop.
                       # If `start` is called in a new thread, and `start` calls `self.run()`, that is correct.
            self.run() # Assuming start() is the entry point for the ProgramRunner's main execution flow.

        except Exception as e:
            logging.error(f"ProgramRunner: Error during start: {e}", exc_info=True)
            self._running = False
            # self._initializing = False # Moved to finally
            if self.scheduler and self.scheduler.running:
                try:
                    self.scheduler.shutdown(wait=False)
                except Exception as e_shutdown:
                    logging.error(f"Error shutting down scheduler after failed start: {e_shutdown}")
        finally:
            self._initializing = False # Ensure initializing is false after attempt.


    def stop(self):
        # Check if already fully stopped and not in the process of stopping
        if not self._running and not self._initializing and not self._stopping:
            logging.info("ProgramRunner.stop called, but program is not running, initializing, or actively stopping.")
            self._running = False
            self._initializing = False
            self._stopping = False 
            return

        logging.info(f"ProgramRunner.stop called. Current state: running={self._running}, initializing={self._initializing}, stopping={self._stopping}")
        
        self._stopping = True 
        self._initializing = False 
        
        try:
            if self._running: # If it thought it was running, mark it as not running anymore.
                self._running = False 
            
            if self.scheduler:
                try:
                    logging.info("Attempting to shut down APScheduler...")
                    if self.scheduler.running:
                        self.scheduler.shutdown(wait=True)
                        logging.info("APScheduler shut down successfully.")
                    else:
                        logging.info("APScheduler was not running when stop was called.")
                except Exception as e:
                    logging.error(f"Error shutting down APScheduler: {e}", exc_info=True)
                self.scheduler = None 
            else:
                logging.info("No APScheduler instance to shut down (was None).")
            
            self._running = False # Final confirmation
            logging.info("ProgramRunner: Stop sequence completed.")

        except Exception as e_stop_main:
            logging.error(f"ProgramRunner: Error during main stop logic: {e_stop_main}", exc_info=True)
            self._running = False # Ensure running is false on error
        finally:
            self._stopping = False # Reset stopping flag
            logging.info(f"ProgramRunner: _stopping flag set to False. Final state: running={self._running}")


    def is_running(self):
        return self._running

    def is_initializing(self): 
        return self._initializing

    def is_stopping(self): 
        return self._stopping

    def get_status(self):
        """Returns the current status of the program as a string."""
        if self.is_initializing():
            return "Starting"
        if self.is_stopping():
            return "Stopping"
        if self.is_running():
            return "Running"
        return "Stopped"

    def run(self):
        # Guard against duplicate starts. We only consider the running flag now.
        if self._running:
            logging.warning("Attempted to start program, but it's already running.")
            return
        
        # Mark as running. The startup sync below runs in its own thread, so the
        # program reports Running while it works.
        self._running = True

        try:
            logging.info("Starting program run loop (monitoring scheduler state)")
            self._running = True  # Make sure running flag is set

            # Pass the is_restart parameter to run_initialization
            self._start_initial_sync(is_restart=getattr(self, '_is_restart', False))

            # Simplified run loop with CPU monitoring
            # The main loop now just keeps the script alive while the scheduler runs.
            # We can add checks here if needed (e.g., monitoring scheduler health).
            
            # CPU monitoring variables
            cpu_monitor_start = time.time()
            cpu_monitor_interval = 300.0  # Log CPU usage every 5 minutes
            loop_count = 0
            
            while self._running or getattr(self, '_reinitializing', False):
                try:
                    # Check scheduler status periodically
                    if not self.scheduler or not self.scheduler.running:
                         if getattr(self, '_reinitializing', False):
                             # reinitialize() intentionally shuts the scheduler down
                             # (and its own __init__ call transiently resets _running)
                             # before building a new one — this is expected, not a crash.
                             time.sleep(1.0)
                             continue
                         logging.error("APScheduler is not running. Stopping program.")
                         self.stop() # Trigger stop if scheduler died
                         break
            
                    # Perform checks that still need to run outside scheduled tasks
                    # e.g., connectivity checks that might pause/resume scheduler jobs
                    if self.connectivity_failure_time or self.queue_paused:
                        # If we're in a failure state, check more aggressively
                        self.check_connectivity_status()

                    # Fail-safe: if connectivity recovery logic never resumes the queue,
                    # kick a watchdog that will forcibly resume after a timeout.
                    self._fail_safe_resume_if_stuck()

                    is_scheduled_pause = self._is_within_pause_schedule()
                    current_pause_type = self.pause_info.get("error_type") if self.pause_info else None

                    if is_scheduled_pause and not self.queue_paused: # Or if paused for a different, non-schedule reason
                        pause_start = get_setting('Queue', 'pause_start_time', '00:00')
                        pause_end = get_setting('Queue', 'pause_end_time', '00:00')
                        new_reason_string = f"Scheduled pause active ({pause_start} - {pause_end})"
                        
                        # Update pause_info for scheduled pause
                        self.pause_info = {
                            "reason_string": new_reason_string,
                            "error_type": "SYSTEM_SCHEDULED",
                            "service_name": "System",
                            "status_code": None,
                            "retry_count": 0
                        }
                        self.pause_queue()
                        logging.info(f"Queue automatically paused due to schedule: {new_reason_string}")
                    elif not is_scheduled_pause and self.queue_paused and current_pause_type == "SYSTEM_SCHEDULED":
                        logging.info("Scheduled pause period ended. Resuming queue.")
                        # Clear pause_info on resume
                        self.pause_info = {
                            "reason_string": None, "error_type": None, "service_name": None,
                            "status_code": None, "retry_count": 0
                        }
                        self.resume_queue()
                    # ... (sleep) ...

                    # CPU monitoring and main loop sleep
                    loop_count += 1
                    current_time = time.time()
                    
                    # Log CPU usage every 5 seconds
                    if current_time - cpu_monitor_start >= cpu_monitor_interval:
                        try:
                            import psutil
                            cpu_percent = psutil.cpu_percent(interval=None)
                            memory_percent = psutil.virtual_memory().percent
                            process = psutil.Process()
                            process_cpu_percent = process.cpu_percent()
                            process_memory_mb = process.memory_info().rss / 1024 / 1024
                            
                            logging.info(f"CPU MONITORING - Loop #{loop_count}: "
                                        f"System CPU: {cpu_percent:.1f}%, "
                                        f"Process CPU: {process_cpu_percent:.1f}%, "
                                        f"Memory: {memory_percent:.1f}% ({process_memory_mb:.1f}MB), "
                                        f"Loop rate: {loop_count / (current_time - cpu_monitor_start + cpu_monitor_interval):.1f} loops/sec")
                            
                            # If system CPU is high but our process isn't using much, log top processes
                            if cpu_percent > 50 and process_cpu_percent < 5:
                                try:
                                    top_processes = []
                                    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                                        try:
                                            if proc.info['cpu_percent'] > 5:  # Only show processes using >5% CPU
                                                top_processes.append(proc.info)
                                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                                            pass
                                    
                                    # Sort by CPU usage and show top 5
                                    top_processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
                                    if top_processes:
                                        logging.info("TOP CPU PROCESSES (system high, app low):")
                                        for proc in top_processes[:5]:
                                            logging.info(f"  PID {proc['pid']}: {proc['name']} - CPU: {proc['cpu_percent']:.1f}%, Memory: {proc['memory_percent']:.1f}%")
                                except Exception as e:
                                    logging.error(f"Error getting top processes: {e}")
                        except ImportError:
                            logging.info(f"CPU MONITORING - Loop #{loop_count}: psutil not available")
                        except Exception as e:
                            logging.error(f"CPU MONITORING - Error getting CPU stats: {e}")
                        
                        # Reset monitoring
                        cpu_monitor_start = current_time
                        loop_count = 0
                    
                    # Main loop sleep
                    try:
                        main_loop_sleep_seconds = float(get_setting('Queue', 'main_loop_sleep_seconds', 5.0))
                    except (ValueError, TypeError):
                        main_loop_sleep_seconds = 5.0
                    # Ensure sleep is not too low. The setting defaults to 0 (it is
                    # meant for the gap between tasks), which made this loop wake
                    # 10 times a second just to check connectivity/pause state.
                    if main_loop_sleep_seconds < 1.0:
                        main_loop_sleep_seconds = 1.0
                    time.sleep(main_loop_sleep_seconds) # Check status based on setting

                except Exception as loop_error:
                     logging.error(f"Error in main monitoring loop: {loop_error}", exc_info=True)
                     time.sleep(10) # Longer sleep on error

            logging.warning("Program run loop exited.")
            # Ensure scheduler is stopped if loop exits unexpectedly
            if self.scheduler and self.scheduler.running:
                 self.stop()

        except Exception as e:
            logging.error(f"Fatal error in run method: {str(e)}")
            logging.error(traceback.format_exc())
            # Ensure stop is called on fatal error
            self.stop()


         # --- END REFACTOR ---


    def update_heartbeat(self):
        """Update the heartbeat file directly."""
        import os

        # Save the current time as the last heartbeat
        current_time = int(time.time())
        
        # Store the heartbeat in memory to reduce I/O operations
        if not hasattr(self, '_last_heartbeat_time'):
            self._last_heartbeat_time = 0
            self._last_heartbeat_file_write = 0
            self._heartbeat_io_slow = False
            self._heartbeat_io_check_time = 0
        
        self._last_heartbeat_time = current_time
        
        # If I/O was previously detected as slow, use a longer interval (5 minutes)
        file_write_interval = 300 if self._heartbeat_io_slow else 30
        
        # Only write to disk periodically to reduce I/O
        if current_time - self._last_heartbeat_file_write >= file_write_interval:
            db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            heartbeat_file = os.path.join(db_content_dir, 'program_heartbeat')
            
            try:
                # Ensure the directory exists
                os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
                
                # Measure how long the I/O operation takes
                io_start_time = time.time()
                
                # Write directly to the heartbeat file
                with open(heartbeat_file, 'w') as f:
                    f.write(str(current_time))
                    f.flush()
                    os.fsync(f.fileno())
                
                io_duration = time.time() - io_start_time
                
                # If I/O takes more than 100ms, it's slow
                if io_duration > 0.1 and not self._heartbeat_io_slow:
                    logging.warning(f"Heartbeat file I/O is slow ({io_duration:.2f}s) - reducing write frequency")
                    self._heartbeat_io_slow = True
                
                # Periodically re-check if I/O speed has improved (every 30 minutes)
                elif self._heartbeat_io_slow and current_time - self._heartbeat_io_check_time > 1800:
                    if io_duration < 0.05:  # If improved to under 50ms
                        logging.info("Heartbeat file I/O speed has improved - resuming normal write frequency")
                        self._heartbeat_io_slow = False
                    self._heartbeat_io_check_time = current_time
                
                self._last_heartbeat_file_write = current_time
            except (IOError, OSError) as e:
                logging.error(f"Failed to update heartbeat file: {e}")
                # Mark I/O as slow if we get errors
                self._heartbeat_io_slow = True


    # Track last health check duration to implement skip-if-slow guard
    _nzb_health_last_duration: float = 0.0
    _NZB_HEALTH_MAX_JOBS_PER_TICK = 30   # cap jobs polled per tick
    _nzb_health_job_cursor: int = 0      # where the next capped tick starts polling
    _nzb_job_started_at: dict = {}       # job_id → datetime its download clock started (timeout anchor)
    _nzb_provider_last_progress = None   # datetime a job last completed at the provider (seeded on first tick)
    _NZB_QUEUED_TIMEOUT_MULTIPLIER = 12  # a 0% job waits at most this many timeouts while the provider is busy
    _NZB_HEALTH_JOB_TIMEOUT = 20         # seconds per job poll before giving up
    _NZB_HEALTH_SKIP_THRESHOLD = 20.0   # if last run took this long, skip next tick
    _nzb_folder_wait_counts: dict = {}   # job_id → consecutive ticks where folder not found
    _NZB_FOLDER_WAIT_MAX = 10            # after this many ticks (~100s) treat as broken
    _NZB_FOLDER_MIN_WAIT_SECONDS = 300   # ...and at least this long since the wait started
    _NZB_GHOST_MIN_WAIT_SECONDS = 120    # a job missing from the queue waits at least this long
    _nzb_folder_wait_since: dict = {}    # job_id → monotonic time its ghost/folder wait started
    _NZB_MASS_HOLD_MAX_SECONDS = 900     # a mass hold (see usenet_tasks) lasts at most this long per job
    _nzb_health_triggered: dict = {}     # entry_name → tick count since trigger (0 = not yet triggered)
    _nzb_confirmed_complete: dict = {}   # job_id → entry_name for jobs confirmed complete+folder found
    _nzb_ghost_repeat_counts: dict = {}  # item_id → consecutive ghost-job cycles for this item
    _NZB_GHOST_BLACKLIST_THRESHOLD = 3   # blacklist instead of retrying after this many ghost repeats


    def reinitialize(self):
        """Force reinitialization of the program runner to pick up new settings"""
        logging.info("Reinitializing ProgramRunner...")
        # Tell the run() monitoring loop to tolerate a momentarily-stopped
        # scheduler during this window, instead of treating it as a crash
        # and tearing down the whole run loop out from under us.
        self._reinitializing = True
        was_running_before_reinit = self._running
        try:
            # Need to shutdown and restart scheduler carefully
            with self.scheduler_lock:
                if self.scheduler and self.scheduler.running:
                    logging.info("Shutting down scheduler for reinitialization...")
                    # wait=False: don't block here until every in-flight job finishes.
                    # shutdown(wait=True) holds APScheduler's internal _jobstores_lock
                    # while it waits — and any running task that itself calls back into
                    # self.scheduler (e.g. task_heartbeat's watchdog, which calls
                    # scheduler.get_jobs()) needs that same lock, deadlocking forever.
                    # In-flight jobs aren't killed by wait=False, they just keep running
                    # to completion on their own threads, detached from this call.
                    self.scheduler.shutdown(wait=False)
                    logging.info("Scheduler stopped.")

            self._initialized_runner_attributes = False
            self.__init__() # Re-runs init, including scheduling initial tasks

            # __init__ unconditionally resets _running to False — restore it so
            # the still-alive run() loop (in the 'was already running' case)
            # doesn't see _running=False and exit on its next iteration.
            if was_running_before_reinit:
                self._running = True

                # __init__ only builds a fresh scheduler object, it doesn't start
                # it — without this, jobs would be scheduled but never fire.
                with self.scheduler_lock:
                    if self.scheduler and not self.scheduler.running:
                        start_paused = self._is_within_pause_schedule()
                        self.scheduler.start(paused=start_paused)
                        self._scheduler_started_once = True
                        logging.info(f"Scheduler restarted after reinitialization. Paused: {start_paused}")

            logging.info("ProgramRunner reinitialized successfully.")
        finally:
            self._reinitializing = False



def process_overseerr_webhook(data):
    notification_type = data.get('notification_type')

    if notification_type == 'TEST_NOTIFICATION':
        logging.info("Received test notification from Overseerr")
        return

    media = data.get('media')
    if not media:
        logging.warning("Received webhook without media information")
        return

    media_type = media.get('media_type')
    tmdb_id = media.get('tmdbId')
    # Extract additional info if available
    imdb_id = media.get('imdbId')
    tvdb_id = media.get('tvdbId')
    title = data.get('subject') # Title might be in subject

    # Extract requester information from webhook payload
    # Webhook payload has flat structure: requestedBy_username, requestedBy_email, etc.
    request_data = data.get('request', {})
    requester_display_name = request_data.get('requestedBy_username', 'Unknown')
    requester_email = request_data.get('requestedBy_email')
    request_id = request_data.get('request_id')  # Overseerr's request ID for tracking


    if not media_type or not tmdb_id:
        logging.error(f"Invalid Overseerr/Agregarr webhook data: missing media_type or tmdbId. Data: {data}")
        return

    logging.info(f"Processing Overseerr/Agregarr webhook: Type={media_type}, TMDB={tmdb_id}, Title='{title}', Requester='{requester_display_name}' (email: {requester_email}), Request ID={request_id}")

    wanted_item = {
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        # Include other IDs if available
        'imdb_id': imdb_id,
        'tvdb_id': tvdb_id,
    }

    # Handle TV Show specific data
    if media_type == 'tv':
        # Requested seasons (array of numbers)
        requested_seasons = media.get('requested_seasons')
        if requested_seasons:
            wanted_item['requested_seasons'] = requested_seasons
            logging.info(f"Added requested seasons to wanted item: {requested_seasons}")

        # Specific episode request? (Usually full season requests)
        # episode_number = media.get('episodeNumber')
        # season_number = media.get('seasonNumber')
        # if season_number is not None and episode_number is not None:
        #     wanted_item['season_number'] = season_number
        #     wanted_item['episode_number'] = episode_number
        #     logging.info(f"Webhook specified specific episode: S{season_number}E{episode_number}")


    wanted_content = [wanted_item]
    logging.debug(f"Processing wanted content from webhook: {wanted_item}")
    from metadata.metadata import process_metadata
    wanted_content_processed = process_metadata(wanted_content)

    if wanted_content_processed:
        # Get the versions for the relevant Overseerr or Agregarr source from settings
        content_sources = ProgramRunner().get_content_sources(force_refresh=False)

        # Determine if this is from Agregarr based on requester name
        is_agregarr = requester_display_name == "Agregarr"

        from content_checkers.overseerr import _source_allows_requester
        from database import add_wanted_items
        from content_checkers.content_source_detail import append_content_source_detail

        if is_agregarr:
            # Agregarr: single source, no requester filtering
            source_key = next((s for s, d in content_sources.items() if s.startswith('Agregarr')), None)
            matched_sources = [(source_key, content_sources[source_key])] if source_key else []
            source_type = 'Agregarr'
        else:
            # Overseerr: match ALL enabled sources whose allowed_requesters includes this requester
            source_type = 'Overseerr'
            matched_sources = []
            for s, d in content_sources.items():
                if not s.startswith('Overseerr') or not d.get('enabled', False):
                    continue
                allowed = d.get('allowed_requesters', ['__all__'])
                if not allowed:
                    allowed = ['__all__']
                if _source_allows_requester(allowed, requester_display_name):
                    matched_sources.append((s, d))

        if not matched_sources:
            logging.warning(f"No enabled {source_type} content source matched requester '{requester_display_name}' for webhook (TMDB ID: {tmdb_id}). Item not added.")
            return

        all_items_base = wanted_content_processed.get('movies', []) + wanted_content_processed.get('episodes', []) + wanted_content_processed.get('anime', [])

        if not all_items_base:
            logging.warning(f"Metadata processing for {source_type} webhook (TMDB ID: {tmdb_id}) resulted in no items to add.")
            return

        for source_key, source_data in matched_sources:
            versions = source_data.get('versions', {})
            import copy
            items_for_source = copy.deepcopy(all_items_base)
            for item in items_for_source:
                item['content_source'] = source_key
                item['content_source_detail'] = requester_display_name
                item['overseerr_request_id'] = request_id
                item = append_content_source_detail(item, source_type=source_type)
            add_wanted_items(items_for_source, versions, force_granular_versions=True)
            logging.info(f"Processed {len(items_for_source)} wanted item(s) from {source_type} webhook via source '{source_key}' (TMDB ID: {tmdb_id}). Requester: {requester_display_name}, Versions: {versions}")


def get_and_add_all_collected_from_plex(bypass=False, backfill=False):
    """
    Get all collected content from Plex/Symlink/Zurg modes.

    BACKFILL OPTIMIZATION (backfill=True):
    - Symlink mode: Uses filesystem-first approach (always)
    - Plex mode: Uses filesystem-first if 'mounted_file_location' is configured
      - Remaps Plex paths (/debrid/*) to CLI mount paths (/media/mount/*)
      - Extracts size_gb and resolution from filesystem (MediaInfo hybrid approach)
      - Skips non-debrid paths (e.g., NAS mounts)
      - Falls back to Plex API if success rate < 50% or mount not available
      - Performance: ~270x faster (90 seconds vs 7 hours for 85K items)

    REGULAR SCAN (backfill=False):
    - Always uses Plex API (preserves existing behavior)
    """
    collected_content = None  # Initialize here
    data_source = 'plex'  # Track data source for accurate logging
    mode = get_setting('File Management', 'file_collection_management')

    # OPTIMIZATION: For Symlink/Plex mode backfill, use filesystem FIRST (instant, no API calls)
    # This prevents 85K+ Plex API calls and rate limiting/timeouts
    use_filesystem_first = False
    mount_path = None

    if backfill and not bypass:
        if mode in ['Symlink', 'Symlinked/Local']:
            # Symlink mode: use location_on_disk as-is
            mount_path = get_setting('File Management', 'symlinked_files_path', '')
            if mount_path and os.path.isdir(mount_path):
                use_filesystem_first = True
                logging.info(f"[BACKFILL_FS] Using filesystem-first approach for Symlink mode")
            else:
                logging.info(f"[BACKFILL_SYMLINK] No valid mount configured, will use Plex API")

        elif mode == 'Plex':
            # Plex mode: remap Plex paths to mount paths
            mount_path = get_setting('Plex', 'mounted_file_location', '')
            if mount_path and os.path.isdir(mount_path):
                # Strip __all__ from mount path since Plex uses individual folders
                if mount_path.endswith('/__all__'):
                    mount_path = mount_path[:-8]  # Remove "/__all__"
                    logging.info(f"[BACKFILL_FS] Stripped __all__ from mount path, using: {mount_path}")

                use_filesystem_first = True
                logging.info(f"[BACKFILL_FS] Using filesystem-first approach for Plex mode with mount: {mount_path}")
            else:
                logging.info(f"[BACKFILL_PLEX] No valid mount configured (path: '{mount_path}'), will use Plex API")

    if use_filesystem_first:
        try:
            from database.database_reading import get_all_media_items
            from utilities.local_library_scan import local_library_scan, remap_plex_paths_to_mount

            # Get all Collected items from database
            db_items = get_all_media_items(state='Collected')
            logging.info(f"[BACKFILL_FS] Got {len(db_items)} items from database for filesystem scan")

            if db_items:
                # For Plex mode, remap paths to mount structure
                if mode == 'Plex' and mount_path:
                    logging.info(f"[BACKFILL_FS] Remapping {len(db_items)} Plex paths to mount structure...")
                    db_items = remap_plex_paths_to_mount(db_items, mount_path)

                    # Separate successfully remapped and failed items
                    remapped_items = [item for item in db_items if not item.get('_remap_failed')]
                    failed_items = [item for item in db_items if item.get('_remap_failed')]

                    if failed_items:
                        logging.warning(f"[BACKFILL_FS] {len(failed_items)} items failed path remapping, will use Plex API fallback per-item")

                    logging.info(f"[BACKFILL_FS] Successfully remapped {len(remapped_items)} items, scanning filesystem...")
                else:
                    remapped_items = db_items
                    failed_items = []

                # Scan filesystem to get size/resolution data (PRIMARY method)
                if remapped_items:
                    scan_results = local_library_scan(remapped_items, extract_resolution=True)
                    logging.info(f"[BACKFILL_FS] Filesystem scan completed for {len(scan_results)} items")

                    # Convert scan results to expected format
                    filesystem_items = list(scan_results.values())
                else:
                    filesystem_items = []

                if filesystem_items:
                    movies = [item for item in filesystem_items if item.get('type') == 'movie']
                    episodes = [item for item in filesystem_items if item.get('type') == 'episode']
                    logging.info(f"[BACKFILL_FS] Filesystem found {len(movies)} movies and {len(episodes)} episodes")

                    collected_content = {'movies': movies, 'episodes': episodes}
                    data_source = 'filesystem'  # Mark that we used filesystem scan

                    # Per-item fallback: Check for items with missing size/resolution and get from Plex
                    # (not when Plex mode is run with Silo: there is no Plex to ask)
                    from utilities.silo_functions import is_silo_library_mode as _silo_lib
                    if mode == 'Plex' and backfill and not _silo_lib():
                        items_needing_plex = []
                        for item in filesystem_items:
                            # Check if size or resolution is missing
                            if item.get('size_gb') is None or item.get('resolution') is None:
                                items_needing_plex.append(item)

                        if items_needing_plex:
                            logging.info(f"[BACKFILL_PLEX_FALLBACK] {len(items_needing_plex)} items missing size/resolution, fetching from Plex API...")

                            try:
                                # Get Plex library content to match against
                                from utilities.plex_functions import get_collected_from_plex
                                plex_content = asyncio.run(get_collected_from_plex())

                                if plex_content:
                                    plex_movies = plex_content.get('movies', [])
                                    plex_episodes = plex_content.get('episodes', [])

                                    # Create lookup dict by unique identifier
                                    plex_lookup = {}
                                    for plex_item in plex_movies + plex_episodes:
                                        # Use imdb_id or tmdb_id + type as key
                                        if plex_item.get('imdb_id'):
                                            key = ('imdb', plex_item['imdb_id'], plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('tmdb_id'):
                                            key = ('tmdb', str(plex_item['tmdb_id']), plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        # For episodes, also use season/episode as key
                                        if plex_item.get('type') == 'episode':
                                            if plex_item.get('imdb_id') and plex_item.get('season_number') and plex_item.get('episode_number'):
                                                key = ('episode', plex_item['imdb_id'], plex_item['season_number'], plex_item['episode_number'])
                                                plex_lookup[key] = plex_item

                                    # Match and update items
                                    updated_count = 0
                                    for item in items_needing_plex:
                                        plex_match = None

                                        # Try to find match in Plex data
                                        if item.get('imdb_id'):
                                            plex_match = plex_lookup.get(('imdb', item['imdb_id'], item.get('type')))
                                        if not plex_match and item.get('tmdb_id'):
                                            plex_match = plex_lookup.get(('tmdb', str(item['tmdb_id']), item.get('type')))
                                        if not plex_match and item.get('type') == 'episode':
                                            if item.get('imdb_id') and item.get('season_number') and item.get('episode_number'):
                                                plex_match = plex_lookup.get(('episode', item['imdb_id'], item['season_number'], item['episode_number']))

                                        if plex_match:
                                            # Update missing fields from Plex
                                            if item.get('size_gb') is None and plex_match.get('size_gb'):
                                                item['size_gb'] = plex_match['size_gb']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated size for {item.get('title', 'Unknown')}: {plex_match['size_gb']}GB")
                                            if item.get('resolution') is None and plex_match.get('resolution'):
                                                item['resolution'] = plex_match['resolution']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated resolution for {item.get('title', 'Unknown')}: {plex_match['resolution']}")
                                            updated_count += 1

                                    logging.info(f"[BACKFILL_PLEX_FALLBACK] Updated {updated_count}/{len(items_needing_plex)} items from Plex API")

                            except Exception as e:
                                logging.error(f"[BACKFILL_PLEX_FALLBACK] Error during per-item Plex fallback: {e}", exc_info=True)

                        # Also fetch data for items that failed path remapping
                        if failed_items:
                            logging.info(f"[BACKFILL_PLEX_FALLBACK] Fetching {len(failed_items)} failed remap items from Plex API...")
                            try:
                                from utilities.plex_functions import get_collected_from_plex
                                plex_content = asyncio.run(get_collected_from_plex())

                                if plex_content:
                                    plex_movies = plex_content.get('movies', [])
                                    plex_episodes = plex_content.get('episodes', [])

                                    # Create lookup dict
                                    plex_lookup = {}
                                    for plex_item in plex_movies + plex_episodes:
                                        if plex_item.get('imdb_id'):
                                            key = ('imdb', plex_item['imdb_id'], plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('tmdb_id'):
                                            key = ('tmdb', str(plex_item['tmdb_id']), plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('type') == 'episode':
                                            if plex_item.get('imdb_id') and plex_item.get('season_number') and plex_item.get('episode_number'):
                                                key = ('episode', plex_item['imdb_id'], plex_item['season_number'], plex_item['episode_number'])
                                                plex_lookup[key] = plex_item

                                    # Match failed items and add to results
                                    matched_count = 0
                                    for failed_item in failed_items:
                                        plex_match = None

                                        # Try to find match
                                        if failed_item.get('imdb_id'):
                                            plex_match = plex_lookup.get(('imdb', failed_item['imdb_id'], failed_item.get('type')))
                                        if not plex_match and failed_item.get('tmdb_id'):
                                            plex_match = plex_lookup.get(('tmdb', str(failed_item['tmdb_id']), failed_item.get('type')))
                                        if not plex_match and failed_item.get('type') == 'episode':
                                            if failed_item.get('imdb_id') and failed_item.get('season_number') and failed_item.get('episode_number'):
                                                plex_match = plex_lookup.get(('episode', failed_item['imdb_id'], failed_item['season_number'], failed_item['episode_number']))

                                        if plex_match:
                                            # Add to results with Plex data
                                            if plex_match.get('type') == 'movie':
                                                movies.append(plex_match)
                                            else:
                                                episodes.append(plex_match)
                                            matched_count += 1

                                    logging.info(f"[BACKFILL_PLEX_FALLBACK] Matched {matched_count}/{len(failed_items)} failed items from Plex API")

                                    # Update collected_content with new items
                                    collected_content = {'movies': movies, 'episodes': episodes}

                            except Exception as e:
                                logging.error(f"[BACKFILL_PLEX_FALLBACK] Error fetching failed items from Plex: {e}", exc_info=True)

                    # Per-item fallback for Symlink mode (same logic as Plex mode), not with Silo
                    elif mode in ['Symlink', 'Symlinked/Local'] and backfill and not _silo_lib() \
                            and get_setting('File Management', 'media_server_type', 'plex') != 'silo':
                        items_needing_plex = []
                        for item in filesystem_items:
                            # Check if size or resolution is missing
                            if item.get('size_gb') is None or item.get('resolution') is None:
                                items_needing_plex.append(item)

                        if items_needing_plex:
                            logging.info(f"[BACKFILL_PLEX_FALLBACK] {len(items_needing_plex)} Symlink items missing size/resolution, fetching from Plex API...")

                            try:
                                from utilities.plex_functions import get_collected_from_plex
                                plex_content = asyncio.run(get_collected_from_plex())

                                if plex_content:
                                    plex_movies = plex_content.get('movies', [])
                                    plex_episodes = plex_content.get('episodes', [])

                                    # Create lookup dict by unique identifier
                                    plex_lookup = {}
                                    for plex_item in plex_movies + plex_episodes:
                                        if plex_item.get('imdb_id'):
                                            key = ('imdb', plex_item['imdb_id'], plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('tmdb_id'):
                                            key = ('tmdb', str(plex_item['tmdb_id']), plex_item.get('type'))
                                            plex_lookup[key] = plex_item
                                        if plex_item.get('type') == 'episode':
                                            if plex_item.get('imdb_id') and plex_item.get('season_number') and plex_item.get('episode_number'):
                                                key = ('episode', plex_item['imdb_id'], plex_item['season_number'], plex_item['episode_number'])
                                                plex_lookup[key] = plex_item

                                    # Match and update items
                                    updated_count = 0
                                    for item in items_needing_plex:
                                        plex_match = None

                                        # Try to find match in Plex data
                                        if item.get('imdb_id'):
                                            plex_match = plex_lookup.get(('imdb', item['imdb_id'], item.get('type')))
                                        if not plex_match and item.get('tmdb_id'):
                                            plex_match = plex_lookup.get(('tmdb', str(item['tmdb_id']), item.get('type')))
                                        if not plex_match and item.get('type') == 'episode':
                                            if item.get('imdb_id') and item.get('season_number') and item.get('episode_number'):
                                                plex_match = plex_lookup.get(('episode', item['imdb_id'], item['season_number'], item['episode_number']))

                                        if plex_match:
                                            # Update missing fields from Plex
                                            if item.get('size_gb') is None and plex_match.get('size_gb'):
                                                item['size_gb'] = plex_match['size_gb']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated size for {item.get('title', 'Unknown')}: {plex_match['size_gb']}GB")
                                            if item.get('resolution') is None and plex_match.get('resolution'):
                                                item['resolution'] = plex_match['resolution']
                                                logging.debug(f"[BACKFILL_PLEX_FALLBACK] Updated resolution for {item.get('title', 'Unknown')}: {plex_match['resolution']}")
                                            updated_count += 1

                                    logging.info(f"[BACKFILL_PLEX_FALLBACK] Updated {updated_count}/{len(items_needing_plex)} Symlink items from Plex API")

                                    # Update collected_content with updated items
                                    movies = [item for item in filesystem_items if item.get('type') == 'movie']
                                    episodes = [item for item in filesystem_items if item.get('type') == 'episode']
                                    collected_content = {'movies': movies, 'episodes': episodes}

                            except Exception as e:
                                logging.error(f"[BACKFILL_PLEX_FALLBACK] Error during Symlink per-item Plex fallback: {e}", exc_info=True)

                    # Check success rate for Plex mode (AFTER fallback completes)
                    if mode == 'Plex':
                        total_items = len(db_items)
                        filesystem_count = len([item for item in filesystem_items if not item.get('_from_plex_fallback')])
                        # Total found = filesystem items + movies/episodes from fallback
                        total_found = len(movies) + len(episodes)
                        success_rate = (total_found / total_items * 100) if total_items > 0 else 0

                        if failed_items:
                            logging.warning(f"[BACKFILL_FS] {len(failed_items)} items failed path remapping - check logs for details")

                        # Log breakdown
                        logging.info(f"[BACKFILL_FS] Filesystem: {filesystem_count} items, Plex fallback: {total_found - filesystem_count} items")
                        logging.info(f"[BACKFILL_FS] Total success rate: {success_rate:.1f}% ({total_found}/{total_items} items)")

                        # If success rate is very low, warn and consider falling back to Plex API
                        if success_rate < 50 and total_items > 100:
                            logging.warning(f"[BACKFILL_FS] Low success rate ({success_rate:.1f}%), check mount configuration. Falling back to Plex API...")
                            collected_content = None  # Trigger Plex API fallback
                else:
                    logging.warning(f"[BACKFILL_FS] Filesystem scan returned 0 items, will fall back to Plex")
            else:
                logging.warning(f"[BACKFILL_FS] No Collected items in database, will fall back to Plex")
        except Exception as e:
            logging.error(f"[BACKFILL_FS] Error during filesystem scan: {e}", exc_info=True)
            logging.warning(f"[BACKFILL_FS] Filesystem scan failed, will fall back to Plex")

    # PLEX API PATH: Used for Plex mode users OR as fallback if filesystem failed/returned 0 items
    # Backward compatible: preserves existing behavior for non-symlink modes
    if collected_content is None and (mode == 'Plex' or bypass or backfill):
        if backfill and mode in ['Symlink', 'Symlinked/Local']:
            logging.info("[BACKFILL_FALLBACK] Filesystem returned no data, falling back to Plex API...")

        if backfill:
            logging.info("[BACKFILL_PLEX] Getting all collected content from Plex API (with size/resolution fetch)...")
        else:
            logging.info("Getting all collected content from Plex...")
        try:
            from utilities.silo_functions import is_silo_library_mode, get_collected_from_silo
            if is_silo_library_mode():
                logging.info("Plex mode is run with Silo: reading Silo's catalog instead of Plex.")
                collected_content = get_collected_from_silo()
            else:
                collected_content = asyncio.run(run_get_collected_from_plex(bypass=bypass, fetch_sizes=backfill))
        except Exception as e:
             logging.error(f"Error running run_get_collected_from_plex: {e}", exc_info=True)
             return None # Return None on error during fetch

    elif collected_content is None and mode == 'Zurg':
        logging.info("Getting all collected content from Zurg...")
        try:
             # Assuming a similar function exists or needs to be created for Zurg full scan
            collected_content = asyncio.run(run_get_collected_from_zurg(bypass=bypass)) # Added bypass
        except Exception as e:
            logging.error(f"Error running run_get_collected_from_zurg: {e}", exc_info=True)
            return None # Return None on error during fetch
    elif collected_content is None:
        logging.info(f"File collection management mode ('{mode}') does not support full library scan for collected items.")
        return None


    if collected_content:
        movies = collected_content.get('movies', []) # Use .get for safety
        episodes = collected_content.get('episodes', [])

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from {mode}.")

        # LEGACY FALLBACK: Keep old logic for edge cases (should rarely trigger now)
        # Only triggers if Plex API was used but returned 0 items
        if backfill and len(movies) == 0 and len(episodes) == 0 and mode == 'Symlinked/Local':
            logging.warning("[BACKFILL_FALLBACK] Plex returned 0 items, falling back to filesystem scan for backfill...")
            try:
                from database.database_reading import get_all_media_items
                from utilities.local_library_scan import local_library_scan

                # Get all Collected items from database
                db_items = get_all_media_items(state='Collected')
                logging.info(f"[BACKFILL_FALLBACK] Got {len(db_items)} items from database for filesystem backfill")

                if db_items:
                    # Scan filesystem to get size/resolution data
                    scan_results = local_library_scan(db_items)
                    logging.info(f"[BACKFILL_FALLBACK] Filesystem scan found data for {len(scan_results)} items")

                    # Convert scan results to the format expected by add_collected_items
                    # scan_results is a dict: {item_id: {updated_item_dict}}
                    filesystem_items = list(scan_results.values())

                    if filesystem_items:
                        movies = [item for item in filesystem_items if item.get('type') == 'movie']
                        episodes = [item for item in filesystem_items if item.get('type') == 'episode']
                        logging.info(f"[BACKFILL_FALLBACK] Filesystem fallback found {len(movies)} movies and {len(episodes)} episodes")

                        # Update collected_content to include filesystem data
                        collected_content = {'movies': movies, 'episodes': episodes}
                        data_source = 'filesystem'  # Mark that we used filesystem scan
            except Exception as e:
                logging.error(f"[BACKFILL_FALLBACK] Error during filesystem fallback: {e}", exc_info=True)

        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items # Keep import local
            add_collected_items(movies + episodes, backfill=backfill, data_source=data_source)
            logging.info(f"Finished adding {len(movies) + len(episodes)} collected items to database.")

            # -------- Memory cleanup to avoid JSON blob retention --------
            try:
                import gc
                collected_content.clear()  # remove references to large lists
                # Conditionally delete large local lists if they exist
                for _var in ['movies', 'episodes', 'all_raw_movies', 'all_raw_episodes']:
                    if _var in locals():
                        locals()[_var].clear()
                # Drop references so GC can reclaim
                movies = episodes = None
                if 'all_raw_movies' in locals():
                    all_raw_movies = None  # type: ignore
                if 'all_raw_episodes' in locals():
                    all_raw_episodes = None  # type: ignore
                gc.collect()
                try:
                    import ctypes as _ct
                    _ct.CDLL('libc.so.6').malloc_trim(0)
                    logging.info("[MemCleanup] Cleared collected content, GC + malloc_trim after Plex full scan.")
                except Exception:
                    logging.info("[MemCleanup] Cleared collected content and forced GC after Plex full scan.")
            except Exception as e_cleanup:
                logging.debug(f"[MemCleanup] Exception during cleanup: {e_cleanup}")
            # ----------------------------------------------------------------
            return collected_content  # Return the original content even if some items were skipped

    logging.warning(f"Failed to retrieve or process collected content from {mode}.")
    return None


def backfill_resolution_from_stored_paths():
    """
    Backfill resolution for Collected items with NULL resolution by extracting from stored paths.
    Uses location_on_disk or filled_by_file fields already in the database.

    Returns:
        dict: Results with counts of updated items
    """
    try:
        from utilities.local_library_scan import extract_resolution_from_filename
        from database.database_reading import get_db_connection

        logging.info("[BACKFILL_RESOLUTION] Starting resolution backfill from stored paths...")

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all Collected items with NULL resolution
        cursor.execute("""
            SELECT id, title, type, location_on_disk, filled_by_file
            FROM media_items
            WHERE state = 'Collected'
              AND resolution IS NULL
              AND (ghostlisted = 0 OR ghostlisted IS NULL)
        """)

        items = cursor.fetchall()
        total_items = len(items)
        logging.info(f"[BACKFILL_RESOLUTION] Found {total_items} Collected items with NULL resolution")

        if total_items == 0:
            conn.close()
            return {'success': True, 'total_items': 0, 'updated': 0, 'failed': 0}

        updated_count = 0
        failed_count = 0
        by_resolution = {}

        for item in items:
            item_id = item['id']
            title = item['title']
            item_type = item['type']
            location = item['location_on_disk']
            filename = item['filled_by_file']

            # Try to extract resolution from location_on_disk first
            resolution = None
            if location:
                resolution = extract_resolution_from_filename(location)
                if resolution:
                    logging.debug(f"[BACKFILL_RESOLUTION] Extracted '{resolution}' from location: {location}")

            # Fallback to filled_by_file if location didn't work
            if not resolution and filename:
                resolution = extract_resolution_from_filename(filename)
                if resolution:
                    logging.debug(f"[BACKFILL_RESOLUTION] Extracted '{resolution}' from filename: {filename}")

            if resolution:
                # Update database
                cursor.execute("""
                    UPDATE media_items
                    SET resolution = ?
                    WHERE id = ?
                """, (resolution, item_id))

                updated_count += 1
                by_resolution[resolution] = by_resolution.get(resolution, 0) + 1

                if updated_count % 100 == 0:
                    logging.info(f"[BACKFILL_RESOLUTION] Progress: {updated_count}/{total_items} items updated")
            else:
                failed_count += 1
                logging.debug(f"[BACKFILL_RESOLUTION] Could not extract resolution for {title} (ID: {item_id})")

        conn.commit()
        conn.close()

        logging.info(f"[BACKFILL_RESOLUTION] Completed: {updated_count} updated, {failed_count} failed")
        logging.info(f"[BACKFILL_RESOLUTION] By resolution: {by_resolution}")

        return {
            'success': True,
            'total_items': total_items,
            'updated': updated_count,
            'failed': failed_count,
            'by_resolution': by_resolution
        }

    except Exception as e:
        logging.error(f"[BACKFILL_RESOLUTION] Error during resolution backfill: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# FIX: Debounce for Plex recent scan to prevent API spam
# Track the last time get_and_add_recent_collected_from_plex was called
_last_plex_recent_scan_time = None
_PLEX_RECENT_SCAN_COOLDOWN_SECONDS = 300  # 5 minutes minimum between scans

def get_and_add_recent_collected_from_plex():
    global _last_plex_recent_scan_time
    from datetime import datetime

    # FIX: Debounce - skip if scan was run recently
    if _last_plex_recent_scan_time is not None:
        elapsed = (datetime.now() - _last_plex_recent_scan_time).total_seconds()
        if elapsed < _PLEX_RECENT_SCAN_COOLDOWN_SECONDS:
            logging.debug(f"[PlexRecentScan] Debounce: Skipping scan, last ran {elapsed:.0f}s ago (cooldown: {_PLEX_RECENT_SCAN_COOLDOWN_SECONDS}s)")
            return None

    _last_plex_recent_scan_time = datetime.now()

    collected_content = None
    mode = get_setting('File Management', 'file_collection_management')
    logging.info(f"Getting recently added content from {mode}...")

    try:
        from utilities.silo_functions import is_silo_library_mode, get_recent_from_silo
        if mode == 'Plex' and is_silo_library_mode():
            collected_content = get_recent_from_silo()
        elif mode == 'Plex':
            collected_content = asyncio.run(run_get_recent_from_plex())
        elif mode == 'Zurg':
            collected_content = asyncio.run(run_get_recent_from_zurg())
        else:
            logging.info(f"File collection management mode ('{mode}') does not support recent library scan.")
            return None
    except Exception as e:
         logging.error(f"Error running recent scan function for {mode}: {e}", exc_info=True)
         return None


    if collected_content:
        movies = collected_content.get('movies', [])
        episodes = collected_content.get('episodes', [])

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} recent episodes from {mode}.")

        # Check and fix any unmatched items before adding to database if enabled
        if get_setting('Debug', 'enable_unmatched_items_check', True):
            logging.info("Checking and fixing unmatched items before adding to database")
            try:
                from utilities.plex_matching_functions import check_and_fix_unmatched_items
                collected_content = check_and_fix_unmatched_items(collected_content)
                # Get updated counts after matching check
                movies = collected_content.get('movies', [])
                episodes = collected_content.get('episodes', [])
                logging.info(f"Counts after unmatched check: {len(movies)} movies, {len(episodes)} episodes.")
            except Exception as e_match:
                logging.error(f"Error during check_and_fix_unmatched_items: {e_match}", exc_info=True)
                # Proceed with potentially unmatched items? Or return? Let's proceed.


        # Don't return None if some items were skipped during add_collected_items
        if len(movies) > 0 or len(episodes) > 0:
            from database import add_collected_items
            try:
                add_collected_items(movies + episodes, recent=True)
                logging.info(f"Finished adding {len(movies) + len(episodes)} recent items to database.")
                return collected_content  # Return the original content even if some items were skipped
            except Exception as e_add:
                 logging.error(f"Error during add_collected_items for recent items: {e_add}", exc_info=True)
                 return None # Return None if adding fails
        else:
            logging.info("No recent movies or episodes to add after processing.")
            return collected_content # Return empty dict if nothing to add

    logging.warning(f"Failed to retrieve or process recent content from {mode}.")
    return None

def run_local_library_scan():
    # ... (This function seems unused/disabled, no changes needed) ...
    logging.info("Full library scan disabled for now")
    #local_library_scan()

def run_recent_local_library_scan():
    # ... (This function seems unused/disabled, no changes needed) ...
    logging.info("Recent library scan disabled for now")
    #recent_local_library_scan()

# Add Listener Setup Method
def _setup_scheduler_listeners(runner_instance):
    """
    Ensures the scheduler is initialized and adds necessary event listeners.
    If runner_instance.scheduler is None, a new scheduler will be created.
    """
    logging.info(f"[_setup_scheduler_listeners] Entered for runner (ID: {id(runner_instance)}).")
    current_scheduler_id = id(runner_instance.scheduler) if runner_instance.scheduler else "None"
    logging.info(f"[_setup_scheduler_listeners] Initial runner_instance.scheduler is {current_scheduler_id}. initial_listeners_setup_complete: {getattr(runner_instance, 'initial_listeners_setup_complete', 'N/A')}")

    scheduler_recreated = False
    if runner_instance.scheduler is None:
        logging.info(f"[_setup_scheduler_listeners] runner_instance.scheduler is None. Creating new BackgroundScheduler.")
        
        try:
            tz_setting = get_setting('UI Settings', 'timezone', 'UTC')
            try:
                tz = pytz.timezone(tz_setting)
            except pytz.exceptions.UnknownTimeZoneError:
                logging.warning(f"[_setup_scheduler_listeners] Unknown timezone '{tz_setting}' in settings. Defaulting to UTC.")
                tz = pytz.utc
            logging.info(f"[_setup_scheduler_listeners] Initializing new APScheduler with timezone: {tz.key if hasattr(tz, 'key') else tz}")
        except Exception as e_tz:
            logging.error(f"[_setup_scheduler_listeners] Failed to get local timezone for scheduler, using UTC fallback: {e_tz}")
            tz = pytz.utc

        _queue_workers, executors, job_defaults = _build_scheduler_config()

        try:
            new_scheduler = BackgroundScheduler(
                executors=executors,
                job_defaults=job_defaults,
                timezone=tz
            )
            runner_instance.scheduler = new_scheduler
            scheduler_recreated = True
            # Mark listeners as NOT setup for this new scheduler instance
            runner_instance.initial_listeners_setup_complete = False 
            logging.info(f"[_setup_scheduler_listeners] New BackgroundScheduler CREATED and ASSIGNED. New scheduler ID: {id(runner_instance.scheduler)}.")
        except Exception as e_create_scheduler:
            logging.error(f"[_setup_scheduler_listeners] FAILED to create new BackgroundScheduler: {e_create_scheduler}", exc_info=True)
            runner_instance.scheduler = None # Ensure it's None if creation failed
            runner_instance.initial_listeners_setup_complete = False
            raise # Re-raise the exception to signal failure to the caller
    
    # Add listeners if scheduler exists and listeners are not yet marked complete for this instance
    # The initial_listeners_setup_complete flag is now specific to a scheduler instance.
    if runner_instance.scheduler and not getattr(runner_instance, 'initial_listeners_setup_complete', False):
        try:
            logging.info(f"[_setup_scheduler_listeners] Setting up APScheduler job listeners for scheduler (ID: {id(runner_instance.scheduler)}).")
            runner_instance.scheduler.add_listener(
                runner_instance._job_listener,
                apscheduler.events.EVENT_JOB_SUBMITTED |
                apscheduler.events.EVENT_JOB_EXECUTED |
                apscheduler.events.EVENT_JOB_ERROR |
                apscheduler.events.EVENT_JOB_MISSED |
                apscheduler.events.EVENT_JOB_MAX_INSTANCES # Added EVENT_JOB_MAX_INSTANCES
            )
            runner_instance.initial_listeners_setup_complete = True # Mark listeners as setup
            logging.info(f"[_setup_scheduler_listeners] APScheduler job listeners added successfully for scheduler (ID: {id(runner_instance.scheduler)}).")
        except Exception as e_add_listener:
            logging.error(f"[_setup_scheduler_listeners] Failed to add APScheduler listener: {e_add_listener}", exc_info=True)
            runner_instance.initial_listeners_setup_complete = False # Failed to setup
            # If listener setup fails, the scheduler might still run but without our custom listener logic.
            # Depending on how critical _job_listener is, might need to raise here.
    elif runner_instance.scheduler:
         logging.info(f"[_setup_scheduler_listeners] Listeners already marked as setup for scheduler (ID: {id(runner_instance.scheduler)}).")
    else: # Should not happen if creation logic is correct
        logging.error(f"[_setup_scheduler_listeners] Cannot setup listeners as runner_instance.scheduler is still None after creation attempt.")


    # If scheduler was recreated, or if initial tasks need to be (re)scheduled for any other reason
    # For instance, if task definitions changed and we need a full reschedule.
    # For now, only do this if scheduler was just recreated.
    if scheduler_recreated and runner_instance.scheduler:
        try:
            logging.info(f"[_setup_scheduler_listeners] Scheduler was recreated. Re-scheduling initial tasks for scheduler (ID: {id(runner_instance.scheduler)}).")
            runner_instance._schedule_initial_tasks() # Populate the new scheduler with tasks
            logging.info(f"[_setup_scheduler_listeners] Initial tasks (re)scheduled successfully for new scheduler (ID: {id(runner_instance.scheduler)}).")
        except Exception as e_schedule_tasks:
            logging.error(f"[_setup_scheduler_listeners] Error (re)scheduling initial tasks for new scheduler: {e_schedule_tasks}", exc_info=True)
            # This is critical. If tasks can't be added, the new scheduler is useless.
            # Consider shutting down the new scheduler and setting runner_instance.scheduler back to None.
            try:
                runner_instance.scheduler.shutdown(wait=False)
            except: pass
            runner_instance.scheduler = None
            runner_instance.initial_listeners_setup_complete = False
            raise RuntimeError(f"Failed to schedule tasks on newly created scheduler: {e_schedule_tasks}")

    logging.info(f"[_setup_scheduler_listeners] Completed. Final runner_instance.scheduler ID: {id(runner_instance.scheduler) if runner_instance.scheduler else 'None'}. initial_listeners_setup_complete: {getattr(runner_instance, 'initial_listeners_setup_complete', 'N/A')}")

def run_program():
    global program_runner
    logging.info("Program start requested")

    if program_runner is None or not program_runner.is_running():
        logging.info("Initializing ProgramRunner...")
        program_runner = ProgramRunner()
        # Setup listeners after init
        try:
            _setup_scheduler_listeners(program_runner) # Use the correct function name
        except Exception as e:
             logging.error(f"Failed to set up scheduler listeners during startup: {e}", exc_info=True)
        # Update the program runner in program_operation_routes
        from routes.program_operation_routes import program_operation_bp
        program_operation_bp.program_runner = program_runner # Ensure routes use the instance
        logging.info("Starting ProgramRunner instance...")
        program_runner.start()  # Starts the scheduler and run loop
    else:
        logging.info("Program is already running")
    return program_runner

if __name__ == "__main__":
    run_program()
