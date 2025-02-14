from flask import redirect, url_for, request
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

def _safe_redirect(endpoint, **kwargs):
    """Helper function to prevent redirect to the same URL"""
    target_url = url_for(endpoint, **kwargs)
    if target_url == request.url:
        logging.warning(f"Prevented self-redirect loop to {target_url}")
        return None
    return redirect(target_url)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if not current_user.is_authenticated or current_user.role != 'admin':
            redirect_response = _safe_redirect('auth.unauthorized')
            if redirect_response is None:
                return "Unauthorized", 403
            return redirect_response
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
            redirect_response = _safe_redirect('auth.login')
            if redirect_response is None:
                return "Please log in", 401
            return redirect_response
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
            redirect_response = _safe_redirect('auth.login')
            if redirect_response is None:
                return "Please log in", 401
            return redirect_response
        if not current_user.onboarding_complete:
            _rate_limited_log_debug("Onboarding not complete, redirecting to onboarding")
            redirect_response = _safe_redirect('onboarding.onboarding_step', step=1)
            if redirect_response is None:
                return "Onboarding required", 403
            return redirect_response
        return f(*args, **kwargs)
    return decorated_function