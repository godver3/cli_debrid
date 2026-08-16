#!/usr/bin/env python3
"""
Unit tests for remove_from_overseerr_by_tmdb_id's request+media deletion logic.

Overseerr/Jellyseerr track a "request" and a "media" record separately — the
media record is what actually drives the "Available"/"Partially Available"
status, so deleting only the request (the old behavior) left items still
showing as available. These tests exercise the real function with the HTTP
layer stubbed, verifying both the request and the media record get deleted,
and that either one succeeding is enough to report overall success.
"""

import unittest
import sys
import os
import types


def _load(get_responses, delete_responses):
    """Load content_checkers/overseerr.py fresh with a stubbed `api` module.

    get_responses: dict endpoint-suffix -> response dict (for api.get(...).json())
    delete_responses: dict url -> (status_code, text)
    """
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: None
    uss.get_all_settings = lambda *a, **k: {}
    sys.modules['utilities.settings'] = uss

    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=''):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f'HTTP {self.status_code}')

        def json(self):
            return self._payload

    class FakeRequestException(Exception):
        pass

    def fake_get(url, headers=None, timeout=None):
        for suffix, payload in get_responses.items():
            if url.endswith(suffix):
                return FakeResponse(200, payload)
        return FakeResponse(404, {})

    def fake_delete(url, headers=None, timeout=None):
        status, text = delete_responses.get(url, (404, 'not found'))
        return FakeResponse(status, text=text)

    rta = types.ModuleType('routes.api_tracker')
    rta.api = types.SimpleNamespace(
        get=fake_get,
        delete=fake_delete,
        exceptions=types.SimpleNamespace(RequestException=FakeRequestException),
    )
    sys.modules['routes.api_tracker'] = rta

    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'content_checkers', 'overseerr.py')
    spec = importlib.util.spec_from_file_location('overseerr_removal_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


MEDIA_WITH_APPROVED_REQUEST = {
    'mediaInfo': {
        'id': 555,
        'requests': [{'id': 999, 'status': 2, 'requestedBy': {'displayName': 'x'}}],
    }
}

MEDIA_NO_REQUESTS = {
    'mediaInfo': {
        'id': 555,
        'requests': [],
    }
}

MEDIA_NOT_FOUND = {}


class TestOverseerrRemoval(unittest.TestCase):

    def test_removes_both_request_and_media(self):
        m = _load(
            get_responses={'/tv/123': MEDIA_WITH_APPROVED_REQUEST},
            delete_responses={
                'http://ov/api/v1/request/999': (204, ''),
                'http://ov/api/v1/media/555': (204, ''),
            },
        )
        result = m.remove_from_overseerr_by_tmdb_id(
            tmdb_id=123, media_type='show', overseerr_url='http://ov', api_key='k')
        self.assertTrue(result['success'])
        self.assertEqual(result['request_id'], 999)
        self.assertIn('request 999', result['message'])
        self.assertIn('media record 555', result['message'])

    def test_removes_media_only_when_no_request(self):
        m = _load(
            get_responses={'/tv/123': MEDIA_NO_REQUESTS},
            delete_responses={
                'http://ov/api/v1/media/555': (204, ''),
            },
        )
        result = m.remove_from_overseerr_by_tmdb_id(
            tmdb_id=123, media_type='show', overseerr_url='http://ov', api_key='k')
        self.assertTrue(result['success'])
        self.assertIsNone(result['request_id'])
        self.assertIn('media record 555', result['message'])

    def test_not_found_when_neither_exists(self):
        m = _load(
            get_responses={'/tv/123': MEDIA_NOT_FOUND},
            delete_responses={},
        )
        result = m.remove_from_overseerr_by_tmdb_id(
            tmdb_id=123, media_type='show', overseerr_url='http://ov', api_key='k')
        self.assertFalse(result['success'])
        self.assertTrue(result.get('not_found'))

    def test_media_delete_still_succeeds_when_request_delete_fails(self):
        # The request delete fails (e.g. request already gone), but the media
        # record still gets deleted — availability status still clears, which
        # is the actual user-visible fix, so this must still report success.
        m = _load(
            get_responses={'/tv/123': MEDIA_WITH_APPROVED_REQUEST},
            delete_responses={
                'http://ov/api/v1/request/999': (404, 'gone'),
                'http://ov/api/v1/media/555': (204, ''),
            },
        )
        result = m.remove_from_overseerr_by_tmdb_id(
            tmdb_id=123, media_type='show', overseerr_url='http://ov', api_key='k')
        self.assertTrue(result['success'])
        self.assertIn('media record 555', result['message'])

    def test_total_failure_when_both_deletes_fail(self):
        m = _load(
            get_responses={'/tv/123': MEDIA_WITH_APPROVED_REQUEST},
            delete_responses={
                'http://ov/api/v1/request/999': (500, 'boom'),
                'http://ov/api/v1/media/555': (500, 'boom'),
            },
        )
        result = m.remove_from_overseerr_by_tmdb_id(
            tmdb_id=123, media_type='show', overseerr_url='http://ov', api_key='k')
        self.assertFalse(result['success'])


if __name__ == '__main__':
    unittest.main()
