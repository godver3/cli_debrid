import logging
from scraper.functions import *
from logging_config import *

def setup_scraper_logger():
    
    scraper_logger = logging.getLogger('scraper_logger')
    scraper_logger.addHandler(logging.NullHandler())
    scraper_logger.propagate = False
    
    return scraper_logger

scraper_logger = setup_scraper_logger()
