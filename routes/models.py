from flask import redirect, url_for
from functools import wraps
from flask_login import current_user

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from routes.settings_routes import is_user_system_enabled
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if not current_user.is_authenticated or current_user.role != 'admin':
            return redirect(url_for('auth.login'))  # Changed 'login' to 'auth.login'
        return f(*args, **kwargs)
    return decorated_function

def user_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from routes.settings_routes import is_user_system_enabled
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))  # Changed 'login' to 'auth.login'
        return f(*args, **kwargs)
    return decorated_function

def onboarding_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from routes.settings_routes import is_user_system_enabled
        from routes.onboarding_routes import get_next_onboarding_step
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if current_user.is_authenticated and not current_user.onboarding_complete:
            next_step = get_next_onboarding_step()
            if next_step <= 5:  # Assuming 5 is the last step
                return redirect(url_for('onboarding.step', step=next_step))  # Changed 'onboarding_step' to 'onboarding.step'
        return f(*args, **kwargs)
    return decorated_function