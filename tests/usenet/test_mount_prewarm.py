#!/usr/bin/env python3
"""Plex next-episode prewarm (utilities/mount_prewarm.py)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import mount_prewarm as mp

EPISODES = [{'ratingKey': str(10 + i), 'parentIndex': s, 'index': e, 'grandparentTitle': 'Show'}
            for i, (s, e) in enumerate([(0, 1), (1, 1), (1, 2), (2, 1)])]
NEXT_ITEM = {'Media': [{'Part': [{'file': '/data/__all__/Show.S01E02.1080p/Show.S01E02.1080p.mkv', 'size': 2_000_000_000}]}]}


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return {'MediaContainer': {'Metadata': self.body}}


def session(offset):
    return {'type': 'episode', 'ratingKey': '11', 'grandparentRatingKey': '9', 'parentIndex': 1, 'index': 1,
            'duration': 1000, 'viewOffset': offset,
            'Media': [{'Part': [{'file': '/data/__all__/Show.S01E01.1080p/Show.S01E01.1080p.mkv'}]}]}


class FakeClient:
    mount_path = '/mnt/clid'

    def __init__(self):
        self.calls = []

    def get_streams(self):
        return [{'folder': 'Show.S01E01.1080p', 'file': 'Show.S01E01.1080p.mkv'}]

    def _job_by_folder(self, folder):
        return {'id': 'job-' + folder}

    def prewarm(self, job_id, file, n):
        self.calls.append((job_id, file, n))
        return True


def settings(*a, **k):
    if a == ('Built-in Mount',):
        return {}
    if a[0] == 'Plex':
        return {'url': 'http://plex', 'token': 't'}.get(a[1], '')
    return ''


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        mp._done.clear()

    def test_next_episode_skips_specials_and_crosses_seasons(self):
        with mock.patch.object(mp.requests, 'get', return_value=_Resp(EPISODES)):
            self.assertEqual(mp.next_episode('u', 't', session(0))['ratingKey'], '12')
            last = dict(session(0), parentIndex=1, index=2)
            self.assertEqual(mp.next_episode('u', 't', last)['ratingKey'], '13')
            self.assertIsNone(mp.next_episode('u', 't', dict(session(0), parentIndex=2, index=1)))

    def test_mount_target(self):
        self.assertEqual(mp.mount_target('/mnt/clid/__all__/F/f.mkv', '/mnt/clid'), ('F', 'f.mkv'))
        self.assertEqual(mp.mount_target('/plexsees/__all__/F/f.mkv', '/mnt/clid'), ('F', 'f.mkv'))
        self.assertIsNone(mp.mount_target('/media/disk/f.mkv', '/mnt/clid'))

    def _run(self, offset):
        client = FakeClient()

        def get(url, **kw):
            if url.endswith('/status/sessions'):
                return _Resp([session(offset)])
            if url.endswith('/allLeaves'):
                return _Resp(EPISODES)
            return _Resp([NEXT_ITEM])
        with mock.patch.object(mp.requests, 'get', side_effect=get), \
                mock.patch.object(mp.np.requests, 'get', side_effect=get):
            return client, mp.check_once(client, settings)

    def test_prewarms_next_episode_once_past_the_trigger(self):
        client, out = self._run(750)
        self.assertEqual(out, [('Show.S01E02.1080p', 'Show.S01E02.1080p.mkv', 256 << 20)], '20% capped at 256 MB')
        self.assertEqual(client.calls[0][0], 'job-Show.S01E02.1080p')
        _, again = self._run(800)
        self.assertEqual(again, [], 'remembered: not prewarmed twice')

    def test_not_before_the_trigger(self):
        self.assertEqual(self._run(500)[1], [])



class ProtectClient(FakeClient):
    def __init__(self, streams):
        super().__init__()
        self.streams = streams
        self.posted = []

    def get_streams(self):
        return self.streams

    def set_protected(self, files, ok, error=''):
        self.posted.append((files, ok, error))
        return True


class PlexProtectionTests(unittest.TestCase):
    STREAMS = [{'job_id': 'j1', 'folder': 'Show.S01E01.1080p', 'file': 'Show.S01E01.1080p.mkv'},
               {'job_id': 'j2', 'folder': 'Other', 'file': 'other.mkv'}]  # e.g. the Plex scanner

    def setUp(self):
        mp._protect_state['last'] = None

    def test_only_files_plex_plays_are_protected(self):
        c = ProtectClient(self.STREAMS)
        with mock.patch.object(mp.np, 'plex_sessions_checked', return_value=([session(100)], '')):
            mp.protect_once(c, settings)
        self.assertEqual(c.posted, [([{'job_id': 'j1', 'file': 'Show.S01E01.1080p.mkv'}], True, '')])

    def test_failed_plex_poll_is_posted_as_failed(self):
        c = ProtectClient(self.STREAMS)
        with mock.patch.object(mp.np, 'plex_sessions_checked', return_value=([], 'timeout')), \
             self.assertLogs(level='WARNING'):
            mp.protect_once(c, settings)
        self.assertEqual(c.posted, [([], False, 'timeout')])

    def test_nothing_open_skips_plex(self):
        c = ProtectClient([])
        with mock.patch.object(mp.np, 'plex_sessions_checked') as plex:
            mp.protect_once(c, settings)
        plex.assert_not_called()
        self.assertEqual(c.posted, [([], True, '')])

    def test_disabled_posts_nothing(self):
        c = ProtectClient(self.STREAMS)
        off = lambda *a, **k: {'plex_playback_protection': False} if a == ('Built-in Mount',) else settings(*a, **k)
        self.assertIsNone(mp.protect_once(c, off))
        self.assertEqual(c.posted, [])


if __name__ == '__main__':
    unittest.main()
