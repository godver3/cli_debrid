"""Renaming and deleting scraping versions, and matching items to a version.

media_items.version is the version name, optionally followed by the reverse
parser's '*' or '**' marker. Lookups must match that name exactly: a prefix or
substring match also caught other versions that share text ("1080p Remux").
"""
from unittest import mock

import pytest


def _insert(conn, rows):
    conn.executemany(
        "INSERT INTO media_items (id, imdb_id, title, type, state, version, location_on_disk) "
        "VALUES (?, 'tt1', 'Movie', 'movie', ?, ?, ?)", rows)
    conn.commit()


def _versions(get_db_connection):
    conn = get_db_connection()
    try:
        return dict(conn.execute("SELECT id, version FROM media_items ORDER BY id").fetchall())
    finally:
        conn.close()


@pytest.fixture
def items(app_db):
    conn = app_db()
    _insert(conn, [
        (1, 'Collected', '1080p', None),
        (2, 'Collected', '1080p*', None),
        (3, 'Collected', '1080p**', None),
        (4, 'Collected', '1080p Remux', None),
        (5, 'Collected', '1080P HDR', None),
        (6, 'Collected', '2160p', None),
    ])
    conn.close()
    return app_db


def test_rename_only_touches_that_version_and_keeps_markers(items):
    from database.database_writing import update_version_name
    assert update_version_name('1080p', 'HD') == 3
    assert _versions(items) == {1: 'HD', 2: 'HD*', 3: 'HD**', 4: '1080p Remux', 5: '1080P HDR', 6: '2160p'}


def test_reassign_includes_marked_items_and_keeps_markers(items):
    from database.database_writing import update_version_for_items
    assert update_version_for_items('1080p', '2160p') == 3
    assert _versions(items) == {1: '2160p', 2: '2160p*', 3: '2160p**', 4: '1080p Remux', 5: '1080P HDR', 6: '2160p'}


def test_reassign_to_none_clears_marked_items_too(items):
    from database.database_writing import update_version_for_items
    assert update_version_for_items('1080p', None) == 3
    assert _versions(items) == {1: None, 2: None, 3: None, 4: '1080p Remux', 5: '1080P HDR', 6: '2160p'}


def test_broken_item_is_rewanted_when_only_a_similarly_named_version_remains(app_db):
    # Item 1 (1080p) is broken; the only other copy is "1080p Remux", a
    # different version, so 1080p has no copy left and must be re-wanted.
    conn = app_db()
    _insert(conn, [(1, 'Collected', '1080p*', '/m/a.mkv'), (2, 'Collected', '1080p Remux', '/m/b.mkv')])
    conn.close()
    import os
    import utilities.analyze_library as al
    db_path = os.path.join(os.environ['USER_DB_CONTENT'], 'media_items.db')
    with mock.patch.object(al, 'move_item_to_wanted') as move, mock.patch.object(al, 'cache_plex_removal'):
        al._process_broken_media_item_analysis(db_path, 1, 'Movie', 'tt1', None, None, '1080p*',
                                                'movie', None, '/m/a.mkv', None)
    move.assert_called_once_with(1, None)


# ---- routes ---------------------------------------------------------------

@pytest.fixture
def client(items, app_settings):
    app_settings.update({
        'Scraping': {'versions': {
            '1080p': {'fallback_version': 'None'},
            '1080p Remux': {'fallback_version': '1080p'},
            '2160p': {'fallback_version': '1080p'},
        }},
        'Content Sources': {'Watchlist_1': {'versions': {'1080p': True, '2160p': False}},
                            'List_1': {'versions': ['1080p']}},
        'Reverse Parser': {'default_version': '1080p', 'version_terms': {'1080p': ['1080']},
                           'version_order': ['2160p', '1080p']},
        'Plex Movie Box Sets': {'grab_version': '1080p'},
        'Usenet Provider': {'repair_version': '1080p'},
    })
    from flask import Flask
    import routes.models as models
    import routes.settings_routes as sr
    import routes.settings_routes.versions as versions_mod
    saved = []
    app = Flask(__name__)
    app.register_blueprint(sr.settings_bp, url_prefix='/settings')
    with mock.patch.object(models, 'is_user_system_enabled', return_value=False), \
            mock.patch.object(versions_mod, 'save_config', side_effect=saved.append), \
            mock.patch.object(versions_mod, '_reload_components_after_settings_change'):
        c = app.test_client()
        c.saved = saved
        c.db = items
        yield c


def test_rename_updates_every_reference_and_keeps_order(client):
    resp = client.post('/settings/versions/rename', json={'old_name': '1080p', 'new_name': ' HD '})
    assert resp.get_json() == {'success': True}
    config = client.saved[-1]
    assert list(config['Scraping']['versions']) == ['HD', '1080p Remux', '2160p']
    assert config['Scraping']['versions']['2160p']['fallback_version'] == 'HD'
    assert config['Content Sources']['Watchlist_1']['versions'] == {'2160p': False, 'HD': True}
    assert config['Content Sources']['List_1']['versions'] == ['HD']
    assert config['Reverse Parser'] == {'default_version': 'HD', 'version_terms': {'HD': ['1080']},
                                        'version_order': ['2160p', 'HD']}
    assert config['Plex Movie Box Sets']['grab_version'] == 'HD'
    assert config['Usenet Provider']['repair_version'] == 'HD'
    assert _versions(client.db)[4] == '1080p Remux'


@pytest.mark.parametrize('new_name', ['2160p', '', '   ', 'None', 'HD*', 'a/b'])
def test_rename_rejects_bad_names_without_touching_anything(client, new_name):
    before = _versions(client.db)
    resp = client.post('/settings/versions/rename', json={'old_name': '1080p', 'new_name': new_name})
    assert resp.status_code == 400
    assert client.saved == []
    assert _versions(client.db) == before


def test_add_rejects_existing_and_reserved_names(client):
    for name in ('2160p', 'none', ''):
        assert client.post('/settings/versions/add', json={'name': name}).status_code == 400
    assert client.saved == []


def test_delete_with_target_moves_items_and_references(client):
    resp = client.post('/settings/versions/delete', json={'version_id': '1080p', 'target_version_id': '2160p'})
    assert resp.get_json()['success'] is True
    config = client.saved[-1]
    assert list(config['Scraping']['versions']) == ['1080p Remux', '2160p']
    assert config['Scraping']['versions']['1080p Remux']['fallback_version'] == '2160p'
    # The target fell back to the deleted version: it can't fall back to itself.
    assert config['Scraping']['versions']['2160p']['fallback_version'] == 'None'
    # A source that already had the target keeps its own on/off setting.
    assert config['Content Sources']['Watchlist_1']['versions'] == {'2160p': False}
    assert config['Content Sources']['List_1']['versions'] == ['2160p']
    assert config['Reverse Parser']['default_version'] == '2160p'
    assert config['Reverse Parser']['version_order'] == ['2160p']
    assert _versions(client.db)[2] == '2160p*'


def test_delete_without_target_resets_references(client):
    resp = client.post('/settings/versions/delete', json={'version_id': '1080p'})
    assert resp.get_json()['success'] is True
    config = client.saved[-1]
    assert config['Scraping']['versions']['2160p']['fallback_version'] == 'None'
    assert config['Content Sources']['Watchlist_1']['versions'] == {'2160p': False}
    assert config['Plex Movie Box Sets']['grab_version'] == 'Default'
    assert config['Usenet Provider']['repair_version'] == ''
    assert config['Reverse Parser']['version_terms'] == {}


@pytest.mark.parametrize('target', ['1080p', 'Missing'])
def test_delete_rejects_bad_target_before_touching_the_database(client, target):
    before = _versions(client.db)
    resp = client.post('/settings/versions/delete', json={'version_id': '1080p', 'target_version_id': target})
    assert resp.status_code == 400
    assert _versions(client.db) == before
    assert client.saved == []


def test_delete_of_unknown_version_leaves_database_alone(client):
    conn = client.db()
    _insert(conn, [(7, 'Collected', 'Ghost*', None)])
    conn.close()
    resp = client.post('/settings/versions/delete', json={'version_id': 'Ghost', 'target_version_id': '2160p'})
    assert resp.status_code == 404
    assert _versions(client.db)[7] == 'Ghost*'


def test_usage_check_counts_marked_items(app_db, app_settings):
    conn = app_db()
    _insert(conn, [(1, 'Collected', '4K*', None)])
    conn.close()
    app_settings['Scraping'] = {'versions': {'4K': {}, '1080p': {}}}
    from flask import Flask
    import routes.models as models
    import routes.settings_routes as sr
    app = Flask(__name__)
    app.register_blueprint(sr.settings_bp, url_prefix='/settings')
    with mock.patch.object(models, 'is_user_system_enabled', return_value=False):
        data = app.test_client().get('/settings/versions/check_usage/4K').get_json()
    assert data['in_use'] is True
    assert data['alternatives'] == ['1080p']
