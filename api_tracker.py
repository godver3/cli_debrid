import requests
import logging
from functools import wraps
from urllib.parse import urlparse

# Setup logging for API calls
api_logger = logging.getLogger('api_calls')
api_logger.setLevel(logging.INFO)
handler = logging.FileHandler('logs/api_calls.log')
formatter = logging.Formatter('%(asctime)s - %(message)s')
handler.setFormatter(formatter)
api_logger.addHandler(handler)

def log_api_call(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            url = args[0] if args else kwargs.get('url')
            method = func.__name__.upper()
            domain = urlparse(url).netloc if isinstance(url, str) else 'Unknown'
            api_logger.info(f"API Call: {method} {url} - Domain: {domain}")
        except Exception as e:
            api_logger.error(f"Error in log_api_call: {str(e)}")
        return func(self, *args, **kwargs)
    return wrapper

class APITracker:
    def __init__(self):
        self.session = requests.Session()
        self.cookies = requests.cookies
        self.exceptions = requests.exceptions
        self.utils = requests.utils
        self.Session = requests.Session

    @log_api_call
    def get(self, url, **kwargs):
        try:
            logging.debug(f"Attempting GET request to: {url}")
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            logging.debug(f"Successful GET request to: {url}. Status code: {response.status_code}")
            return response
        except requests.exceptions.RequestException as e:
            logging.error(f"Error in GET request to {url}: {str(e)}")
            raise  # Re-raise the exception after logging

    @log_api_call
    def post(self, *args, **kwargs):
        return requests.post(*args, **kwargs)

    @log_api_call
    def put(self, *args, **kwargs):
        return requests.put(*args, **kwargs)

    @log_api_call
    def delete(self, *args, **kwargs):
        return requests.delete(*args, **kwargs)

    # Add other HTTP methods as needed

api = APITracker()