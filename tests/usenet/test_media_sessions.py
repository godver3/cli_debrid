#!/usr/bin/env python3
"""Jellyfin/Emby sessions for stream protection, show precache and Now
playing (utilities/media_sessions.py). Payloads are trimmed from real
Jellyfin 12.4 / Emby 4.10 answers (same shape on both)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import media_sessions as ms
from utilities import mount_now_playing as np
from utilities import mount_prewarm as mp

URL, KEY = 'http://jf:8096', 'k'
EP1 = '/media/tv/Show (2020)/Season 01/Show (2020) - S01E01 - Pilot - tt1 - 1080p - (Show.S01E01.1080p-GRP.mkv).mkv'
SESSIONS = [
    {'Id': 'sess1', 'UserName': 'alice', 'Client': 'Jellyfin Web', 'DeviceName': 'Firefox',
     'NowPlayingItem': {'Id': 'e1', 'Type': 'Episode', 'Name': 'Pilot', 'SeriesName': 'Show', 'SeriesId': 'ser',
                        'ParentIndexNumber': 1, 'IndexNumber': 1, 'RunTimeTicks': 1_000 * 10_000,
                        'Path': EP1, 'ProductionYear': 2020},
     'PlayState': {'PositionTicks': 800 * 10_000, 'MediaSourceId': 'e1', 'PlayMethod': 'DirectPlay', 'IsPaused': False}},
    {'Id': 'idle', 'UserName': 'bob'},  # logged in, nothing playing
]
EPISODES = {'Items': [
    {'Id': 'e0', 'Type': 'Episode', 'ParentIndexNumber': 0, 'IndexNumber': 1, 'Path': '/media/special.mkv'},
    {'Id': 'e1', 'Type': 'Episode', 'ParentIndexNumber': 1, 'IndexNumber': 1, 'Path': EP1},
    {'Id': 'e3', 'Type': 'Episode', 'SeriesName': 'Show', 'SeriesId': 'ser', 'ParentIndexNumber': 2, 'IndexNumber': 1,
     'Path': '/x/__all__/Show.S02E01.1080p-GRP/Show.S02E01.1080p-GRP.mkv'},
    {'Id': 'e2', 'Type': 'Episode', 'SeriesName': 'Show', 'SeriesId': 'ser', 'ParentIndexNumber': 1, 'IndexNumber': 2,
     'Path': '/x/__all__/Show.S01E02.1080p-GRP/Show.S01E02.1080p-GRP.mkv',
     'MediaSources': [{'Path': '/x/__all__/Show.S01E02.1080p-GRP/Show.S01E02.1080p-GRP.mkv', 'Size': 2_000_000_000}]},
]}


class _Resp:
    def __init__(self, body, code=200):
        self.body, self.status_code = body, code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self.body


def fake_get(url, headers=None, timeout=None):
    assert headers['Authorization'] == 'MediaBrowser Token="k"', headers
    if url.endswith('/Sessions'):
        return _Resp(SESSIONS)
    if '/Shows/ser/Episodes' in url:
        return _Resp(EPISODES)
    if url.endswith('/System/Info'):
        return _Resp({'ServerName': 'box', 'Version': '12.4.0'})
    raise AssertionError(url)


def settings(jf=True, plex=False):
    def get(section, key=None, default=None):
        if section == 'Built-in Mount' and key is None:
            return {}
        if jf and section == 'Debug':
            return {'emby_jellyfin_url': URL + '/', 'emby_jellyfin_token': KEY}.get(key, default)
        if plex and section == 'Plex':
            return {'url': 'http://plex', 'token': 't'}.get(key, default)
        return default
    return get


class JellyfinSessionTests(unittest.TestCase):
    def test_sessions_in_plex_shape(self):
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get):
            got, err = ms.jellyfin_sessions_checked(URL, KEY)
        self.assertEqual(err, '')
        self.assertEqual(len(got), 1)  # the idle session is left out
        s = got[0]
        self.assertEqual((s['type'], s['grandparentTitle'], s['parentIndex'], s['index']), ('episode', 'Show', 1, 1))
        self.assertEqual((s['viewOffset'], s['duration']), (800, 1000))
        self.assertEqual(np._session_files(s), [EP1])
        self.assertEqual(s['User']['title'], 'alice')
        self.assertTrue(s['ratingKey'].startswith(ms.JF) and s['sessionKey'].startswith(ms.JF))

    def test_server_down_is_an_error(self):
        with mock.patch.object(ms.requests, 'get', return_value=_Resp({}, 500)):
            got, err = ms.jellyfin_sessions_checked(URL, KEY)
        self.assertEqual(got, [])
        self.assertIn('500', err)

    def test_next_episode_by_season_then_episode(self):
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get):
            s = ms.jellyfin_sessions_checked(URL, KEY)[0][0]
            nxt = ms.jellyfin_next_episode(URL, KEY, s)
            s2 = dict(s, index=2)
            after = ms.jellyfin_next_episode(URL, KEY, s2)  # season finale -> next season
        self.assertEqual((nxt['parentIndex'], nxt['index']), (1, 2))
        self.assertEqual(nxt['Media'][0]['Part'][0]['size'], 2_000_000_000)
        self.assertEqual((after['parentIndex'], after['index']), (2, 1))

    def test_url_and_key_sources(self):
        url, key, us, ks = ms.jellyfin_server_source(settings())
        self.assertEqual((url, key, us), (URL, KEY, 'Settings → Jellyfin/Emby'))
        mine = lambda s, k=None, d=None: {'jellyfin_url': 'http://mine', 'jellyfin_token': 'm'}.get(k, d) \
            if s == 'Built-in Mount' else settings()(s, k, d)
        self.assertEqual(ms.jellyfin_server(mine), ('http://mine', 'm'))
        self.assertEqual(ms.jellyfin_server(settings(jf=False)), ('', ''))

    def test_no_server_set_is_an_error(self):
        self.assertEqual(ms.sessions_checked(settings(jf=False)), ([], 'no media server set (Plex, or Jellyfin/Emby)'))

    def test_both_servers_merged(self):
        plex_s = {'type': 'movie', 'ratingKey': '5', 'Media': [{'Part': [{'file': '/m.mkv'}]}]}
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get), \
                mock.patch.object(ms.np, 'plex_sessions_checked', return_value=([plex_s], '')):
            got, err = ms.sessions_checked(settings(plex=True))
        self.assertEqual(err, '')
        self.assertEqual([x['ratingKey'] for x in got], ['5', 'jf:e1'])
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get), \
                mock.patch.object(ms.np, 'plex_sessions_checked', return_value=([], 'timeout')):
            got, err = ms.sessions_checked(settings(plex=True))
        self.assertEqual(err, 'Plex: timeout')  # one server down: protection off
        self.assertEqual(len(got), 1)


class FakeClient:
    mount_path = '/mnt/clid'

    def __init__(self, streams):
        self.streams, self.posted, self.calls = streams, [], []

    def get_streams(self):
        return self.streams

    def set_protected(self, files, ok, error=''):
        self.posted.append((files, ok, error))
        return True

    def _job_by_folder(self, folder):
        return {'id': 'job-' + folder}

    def find_file(self, names):
        return None

    def prewarm(self, job_id, file, n):
        self.calls.append((job_id, file, n))
        return True


STREAMS = [{'job_id': 'j1', 'folder': 'Show.S01E01.1080p-GRP', 'file': 'Show.S01E01.1080p-GRP.mkv'},
           {'job_id': 'j2', 'folder': 'Other', 'file': 'other.mkv'}]


class MountFeatureTests(unittest.TestCase):
    def setUp(self):
        mp._protect_state['last'] = None
        mp._done.clear()

    def test_stream_protection_from_jellyfin(self):
        c = FakeClient(STREAMS)
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get):
            mp.protect_once(c, settings())
        self.assertEqual(c.posted, [([{'job_id': 'j1', 'file': 'Show.S01E01.1080p-GRP.mkv'}], True, '')])

    def test_show_precache_from_jellyfin(self):
        c = FakeClient(STREAMS)
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get):
            got = mp.check_once(c, settings())
        self.assertEqual(got, [('job-Show.S01E02.1080p-GRP', 'Show.S01E02.1080p-GRP.mkv', 256 << 20)])
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get):
            self.assertEqual(mp.check_once(c, settings()), [])  # once per episode

    def test_now_playing_from_jellyfin(self):
        with mock.patch.object(ms.requests, 'get', side_effect=fake_get):
            out = np.now_playing(ms.sessions(settings()), STREAMS, '/mnt/clid', ms.lookup(settings()))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]['user'], out[0]['player'], out[0]['progress'], out[0]['decision']),
                         ('alice', 'Firefox', 80.0, 'direct play'))


if __name__ == '__main__':
    unittest.main()
