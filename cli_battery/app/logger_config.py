import logging
import colorlog
from logging.handlers import RotatingFileHandler
import os

class ImmediateRotatingFileHandler(RotatingFileHandler):
    """A RotatingFileHandler that flushes immediately after each write"""
    def emit(self, record):
        super().emit(record)
        self.flush()  # Force immediate flush

# Create a filter to exclude logs from specific files
class ExcludeFilter(logging.Filter):
    def filter(self, record):
        return not (record.filename == 'rules.py' or record.filename == 'rebulk.py' or record.filename == 'processors.py')

def setup_logger():
    # Get log directory from environment variable with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    os.makedirs(log_dir, exist_ok=True)

    # Create logger
    logger = colorlog.getLogger('cli_battery')
    logger.setLevel(logging.DEBUG)  # Ensure logger itself allows DEBUG

    # Clear any existing handlers
    logger.handlers.clear()

    # Create console handler with color formatting
    console_handler = colorlog.StreamHandler()
    console_handler.setLevel(logging.INFO)  # Keep INFO for console
    
    formatter = colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s - %(filename)s:%(funcName)s - %(levelname)s - %(message)s',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )

    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Add file handler with immediate flushing for debug logs only
    log_file = os.path.join(log_dir, 'battery_debug.log')
    file_handler = ImmediateRotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB - reduced from 50MB
        backupCount=2,  # Keep 2 backup files for important history
        encoding='utf-8',
        errors='replace'
    )
    file_handler.setLevel(logging.DEBUG)
    
    # Add filters to exclude unwanted messages
    file_handler.addFilter(lambda record: not record.name.startswith(('urllib3', 'requests', 'charset_normalizer')))
    file_handler.addFilter(ExcludeFilter())
    
    # Use a simpler formatter for file logs to reduce overhead
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    logger.addHandler(file_handler)

    # Prevent propagation to avoid duplicate logs
    logger.propagate = False

    # Configure root logger to allow DEBUG
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    return logger

# Create and configure the logger
logger = setup_logger()