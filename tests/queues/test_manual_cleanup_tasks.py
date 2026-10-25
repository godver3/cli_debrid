"""Debug -> Run task manually: purge the not-wanted lists, reset Trakt auth.

Only magnets could be purged from there; URLs and the two NZB stores needed
the not-wanted page or deleting files by hand, and a stuck Trakt login meant
deleting .pytrakt.json and restarting.
"""
import importlib.util
import json
import os
import pickle
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PurgeNotWantedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir, ignore_errors=True))
        self.nw = _load('not_wanted_magnets_under_test', 'database/not_wanted_magnets.py')
        self.files = {}
        for const, entries in (('NOT_WANTED_MAGNETS_FILE', {'a' * 40, 'b' * 40}),
                               ('NOT_WANTED_URLS_FILE', {'https://x/1.torrent'}),
                               ('NOT_WANTED_NZB_SEGMENTS_FILE', {'seg1@news', 'seg2@news', 'seg3@news'}),
                               ('NOT_WANTED_NZB_GUIDS_FILE', {'guid-1'})):
            path = os.path.join(self.dir, const.lower() + '.pkl')
            with open(path, 'wb') as f:
                pickle.dump(entries, f)
            patcher = mock.patch.object(self.nw, const, path)
            patcher.start()
            self.addCleanup(patcher.stop)
            self.files[const] = path

    def _count(self, const):
        with open(self.files[const], 'rb') as f:
            return len(pickle.load(f))

    def test_urls_only(self):
        self.assertEqual(self.nw.purge_not_wanted_urls_file(), 1)
        self.assertEqual(self._count('NOT_WANTED_URLS_FILE'), 0)
        self.assertEqual(self._count('NOT_WANTED_MAGNETS_FILE'), 2)

    def test_nzbs_clear_segments_and_guids(self):
        self.assertEqual(self.nw.purge_not_wanted_nzbs_files(), 4)
        self.assertEqual(self._count('NOT_WANTED_NZB_SEGMENTS_FILE'), 0)
        self.assertEqual(self._count('NOT_WANTED_NZB_GUIDS_FILE'), 0)
        self.assertEqual(self._count('NOT_WANTED_URLS_FILE'), 1)

    def test_all(self):
        self.assertEqual(self.nw.purge_all_not_wanted_files(), {'magnets': 2, 'urls': 1, 'nzbs': 4})
        for const in self.files:
            self.assertEqual(self._count(const), 0, const)

    def test_cached_list_sees_the_purge(self):
        self.assertEqual(len(self.nw.load_not_wanted_magnets()), 2)  # now cached
        self.nw.purge_not_wanted_magnets_file()
        self.assertEqual(self.nw.load_not_wanted_magnets(), set())


class ResetTraktAuthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir, ignore_errors=True))
        self.settings = {'client_id': 'cid', 'client_secret': 'secret', 'access_token': 'tok',
                         'refresh_token': 'ref', 'expires_at': '2026-11-01', 'last_refresh': 'x'}
        fake_settings = types.SimpleNamespace(
            get_setting=lambda section, key, default=None: self.settings.get(key, default),
            set_setting=lambda section, key, value: self.settings.__setitem__(key, value))
        self.core = types.SimpleNamespace(CLIENT_ID='cid', CLIENT_SECRET='secret', OAUTH_TOKEN='tok',
                                          OAUTH_REFRESH='ref', OAUTH_EXPIRES_AT=1, OAUTH_TOKEN_VALID=True)
        patcher = mock.patch.dict(sys.modules, {'utilities.settings': fake_settings, 'trakt.core': self.core})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tc = _load('trakt_auth_cleanup_under_test', 'utilities/trakt_auth_cleanup.py')
        self.path = os.path.join(self.dir, '.pytrakt.json')

    def _legacy(self):
        with open(self.path) as f:
            return json.load(f)

    def test_tokens_cleared_everywhere_client_kept(self):
        with open(self.path, 'w') as f:
            json.dump({'CLIENT_ID': 'cid', 'CLIENT_SECRET': 'secret', 'OAUTH_TOKEN': 'tok',
                       'OAUTH_REFRESH': 'ref', 'OAUTH_EXPIRES_AT': 1, 'LAST_REFRESH': 'x'}, f)
        result = self.tc.reset_trakt_auth(self.dir)
        self.assertEqual(sorted(result['config_keys_cleared']),
                         ['access_token', 'expires_at', 'last_refresh', 'refresh_token'])
        self.assertEqual((self.settings['client_id'], self.settings['client_secret']), ('cid', 'secret'))
        self.assertEqual(self.settings['access_token'], '')
        legacy = self._legacy()
        self.assertEqual((legacy['CLIENT_ID'], legacy['CLIENT_SECRET'], legacy['OAUTH_TOKEN'], legacy['OAUTH_REFRESH']),
                         ('cid', 'secret', '', ''))
        with open(result['legacy_backup']) as f:
            self.assertEqual(json.load(f)['OAUTH_TOKEN'], 'tok')

    def test_corrupt_file_replaced(self):
        with open(self.path, 'w') as f:
            f.write('{not json')
        result = self.tc.reset_trakt_auth(self.dir)
        self.assertTrue(result['legacy_written'])
        self.assertEqual(self._legacy()['OAUTH_TOKEN'], '')
        self.assertTrue(os.path.exists(self.path + '.bak'))

    def test_missing_file_written(self):
        result = self.tc.reset_trakt_auth(self.dir)
        self.assertIsNone(result['legacy_backup'])
        self.assertEqual(self._legacy()['CLIENT_ID'], 'cid')

    def test_trakt_library_rereads_on_next_use(self):
        # pytrakt only reloads its file while CLIENT_ID/CLIENT_SECRET are unset
        self.tc.reset_trakt_auth(self.dir)
        for name in ('CLIENT_ID', 'CLIENT_SECRET', 'OAUTH_TOKEN', 'OAUTH_REFRESH', 'OAUTH_EXPIRES_AT', 'OAUTH_TOKEN_VALID'):
            self.assertIsNone(getattr(self.core, name), name)


class ManualTaskListTests(unittest.TestCase):
    def test_tasks_exist_on_the_runner(self):
        source = open(os.path.join(ROOT, 'queues/run_program.py')).read()
        listed = open(os.path.join(ROOT, 'routes/debug_routes.py')).read()
        for task in ('task_purge_not_wanted_urls_file', 'task_purge_not_wanted_nzbs_files',
                     'task_purge_all_not_wanted_files', 'task_reset_trakt_auth'):
            self.assertIn(f'def {task}(self)', source)
            self.assertIn(f"'id': '{task}'", listed)


if __name__ == '__main__':
    unittest.main()
