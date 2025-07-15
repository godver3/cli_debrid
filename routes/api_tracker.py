import requests
import logging
import logging.handlers
from functools import wraps
from urllib.parse import urlparse, parse_qs
import time
from collections import defaultdict
from flask import current_app, g
from requests.exceptions import RequestException
import os

def setup_api_logging():
    print("Setting up API logging")
    global api_logger
    api_logger = logging.getLogger('api_calls')
    api_logger.setLevel(logging.INFO)
    api_logger.propagate = False  # Prevent propagation to root logger
    
    # Use the environment variable, with a fallback to the Unix-style path
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_path = os.path.join(log_dir, 'api_calls.log')
    
    # Setup rotating file handler with compression
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10*1024*1024,  # 10MB per file
        backupCount=5,  # Keep 5 backup files
        encoding='utf-8',
        errors='replace'
    )
    
    # Use a more compact log format
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', 
                                datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    
    # Compress old log files on rotation
    def namer(name):
        return name + ".gz"
        
    def rotator(source, dest):
        import gzip
        with open(source, 'rb') as f_in:
            with gzip.open(dest, 'wb') as f_out:
                f_out.writelines(f_in)
        os.remove(source)
        
    handler.rotator = rotator
    handler.namer = namer
    
    api_logger.addHandler(handler)

def log_api_call(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            url = args[0] if args else kwargs.get('url')
            if not isinstance(url, str):
                return func(self, *args, **kwargs)
                
            method = func.__name__.upper()
            parsed = urlparse(url)
            # Only log domain and path, skip query parameters
            domain = parsed.netloc
            path = parsed.path
            api_logger.info(f"{method} {domain}{path}")
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
        
        # Clean up old calls
        self.hourly_calls[domain] = [t for t in self.hourly_calls[domain] if current_time - t < 3600]
        self.five_minute_calls[domain] = [t for t in self.five_minute_calls[domain] if current_time - t < 300]
        
        # Add current call
        self.hourly_calls[domain].append(current_time)
        self.five_minute_calls[domain].append(current_time)
        
        # Track if limits are exceeded but don't enforce
        hourly_exceeded = len(self.hourly_calls[domain]) > self.hourly_limit
        five_min_exceeded = len(self.five_minute_calls[domain]) > self.five_minute_limit
        
        # Only try to set g.rate_limit_warning if we're in a request context
        try:
            if hourly_exceeded or five_min_exceeded:
                g.rate_limit_warning = True
            else:
                g.rate_limit_warning = False
        except RuntimeError:
            # We're outside of request context, just continue
            pass
            
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
            self.rate_limiter.check_limits(domain)
        
        try:
            self.current_url = url
            self._args = None
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            return response
        except RequestException as e:
            api_logger.error(f"Error: {domain} - {str(e)}")
            raise

    @log_api_call
    def post(self, url, **kwargs):
        domain = urlparse(url).netloc
        if domain in self.monitored_domains:
            self.rate_limiter.check_limits(domain)
        
        try:
            self.current_url = url
            self._args = None
            response = self.session.post(url, **kwargs)
            response.raise_for_status()
            return response
        except RequestException as e:
            api_logger.error(f"Error: {domain} - {str(e)}")
            raise

    @log_api_call
    def put(self, url, **kwargs):
        domain = urlparse(url).netloc
        if domain in self.monitored_domains:
            self.rate_limiter.check_limits(domain)
        
        try:
            self.current_url = url
            self._args = None
            response = self.session.put(url, **kwargs)
            response.raise_for_status()
            return response
        except RequestException as e:
            api_logger.error(f"Error: {domain} - {str(e)}")
            raise

    @log_api_call
    def delete(self, url, **kwargs):
        domain = urlparse(url).netloc
        if domain in self.monitored_domains:
            self.rate_limiter.check_limits(domain)
        
        try:
            self.current_url = url
            self._args = None
            response = self.session.delete(url, **kwargs)
            response.raise_for_status()
            return response
        except RequestException as e:
            api_logger.error(f"Error: {domain} - {str(e)}")
            raise

    # Add other HTTP methods as needed

    def get_query_params(self):
        if self.current_url:
            parsed_url = urlparse(self.current_url)
            return parse_qs(parsed_url.query)
        return {}

api = APITracker()

def is_rate_limited():
    try:
        return getattr(g, 'rate_limit_warning', False)
    except RuntimeError:
        # We're outside of request context
        return False

def get_blocked_domains():
    return []  # No domains are blocked anymore since we're not enforcing limits