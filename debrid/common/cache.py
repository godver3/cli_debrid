from datetime import datetime, timedelta
from functools import wraps, lru_cache
from typing import Any, Callable

def timed_lru_cache(seconds: int, maxsize: int = 128):
    """
    Decorator that provides a timed LRU cache.
    Cache entries expire after the specified number of seconds.
    """
    def wrapper_cache(func: Callable) -> Callable:
        func = lru_cache(maxsize=maxsize)(func)
        func.lifetime = timedelta(seconds=seconds)
        func.expiration = datetime.utcnow() + func.lifetime

        @wraps(func)
        def wrapped_func(*args, **kwargs) -> Any:
            if datetime.utcnow() >= func.expiration:
                func.cache_clear()
                func.expiration = datetime.utcnow() + func.lifetime

            return func(*args, **kwargs)

        return wrapped_func

    return wrapper_cache
