"""Plex-known subtitles: episode ratingKey lookup (ms_item_id is the SHOW's
for episodes), embedded-track cache, and good embedded tracks counting as
having the language."""
import os
import sqlite3
import tempfile
from unittest import mock

import pytest

pytest.importorskip('subliminal')


class _R:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return {'MediaContainer': {'Metadata': self._d}}


LEAVES = [{'ratingKey': '501', 'parentIndex': 1, 'index': 1}, {'ratingKey': '502', 'parentIndex': 1, 'index': 2}]


def _stream(codec, lang, forced=False, external=False):
    st = {'streamType': 3, 'codec': codec, 'languageTag': lang, 'languageCode': lang[:2] + 'g', 'forced': forced}
    if external:
        st['key'] = '/library/streams/9'
    return st


def _meta(rk, streams):
    return {'ratingKey': rk, 'Media': [{'Part': [{'Stream': [{'streamType': 1}] + streams}]}]}


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix='psi_')
    monkeypatch.setenv('USER_DB_CONTENT', tmp)
    import database  # noqa: F401
    from utilities import plex_subtitle_info as psi
    psi._LEAVES.clear()
    monkeypatch.setattr(psi, '_conn', lambda: ('http://plex', 't'))
    conn = sqlite3.connect(os.path.join(tmp, 'media_items.db'))
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, title TEXT, year INTEGER,
        state TEXT, type TEXT, episode_title TEXT, season_number INTEGER, episode_number INTEGER,
        collected_at TEXT, filled_by_file TEXT, location_on_disk TEXT, version TEXT, ms_item_id TEXT)""")
    rows = [(1, 'tt1', 'Show', 2020, 'Collected', 'episode', None, 1, 1, '2026-11-01', 'a.mkv', '/lib/a.mkv', '1080p', '500'),
            (2, 'tt1', 'Show', 2020, 'Collected', 'episode', None, 1, 2, '2026-11-02', 'b.mkv', '/lib/b.mkv', '1080p', '500'),
            (3, 'tt2', 'Film', 2021, 'Collected', 'movie', None, None, None, '2026-11-03', 'c.mkv', '/lib/c.mkv', '1080p', '600')]
    conn.executemany("INSERT INTO media_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith('/library/metadata/500/allLeaves'):
            return _R(LEAVES)
        ids = url.rsplit('/', 1)[1].split(',')
        data = {'501': _meta('501', [_stream('srt', 'en')]),
                '502': _meta('502', [_stream('pgs', 'en'), _stream('srt', 'en', forced=True)]),
                '600': _meta('600', [_stream('ass', 'en'), _stream('srt', 'en', external=True)])}
        return _R([data[i] for i in ids if i in data])
    monkeypatch.setattr(psi.requests, 'get', get)
    return psi


def test_episode_rating_key_comes_from_the_show(env):
    assert env.rating_key({'type': 'episode', 'ms_item_id': '500', 'season_number': 1, 'episode_number': 2}) == '502'
    assert env.rating_key({'type': 'movie', 'ms_item_id': '600'}) == '600'


def test_only_text_non_forced_embedded_tracks_count(env):
    items = [{'type': 'episode', 'ms_item_id': '500', 'season_number': 1, 'episode_number': e} for e in (1, 2)]
    items.append({'type': 'movie', 'ms_item_id': '600'})
    env.fetch_tracks([(env.cache_key(i), env.rating_key(i)) for i in items])
    cached = env.cached_tracks([env.cache_key(i) for i in items])
    assert 'en' in env.good_embedded_langs(cached['500:1:1'])          # SRT
    assert env.good_embedded_langs(cached['500:1:2']) == set()        # PGS + forced SRT
    assert env.good_embedded_langs(cached['600']) == set()            # ASS (external SRT is a file, not embedded)


def test_status_counts_good_embedded_as_present(env):
    from utilities import subtitle_status
    items = [{'type': 'episode', 'ms_item_id': '500', 'season_number': 1, 'episode_number': e} for e in (1, 2)]
    env.fetch_tracks([(env.cache_key(i), env.rating_key(i)) for i in items])
    vals = {('File Management', 'file_collection_management'): 'Plex',
            ('Subtitle Settings', 'subtitle_languages'): 'eng'}
    with mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: vals.get((s, k), d)), \
            mock.patch('utilities.config.downsub_config.get_setting',
                       side_effect=lambda s, k, d=None: vals.get((s, k), d)):
        rows = {r['id']: r for r in subtitle_status.with_status(subtitle_status.collected())}
    assert rows[1]['missing'] is False and rows[1]['langs'][0]['embedded'] is True
    assert rows[2]['missing'] is True                                  # only PGS / forced
    assert rows[3]['missing'] is True                                  # not scanned yet


def test_background_scan_fills_cache(env):
    import time
    from utilities import subtitle_status
    items = subtitle_status.collected('', 'all', full=True)
    assert env.start_scan(items)
    for _ in range(100):
        if not env.scan_status()['running']:
            break
        time.sleep(0.05)
    assert set(env.cached_tracks([env.cache_key(i) for i in items])) == {'500:1:1', '500:1:2', '600'}


def test_jellyfin_scan_matches_by_file_name(monkeypatch, tmp_path):
    monkeypatch.setenv('USER_DB_CONTENT', str(tmp_path))
    import database  # noqa: F401
    from utilities import plex_subtitle_info as psi
    vals = {('File Management', 'file_collection_management'): 'Symlinked/Local',
            ('File Management', 'media_server_type'): 'jellyfin',
            ('Debug', 'emby_jellyfin_url'): 'http://jf', ('Debug', 'emby_jellyfin_token'): 't'}
    pages = [[{'Path': '/media/TV/Show - S01E01.mkv', 'MediaStreams': [
                  {'Type': 'Subtitle', 'Language': 'eng', 'Codec': 'subrip', 'IsForced': False},
                  {'Type': 'Subtitle', 'Language': 'eng', 'Codec': 'subrip', 'Title': "Director's Commentary"}]},
              {'Path': '/media/TV/Show - S01E02.mkv', 'MediaStreams': [
                  {'Type': 'Subtitle', 'Language': 'eng', 'Codec': 'PGSSUB'}]}]]

    class S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None, timeout=None):
            class R:
                status_code = 200

                def __init__(self, d):
                    self.d = d

                def json(self):
                    return self.d

                def raise_for_status(self):
                    pass
            if url.endswith('/Users'):
                return R([{'Id': 'u1'}])
            return R({'Items': pages[0] if params['StartIndex'] == 0 else []})
    monkeypatch.setattr(psi.requests, 'Session', S)
    items = [{'id': 1, 'type': 'episode', 'location_on_disk': '/lib/TV/Show - S01E01.mkv'},
             {'id': 2, 'type': 'episode', 'location_on_disk': '/lib/TV/Show - S01E02.mkv'}]
    with mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: vals.get((s, k), d)):
        assert psi.server() == 'jellyfin'
        assert psi.start_scan(items)
        import time
        for _ in range(100):
            if not psi.scan_status()['running']:
                break
            time.sleep(0.05)
        cached = psi.cached_tracks(['item:1', 'item:2'])
    assert psi.good_embedded_langs(cached['item:1']) >= {'eng'}
    assert len(cached['item:1']) == 1                                    # commentary skipped
    assert psi.good_embedded_langs(cached['item:2']) == set()          # PGS doesn't count
