import logging
from collections import deque
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from settings import get_setting

# Load settings
use_single_log_file = get_setting('Logging', 'use_single_log_file', 'False').lower() == 'true'
logging_level = get_setting('Logging', 'logging_level', 'INFO').upper()
log_messages = deque(maxlen=28)  # Store last 28 log messages

class CustomHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        log_messages.append(log_entry)

# Set up logger
logger = logging.getLogger('shared_logger')
logger.propagate = False

# Custom handler for in-memory logging
custom_handler = CustomHandler()
custom_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Stream handler for console output
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Directory for log files
log_directory = 'logs'
if not os.path.exists(log_directory):
    os.makedirs(log_directory)

def create_rotating_file_handler(level, filename):
    file_handler = RotatingFileHandler(
        filename=filename,
        maxBytes=100 * 1024 * 1024,  # 100 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    return file_handler

# Create separate handlers for DEBUG and INFO levels
debug_log_file = os.path.join(log_directory, 'debug.log')
info_log_file = os.path.join(log_directory, 'info.log')

debug_handler = create_rotating_file_handler(logging.DEBUG, debug_log_file)
info_handler = create_rotating_file_handler(logging.INFO, info_log_file)

logger.addHandler(debug_handler)
logger.addHandler(info_handler)
logger.addHandler(stream_handler)  # Always add stream handler

# Set logging level for the console based on settings
try:
    stream_handler.setLevel(logging_level)
    logger.setLevel(logging.DEBUG)  # Set to DEBUG to ensure all levels are passed to handlers
except ValueError:
    stream_handler.setLevel(logging.INFO)
    logger.setLevel(logging.DEBUG)
    logger.error(f"Invalid logging level '{logging_level}', defaulting to INFO")

def get_logger():
    return logger

def get_log_messages():
    try:
        return list(log_messages)
    except Exception as e:
        logger.error(e, exc_info=True)
        return []

# Function to engage the custom handler
def engage_custom_handler():
    if stream_handler in logger.handlers:
        logger.removeHandler(stream_handler)
    logger.addHandler(custom_handler)
    logger.info("Custom handler engaged.")

# Function to disengage the custom handler
def disengage_custom_handler():
    if custom_handler in logger.handlers:
        logger.removeHandler(custom_handler)
    logger.addHandler(stream_handler)
    logger.info("Custom handler disengaged.")

# Function to remove console stream handler
def remove_console_handler():
    if stream_handler in logger.handlers:
        logger.removeHandler(stream_handler)

# Function to add console stream handler
def add_console_handler():
    if stream_handler not in logger.handlers:
        logger.addHandler(stream_handler)
