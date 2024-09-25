import logging
from logging.handlers import RotatingFileHandler
import os

def setup_logger():
    log_dir = '/user/logs'
    os.makedirs(log_dir, exist_ok=True)

    # Create a logger
    logger = logging.getLogger('app')
    logger.setLevel(logging.DEBUG)

    # Clear any existing handlers
    logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(filename)s:%(funcName)s - %(levelname)s - %(message)s')

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Add file handler
    file_handler = RotatingFileHandler('/user/logs/battery_debug.log', maxBytes=1024 * 1024, backupCount=10)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

# Create and configure the logger
logger = setup_logger()