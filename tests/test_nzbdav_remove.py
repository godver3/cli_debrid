#!/usr/bin/env python3
"""
Unit tests for NzbdavClient.remove_nzb's id-then-exact-name deletion logic.

Exercises the real class with the two cli-debrid imports stubbed. The HTTP layer
(_raw_delete_by_id) and history fetch (_history_slots) are monkeypatched per test
so the ORCHESTRATION is what's under test: delete-by-id first, then an exact
full-name fallback for items whose stored id is no longer a live nzo_id (e.g.
migrated between providers), with a hard refusal on ambiguous names.
"""

import unittest
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    # Return an enabled config so the client is "live".
    uss.get_setting = lambda *a, **k: {'enabled': True, 'url': 'http://x:3000', 'api_token': 't'}
    sys.modules['utilities.settings'] = uss
    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')
    rta.api = types.SimpleNamespace(get=None, post=None)
    sys.modules['routes.api_tracker'] = rta
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'nzbdav_client.py')
    spec = importlib.util.spec_from_file_location('nzbdav_client_remove_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


nc = _load()

HISTORY = [
    {'nzo_id': 'live-1', 'name': 'Movie.A.2020.1080p.BluRay.x264-GRP'},
    {'nzo_id': 'live-2', 'name': 'Movie.B.2021.2160p.BluRay.x265-GRP'},
    {'nzo_id': 'dup-1',  'name': 'Dup.Title.2019.1080p-GRP'},
    {'nzo_id': 'dup-2',  'name': 'Dup.Title.2019.1080p-GRP'},   # same normalised name
]


def _client(deletable):
    c = nc.NzbdavClient()
    c.enabled = True
    c.base_url = 'http://x:3000'
    c._history_slots = lambda *a, **k: list(HISTORY)
    calls = []
    def fake_delete(i):
        calls.append(i)
        return i in deletable
    c._raw_delete_by_id = fake_delete
    c._delete_calls = calls
    return c


class TestReleaseNameKey(unittest.TestCase):
    def test_strips_ext_and_punct(self):
        k = nc.NzbdavClient._release_name_key
        self.assertEqual(k('Movie.A.2020.1080p.x264-GRP.mkv'), k('Movie.A.2020.1080p.x264-GRP'))
        self.assertEqual(k('a/b/Some Name-GRP'), 'somenamegrp')

    def test_distinguishes_resolution(self):
        k = nc.NzbdavClient._release_name_key
        self.assertNotEqual(k('Movie.2020.1080p.x264-GRP'), k('Movie.2020.2160p.x265-GRP'))


class TestRemoveNzb(unittest.TestCase):
    def test_native_id_deletes(self):
        c = _client(deletable={'live-1'})
        self.assertTrue(c.remove_nzb('live-1', 'whatever'))
        self.assertEqual(c._delete_calls, ['live-1'])

    def test_id_present_but_delete_fails_does_not_guess(self):
        # live-2 is in history but not deletable -> hard failure, NO name fallback.
        c = _client(deletable=set())
        self.assertFalse(c.remove_nzb('live-2', 'Movie.B.2021.2160p.BluRay.x265-GRP'))
        self.assertEqual(c._delete_calls, ['live-2'])  # never tried a second id

    def test_migrated_stale_id_unique_name_fallback(self):
        # stale id not in history -> fallback resolves the unique name to live-1.
        c = _client(deletable={'live-1'})
        self.assertTrue(c.remove_nzb('nzb-stale-uuid', 'Movie.A.2020.1080p.BluRay.x264-GRP'))
        self.assertEqual(c._delete_calls, ['nzb-stale-uuid', 'live-1'])

    def test_fallback_matches_on_full_name_not_substring(self):
        # The 2160p title must resolve ONLY to its own slot, never the 1080p one.
        c = _client(deletable={'live-2'})
        self.assertTrue(c.remove_nzb('stale', 'Movie.B.2021.2160p.BluRay.x265-GRP'))
        self.assertEqual(c._delete_calls, ['stale', 'live-2'])

    def test_ambiguous_name_refuses(self):
        c = _client(deletable={'dup-1', 'dup-2'})
        self.assertFalse(c.remove_nzb('stale', 'Dup.Title.2019.1080p-GRP'))
        # only the stale-id attempt; never deletes either ambiguous match
        self.assertEqual(c._delete_calls, ['stale'])

    def test_absent_name_no_match(self):
        c = _client(deletable=set())
        self.assertFalse(c.remove_nzb('stale', 'Nothing.That.Exists-GRP'))

    def test_no_name_no_fallback(self):
        c = _client(deletable=set())
        self.assertFalse(c.remove_nzb('stale', ''))
        self.assertEqual(c._delete_calls, ['stale'])

    def test_disabled_client_returns_false(self):
        c = _client(deletable={'live-1'})
        c.enabled = False
        self.assertFalse(c.remove_nzb('live-1', 'x'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
