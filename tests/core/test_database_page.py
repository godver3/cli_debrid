"""Database page: whole-filter unique count, "select all matching" ids, paging."""
import importlib
import sqlite3
from unittest import mock

import pytest

import database  # noqa: F401  (app import order)
# The package re-exports the view as `index`, shadowing the module name.
index_mod = importlib.import_module('routes.database_routes.index')
unique_key_count_sql = index_mod.unique_key_count_sql

COLUMNS = ['id', 'title', 'year', 'type', 'state', 'version', 'season_number', 'episode_number',
           'ghostlisted', 'filled_by_torrent_id', 'debrid_provider', 'imdb_id', 'tmdb_id', 'size']

ROWS = [
    # Two versions of one movie count once.
    (1, 'Alien', 1979, 'movie', 'Collected', '1080p', None, None, 0, 'RD1', 'Real-Debrid', 'tt1', '1', 5.0),
    (2, 'Alien', 1979, 'movie', 'Wanted', '4k', None, None, 0, None, None, 'tt1', '1', None),
    # Same title, other year: a different movie.
    (3, 'Alien', 2030, 'movie', 'Wanted', '1080p', None, None, 0, None, None, 'tt9', '9', None),
    (4, 'Andor', 2022, 'episode', 'Collected', '1080p', 1, 1, 0, 'nzb:x', None, 'tt2', '2', 1.0),
    (5, 'Andor', 2022, 'episode', 'Collected', '4k', 1, 1, 0, 'nzb:y', None, 'tt2', '2', 2.0),
    (6, 'Andor', 2022, 'episode', 'Wanted', '1080p', 1, 2, 0, None, None, 'tt2', '2', None),
    (7, 'Bluey', None, 'episode', 'Wanted', '1080p', 2, 1, 0, None, None, 'tt3', '3', None),
    # Ghostlisted rows are hidden unless asked for.
    (8, 'Cobra', 2001, 'movie', 'Blacklisted', '1080p', None, None, 1, None, None, 'tt4', '4', None),
]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'media_items.db'
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE media_items ({', '.join(COLUMNS)})")
    conn.executemany(f"INSERT INTO media_items VALUES ({', '.join('?' * len(COLUMNS))})", ROWS)
    conn.commit()
    conn.close()
    return path


def test_unique_sql_matches_consolidate_items(db):
    from routes.queues_routes import consolidate_items
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT * FROM media_items')]
    (sql_count,) = conn.execute(f'SELECT {unique_key_count_sql(COLUMNS)} FROM media_items').fetchone()
    assert sql_count == len(consolidate_items(rows)[0]) == 6


def test_unique_sql_without_episode_columns():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE media_items (id, title, year)')
    conn.executemany('INSERT INTO media_items VALUES (?,?,?)', [(1, 'A', 1), (2, 'A', 1), (3, 'B', None)])
    (n,) = conn.execute(f"SELECT {unique_key_count_sql(['id', 'title', 'year'])} FROM media_items").fetchone()
    assert n == 2


def _get(db, **args):
    from flask import Flask
    app = Flask(__name__)
    app.secret_key = 'test'
    counts = {'total_movies': 0, 'total_shows': 0, 'total_episodes': 0}
    with mock.patch('database.get_db_connection', side_effect=lambda: sqlite3.connect(db)), \
            mock.patch.object(index_mod, 'get_basic_collection_counts', return_value=counts), \
            mock.patch.object(index_mod, 'get_content_source_display_names', return_value={}), \
            app.test_request_context('/database/', query_string=args):
        rv = index_mod.index.__wrapped__()
    return rv.get_json()


def test_unique_count_covers_whole_filter_not_page(db):
    data = _get(db, ajax='1', per_page='100', page='1')
    assert data['total_count'] == 7  # ghostlisted row hidden
    assert data['unique_result_count'] == 5
    assert data['result_count'] == 7


def test_ids_only_returns_every_match_in_view_order(db):
    filters = '[{"column": "title", "operator": "contains", "value": "Andor"}]'
    data = _get(db, ids_only='1', filters=filters, sort_column='id', sort_order='desc')
    assert data == {'ids': ['6', '5', '4']}


def test_ids_only_respects_content_type(db):
    data = _get(db, ids_only='1', content_type='movie')
    assert data == {'ids': ['1', '2', '3']}


def test_per_page_above_cap_uses_biggest_offered(db):
    assert _get(db, ajax='1', per_page='5000')['per_page'] == 1000
    assert _get(db, ajax='1', per_page='7')['per_page'] == 250


def test_scroll_load_skips_counts(db):
    data = _get(db, ajax='1', scroll='1', per_page='100', page='1')
    assert data['result_count'] == 7
    assert data['total_count'] is None and data['unique_result_count'] is None
    assert _get(db, ajax='1', scroll='1', per_page='100', page='9')['items'] == []


def test_bare_visit_renders_shell_without_querying(db):
    from flask import Flask
    app = Flask(__name__)
    with mock.patch('database.get_db_connection') as conn, \
            mock.patch.object(index_mod, 'render_template', return_value='shell') as render, \
            app.test_request_context('/database/'):
        assert index_mod.index.__wrapped__() == 'shell'
    render.assert_called_once_with('database.html', shell=True)
    conn.assert_not_called()
