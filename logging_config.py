import logging
import logging.handlers
from settings import get_setting

class OverwriteFileHandler(logging.FileHandler):
    def emit(self, record):
        # Open the file in write mode ('w') for each emission
        self.baseFilename = self.baseFilename
        with open(self.baseFilename, 'w') as f:
            f.write(self.format(record) + self.terminator)

def setup_logging():
    # Get logging level from settings
    console_level = get_setting("Logging", "logging_level", "INFO")
    
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(filename)s:%(funcName)s:%(lineno)d - %(levelname)s - %(message)s')
    
    # Set up root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Remove any existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, console_level.upper()))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Debug file handler
    debug_handler = logging.handlers.RotatingFileHandler(
        'logs/debug.log', maxBytes=100*1024*1024, backupCount=5)
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter)
    root_logger.addHandler(debug_handler)
    
    # Info file handler
    info_handler = logging.handlers.RotatingFileHandler(
        'logs/info.log', maxBytes=100*1024*1024, backupCount=5)
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    root_logger.addHandler(info_handler)
    
    # Queue file handler (overwriting on each log)
    queue_handler = OverwriteFileHandler('logs/queue.log')
    queue_handler.setLevel(logging.INFO)
    queue_handler.setFormatter(formatter)
    
    # Create a separate logger for queue logs
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.setLevel(logging.INFO)
    queue_logger.addHandler(queue_handler)
    queue_logger.propagate = False  # Prevent queue logs from propagating to root logger
    
    # Raise logging level for urllib3 to reduce noise
    logging.getLogger('urllib3').setLevel(logging.INFO)

if __name__ == "__main__":
    setup_logging()
    # Example usage
    logging.debug("This is a debug message")
    logging.info("This is an info message")
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.info("This is a queue message")
