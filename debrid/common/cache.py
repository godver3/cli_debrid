from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable
import logging

def timed_lru_cache(seconds: int, maxsize: int = 128):
    """
    Decorator that provides a timed LRU cache.
    Cache entries expire after the specified number of seconds.
    """
    def wrapper_cache(func: Callable) -> Callable:
        cache = {}
        expiration_times = {}
        lifetime = timedelta(seconds=seconds)

        @wraps(func)
        def wrapped_func(*args, **kwargs) -> Any:
            key = str(args) + str(kwargs)
            now = datetime.utcnow()

            # Check if entry exists and is still valid
            if key in cache:
                time_until_expiry = expiration_times[key] - now
                if now < expiration_times[key]:
                    #logging.debug(f"Cache hit for {func.__name__} with key {key}. Expires in {time_until_expiry.total_seconds():.1f} seconds")
                    return cache[key]

            result = func(*args, **kwargs)
            cache[key] = result
            expiration_times[key] = now + lifetime
            #logging.debug(f"Cached new value for {func.__name__} with key {key}. Will expire in {seconds} seconds")

            # Implement LRU by removing oldest entries if we exceed maxsize
            if len(cache) > maxsize:
                oldest_key = min(expiration_times, key=expiration_times.get)
                #logging.debug(f"Cache size exceeded {maxsize}. Removing oldest entry with key {oldest_key}")
                del cache[oldest_key]
                del expiration_times[oldest_key]

            return result

        return wrapped_func

    return wrapper_cache
