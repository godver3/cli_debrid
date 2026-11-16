"""Tools, Maintenance and Diagnostics pages: the old Debug page, split by purpose.

The cards live in templates/tool_cards/*.html and keep calling the same /debug/...
endpoints in routes/debug_routes.py; only the pages changed.
"""
import os

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

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


@tool_pages_bp.route('/api/command/items')
@admin_required
def command_items():
    """Library items matching a title or an item ID, for the home page command bar
    (/rescrape, /blacklist)."""
    from database.core import get_db_connection
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'items': []})
    conn = get_db_connection()
    try:
        cols = 'id, title, type, season_number, episode_number, state, version, year'
        if q.isdigit():
            rows = conn.execute(f'SELECT {cols} FROM media_items WHERE id = ?', (int(q),)).fetchall()
        else:
            rows = conn.execute(
                f'SELECT {cols} FROM media_items WHERE title LIKE ? '
                'ORDER BY title, season_number, episode_number LIMIT 25', (f'%{q}%',)).fetchall()
    finally:
        conn.close()
    keys = ['id', 'title', 'type', 'season_number', 'episode_number', 'state', 'version', 'year']
    return jsonify({'items': [dict(zip(keys, tuple(r))) for r in rows]})


@tool_pages_bp.route('/api/command/titles')
@admin_required
def command_titles():
    """Movies and shows matching a title or an item ID, for the command bar's /delete.

    ?season= / ?episode= narrow the item count to that season or episode; a numeric
    q is an item ID and its own season/episode is used. One row per (imdb_id, kind),
    since the library delete routes work by IMDb ID.
    """
    from database.core import get_db_connection
    q = (request.args.get('q') or '').strip()
    season = request.args.get('season', type=int)
    episode = request.args.get('episode', type=int)
    if not q:
        return jsonify({'titles': []})
    conn = get_db_connection()
    try:
        if q.isdigit():
            row = conn.execute('SELECT imdb_id, type, season_number, episode_number FROM media_items WHERE id = ?',
                               (int(q),)).fetchone()
            if not row or not row['imdb_id']:
                return jsonify({'titles': []})
            where, params = 'imdb_id = ?', [row['imdb_id']]
            if row['type'] == 'episode':
                season, episode = row['season_number'], row['episode_number']
                where += " AND type = 'episode'"
            else:
                where += " AND type = 'movie'"
        else:
            where, params = 'title LIKE ?', [f'%{q}%']
        where += " AND imdb_id IS NOT NULL AND imdb_id != ''"
        if season is not None:
            where += " AND type = 'episode' AND season_number = ?"
            params.append(season)
            if episode is not None:
                where += ' AND episode_number = ?'
                params.append(episode)
        rows = conn.execute(
            "SELECT imdb_id, MIN(title) AS title, MIN(year) AS year, "
            "CASE WHEN type = 'movie' THEN 'movie' ELSE 'show' END AS kind, "
            "COUNT(*) AS items, GROUP_CONCAT(DISTINCT state) AS states "
            f"FROM media_items WHERE {where} GROUP BY imdb_id, kind ORDER BY title LIMIT 15",
            params).fetchall()
    finally:
        conn.close()
    titles = [{'imdb_id': r['imdb_id'], 'title': r['title'], 'year': r['year'], 'kind': r['kind'],
               'items': r['items'], 'states': sorted(set((r['states'] or '').split(',')) - {''})}
              for r in rows]
    return jsonify({'titles': titles, 'season': season, 'episode': episode})


@tool_pages_bp.route('/api/command/progress')
@admin_required
def command_progress():
    """States and a simplified log feed for a title requested from the command bar.

    Without ?since it only returns the server's log clock ({'now': ...}): the
    terminal reads that just before requesting, then polls with
    ?tmdb_id=&type=movie|show&since=<that value> until every item is done.
    """
    from database.core import get_db_connection
    from utilities import request_progress as rp
    since = request.args.get('since') or ''
    if not since:
        return jsonify({'now': rp.now_ts()})
    if not rp.valid_ts(since):
        return jsonify({'error': 'since must be a value returned by this endpoint'}), 400
    tmdb_id = (request.args.get('tmdb_id') or '').strip()
    if not tmdb_id:
        return jsonify({'error': 'tmdb_id is required'}), 400
    kind = 'movie' if request.args.get('type') == 'movie' else 'episode'
    conn = get_db_connection()
    try:
        rows = conn.execute(
            'SELECT id, type, season_number, episode_number, version, state FROM media_items '
            'WHERE tmdb_id = ? AND type = ? ORDER BY season_number, episode_number, version',
            (tmdb_id, kind)).fetchall()
    finally:
        conn.close()
    keys = ['id', 'type', 'season_number', 'episode_number', 'version', 'state']
    items = [dict(zip(keys, tuple(r))) for r in rows]
    return jsonify({'summary': rp.summarize(items), 'events': rp.events_for(items, since)})


@tool_pages_bp.route('/api/command/versions')
@admin_required
def command_versions():
    """Configured versions, for /request in the command bar."""
    from utilities.settings import get_setting
    return jsonify({'versions': list((get_setting('Scraping', 'versions', {}) or {}).keys())})

