import logging
import os
from pathlib import Path
from scraper.functions import *

def setup_scraper_logger():
    # Use environment variable for log directory with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_dir = Path(log_dir)
    
    # Create log directory if it doesn't exist
    log_dir.mkdir(parents=True, exist_ok=True)
    
    scraper_logger = logging.getLogger('scraper_logger')
    scraper_logger.setLevel(logging.DEBUG)
    scraper_logger.propagate = False  # Prevent propagation to the root logger
    
    # Remove all existing handlers
    for handler in scraper_logger.handlers[:]:
        scraper_logger.removeHandler(handler)
    
    log_file = log_dir / 'scraper.log'
    file_handler = logging.FileHandler(str(log_file))
    file_handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    scraper_logger.addHandler(file_handler)
    
    return scraper_logger

scraper_logger = setup_scraper_logger()

def log_filter_result(title: str, resolution: str, filter_reason: str = None):
    if filter_reason:
        logging.debug(f"Release: '{title}' (Resolution: {resolution}) - Filtered out: {filter_reason}")
    else:
        logging.debug(f"Release: '{title}' (Resolution: {resolution}) - Passed filters")