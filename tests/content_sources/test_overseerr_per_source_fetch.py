"""Each Overseerr source reads only its own server and applies only its own settings.

Before: every Overseerr source's run fetched requests from every enabled Overseerr source and
tagged all of them with the running source's versions, so with two different Seerr servers
server B's requests were added with server A's versions (and the reverse). The poll path also
ignored allowed_requesters, which only the webhook path honoured.
"""
import unittest
from unittest import mock

import database  # noqa: F401  (app import order; avoids a circular import)
from content_checkers import overseerr


def _request(req_id, tmdb_id, requester):
    return {
        'id': req_id,
        'media': {'mediaType': 'movie', 'tmdbId': tmdb_id},
        'requestedBy': {'displayName': requester},
        'tags': [],
    }


SERVER_A = 'http://seerr-a:5055'
SERVER_B = 'http://seerr-b:5055'
REQUESTS = {
    SERVER_A: [_request(1, 100, 'alice'), _request(2, 101, 'bob')],
    SERVER_B: [_request(3, 200, 'carol')],
}


class PerSourceFetchTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            'Overseerr_1': {'type': 'Overseerr', 'enabled': True, 'url': SERVER_A, 'api_key': 'a',
                            'versions': ['1080p'], 'allowed_requesters': ['alice']},
            'Overseerr_2': {'type': 'Overseerr', 'enabled': True, 'url': SERVER_B, 'api_key': 'b',
                            'versions': ['2160p']},
        }
        patches = [
            mock.patch.object(overseerr, 'get_all_settings',
                              return_value={'Content Sources': self.sources}),
            mock.patch.object(overseerr, 'fetch_overseerr_wanted_content',
                              side_effect=lambda url, key: list(REQUESTS[url])),
            mock.patch.object(overseerr, '_clear_overseerr_auth_failure'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _tmdb_ids(self, result):
        return sorted(item['tmdb_id'] for items, _versions in result for item in items)

    def test_source_reads_only_its_own_server(self):
        result = overseerr.get_wanted_from_overseerr(['2160p'], source_id='Overseerr_2')
        self.assertEqual(self._tmdb_ids(result), [200])
        self.assertEqual([versions for _items, versions in result], [['2160p']])
        overseerr.fetch_overseerr_wanted_content.assert_called_once_with(SERVER_B, 'b')

    def test_allowed_requesters_applied_on_poll(self):
        result = overseerr.get_wanted_from_overseerr(['1080p'], source_id='Overseerr_1')
        self.assertEqual(self._tmdb_ids(result), [100])

    def test_unknown_source_fetches_nothing(self):
        self.assertEqual(overseerr.get_wanted_from_overseerr(['1080p'], source_id='Overseerr_9'), [])
        overseerr.fetch_overseerr_wanted_content.assert_not_called()

    def test_no_source_id_keeps_reading_all_enabled_sources(self):
        result = overseerr.get_wanted_from_overseerr(['1080p'])
        self.assertEqual(self._tmdb_ids(result), [100, 200])


if __name__ == '__main__':
    unittest.main()
