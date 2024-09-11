from flask import Blueprint, jsonify, redirect, url_for, render_template, g, request, abort, flash
from functools import wraps
import json
import os
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
from .statistics_routes import statistics_bp
from .webhook_routes import webhook_bp
from .debug_routes import debug_bp
from .trakt_routes import trakt_bp
from .log_viewer_routes import logs_bp
from .settings_routes import settings_bp
from .program_operation_routes import program_operation_bp
from api_tracker import is_rate_limited, get_blocked_domains, APIRateLimiter, api  # Add this import at the top of the file
from extensions import app
from flask_login import current_user

tooltip_bp = Blueprint('tooltip', __name__)

@tooltip_bp.route('/tooltips')
def get_tooltips():
    tooltip_path = os.path.join(os.path.dirname(__file__), '..', 'tooltip_schema.json')
    with open(tooltip_path, 'r') as f:
        tooltips = json.load(f)
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
    hourly_limit = 1000
    five_minute_limit = 200
    return render_template('over_usage.html', blocked_domains=blocked_domains, is_rate_limited=True, hourly_limit=hourly_limit, five_minute_limit=five_minute_limit)

# Create a new Blueprint for admin routes
admin_bp = Blueprint('admin', __name__)

@app.route('/reset_rate_limits')
def reset_rate_limits():
    api.rate_limiter.reset_limits()
    flash('Rate limits have been reset successfully.', 'success')
    return redirect(url_for('statistics.index'))

@app.context_processor
def inject_menu_items():
    menu_items = [
        ('statistics.index', 'Home'),
        ('queues.index', 'Queues'),
        ('scraper.index', 'Scraper'),
        ('logs.logs', 'Logs'),
        ('settings.index', 'Settings'),
        ('database.index', 'Database'),
        ('scraper.scraper_tester', 'Tester'),
        ('debug.debug_functions', 'Debug')
    ]
    
    def is_user_system_enabled():
        # Implement this function based on your application's logic
        return True  # or False, depending on your setup

    visible_menu_items = []
    if is_user_system_enabled():
        if current_user.is_authenticated:
            for route, label in menu_items:
                if current_user.role == 'admin' or menu_items.index((route, label)) < 3:
                    visible_menu_items.append((route, label))
            if current_user.role == 'admin':
                visible_menu_items.append(('user_management.manage_users', 'Users'))
    else:
        visible_menu_items = menu_items

    return dict(menu_items=visible_menu_items, is_user_system_enabled=is_user_system_enabled)

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
        (webhook_bp, '/webhook'),
        (debug_bp, '/debug'),
        (trakt_bp, '/trakt'),
        (logs_bp, '/logs'),
        (settings_bp, '/settings'),
        (program_operation_bp, '/program_operation'),
        (real_time_api_bp, '/realtime_api_calls'),
        (tooltip_bp, '/tooltip'),
        (over_usage_bp, '/over_usage'),
    ]
    
    for blueprint, url_prefix in blueprints:
        app.register_blueprint(blueprint, url_prefix=url_prefix)

    # Remove rate_limit_check decorator from all routes
    # for endpoint, view_func in app.view_functions.items():
    #     if not endpoint.startswith('over_usage.'):
    #         app.view_functions[endpoint] = rate_limit_check(view_func)

__all__ = ['register_blueprints', 'admin_required', 'user_required', 'auth_bp', 'statistics_bp', 'rate_limit_check']