"""Bazarr → Subtitle Settings import mapping (Bazarr 1.6 API shapes)."""
from unittest import mock

import pytest

pytest.importorskip('babelfish')

from utilities import bazarr_import  # noqa: E402

SETTINGS = {
    'general': {
        'enabled_providers': ['opensubtitlescom', 'tvsubtitles', 'yifysubtitles', 'wizdom',
                              'supersubtitles', 'gestdown', 'embeddedsubtitles'],
        'serie_default_enabled': True, 'serie_default_profile': 1,
        'movie_default_enabled': True, 'movie_default_profile': 1,
        'use_embedded_subs': True, 'ignore_pgs_subs': True, 'ignore_vobsub_subs': True,
        'ignore_ass_subs': True, 'minimum_score': 60, 'minimum_score_movie': 70, 'upgrade_subs': True,
    },
    'opensubtitlescom': {'username': 'me', 'password': 'pw', 'use_hash': True},
    'subsync': {'use_subsync': True, 'max_offset_seconds': 45},
}
PROFILES = [
    {'profileId': 1, 'name': 'English-Full', 'items': [
        {'id': 1, 'language': 'en', 'hi': 'False', 'forced': 'False'},
        {'id': 2, 'language': 'pt-BR', 'hi': 'True', 'forced': 'False'}]},
    {'profileId': 2, 'name': 'Other', 'items': [{'id': 1, 'language': 'fr', 'hi': 'False', 'forced': 'False'}]},
]


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


def _fake_get(url, headers=None, timeout=None):
    assert headers['X-API-KEY'] == 'k'
    return _Resp(SETTINGS if url.endswith('/settings') else PROFILES)


def test_plan_maps_supported_settings():
    with mock.patch.object(bazarr_import.requests, 'get', side_effect=_fake_get):
        plan = bazarr_import.fetch_plan('http://h:6767/', 'k')
    assert plan['subtitle_languages'] == 'eng,pt-BR'          # default profile only
    assert plan['subtitle_providers'] == ['opensubtitlescom', 'tvsubtitles', 'gestdown']
    assert plan['opensubtitles_username'] == 'me' and plan['opensubtitles_password'] == 'pw'
    assert plan['probe_for_embedded_subtitles'] is True
    assert plan['text_subtitles_only'] is True
    assert plan['sync_subtitles'] == 'unless_hash' and plan['sync_max_offset'] == 45
    joined = ' '.join(plan['not_carried'])
    assert 'yifysubtitles' in joined and 'hearing-impaired' in joined and '1 non-default' in joined
    assert 'embeddedsubtitles' not in joined


def test_preview_hides_password():
    with mock.patch.object(bazarr_import.requests, 'get', side_effect=_fake_get):
        plan = bazarr_import.fetch_plan('http://h:6767', 'k')
    with mock.patch('utilities.settings.get_setting', return_value=None):
        pub = bazarr_import.public_plan(plan)
    assert pub['opensubtitles_password'] == 'set'


def test_bad_key_and_url():
    with mock.patch.object(bazarr_import.requests, 'get', return_value=_Resp({}, 401)):
        with pytest.raises(bazarr_import.BazarrError, match='API key'):
            bazarr_import.fetch_plan('http://h:6767', 'k')
    with pytest.raises(bazarr_import.BazarrError, match='http'):
        bazarr_import.fetch_plan('h:6767', 'k')


def test_apply_writes_settings():
    with mock.patch.object(bazarr_import.requests, 'get', side_effect=_fake_get):
        plan = bazarr_import.fetch_plan('http://h:6767', 'k')
    written = {}
    with mock.patch('utilities.settings.set_setting', side_effect=lambda s, k, v: written.__setitem__((s, k), v)):
        bazarr_import.apply_plan(plan, enable=True, stop_bazarr_feed=True)
    assert written[('Subtitle Settings', 'subtitle_languages')] == 'eng,pt-BR'
    assert written[('Subtitle Settings', 'opensubtitles_password')] == 'pw'
    assert written[('Subtitle Settings', 'enable_subtitles')] is True
    assert written[('Subtitle Settings', 'sync_subtitles')] == 'unless_hash'
    assert written[('Bazarr Integration', 'enabled')] is False


def test_handled_subtitles_marked_checked(tmp_path, monkeypatch):
    import hashlib
    a, b, c = tmp_path / 'a.en.srt', tmp_path / 'b.en.srt', tmp_path / 'c.en.srt'
    for f in (a, b, c):
        f.write_bytes(f.name.encode())
    hist = {'data': list(reversed([
        {'action': 5, 'score': None, 'subtitles_path': str(a)},
        {'action': 1, 'score': '80.0%', 'subtitles_path': str(a)},
        {'action': 1, 'score': '95.5%', 'subtitles_path': str(b)},
        {'action': 1, 'score': '70.0%', 'subtitles_path': str(c)},
        {'action': 1, 'score': '99%', 'subtitles_path': str(tmp_path / 'gone.srt')},
    ]))}

    def fake_get(url, headers=None, timeout=None):
        return _Resp(hist if 'episodes/history' in url else {'data': []})
    marked = {}
    with mock.patch.object(bazarr_import.requests, 'get', side_effect=fake_get), \
            mock.patch('database.subtitle_history.mark_sync_checked',
                       side_effect=lambda d, r: marked.__setitem__(d, r)):
        assert bazarr_import.mark_handled('http://h', 'k') == (2, 1)
    assert marked[hashlib.sha1(b'a.en.srt').hexdigest()] == 'synced by Bazarr'
    assert marked[hashlib.sha1(b'b.en.srt').hexdigest()].startswith('Bazarr match 96')
    assert hashlib.sha1(b'c.en.srt').hexdigest() not in marked


def test_old_sync_does_not_cover_replaced_low_score_subtitle():
    # real API shape: no ids, newest first
    hist = {'data': list(reversed([
        {'action': 5, 'subtitles_path': '/s/x.srt'},          # sync of first download
        {'action': 1, 'score': '80%', 'subtitles_path': '/s/x.srt'},
        {'action': 3, 'score': '70%', 'subtitles_path': '/s/x.srt'},  # replaced, sync failed
        {'action': 5, 'subtitles_path': '/s/y.srt'},
        {'action': 1, 'score': '60%', 'subtitles_path': '/s/y.srt'},  # synced
        {'action': 1, 'score': '92%', 'subtitles_path': '/s/z.srt'},
    ]))}
    with mock.patch.object(bazarr_import.requests, 'get',
                           side_effect=lambda url, **k: _Resp(hist if 'episodes' in url else {'data': []})):
        got = bazarr_import.handled_subtitles('http://h', 'k')
    assert got == {'/s/y.srt': 'synced by Bazarr', '/s/z.srt': 'Bazarr match 92%'}
