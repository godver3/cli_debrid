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
