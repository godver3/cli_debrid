"""Tools, Maintenance and Diagnostics pages: the old Debug page, split by purpose.

The cards live in templates/tool_cards/*.html and keep calling the same /debug/...
endpoints in routes/debug_routes.py; only the pages changed.
"""
import os

from flask import Blueprint, redirect, render_template, request, url_for

from routes.models import admin_required
from utilities.settings import get_all_settings

tool_pages_bp = Blueprint('tool_pages', __name__)


def _card_context():
    """Template values the moved cards read (same as the old Debug page passed)."""
    from routes.debug_routes import get_cache_files
    from utilities.settings import get_nas_paths
    content_sources = get_all_settings().get('Content Sources', {})
    cache_data = get_cache_files()
    return {
        'content_sources': {k: v for k, v in content_sources.items() if v.get('enabled', False)},
        'cache_files': cache_data.get('content_source', []),
        'other_cache_files': cache_data.get('other', []),
        'environment_mode': os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full'),
        'nas_paths': get_nas_paths(),
    }


@tool_pages_bp.route('/tools')
@admin_required
def tools():
    return render_template('tools.html', **_card_context())


@tool_pages_bp.route('/maintenance')
@admin_required
def maintenance():
    return render_template('maintenance.html', **_card_context())


@tool_pages_bp.route('/diagnostics')
@admin_required
def diagnostics():
    # Shown as a tab of the Performance Dashboard (loaded with ?embed=1).
    if not request.args.get('embed'):
        return redirect(url_for('performance.performance_dashboard') + '#diagnostics')
    return render_template('diagnostics.html', **_card_context())
