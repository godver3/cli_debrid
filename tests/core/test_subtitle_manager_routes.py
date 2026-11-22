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


def _plex_item(env):
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.environ['USER_DB_CONTENT'], 'media_items.db'))
    conn.execute("UPDATE media_items SET ms_item_id = '777' WHERE id = 1")
    conn.commit()
    conn.close()


class _R:
    def __init__(self, status=200, data=None, content=b''):
        self.status_code, self._d, self.content = status, data, content

    def json(self):
        return self._d


_META = {'MediaContainer': {'Metadata': [{'Media': [{'Part': [{'Stream': [
    {'streamType': 1, 'id': 1},
    {'streamType': 3, 'id': 10, 'codec': 'srt', 'languageTag': 'en', 'displayTitle': 'English'},
    {'streamType': 3, 'id': 11, 'key': '/library/streams/11', 'codec': 'srt', 'languageTag': 'en',
     'displayTitle': 'English (SRT External)'},
]}]}]}]}}


def test_plex_delete_only_external_streams(env):
    _plex_item(env)
    calls = []
    with mock.patch('requests.get', return_value=_R(200, _META)), \
            mock.patch('utilities.subtitle_resync.item_rk', return_value='777'), \
            mock.patch('requests.delete', side_effect=lambda url, **k: calls.append(url) or _R(200)), \
            mock.patch('utilities.settings.get_setting',
                       side_effect=lambda s, k, d=None: {('Plex', 'url'): 'http://plex:32400',
                                                         ('Plex', 'token'): 't'}.get((s, k), d)):
        c = env['client']
        assert c.post('/api/subtitles/item/1/plex_delete', json={'stream_id': 10}).status_code == 400
        assert c.post('/api/subtitles/item/1/plex_delete', json={'stream_id': 11}).get_json()['success']
    assert calls == ['http://plex:32400/library/streams/11']


def test_plex_resync_uploads_then_removes_old(env):
    _plex_item(env)
    deleted, uploaded = [], []

    def fake_get(url, **k):
        return _R(200, _META) if url.endswith('/metadata/777') else _R(200, content=b'old subtitle')

    with mock.patch('requests.get', side_effect=fake_get), \
            mock.patch('utilities.subtitle_resync.item_rk', return_value='777'), \
            mock.patch('requests.delete', side_effect=lambda url, **k: deleted.append(url) or _R(200)), \
            mock.patch('utilities.downsub.sync_content', return_value=(b'new subtitle', 'synced: shifted -2.00s')), \
            mock.patch('utilities.downsub.upload_subtitle_to_plex',
                       side_effect=lambda rk, c, lang, fmt='srt': uploaded.append((rk, c, lang)) or True), \
            mock.patch('utilities.downsub.resolve_item_path', return_value='/mnt/file.mkv'), \
            mock.patch('utilities.settings.get_setting',
                       side_effect=lambda s, k, d=None: {('Plex', 'url'): 'http://plex:32400',
                                                         ('Plex', 'token'): 't'}.get((s, k), d)):
        d = env['client'].post('/api/subtitles/item/1/sync', json={'stream_id': 11}).get_json()
    assert d['changed'] and uploaded == [('777', b'new subtitle', 'en')]
    assert deleted == ['http://plex:32400/library/streams/11']


def test_sidecar_resync_rewrites_file(env):
    target = os.path.join(env['lib'], 'Show - S01E01.en.srt')
    with open(target, 'wb') as f:
        f.write(b'old')
    with mock.patch('utilities.downsub.sync_content', return_value=(b'new', 'synced: shifted +1.00s')):
        d = env['client'].post('/api/subtitles/item/1/sync', json={'path': target}).get_json()
    assert d['changed'] and open(target, 'rb').read() == b'new'
    with mock.patch('utilities.downsub.sync_content', side_effect=lambda v, c, f, **k: (c, 'already in sync')):
        d = env['client'].post('/api/subtitles/item/1/sync', json={'path': target}).get_json()
    assert not d['changed'] and d['message'] == 'already in sync'


def test_auto_says_what_happened(env):
    """Live 2026-11-22: Auto said 'Subtitles saved' for a file that already
    had every language embedded (nothing downloaded)."""
    from utilities import downsub
    c = env['client']

    def fake(note, ok):
        def run(path, item=None, **kw):
            downsub._note(item, note)
            return ok
        return run
    for note, ok, want in [('every language already embedded in the file', True, 'Nothing to download: every language'),
                           ('en from subdl, score 942/971', True, 'Saved: en from subdl'),
                           ('nothing passed the match threshold (subdl)', False, 'Nothing passed the match threshold')]:
        with mock.patch.object(downsub, 'download_subtitles_for_video', side_effect=fake(note, ok)):
            d = c.post('/api/subtitles/item/2/auto').get_json()
        assert d['success'] is ok and d['message'].startswith(want), d
