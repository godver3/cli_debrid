"""Real-Debrid 451 (legal block) memory.

A 451 hash stays off the install-wide not-wanted list (another provider may
serve it), but each Adding attempt re-sent every blocked magnet to RD, about
40 addMagnet calls per attempt, until RD answered 429 (By Any Means, 2026-10-27).
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  (app import order)
from debrid.base import ProviderUnavailableError
from debrid.real_debrid import blocked_hashes
from debrid.real_debrid.client import RealDebridProvider

HASH = 'a' * 40
MAGNET = f'magnet:?xt=urn:btih:{HASH}'


class RdBlockedHashMemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {'USER_DB_CONTENT': self._tmp.name})
        self._env.start()
        blocked_hashes._reset_for_tests()
        self.provider = RealDebridProvider.__new__(RealDebridProvider)
        self.provider._load_api_key = lambda: 'k'

    def tearDown(self):
        blocked_hashes._reset_for_tests()
        self._env.stop()
        self._tmp.cleanup()

    def _add(self):
        return self.provider.add_torrent(MAGNET)

    def test_second_add_of_blocked_hash_makes_no_request(self):
        err = ProviderUnavailableError('Request failed: 451 Client Error: Unavailable For Legal Reasons')
        with mock.patch('debrid.real_debrid.client.make_request', side_effect=err) as req:
            with self.assertRaises(ProviderUnavailableError):
                self._add()
            with self.assertRaises(ProviderUnavailableError) as ctx:
                self._add()
        self.assertEqual(req.call_count, 1)
        self.assertIn('451', str(ctx.exception))  # callers still see a 451 provider fault

    def test_memory_survives_restart_and_expires(self):
        blocked_hashes.mark_blocked(HASH.upper())
        blocked_hashes._reset_for_tests()  # restart: reload from disk
        self.assertTrue(blocked_hashes.is_blocked(HASH))
        with mock.patch('time.time', return_value=time.time() + blocked_hashes.TTL_SECONDS + 1):
            self.assertFalse(blocked_hashes.is_blocked(HASH))

    def test_other_errors_are_not_remembered(self):
        with mock.patch('debrid.real_debrid.client.make_request',
                        side_effect=ProviderUnavailableError('Request failed: 503')):
            with self.assertRaises(ProviderUnavailableError):
                self._add()
        self.assertFalse(blocked_hashes.is_blocked(HASH))


if __name__ == '__main__':
    unittest.main()
