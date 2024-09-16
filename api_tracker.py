import requests
import logging
from functools import wraps
from urllib.parse import urlparse, parse_qs
import time
from collections import defaultdict
from flask import current_app
from requests.exceptions import RequestException

def setup_api_logging():
    print("Setting up API logging")
    # Setup logging for API calls
    global api_logger
    api_logger = logging.getLogger('api_calls')
    api_logger.setLevel(logging.INFO)
    api_logger.propagate = False  # Prevent propagation to root logger
    handler = logging.FileHandler('user/logs/api_calls.log')
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

class APIRateLimiter:
    def __init__(self):
        self.hourly_limit = 2000
        self.five_minute_limit = 1000
        self.hourly_calls = defaultdict(list)
        self.five_minute_calls = defaultdict(list)
        self.blocked_domains = set()

    def check_limits(self, domain):
        current_time = time.time()
        hour_ago = current_time - 3600
        five_minutes_ago = current_time - 300

        self.hourly_calls[domain] = [t for t in self.hourly_calls[domain] if t > hour_ago]
        self.five_minute_calls[domain] = [t for t in self.five_minute_calls[domain] if t > five_minutes_ago]

        if len(self.hourly_calls[domain]) >= self.hourly_limit or len(self.five_minute_calls[domain]) >= self.five_minute_limit:
            self.blocked_domains.add(domain)
            self.stop_program()
            return False

        self.hourly_calls[domain].append(current_time)
        self.five_minute_calls[domain].append(current_time)
        return True

    def stop_program(self):
        try:
            with current_app.app_context():
                from routes.program_operation_routes import stop_program
                stop_program()
            api_logger.warning("Program stopped due to rate limiting.")
        except Exception as e:
            api_logger.error(f"Failed to stop program: {str(e)}")

    def reset_limits(self):
        self.hourly_calls.clear()
        self.five_minute_calls.clear()
        self.blocked_domains.clear()
        api_logger.info("Rate limits have been manually reset.")

class APITracker:
    def __init__(self):
        self.session = requests.Session()
        self.cookies = requests.cookies
        self.exceptions = requests.exceptions
        self.utils = requests.utils
        self.Session = requests.Session
        self.current_url = None
        self._args = None
        self.rate_limiter = APIRateLimiter()
        self.monitored_domains = {'api.real-debrid.com', 'api.trakt.tv', 'torrentio.strem.fun'}

    @property
    def args(self):
        if self._args is None:
            self._args = Args(self.get_query_params())
        return self._args

    @log_api_call
    def get(self, url, **kwargs):
        domain = urlparse(url).netloc
        if domain in self.monitored_domains:
            if not self.rate_limiter.check_limits(domain):
                api_logger.error(f"Rate limit exceeded for {domain}. Blocking further requests.")
                raise RequestException(f"Rate limit exceeded for {domain}")
        
        try:
            api_logger.debug(f"Attempting GET request to: {url}")
            self.current_url = url  # Store the current URL
            self._args = None  # Reset args
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            api_logger.debug(f"Successful GET request to: {url}. Status code: {response.status_code}")
            return response
        except RequestException as e:
            api_logger.error(f"Error in GET request to {url}: {str(e)}")
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

def is_rate_limited():
    return bool(api.rate_limiter.blocked_domains)

def get_blocked_domains():
    return list(api.rate_limiter.blocked_domains)