import logging
import logging.handlers
from settings import get_setting

class OverwriteFileHandler(logging.FileHandler):
    def emit(self, record):
        # Open the file in write mode ('w') for each emission
        self.baseFilename = self.baseFilename
        with open(self.baseFilename, 'w') as f:
            f.write(self.format(record) + self.terminator)

class DynamicConsoleHandler(logging.StreamHandler):
    def __init__(self):
        super().__init__()
        self.setLevel(self.get_level())

    def get_level(self):
        console_level = get_setting("Debug", "logging_level", "INFO")
        return getattr(logging, console_level.upper())

    def emit(self, record):
        self.setLevel(self.get_level())
        super().emit(record)

def setup_logging():
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

if __name__ == "__main__":
    setup_logging()
    # Example usage
    logging.debug("This is a debug message")
    logging.info("This is an info message")
    queue_logger = logging.getLogger('queue_logger')
    queue_logger.info("This is a queue message")
