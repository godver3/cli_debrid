import logging
from collections import deque
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

log_messages = deque(maxlen=5)  # Store last 28 log messages

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
logger.addHandler(custom_handler)

# File handler for date/time stamped logs
log_directory = 'logs'
if not os.path.exists(log_directory):
    os.makedirs(log_directory)

def create_rotating_file_handler():
    current_time = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    filename = os.path.join(log_directory, f'program_{current_time}.log')

    file_handler = RotatingFileHandler(
        filename=filename,
        maxBytes=100 * 1024 * 1024,  # 100 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    return file_handler

file_handler = create_rotating_file_handler()
logger.addHandler(file_handler)

logger.setLevel(logging.INFO)

def get_logger():
    return logger

def get_log_messages():
    try:
        messages = list(log_messages)
        #logger.debug(f"Retrieved {len(messages)} log messages from deque")
        return messages
    except Exception as e:
        #logger.error(f"Error in get_log_messages: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def delete_oldest_files(log_directory, num_files_to_keep):
    log_files = sorted(
        [f for f in os.listdir(log_directory) if f.startswith('program_') and f.endswith('.log')],
        key=lambda x: os.path.getmtime(os.path.join(log_directory, x))
    )
    if len(log_files) > num_files_to_keep:
        for file in log_files[:-num_files_to_keep]:
            os.remove(os.path.join(log_directory, file))
    logger.info("Deleted oldest logs.")
# Function to switch to a new file if current file exceeds 100MB
def rotate_file_if_needed():
    if file_handler.stream.tell() > 100 * 1024 * 1024:  # If file size > 100MB
        logger.removeHandler(file_handler)
        file_handler.close()
        new_file_handler = create_rotating_file_handler()
        logger.addHandler(new_file_handler)
        delete_oldest_files(log_directory, 5)
