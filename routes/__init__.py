from flask import Blueprint
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

def register_blueprints(app):
    blueprints = [
        (auth_bp, '/auth'),
        (scraper_bp, '/scraper'),
        (queues_bp, '/queues'),
        (api_summary_bp, '/api_summary'),
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
        (real_time_api_bp, '/real_time_api'),
    ]
    
    for blueprint, url_prefix in blueprints:
        app.register_blueprint(blueprint, url_prefix=url_prefix)

__all__ = ['register_blueprints', 'admin_required', 'user_required', 'auth_bp', 'statistics_bp']