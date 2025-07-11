from .trakt_friends_routes import trakt_friends_bp
from flask import Blueprint, jsonify, redirect, url_for, render_template, g, request, abort, flash
from functools import wraps
import json
import os
import sys
from requests.exceptions import RequestException
from werkzeug.exceptions import TooManyRequests

from .models import admin_required, user_required
from .auth_routes import auth_bp
from .scraper_routes import scraper_bp
from .queues_routes import queues_bp
from .api_summary_routes import api_summary_bp, real_time_api_bp
from .onboarding_routes import onboarding_bp
from .user_management_routes import user_management_bp
from .database_routes import database_bp
from .statistics_routes import statistics_bp, root_bp
from .webhook_routes import webhook_bp
from .trakt_friends_routes import trakt_friends_bp
from .debug_routes import debug_bp
from .trakt_routes import trakt_bp
from .log_viewer_routes import logs_bp
from .settings_routes import settings_bp
from .program_operation_routes import program_operation_bp
from .video_routes import video_routes
from routes.api_tracker import is_rate_limited, get_blocked_domains, APIRateLimiter, api  # Add this import at the top of the file
from routes.extensions import app
from .content_requestor_routes import content_requestor_bp
from .base_routes import base_bp  # Add this import
from .library_management_routes import library_management
from .cors_testing_routes import cors_testing_bp
from .magnet_routes import magnet_bp
from .performance_routes import performance_bp
from .torrent_status_routes import torrent_status_bp
from .settings_validation_routes import settings_validation_bp
from .content_requestor_routes import content_requestor_bp
from .connections_routes import connections_bp
from .user_token_routes import user_token_bp

tooltip_bp = Blueprint('tooltip', __name__)

def get_tooltip_schema():
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundle, the PyInstaller bootloader
        # extends the sys module by a flag frozen=True and sets the app 
        # path into variable _MEIPASS'.
        application_path = sys._MEIPASS
    else:
        application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    tooltip_path = os.path.join(application_path, 'tooltip_schema.json')
    
    with open(tooltip_path, 'r') as f:
        return json.load(f)

@tooltip_bp.route('/tooltips')
def get_tooltips():
    tooltips = get_tooltip_schema()
    return jsonify(tooltips)

def is_api_request():
    return request.path.startswith('/api/') or request.accept_mimetypes.best in ['application/json', 'text/javascript']

def is_html_request():
    return 'text/html' in request.accept_mimetypes.values()

def rate_limit_check(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if is_rate_limited():
            g.is_rate_limited = True
            # Allow access to login page, over_usage page, and static files even when rate-limited
            if request.endpoint in ['auth.login', 'over_usage.index'] or request.path.startswith('/static/'):
                return f(*args, **kwargs)
            if is_html_request():
                return redirect(url_for('over_usage.index'))
            else:
                return jsonify(error="Rate limit exceeded", is_rate_limited=True), 429
        try:
            return f(*args, **kwargs)
        except RequestException as e:
            if "Rate limit exceeded" in str(e):
                g.is_rate_limited = True
                if is_html_request():
                    return redirect(url_for('over_usage.index'))
                else:
                    return jsonify(error="Rate limit exceeded", is_rate_limited=True), 429
            raise
    return decorated_function

over_usage_bp = Blueprint('over_usage', __name__)

@app.before_request
def check_rate_limit():
    g.is_rate_limited = is_rate_limited()
    
    # List of endpoints that should always be accessible
    allowed_endpoints = ['auth.login', 'over_usage.index', 'static', 'reset_rate_limits']
    
    if g.is_rate_limited and request.endpoint not in allowed_endpoints:
        if request.endpoint == 'reset_rate_limits':
            return  # Allow the reset_rate_limits route to proceed
        return redirect(url_for('over_usage.index'))

@app.errorhandler(429)
def handle_too_many_requests(e):
    return redirect(url_for('over_usage.index'))

# Remove the check_rate_limit_api route as it's no longer needed

@over_usage_bp.route('/')
def index():
    blocked_domains = get_blocked_domains()
    hourly_limit = 2000
    five_minute_limit = 1000
    return render_template('over_usage.html', blocked_domains=blocked_domains, is_rate_limited=True, hourly_limit=hourly_limit, five_minute_limit=five_minute_limit)

# Create a new Blueprint for admin routes
admin_bp = Blueprint('admin', __name__)

@app.route('/reset_rate_limits')
def reset_rate_limits():
    api.rate_limiter.reset_limits()
    flash('Rate limits have been reset successfully.', 'success')
    return redirect(url_for('root.root'))

def register_blueprints(app):
    blueprints = [
        (auth_bp, '/auth'),
        (scraper_bp, '/scraper'),
        (queues_bp, '/queues'),
        (api_summary_bp, '/api_call_summary'),
        (onboarding_bp, '/onboarding'),
        (user_management_bp, '/user_management'),
        (database_bp, '/database'),
        (statistics_bp, '/statistics'),
        (trakt_friends_bp, '/trakt_friends'),
        (webhook_bp, '/webhook'),
        (debug_bp, '/debug'),
        (trakt_bp, '/trakt'),
        (logs_bp, '/logs'),
        (settings_bp, '/settings'),
        (program_operation_bp, '/program_operation'),
        (real_time_api_bp, '/realtime_api_calls'),
        (tooltip_bp, '/tooltip'),
        (over_usage_bp, '/over_usage'),
        (video_routes, '/video'),
        (content_requestor_bp, '/content'),
        (base_bp, '/base'),
        (root_bp, '/'),
        (library_management, '/library_management'),
        (cors_testing_bp, '/cors_testing'),
        (magnet_bp, '/magnet'),
        (performance_bp, '/performance'),
        (torrent_status_bp, '/torrent_status'),
        (settings_validation_bp, '/settings_validation'),
        (connections_bp, '/connections'),
        (user_token_bp, '/user_token')
    ]
    
    for blueprint, url_prefix in blueprints:
        app.register_blueprint(blueprint, url_prefix=url_prefix)

    # Remove rate_limit_check decorator from all routes
    # for endpoint, view_func in app.view_functions.items():
    #     if not endpoint.startswith('over_usage.'):
    #         app.view_functions[endpoint] = rate_limit_check(view_func)

__all__ = ['register_blueprints', 'admin_required', 'user_required', 'auth_bp', 'statistics_bp', 'rate_limit_check']
