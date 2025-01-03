import time
from functools import wraps
from typing import Callable

class RateLimiter:
    """Rate limiter for API calls"""
    def __init__(self, calls_per_second: float = 1):
        self.calls_per_second = calls_per_second
        self.last_call = 0

    def wait(self):
        current_time = time.time()
        time_since_last_call = current_time - self.last_call
        if time_since_last_call < 1 / self.calls_per_second:
            time.sleep((1 / self.calls_per_second) - time_since_last_call)
        self.last_call = time.time()

def rate_limited_request(func: Callable) -> Callable:
    """Decorator to rate limit API requests"""
    rate_limiter = RateLimiter(calls_per_second=0.5)
    
    @wraps(func)
    def wrapper(*args, **kwargs):
        rate_limiter.wait()
        return func(*args, **kwargs)
    return wrapper
