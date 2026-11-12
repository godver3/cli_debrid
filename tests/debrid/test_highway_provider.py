"""HighWay provider against a fake transfer API.

HighWay links known torrents at once and queues the rest under a 32-hex queue id
that disappears once the torrent starts (it then gets a numeric id). The provider
hands out "<queue id>-<hash>" for queued torrents so it can still find them.
"""

import asyncio
import unittest
from unittest import mock

import database  # noqa: F401 - app import order; debrid alone hits a routes<->debrid cycle

from debrid.highway import api as hw_api
from debrid.highway.client import HighwayProvider
from debrid.highway.exceptions import HighwayAPIError

CACHED = 'a' * 40
UNCACHED = 'b' * 40
QUEUE_ID = 'c' * 32


def _transfer(transfer_id, hash_value, state, name='Show.S01'):
    return {
        'id': transfer_id, 'service': 'torrent', 'name': name, 'state': state, 'progress': 100 if state == 'completed' else 0,
        'size': 2000, 'hash': hash_value, 'added_at': 1762900000,
        'files': [
            {'id': 'f1', 'index': 0, 'path': f'{name}/Show.S01E01.mkv', 'size': 1000, 'partial': False, 'download_url': 'https://dl/1'},
            {'id': 'f2', 'index': 1, 'path': f'{name}/Show.S01E02.mkv', 'size': 1000, 'partial': False, 'download_url': 'https://dl/2'},
        ],
    }


class FakeHighwayApi:
    """Answers make_request like the transfer API."""

    def __init__(self):
        self.transfers = {'11': _transfer('11', CACHED, 'completed')}
        self.queue = {}
        self.calls = []

    def __call__(self, method, api_key, params=None, data=None, files=None, timeout=30):
        params = params or {}
        self.calls.append((method, dict(params), dict(data or {})))
        if method == 'GET' and 'check' in params:
            results = {}
            for h in params['check'].split(','):
                owned = next((t['id'] for t in self.transfers.values() if t['hash'] == h), None)
                results[h] = {'known': h == CACHED, 'ready': h == CACHED, 'id': owned, 'name': None, 'size': 0, 'progress': 0}
            return {'service': 'torrent', 'results': results}
        if method == 'GET' and 'id' in params:
            if params['id'] in self.transfers:
                return self.transfers[params['id']]
            raise HighwayAPIError('HighWay request failed: 404 not_found: Transfer not found')
        if method == 'GET' and params.get('stats') == '1':
            return {'username': 'tester', 'user_id': 5, 'plan': 'Pro', 'premium': True, 'plan_expires_at': None,
                    'transfers': {'active': 2, 'limit': 30, 'available': 28}}
        if method == 'GET':
            items = list(self.transfers.values())
            items += [{'id': q, 'service': 'torrent', 'name': 'Queued', 'state': 'queued', 'progress': 0, 'size': 0, 'hash': None}
                      for q in self.queue]
            return {'transfers': items, 'total': len(items)}
        if method == 'POST':
            magnet = data['magnet']
            if CACHED in magnet:
                self.transfers.setdefault('11', _transfer('11', CACHED, 'completed'))
                return {'id': '11', 'service': 'torrent', 'state': 'completed'}
            self.queue[QUEUE_ID] = UNCACHED
            return {'id': QUEUE_ID, 'service': 'torrent', 'state': 'queued'}
        if method == 'DELETE':
            if self.transfers.pop(params['id'], None) is not None:
                return {'removed': True}
            return {'removed': False, 'reason': 'not_found'}
        raise AssertionError(f'unexpected call {method} {params}')


def _provider():
    p = HighwayProvider.__new__(HighwayProvider)
    p._status = {}
    p._cached_torrent_ids, p._cached_torrent_titles = {}, {}
    p._api_key = 'token'
    p._setup_encryption()
    return p


class HighwayProviderTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeHighwayApi()
        patcher = mock.patch.object(hw_api, 'make_request', self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = _provider()

    def test_cache_check_is_one_request_and_adds_nothing(self):
        result = asyncio.run(self.provider.is_cached([f'magnet:?xt=urn:btih:{CACHED}', UNCACHED]))
        self.assertEqual(result, {CACHED: True, UNCACHED: False})
        self.assertEqual([c[0] for c in self.api.calls], ['GET'])

    def test_cached_torrent_is_ready_at_once(self):
        torrent_id = self.provider.add_torrent(f'magnet:?xt=urn:btih:{CACHED}')
        self.assertEqual(torrent_id, '11')
        info = self.provider.get_torrent_info(torrent_id)
        self.assertEqual(info['status'], 'downloaded')
        self.assertEqual(info['debrid_folder_name'], 'Show.S01')
        self.assertEqual([f['path'] for f in info['files']], ['Show.S01/Show.S01E01.mkv', 'Show.S01/Show.S01E02.mkv'])
        self.assertEqual(self.provider.get_cached_torrent_id(CACHED), '11')

    def test_queued_torrent_is_found_by_hash_after_it_started(self):
        torrent_id = self.provider.add_torrent(f'magnet:?xt=urn:btih:{UNCACHED}')
        self.assertEqual(torrent_id, f'{QUEUE_ID}-{UNCACHED}')

        # The queue id is gone, the torrent now has a numeric id
        self.api.queue.clear()
        self.api.transfers['12'] = _transfer('12', UNCACHED, 'downloading', name='Other')
        info = self.provider.get_torrent_info(torrent_id)
        self.assertEqual(info['id'], '12')
        self.assertEqual(info['status'], 'downloading')

        self.assertTrue(self.provider.remove_torrent(torrent_id))
        self.assertNotIn('12', self.api.transfers)

    def test_unknown_torrent_reports_not_found(self):
        status = self.provider.get_torrent_info_with_status('999')
        self.assertEqual(status.http_status_code, 404)

    def test_file_list_keeps_a_torrent_the_account_already_has(self):
        files, name, torrent_id = self.provider.get_torrent_file_list(f'magnet:?xt=urn:btih:{CACHED}')
        self.assertEqual((name, torrent_id, len(files)), ('Show.S01', '11', 2))
        self.assertIn('11', self.api.transfers)
        self.assertNotIn('DELETE', [c[0] for c in self.api.calls])

    def test_file_list_removes_a_temporary_add(self):
        del self.api.transfers['11']
        files, name, torrent_id = self.provider.get_torrent_file_list(f'magnet:?xt=urn:btih:{CACHED}')
        self.assertEqual(len(files), 2)
        self.assertNotIn('11', self.api.transfers)

    def test_account_and_slots(self):
        self.assertEqual(self.provider.get_active_downloads(), (2, 30))
        status = self.provider.get_subscription_status()
        self.assertEqual((status['username'], status['premium'], status['type']), ('tester', True, 'Pro'))

    def test_capabilities(self):
        self.assertTrue(self.provider.supports_direct_cache_check)
        self.assertTrue(self.provider.supports_bulk_cache_checking)
        self.assertTrue(self.provider.supports_uncached)


if __name__ == '__main__':
    unittest.main()
