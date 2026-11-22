"""Tools → Subtitles API: status from sidecars + history, item detail, sidecar delete guard."""
import os
import sqlite3
import tempfile
from unittest import mock

import pytest

pytest.importorskip('flask')
pytest.importorskip('subliminal')


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix='subs_mgr_')
    db_dir = os.path.join(tmp, 'db')
    lib = os.path.join(tmp, 'lib', 'Show (2020)', 'Season 01')
    os.makedirs(db_dir)
    os.makedirs(lib)
    monkeypatch.setenv('USER_DB_CONTENT', db_dir)
    ep1 = os.path.join(lib, 'Show - S01E01.mkv')
    ep2 = os.path.join(lib, 'Show - S01E02.mkv')
    for p in (ep1, ep2):
        open(p, 'wb').close()
    open(os.path.join(lib, 'Show - S01E01.en.srt'), 'w').close()
    open(os.path.join(lib, 'Show - S01E01.es.ass'), 'w').close()
    open(os.path.join(tmp, 'outside.srt'), 'w').close()

    conn = sqlite3.connect(os.path.join(db_dir, 'media_items.db'))
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, tmdb_id TEXT,
        title TEXT, year INTEGER, state TEXT, type TEXT, episode_title TEXT, season_number INTEGER,
        episode_number INTEGER, collected_at TEXT, filled_by_file TEXT, filled_by_title TEXT,
        location_on_disk TEXT, version TEXT, ms_item_id TEXT, original_scraped_torrent_title TEXT)""")
    for i, (p, e) in enumerate(((ep1, 1), (ep2, 2)), start=1):
        conn.execute("INSERT INTO media_items (id, imdb_id, title, year, state, type, season_number, "
                     "episode_number, collected_at, filled_by_file, location_on_disk, version) "
                     "VALUES (?, 'tt1', 'Show', 2020, 'Collected', 'episode', 1, ?, ?, ?, ?, '1080p')",
                     (i, e, f'2026-11-0{i} 10:00:00', os.path.basename(p), p))
    conn.commit()
    conn.close()

    settings = {('File Management', 'file_collection_management'): 'Symlinked/Local',
                ('Subtitle Settings', 'subtitle_languages'): 'eng,spa'}

    def fake_get_setting(section, key, default=None):
        return settings.get((section, key), default)

    import flask
    import database  # noqa: F401  (app import order; routes first is circular)
    from routes import subtitle_manager_routes as smr
    from database import subtitle_history
    monkeypatch.setattr(subtitle_history, '_ready', False)
    patches = [mock.patch('utilities.settings.get_setting', side_effect=fake_get_setting),
               mock.patch('utilities.config.downsub_config.get_setting', side_effect=fake_get_setting),
               mock.patch('routes.models.is_user_system_enabled', return_value=False)]
    for p in patches:
        p.start()
    app = flask.Flask(__name__)
    app.register_blueprint(smr.subtitle_manager_bp)
    yield {'client': app.test_client(), 'lib': lib, 'tmp': tmp, 'ep1': ep1}
    for p in patches:
        p.stop()


def test_overview_status_from_sidecars(env):
    c = env['client']
    rows = c.get('/api/subtitles/overview?status=all').get_json()
    by_id = {r['id']: r for r in rows['items']}
    assert {lang['lang']: lang['has'] for lang in by_id[1]['langs']} == {'en': True, 'es': True}
    assert {lang['lang']: lang['has'] for lang in by_id[2]['langs']} == {'en': False, 'es': False}
    missing = c.get('/api/subtitles/overview?status=missing').get_json()
    assert [r['id'] for r in missing['items']] == [2]


def test_history_counts_as_having_language(env):
    from database import subtitle_history
    subtitle_history.record(2, 'Show S01E02', 'en', 'gestdown', 900, 971, ['series'], 'x', 'srt',
                            'plex', None, 'auto')
    rows = env['client'].get('/api/subtitles/overview?status=all').get_json()
    by_id = {r['id']: r for r in rows['items']}
    assert {lang['lang']: lang['has'] for lang in by_id[2]['langs']}['en'] is True
    hist = env['client'].get('/api/subtitles/history').get_json()['rows']
    assert hist[0]['provider'] == 'gestdown' and hist[0]['matches'] == ['series']


def test_item_detail_lists_sidecars(env):
    d = env['client'].get('/api/subtitles/item/1').get_json()
    assert sorted(s['name'] for s in d['sidecars']) == ['Show - S01E01.en.srt', 'Show - S01E01.es.ass']
    assert env['client'].get('/api/subtitles/item/99').status_code == 404


def test_delete_only_this_items_sidecars(env):
    c = env['client']
    outside = os.path.join(env['tmp'], 'outside.srt')
    assert c.post('/api/subtitles/item/1/delete', json={'path': outside}).status_code == 400
    assert c.post('/api/subtitles/item/1/delete', json={'path': env['ep1']}).status_code == 400
    assert os.path.exists(outside) and os.path.exists(env['ep1'])
    target = os.path.join(env['lib'], 'Show - S01E01.en.srt')
    assert c.post('/api/subtitles/item/1/delete', json={'path': target}).get_json()['success']
    assert not os.path.exists(target)


def test_download_with_expired_token(env):
    r = env['client'].post('/api/subtitles/item/1/download', json={'token': 'nope', 'index': 0})
    assert r.status_code == 410
