from flask import redirect, url_for
from functools import wraps
from flask_login import current_user, login_required
from .utils import is_user_system_enabled
import logging

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
            logging.debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            logging.debug("User not authenticated, redirecting to login")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def onboarding_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            logging.debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            logging.debug("User not authenticated, redirecting to login")
            return redirect(url_for('auth.login'))
        if not current_user.onboarding_complete:
            logging.debug("Onboarding not complete, redirecting to onboarding")
            return redirect(url_for('onboarding.onboarding_step', step=1))
        return f(*args, **kwargs)
    return decorated_function