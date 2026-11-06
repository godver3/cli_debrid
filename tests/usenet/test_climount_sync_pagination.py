import sys
import types
import unittest
from unittest import mock

from usenet import climount_sync


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    base_url = 'http://cli-mount:8282'

    @staticmethod
    def is_enabled():
        return True

    @staticmethod
    def _headers():
        return {'Authorization': 'Bearer test'}


class ClimountSyncPaginationTests(unittest.TestCase):
    def _modules(self, get):
        client_module = types.ModuleType('usenet.climount_client')
        client_module.get_climount_client = lambda: _Client()
        tracker_module = types.ModuleType('routes.api_tracker')
        tracker_module.api = types.SimpleNamespace(get=get)
        return mock.patch.dict(
            sys.modules,
            {
                'usenet.climount_client': client_module,
                'routes.api_tracker': tracker_module,
            },
        )

    def test_fetches_every_paged_response(self):
        calls = []
        payloads = [
            {'changes': [{'info_hash': 'a'}], 'has_more': True},
            {'changes': [{'info_hash': 'b'}], 'has_more': False},
        ]

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(payloads[len(calls) - 1])

        with self._modules(get):
            result = climount_sync._fetch_changes(123)

        self.assertEqual([item['info_hash'] for item in result], ['a', 'b'])
        self.assertEqual([call[1]['params']['page'] for call in calls], [1, 2])
        self.assertTrue(all(call[1]['params']['since'] == 123 for call in calls))
        self.assertTrue(all(call[1]['params']['limit'] == 250 for call in calls))

    def test_legacy_bare_list_is_requested_once(self):
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return _Response([{'info_hash': 'legacy'}])

        with self._modules(get):
            result = climount_sync._fetch_changes(0)

        self.assertEqual(result, [{'info_hash': 'legacy'}])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('since', calls[0][1]['params'])

    def test_failed_later_page_is_not_marked_complete(self):
        responses = [
            _Response({'changes': [{'info_hash': 'a'}], 'has_more': True}),
            _Response({}, status_code=503),
        ]

        def get(url, **kwargs):
            return responses.pop(0)

        state = {}
        with self._modules(get):
            pages = list(climount_sync._iter_change_pages(123, state))

        self.assertEqual(pages, [[{'info_hash': 'a'}]])
        self.assertFalse(state['complete'])


if __name__ == '__main__':
    unittest.main()
