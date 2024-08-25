import requests
import logging
from functools import wraps
from urllib.parse import urlparse, parse_qs

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

class Args:
    def __init__(self, query_params):
        self._params = query_params

    def get(self, key, default=None, type=None):
        value = self._params.get(key, [default])
        if value == [default]:
            return default
        
        value = value[0]
        
        if type is not None:
            try:
                return type(value)
            except ValueError:
                return default
        
        return value

class APITracker:
    def __init__(self):
        self.session = requests.Session()
        self.cookies = requests.cookies
        self.exceptions = requests.exceptions
        self.utils = requests.utils
        self.Session = requests.Session
        self.current_url = None
        self._args = None

    @property
    def args(self):
        if self._args is None:
            self._args = Args(self.get_query_params())
        return self._args

    @log_api_call
    def get(self, url, **kwargs):
        try:
            logging.debug(f"Attempting GET request to: {url}")
            self.current_url = url  # Store the current URL
            self._args = None  # Reset args
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

    def get_query_params(self):
        if self.current_url:
            parsed_url = urlparse(self.current_url)
            return parse_qs(parsed_url.query)
        return {}

api = APITracker()