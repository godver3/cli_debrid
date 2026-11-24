"""Fixes from the Nov 2026 code review: watch-history lookups and overlay element conditions."""
import os
import sqlite3

import pytest


WATCH_HISTORY_SCHEMA = '''
    CREATE TABLE watch_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, type TEXT NOT NULL, watched_at TIMESTAMP,
        media_id TEXT, imdb_id TEXT, tmdb_id TEXT, tvdb_id TEXT,
        season INTEGER, episode INTEGER, show_title TEXT,
        duration INTEGER, watch_progress INTEGER, source TEXT,
        UNIQUE(title, type, watched_at),
        UNIQUE(show_title, season, episode, watched_at)
    )
'''


def _watch_history_db(rows):
    path = os.path.join(os.environ['USER_DB_CONTENT'], 'watch_history.db')
    conn = sqlite3.connect(path)
    conn.execute(WATCH_HISTORY_SCHEMA)
    conn.executemany(
        'INSERT INTO watch_history (title, type, watched_at, imdb_id, tmdb_id, season, episode, show_title) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
    conn.commit()
    return conn


def _movie(tmdb_id='603', imdb_id='tt0133093'):
    return {'title': 'The Matrix', 'year': 1999, 'imdb_id': imdb_id, 'tmdb_id': tmdb_id,
            'release_date': '1999-03-31', 'media_type': 'movie'}


@pytest.fixture
def skip_watched(app_db, app_settings):
    app_settings['Debug'] = {'do_not_add_plex_watch_history_items_to_queue': True}
    app_settings['Scraping'] = {'versions': {'1080p': {}}}
    return app_db


def _wanted_movies(get_db_connection):
    conn = get_db_connection()
    try:
        return conn.execute("SELECT tmdb_id FROM media_items WHERE type = 'movie'").fetchall()
    finally:
        conn.close()


def test_watched_episode_with_same_tmdb_number_does_not_skip_movie(skip_watched):
    # TV and movie TMDB ids are separate namespaces: show 603 is not movie 603.
    _watch_history_db([('Some Show', 'episode', '2026-01-01', None, '603', 1, 1, 'some show')]).close()
    from database.wanted_items import add_wanted_items
    add_wanted_items([_movie()], {'1080p': True})
    assert len(_wanted_movies(skip_watched)) == 1


def test_watched_movie_is_still_skipped(skip_watched):
    _watch_history_db([('The Matrix', 'movie', '2026-01-01', None, '603', None, None, None)]).close()
    from database.wanted_items import add_wanted_items
    add_wanted_items([_movie(imdb_id=None)], {'1080p': True})
    assert _wanted_movies(skip_watched) == []


def test_watch_history_id_lookups_use_an_index(skip_watched):
    conn = _watch_history_db([])
    from database.core import ensure_watch_history_indexes
    ensure_watch_history_indexes(conn)
    for column in ('imdb_id', 'tmdb_id'):
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT 1 FROM watch_history "
            f"WHERE type = 'episode' AND season = ? AND episode = ? AND {column} = ?", (1, 1, 'x')).fetchall()
        assert any('USING INDEX' in row[-1] for row in plan), plan
    conn.close()


@pytest.mark.parametrize('condition, value, expected', [
    ('hdr_format IS NOT NULL', 'HDR10', True),
    ('hdr_format IS NOT NULL', None, False),
    ('hdr_format IS NULL', None, True),
    ('hdr_format IS NOT NULL AND NOT hdr', 'HDR10', True),
])
def test_element_conditions_with_is_not_null(condition, value, expected):
    from overlays.renderer import OverlayRenderer
    renderer = OverlayRenderer(asset_dir=os.environ['USER_DB_CONTENT'])
    media_info = {'hdr_format': value, 'hdr': False}
    assert renderer._evaluate_condition({'condition': condition}, media_info) is expected
