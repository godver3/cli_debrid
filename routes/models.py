from flask import jsonify, redirect, url_for, request
from functools import wraps, lru_cache
from flask_login import current_user
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

def _check_api_token():
    """Return True if a valid api_token is present in ?token= or Authorization header."""
    from routes.auth_routes import User
    token = request.args.get('token', '') or ''
    if not token:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
    if not token:
        return False
    return User.query.filter_by(api_token=token).first() is not None

# Requests that need no login when the user system is on: the login flow, static
# assets, and integrations that bring their own credentials (Overseerr/rclone/Plex
# webhooks, and Bazarr's Sonarr/Radarr emulation, which checks its own API key).
_PUBLIC_ENDPOINTS = frozenset({
    'static', 'favicon', 'dynamic_favicon', 'manifest', 'redirect_placeholder',
    'auth.login', 'auth.unauthorized', 'oidc.login', 'oidc.callback',
    'over_usage.index', 'tooltip.get_tooltips',
    # The built-in mount engine's callbacks (debrid link, repair check): they
    # check the engine's own token and localhost (_engine_caller_ok), and the
    # engine has no login session, so the login gate made every debrid read
    # fail with 401 whenever the user system was on.
    'mount.internal_link', 'mount.internal_check',
    # MCP clients have no session; the endpoint checks its own token
    # (routes/mcp_routes.py) whether or not the user system is on.
    'mcp.endpoint',
})
_PUBLIC_BLUEPRINTS = frozenset({'webhook'})
_BAZARR_PATHS = ('/api/v3/', '/signalr')


def _is_public_request():
    if request.endpoint in _PUBLIC_ENDPOINTS or request.blueprint in _PUBLIC_BLUEPRINTS:
        return True
    return request.path == '/api' or request.path.startswith(_BAZARR_PATHS)


def _wants_html():
    """True when the client named text/html and prefers it over JSON.

    Don't use accept_mimetypes.best: werkzeug ranks entries with parameters
    first, so Chrome's "...,application/signed-exchange;v=b3;q=0.7" header
    makes best == 'application/signed-exchange' and a page load got the JSON
    401 instead of the login redirect. A bare */* (fetch's default) has no
    explicit text/html, so API callers still get the 401.
    """
    accept = request.accept_mimetypes
    return ('text/html' in accept.values()
            and accept.best_match(['text/html', 'application/json']) == 'text/html')


def require_login():
    """App-wide before_request: with the user system on, every route needs a
    logged-in session or an API token unless it is listed as public above.

    Routes still add their own decorators for role checks (admin_required etc.);
    this only makes a route that forgot one fail closed instead of open.
    """
    if request.endpoint is None or _is_public_request():
        return None  # unknown URL (Flask answers 404/405) or public
    if not is_user_system_enabled():
        return None
    if current_user.is_authenticated or _check_api_token():
        return None
    if request.method == 'GET' and _wants_html():
        return redirect(url_for('auth.login'))
    return jsonify({'error': 'Authentication required'}), 401


def user_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            _rate_limited_log_debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if current_user.is_authenticated:
            return f(*args, **kwargs)
        # Allow API token auth for non-browser clients (homepage widgets, OpenClaw, etc.)
        if _check_api_token():
            return f(*args, **kwargs)
        _rate_limited_log_debug("User not authenticated, redirecting to login")
        return redirect(url_for('auth.login'))
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
            _rate_limited_log_debug("User onboarding not complete, redirecting to onboarding")
            from routes.onboarding_routes import get_next_onboarding_step
            return redirect(url_for('onboarding.onboarding_step', step=get_next_onboarding_step()))
        return f(*args, **kwargs)
    return decorated_function

def scraper_permission_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            _rate_limited_log_debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            _rate_limited_log_debug("User not authenticated, redirecting to login")
            return redirect(url_for('auth.login'))
        if current_user.role == 'requester':
            _rate_limited_log_debug("User is a requester, not allowed to use scraper")
            return redirect(url_for('auth.unauthorized'))
        return f(*args, **kwargs)
    return decorated_function

def scraper_view_access_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            _rate_limited_log_debug("User system disabled, allowing access")
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            _rate_limited_log_debug("User not authenticated, redirecting to login")
            return redirect(url_for('auth.login'))
        # Allow all authenticated users, including requesters, to view
        return f(*args, **kwargs)
    return decorated_function