#!/usr/bin/env python3
"""
Unit tests for CliMountClient.remove_nzb_exact / remove_nzb 404 handling.

routes.api_tracker.api.delete() calls response.raise_for_status(), so an
error response (4xx/5xx) surfaces as a raised exception carrying a
`.response` with the real status code, not a returned Response object.
These tests simulate that exactly and confirm a 404 ("already gone") is
treated as success rather than falling through to the generic-failure path.
"""

import importlib.util
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeHTTPError(Exception):
    def __init__(self, status_code):
        super().__init__(f'{status_code} Client Error')
        self.response = _FakeResponse(status_code)


def _load(status_map, connection_error_urls=frozenset()):
    """Load climount_client.py fresh with routes.api_tracker.api.delete()
    stubbed to mimic raise_for_status(): a mapped status >=400 raises
    _FakeHTTPError, anything else returns a Response with that status.
    """
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: {'enabled': True, 'url': 'http://x:8383', 'api_token': 't'}
    sys.modules['utilities.settings'] = uss

    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')
    calls = []

    def fake_delete(url, **kwargs):
        calls.append(url)
        if url in connection_error_urls:
            raise ConnectionError('connection refused')
        status = status_map.get(url, 200)
        if status >= 400:
            raise _FakeHTTPError(status)
        return _FakeResponse(status)

    rta.api = types.SimpleNamespace(delete=fake_delete, get=None, post=None)
    sys.modules['routes.api_tracker'] = rta

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'usenet', 'climount_client.py')
    spec = importlib.util.spec_from_file_location('climount_client_remove_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, calls


class TestRemoveNzbExact(unittest.TestCase):
    def test_404_is_treated_as_already_removed(self):
        url = 'http://x:8383/api/browse/torrents/stale-hash'
        m, calls = _load(status_map={url: 404})
        client = m.CliMountClient()
        self.assertTrue(client.remove_nzb_exact('stale-hash'))
        self.assertEqual(calls, [url])

    def test_200_is_success(self):
        url = 'http://x:8383/api/browse/torrents/live-hash'
        m, _ = _load(status_map={url: 200})
        client = m.CliMountClient()
        self.assertTrue(client.remove_nzb_exact('live-hash'))

    def test_500_is_failure(self):
        url = 'http://x:8383/api/browse/torrents/broken-hash'
        m, _ = _load(status_map={url: 500})
        client = m.CliMountClient()
        self.assertFalse(client.remove_nzb_exact('broken-hash'))

    def test_connection_error_is_failure(self):
        url = 'http://x:8383/api/browse/torrents/unreachable-hash'
        m, _ = _load(status_map={}, connection_error_urls={url})
        client = m.CliMountClient()
        self.assertFalse(client.remove_nzb_exact('unreachable-hash'))


class TestRemoveNzb(unittest.TestCase):
    def test_primary_404_is_treated_as_already_gone(self):
        url = 'http://x:8383/api/browse/torrents/stale-hash'
        m, calls = _load(status_map={url: 404})
        client = m.CliMountClient()
        self.assertTrue(client.remove_nzb('stale-hash'))
        # Only the primary browse-delete should fire — no fallback needed.
        self.assertEqual(calls, [url])

    def test_primary_500_falls_back_to_queue_delete(self):
        primary = 'http://x:8383/api/browse/torrents/broken-hash'
        fallback = 'http://x:8383/api/torrents'
        m, calls = _load(status_map={primary: 500, fallback: 200})
        client = m.CliMountClient()
        self.assertTrue(client.remove_nzb('broken-hash'))
        self.assertEqual(calls, [primary, fallback])


if __name__ == '__main__':
    unittest.main(verbosity=2)
