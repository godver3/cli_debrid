#!/usr/bin/env python3
"""
Contract tests for usenet/base.py and the cli_mount helpers callers now use
instead of building cli_mount URLs by hand.

- Every client implements the whole UsenetBackend method set, so a caller that
  skips a capability check never hits AttributeError.
- CliMountClient declares every capability; NzbdavClient (NzbDAV + Zurg)
  declares none and its cli_mount-only methods are inert.
- supports() probes for the method when a client declares no CAPABILITIES.
- The new CliMountClient helpers page, stop and report failures the way the
  hand-written loops they replaced did.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class _HTTPError(Exception):
    def __init__(self, response):
        super().__init__(f'HTTP {response.status_code}')
        self.response = response


class _Base(unittest.TestCase):
    def setUp(self):
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        for name in list(sys.modules):
            if name == 'usenet' or name.startswith('usenet.'):
                del sys.modules[name]
        if 'utilities' not in sys.modules:
            sys.modules['utilities'] = types.ModuleType('utilities')
        uss = types.ModuleType('utilities.settings')
        uss.get_setting = lambda key, *a, **k: (
            {'provider': 'climount', 'enabled': True, 'url': 'http://cm:8888', 'api_token': 't'}
            if key == 'Usenet Provider' else {})
        sys.modules['utilities.settings'] = uss
        if 'routes' not in sys.modules:
            sys.modules['routes'] = types.ModuleType('routes')
        rta = types.ModuleType('routes.api_tracker')
        self.api = types.SimpleNamespace(get=None, post=None, delete=None)
        rta.api = self.api
        sys.modules['routes.api_tracker'] = rta

        import usenet.base as base
        import usenet.climount_client as cc
        import usenet.nzbdav_client as nc
        self.base, self.cc, self.nc = base, cc, nc
        self.client = cc.CliMountClient.__new__(cc.CliMountClient)
        self.client.base_url = 'http://cm:8888'
        self.client._headers = lambda: {'Authorization': 'Bearer t'}


class TestContract(_Base):
    def _protocol_methods(self):
        return [n for n, v in vars(self.base.UsenetBackend).items()
                if callable(v) and not n.startswith('_')]

    def test_every_client_has_every_protocol_method(self):
        for cls in (self.cc.CliMountClient, self.nc.NzbdavClient):
            for name in self._protocol_methods():
                with self.subTest(cls=cls.__name__, method=name):
                    self.assertTrue(callable(getattr(cls, name, None)))

    def test_capabilities(self):
        cm = self.cc.CliMountClient.__new__(self.cc.CliMountClient)
        nz = self.nc.NzbdavClient.__new__(self.nc.NzbdavClient)
        for cap in self.base.ALL_CAPABILITIES:
            with self.subTest(cap=cap):
                self.assertTrue(self.base.supports(cm, cap))
                self.assertFalse(self.base.supports(nz, cap))

    def test_sab_style_no_ops_are_inert(self):
        nz = self.nc.NzbdavClient.__new__(self.nc.NzbdavClient)
        self.assertFalse(nz.register_cli_ids('h', {'a': 1}))
        self.assertEqual(nz.rename_nzb_with_status('h', 'n'), (False, True))
        self.assertEqual(list(nz.iter_queue_jobs()), [])
        self.assertEqual(nz.find_job_hash_by_name('x'), '')
        self.assertIsNone(nz.list_repair_health())
        self.assertFalse(nz.run_health_scan('torrent'))

    def test_supports_probes_undeclared_clients(self):
        class Duck:
            def remove_nzb_exact(self, h):
                return True
        self.assertTrue(self.base.supports(Duck(), self.base.EXACT_REMOVE))
        self.assertFalse(self.base.supports(Duck(), self.base.RENAME))
        self.assertFalse(self.base.supports(None, self.base.RENAME))


class TestQueueHelpers(_Base):
    def test_iter_queue_jobs_pages_until_has_next_false(self):
        pages = {1: {'torrents': [{'name': 'a', 'info_hash': '1'}], 'has_next': True},
                 2: {'torrents': [{'name': 'b', 'info_hash': '2'}], 'has_next': False}}
        calls = []

        def get(url, params=None, headers=None, timeout=None):
            calls.append(dict(params))
            return _Resp(200, pages[params['page']])
        with mock.patch.object(self.cc.requests, 'get', side_effect=get):
            jobs = list(self.client.iter_queue_jobs(search='x', page_size=100))
        self.assertEqual([j['name'] for j in jobs], ['a', 'b'])
        self.assertEqual(calls[0], {'limit': 100, 'sort_by': 'added_on', 'sort_order': 'desc',
                                    'search': 'x', 'page': 1})

    def test_iter_queue_jobs_stops_on_error_keeping_earlier_pages(self):
        def get(url, params=None, headers=None, timeout=None):
            if params['page'] == 1:
                return _Resp(200, {'torrents': [{'name': 'a'}], 'has_next': True})
            raise ConnectionError('down')
        with mock.patch.object(self.cc.requests, 'get', side_effect=get):
            jobs = list(self.client.iter_queue_jobs(retries=3))
        self.assertEqual([j['name'] for j in jobs], ['a'])

    def test_find_job_hash_by_name_is_exact_after_strip(self):
        listing = {'torrents': [{'name': 'Show S01E01 extra', 'info_hash': 'no'},
                                {'name': ' Show S01E01 ', 'info_hash': 'yes'}], 'has_next': False}
        with mock.patch.object(self.cc.requests, 'get', return_value=_Resp(200, listing)):
            self.assertEqual(self.client.find_job_hash_by_name('Show S01E01'), 'yes')
            self.assertEqual(self.client.find_job_hash_by_name('Missing'), '')


class TestRepairApi(_Base):
    def test_call_unwraps_http_error_status(self):
        def delete(url, **kw):
            raise _HTTPError(_Resp(404))
        self.api.delete = delete
        self.assertEqual(self.client.delete_mount_torrent('h'), 404)

    def test_call_transport_error_is_none(self):
        def post(url, **kw):
            raise ConnectionError('down')
        self.api.post = post
        self.assertIsNone(self.client.stop_repair())

    def test_list_repair_health_none_on_failure(self):
        self.api.get = lambda url, **kw: _Resp(500, {})
        self.assertIsNone(self.client.list_repair_health())
        self.api.get = lambda url, **kw: _Resp(200, {'entries': [{'status': 'broken'}]})
        self.assertEqual(self.client.list_repair_health(), [{'status': 'broken'}])

    def test_sync_torrent_passes_rd_id(self):
        seen = {}

        def post(url, params=None, **kw):
            seen['url'], seen['params'] = url, params
            return _Resp(200, {})
        self.api.post = post
        self.assertEqual(self.client.sync_torrent('abc', rd_id='RD1'), 200)
        self.assertEqual(seen, {'url': 'http://cm:8888/api/torrents/abc/sync', 'params': {'rdId': 'RD1'}})

    def test_run_health_scan_params(self):
        seen = {}

        def post(url, params=None, **kw):
            seen['url'], seen['params'] = url, params
            return _Resp(202, {})
        self.api.post = post
        self.assertTrue(self.client.run_health_scan('torrent', full=True))
        self.assertEqual(seen['url'], 'http://cm:8888/api/repair/run')
        self.assertEqual(seen['params'], {'protocol': 'torrent', 'ignore_last_checked': 'true'})


if __name__ == '__main__':
    unittest.main()
