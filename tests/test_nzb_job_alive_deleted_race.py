#!/usr/bin/env python3
"""
Unit tests for is_nzb_job_alive()'s handling of a job cli_debrid itself just
deleted.

Real incident: a dead NZB job (folder never appeared) was declared broken,
deleted via remove_nzb(), and then re-checked via is_nzb_job_alive() a
couple seconds later as part of a reuse attempt. The provider's delete
returning success didn't guarantee its other read endpoints reflected the
removal yet, so the fresh liveness check still reported it alive, and the
same dead job got reused (and re-declared broken, and re-deleted...) over a
dozen times before the reuse loop finally gave up - burning ~20 minutes and
ultimately blacklisting an item that had a perfectly good release available.

These tests confirm: (1) a job we just deleted is reported dead immediately,
with no provider round-trip at all, and (2) an unrelated job's liveness is
still correctly checked (and cached) against the provider as before.
"""

import importlib.util
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    """Load climount_client.py fresh, with routes.api_tracker.api stubbed
    so get() calls are observable and delete() always succeeds (200).
    """
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: {'enabled': True, 'url': 'http://x:8383', 'api_token': 't'}
    sys.modules['utilities.settings'] = uss

    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')

    # climount_client.py lazily imports debrid.common.cache.timed_lru_cache.
    # Importing that submodule normally executes the *parent* debrid package's
    # __init__.py too, which pulls in every real provider class and their own
    # heavy imports - unnecessary for these tests and easy to break in an
    # isolated sandbox. Load the real cache module directly by file path
    # instead, bypassing the package hierarchy, so the actual timed_lru_cache
    # implementation is exercised without dragging that in.
    if 'debrid' not in sys.modules:
        sys.modules['debrid'] = types.ModuleType('debrid')
    if 'debrid.common' not in sys.modules:
        sys.modules['debrid.common'] = types.ModuleType('debrid.common')
    if 'debrid.common.cache' not in sys.modules:
        cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'debrid', 'common', 'cache.py')
        cache_spec = importlib.util.spec_from_file_location('debrid.common.cache', cache_path)
        cache_mod = importlib.util.module_from_spec(cache_spec)
        cache_spec.loader.exec_module(cache_mod)
        sys.modules['debrid.common.cache'] = cache_mod

    get_calls = []

    class _FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_delete(url, **kwargs):
        return _FakeResponse(200)

    def fake_get(url, **kwargs):
        get_calls.append((url, kwargs.get('params')))
        # Provider still lists the job as alive/downloading (simulates the
        # post-delete eventual-consistency gap).
        job_id = (kwargs.get('params') or {}).get('search', '')
        return _FakeResponse(200, {'torrents': [
            {'info_hash': job_id, 'state': 'downloading', 'progress': 0.5, 'name': 'still-here'}
        ]})

    rta.api = types.SimpleNamespace(delete=fake_delete, get=fake_get, post=None)
    sys.modules['routes.api_tracker'] = rta

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'usenet', 'climount_client.py')
    spec = importlib.util.spec_from_file_location('climount_client_alive_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, get_calls


class TestRecentlyDeletedOverride(unittest.TestCase):
    def test_job_we_just_deleted_is_reported_dead_without_provider_query(self):
        m, get_calls = _load()
        client = m.CliMountClient()

        self.assertTrue(client.remove_nzb('dead-hash'))
        # Provider would still say "alive" (see fake_get) if actually queried.
        self.assertFalse(m.is_nzb_job_alive('dead-hash'))
        # The whole point: no round-trip needed once we know we deleted it.
        self.assertEqual(get_calls, [])

    def test_remove_nzb_exact_also_marks_deleted(self):
        m, get_calls = _load()
        client = m.CliMountClient()

        self.assertTrue(client.remove_nzb_exact('dead-hash-2'))
        self.assertFalse(m.is_nzb_job_alive('dead-hash-2'))
        self.assertEqual(get_calls, [])

    def test_unrelated_job_still_checked_against_provider(self):
        m, get_calls = _load()
        client = m.CliMountClient()
        client.remove_nzb('dead-hash')

        # A different, never-deleted hash must still be checked for real.
        self.assertTrue(m.is_nzb_job_alive('live-hash'))
        self.assertEqual(len(get_calls), 1)

    def test_liveness_cache_persists_across_calls(self):
        # Regression guard for the fix's other half: the cache decorator
        # used to wrap a function rebuilt fresh on every call, so it never
        # actually cached anything across separate is_nzb_job_alive() calls.
        m, get_calls = _load()
        m.is_nzb_job_alive('live-hash')
        m.is_nzb_job_alive('live-hash')
        m.is_nzb_job_alive('live-hash')
        self.assertEqual(len(get_calls), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
