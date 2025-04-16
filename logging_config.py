import logging
import logging.handlers
from utilities.settings import get_setting
import psutil
import os
import time
import threading
import cProfile
import pstats
import io
import json
import re
from datetime import datetime
from queues.performance_monitor import monitor, start_performance_monitoring

# Global profiler
global_profiler = cProfile.Profile()

def start_global_profiling():
    global global_profiler
    try:
        # First try to disable any existing profiler
        try:
            global_profiler.disable()
        except:
            pass
        # Create a new profiler instance
        global_profiler = cProfile.Profile()
        global_profiler.enable()
    except Exception as e:
        logging.warning(f"Failed to start profiling: {str(e)}")
        # Continue without profiling if it fails
        pass

def stop_global_profiling():
    global global_profiler
    try:
        global_profiler.disable()
    except Exception as e:
        logging.warning(f"Failed to stop profiling: {str(e)}")

class OverwriteFileHandler(logging.FileHandler):
    def __init__(self, filename, mode='w', encoding=None, delay=False):
        super().__init__(filename, mode, encoding, delay)
    
    def emit(self, record):
        # Open the file in write mode ('w') for each emission
        self.baseFilename = self.baseFilename
        with open(self.baseFilename, 'w', encoding=self.encoding) as f:
            f.write(self.format(record) + self.terminator)

class DynamicConsoleHandler(logging.StreamHandler):
    def __init__(self):
        import sys
        super().__init__()
        if sys.platform == 'win32':
            import locale
            # Set UTF-8 encoding for Windows console
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
            # Set console to UTF-8 mode
            try:
                locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
            except locale.Error:
                pass
        self.setLevel(self.get_level())

    def get_level(self):
        console_level = get_setting("Debug", "logging_level", "INFO")
        return getattr(logging, console_level.upper())

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

class APSchedulerQueueJobFilter(logging.Filter):
    """
    Filters out INFO level logs regarding the start and successful
    completion of known queue-related jobs, regardless of the
    originating logger.
    """
    # !!! IMPORTANT: Verify and update this set with the exact names
    # of all your queue-related APScheduler jobs !!!
    QUEUE_JOB_NAMES = {
        "Wanted",
        "task_send_notifications",
        "task_process_scraping_adding",
        "task_process_pending_rclone_paths",
        # Add other queue job names here if necessary
    }

    def filter(self, record):
        if record.levelno == logging.INFO:
            try:
                msg = record.getMessage()
                if "Running job" in msg or "executed successfully" in msg:
                    # Corrected regex to capture only the job name
                    match = re.search(r'(?:Running job|Job) "(.+?)\s*\(' , msg)
                    job_name = match.group(1) if match else None

                    if job_name and job_name in self.QUEUE_JOB_NAMES:
                        return False # Don't log this record
            except Exception:
                 # In case of error during filtering, allow the log to pass
                 pass

        # For all other records, allow them to be logged
        return True

# --- Add Filter for APScheduler DEBUG noise ---
class APSchedulerDebugNoiseFilter(logging.Filter):
    """
    Filters out specific DEBUG messages from apscheduler.scheduler.
    """
    def filter(self, record):
        # Check if the log record is from apscheduler.scheduler and is DEBUG level
        if record.levelno == logging.DEBUG and record.name == 'apscheduler.scheduler':
            msg = record.getMessage()
            # Check if it's one of the messages we want to suppress
            if "Looking for jobs to run" in msg or "Next wakeup is due at" in msg:
                return False # Suppress these specific messages

        # Allow all other records
        return True
# --- End of APSchedulerDebugNoiseFilter ---

def log_system_stats():
    """Start the performance monitoring system"""
    monitor.start_monitoring()

# Create a filter to exclude logs from specific files
class ExcludeFilter(logging.Filter):
    def filter(self, record):
        # Exclude logs from specific files and profiling messages
        if record.filename == 'rules.py' or record.filename == 'rebulk.py' or record.filename == 'processors.py' or 'profiling' in record.msg.lower():
            return False
            
        # Exclude Flask application context error during startup
        if record.filename == 'settings_routes.py' and record.funcName == 'get_enabled_notifications' and 'Working outside of application context' in str(record.msg):
            return False
            
        # Exclude logs from the item_tracker logger from other handlers
        if record.name == 'item_tracker':
            return False
            
        return True

# Create a new JSONFormatter
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            'timestamp': datetime.utcfromtimestamp(record.created).isoformat() + 'Z',
            'level': record.levelname,
            'name': record.name,
        }
        if isinstance(record.msg, dict):
            log_record.update(record.msg) # Merge the dictionary message
        else:
            log_record['message'] = record.getMessage()

        # Include exception info if available
        if record.exc_info:
            log_record['exception'] = self.formatException(record.exc_info)
        if record.stack_info:
            log_record['stack_info'] = self.formatStack(record.stack_info)
            
        return json.dumps(log_record)

def setup_debug_logging(log_dir):
    # Debug file handler with immediate flush
    class ImmediateRotatingFileHandler(logging.handlers.RotatingFileHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()  # Force immediate flush
            
    debug_handler = ImmediateRotatingFileHandler(
        os.path.join(log_dir, 'debug.log'), 
        maxBytes=50*1024*1024, 
        backupCount=5, 
        encoding='utf-8', 
        errors='replace'
    )
    debug_handler.setLevel(logging.DEBUG)
    
    # Add filters to exclude unwanted messages
    debug_handler.addFilter(lambda record: not record.name.startswith(('urllib3', 'requests', 'charset_normalizer')))
    debug_handler.addFilter(ExcludeFilter())
    debug_handler.addFilter(APSchedulerQueueJobFilter())
    debug_handler.addFilter(APSchedulerDebugNoiseFilter())
    
    formatter = logging.Formatter('%(asctime)s - %(filename)s:%(funcName)s:%(lineno)d - %(levelname)s - %(message)s')
    debug_handler.setFormatter(formatter)
    logging.getLogger().addHandler(debug_handler)

def setup_info_logging(log_dir):
    # Add console handler for info logs
    console_handler = DynamicConsoleHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    
    # Add filters to exclude unwanted messages
    console_handler.addFilter(lambda record: not record.name.startswith(('urllib3', 'requests', 'charset_normalizer')))
    console_handler.addFilter(ExcludeFilter())
    console_handler.addFilter(APSchedulerQueueJobFilter())
    
    # Add handler to root logger
    logging.getLogger().addHandler(console_handler)

def setup_error_logging(log_dir):
    # Error file handler
    error_logger = logging.getLogger('error_logger')
    error_logger.addHandler(logging.NullHandler())
    error_logger.propagate = False

def setup_queue_logging(log_dir):
    # Queue file handler
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.addHandler(logging.NullHandler())
    queue_logger.propagate = False

def setup_performance_logging(log_dir):
    # Performance file handler with immediate flush
    performance_formatter = logging.Formatter('%(message)s')
    performance_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, 'performance.log'), 
        maxBytes=50*1024*1024, 
        backupCount=0, 
        encoding='utf-8', 
        errors='replace'
    )
    performance_handler.setLevel(logging.INFO)
    performance_handler.setFormatter(performance_formatter)
    
    # Create a separate logger for performance logs
    performance_logger = logging.getLogger('performance_logger')
    performance_logger.setLevel(logging.INFO)
    performance_logger.addHandler(performance_handler)
    performance_logger.propagate = False  # Prevent performance logs from propagating to root logger

def setup_item_tracker_logging(log_dir):
    """Sets up the logger for tracking item lifecycle events."""
    tracker_file = os.path.join(log_dir, 'item_tracker.log')
    tracker_handler = logging.handlers.RotatingFileHandler(
        tracker_file,
        maxBytes=50*1024*1024, # 50 MB
        backupCount=5,
        encoding='utf-8'
    )
    tracker_handler.setLevel(logging.INFO) # Log INFO level and above
    
    formatter = JSONFormatter()
    tracker_handler.setFormatter(formatter)
    
    tracker_logger = logging.getLogger('item_tracker')
    tracker_logger.setLevel(logging.INFO) # Set logger level
    tracker_logger.addHandler(tracker_handler)
    tracker_logger.propagate = False # Prevent logs from going to root logger/console

def setup_logging():
    """Initialize logging configuration"""
    # Create log directory if it doesn't exist
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Set to DEBUG to see all debug messages
    
    # Clear any existing handlers
    root_logger.handlers.clear()
    
    # Configure handlers
    setup_debug_logging(log_dir)
    setup_info_logging(log_dir)
    setup_error_logging(log_dir)
    setup_queue_logging(log_dir)
    setup_performance_logging(log_dir)
    setup_item_tracker_logging(log_dir)
    
    # Start performance monitoring after logging is set up
    start_performance_monitoring()

if __name__ == "__main__":
    setup_logging()
    # Example usage
    logging.debug("This is a debug message")
    logging.info("This is an info message")
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.info("This is a queue message")