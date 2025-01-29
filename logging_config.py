import logging
import logging.handlers
from settings import get_setting
import psutil
import os
import time
import threading
import cProfile
import pstats
import io
from performance_monitor import monitor

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

def log_system_stats():
    """Start the performance monitoring system"""
    monitor.start_monitoring()

def setup_logging():
    # Get log directory from environment variable with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')

    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(filename)s:%(funcName)s:%(lineno)d - %(levelname)s - %(message)s')
    
    # Set up root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Set logging level for selector module
    logging.getLogger('selector').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    # Remove any existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler
    console_handler = DynamicConsoleHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Debug file handler
    debug_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, 'debug.log'), maxBytes=50*1024*1024, backupCount=3, encoding='utf-8', errors='replace')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter)
    root_logger.addHandler(debug_handler)
    
    # Info file handler
    info_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, 'info.log'), maxBytes=50*1024*1024, backupCount=3, encoding='utf-8', errors='replace')
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    root_logger.addHandler(info_handler)
    
    # Queue file handler (overwriting on each log)
    queue_handler = OverwriteFileHandler(os.path.join(log_dir, 'queue.log'), encoding='utf-8')
    queue_handler.setLevel(logging.INFO)
    queue_handler.setFormatter(formatter)
    
    # Create a separate logger for queue logs
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.setLevel(logging.INFO)
    queue_logger.addHandler(queue_handler)
    queue_logger.propagate = False  # Prevent queue logs from propagating to root logger
    
    # Raise logging level for urllib3 to reduce noise
    logging.getLogger('urllib3').setLevel(logging.INFO)

    # Create a filter to exclude logs from specific files
    class ExcludeFilter(logging.Filter):
        def filter(self, record):
            return not (record.filename == 'rules.py' or record.filename == 'rebulk.py' or record.filename == 'processors.py')

    # Apply the filter to all handlers
    for handler in root_logger.handlers:
        handler.addFilter(ExcludeFilter())

    # Apply the filter to all existing loggers
    for name in logging.root.manager.loggerDict:
        logger = logging.getLogger(name)
        logger.addFilter(ExcludeFilter())

    # Add the filter to the root logger
    root_logger.addFilter(ExcludeFilter())

    # Performance file handler
    performance_formatter = logging.Formatter('%(message)s')
    performance_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, 'performance.log'), maxBytes=50*1024*1024, backupCount=0, encoding='utf-8', errors='replace')
    performance_handler.setLevel(logging.INFO)
    performance_handler.setFormatter(performance_formatter)
    
    # Create a separate logger for performance logs
    performance_logger = logging.getLogger('performance_logger')
    performance_logger.setLevel(logging.INFO)
    performance_logger.addHandler(performance_handler)
    performance_logger.propagate = False  # Prevent performance logs from propagating to root logger

    # Start the system stats logging thread
    stats_thread = threading.Thread(target=log_system_stats, daemon=True)
    stats_thread.start()

    # Start global profiling
    start_global_profiling()

if __name__ == "__main__":
    setup_logging()
    # Example usage
    logging.debug("This is a debug message")
    logging.info("This is an info message")
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.info("This is a queue message")