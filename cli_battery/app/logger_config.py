import logging
import colorlog
from logging.handlers import RotatingFileHandler
import os

def setup_logger():
    # Get log directory from environment variable with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    os.makedirs(log_dir, exist_ok=True)

    # Create logger
    logger = colorlog.getLogger('app')
    logger.setLevel(logging.DEBUG)

    # Clear any existing handlers
    logger.handlers.clear()

    # Create console handler with color formatting
    console_handler = colorlog.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

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

    # Add file handler
    log_file = os.path.join(log_dir, 'battery_debug.log')
    file_handler = RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=10)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(filename)s:%(funcName)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

    return logger

# Create and configure the logger
logger = setup_logger()