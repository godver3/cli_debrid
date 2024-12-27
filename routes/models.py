from flask import redirect, url_for
from functools import wraps, lru_cache
from flask_login import current_user, login_required
from .utils import is_user_system_enabled
import logging
import time

# Cache the logging calls for 5 minutes
@lru_cache(maxsize=128)
def _cached_log_debug(message, timestamp):
    # timestamp is used to ensure we get a new cache entry every 5 minutes
    logging.debug(message)

def _rate_limited_log_debug(message):
    # Create a new timestamp every 5 minutes
    timestamp = int(time.time() / 300)  # 300 seconds = 5 minutes
    _cached_log_debug(message, timestamp)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if not current_user.is_authenticated or current_user.role != 'admin':
            return redirect(url_for('auth.unauthorized'))
        return f(*args, **kwargs)
    return decorated_function

def user_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            _rate_limited_log_debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            _rate_limited_log_debug("User not authenticated, redirecting to login")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def onboarding_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            _rate_limited_log_debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            _rate_limited_log_debug("User not authenticated, redirecting to login")
            return redirect(url_for('auth.login'))
        if not current_user.onboarding_complete:
            _rate_limited_log_debug("Onboarding not complete, redirecting to onboarding")
            return redirect(url_for('onboarding.onboarding_step', step=1))
        return f(*args, **kwargs)
    return decorated_function