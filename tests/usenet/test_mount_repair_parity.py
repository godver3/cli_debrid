#!/usr/bin/env python3
"""The built-in mount marks debrid files broken by cli_mount's rules
(debrid/mount_proof.py): only a provider's proof that a file is gone counts;
timeouts, 5xx and rate limits never do. Also: the engine's health records
reach cli_debrid's debrid repair as protocol "torrent" with the torrent hash."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401,E402  (app import order)
from debrid.mount_proof import MountFileGone, BROKEN, HEALTHY, UNKNOWN  # noqa: E402
from utilities import mount_debrid as md  # noqa: E402

KEY = mock.PropertyMock(return_value='k')


class RealDebridRules(unittest.TestCase):
    INFO = {'status': 'downloaded', 'filename': 'M', 'links': ['L2'],
            'files': [{'id': 2, 'path': '/M/m.mkv', 'bytes': 10, 'selected': 1}]}

    def rd(self, post):
        from debrid.real_debrid import client as rdc
        rd = rdc.RealDebridProvider.__new__(rdc.RealDebridProvider)
        p = [mock.patch.object(rdc, 'make_request', return_value=self.INFO),
             mock.patch.object(rd, '_mount_rd_post', side_effect=post),
             mock.patch.object(type(rd), 'api_key', new_callable=lambda: KEY)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        return rd

    def test_link_error_codes_19_24_35_are_proof(self):
        for code in (19, 24, 35):
            rd = self.rd(lambda ep, d, c=code: (503, {'error': 'unavailable_file', 'error_code': c}))
            with self.assertRaises(MountFileGone) as gone:
                rd.mount_link('T', '2')
            self.assertEqual(gone.exception.reason, 'hoster_unavailable')

    def test_other_link_failures_prove_nothing(self):
        for status, body in ((503, {'error': 'service_unavailable', 'error_code': 25}), (429, {}), (500, {})):
            rd = self.rd(lambda ep, d, s=status, b=body: (s, b))
            with self.assertRaises(ValueError):
                rd.mount_link('T', '2')

    def test_check_uses_unrestrict_check(self):
        for status, want in ((404, (BROKEN, 'hoster_unavailable')), (200, (HEALTHY, ''))):
            rd = self.rd(lambda ep, d, s=status: (s, {}) if ep == '/unrestrict/check' else (0, {}))
            self.assertEqual(rd.mount_check('T', '2'), want)
        rd = self.rd(lambda ep, d: (503, {}))
        self.assertEqual(rd.mount_check('T', '2')[0], UNKNOWN)
        self.assertEqual(rd.mount_check('T', '9'), (BROKEN, 'missing_provider_link'))

    def test_torrent_lookup_failure_is_unknown(self):
        from debrid.real_debrid import client as rdc
        rd = rdc.RealDebridProvider.__new__(rdc.RealDebridProvider)
        with mock.patch.object(rdc, 'make_request', side_effect=RuntimeError('429')), \
                mock.patch.object(type(rd), 'api_key', new_callable=lambda: KEY):
            self.assertEqual(rd.mount_check('T', '2')[0], UNKNOWN)


class OtherProviderRules(unittest.TestCase):
    def test_torbox_download_present(self):
        from debrid.torbox import client as tbc
        tb = tbc.TorboxProvider.__new__(tbc.TorboxProvider)
        info = {'id': 1, 'download_present': True, 'files': [{'id': 0, 'name': 'M/m.mkv', 'size': 5}]}
        with mock.patch.object(tbc, 'make_request', return_value={'success': True, 'data': info}), \
                mock.patch.object(type(tb), 'api_key', new_callable=lambda: KEY):
            self.assertEqual(tb.mount_check('1', '0'), (HEALTHY, ''))
            self.assertEqual(tb.mount_check('1', '7'), (BROKEN, 'missing_provider_link'))
            info['download_present'] = False
            self.assertEqual(tb.mount_check('1', '0'), (BROKEN, 'hoster_unavailable'))
        with mock.patch.object(tbc, 'make_request', side_effect=RuntimeError('timeout')), \
                mock.patch.object(type(tb), 'api_key', new_callable=lambda: KEY):
            self.assertEqual(tb.mount_check('1', '0')[0], UNKNOWN)
        with mock.patch.object(tbc, 'make_request', return_value={'success': True, 'data': None}), \
                mock.patch.object(type(tb), 'api_key', new_callable=lambda: KEY):
            self.assertEqual(tb.mount_check('1', '0')[0], UNKNOWN, 'gone from the account: the mirror removes it')

    def test_alldebrid_link_infos_without_unlocking(self):
        from debrid.alldebrid import client as adc
        ad = adc.AllDebridProvider.__new__(adc.AllDebridProvider)
        calls = []

        def answer(info):
            def req(method, endpoint, key, **kw):
                calls.append(endpoint)
                return info
            return req
        link = 'https://alldebrid.com/f/X'
        for info, want in (({'status': 'success', 'data': {'infos': [{'link': link, 'filename': 'm.mkv'}]}}, HEALTHY),
                           ({'status': 'success', 'data': {'infos': [{'link': link, 'error': {'code': 'LINK_DOWN'}}]}}, BROKEN),
                           ({'status': 'error', 'error': {'message': 'busy'}}, UNKNOWN)):
            with mock.patch.object(adc, 'make_request', side_effect=answer(info)), \
                    mock.patch.object(type(ad), 'api_key', new_callable=lambda: KEY):
                self.assertEqual(ad.mount_check('7', link)[0], want)
        self.assertNotIn('/v4/link/unlock', calls, 'a check must not generate a link (AD suspends for that)')

    def test_premiumize(self):
        from debrid.premiumize import client as pmc
        pm = pmc.PremiumizeProvider.__new__(pmc.PremiumizeProvider)
        with mock.patch.object(pm, '_req', return_value={'id': 'i1', 'link': ''}):
            with self.assertRaises(MountFileGone):
                pm.mount_link('T', 'i1')
            self.assertEqual(pm.mount_check('T', 'i1'), (HEALTHY, ''), 'cli_mount: details load = fine')
        with mock.patch.object(pm, '_req', return_value={'status': 'error', 'message': 'busy'}):
            with self.assertRaises(ValueError):
                pm.mount_link('T', 'i1')
            self.assertEqual(pm.mount_check('T', 'i1')[0], UNKNOWN)
        self.assertEqual(pm.mount_check('T', ''), (BROKEN, 'missing_provider_link'))

    def test_debridlink_link_status(self):
        from debrid.debridlink import client as dlc
        from debrid import mount_proof
        dl = dlc.DebridLinkProvider.__new__(dlc.DebridLinkProvider)
        info = {'files': [{'link': 'https://dl/a'}, {'link': ''}]}
        with mock.patch.object(dl, 'get_torrent_info', return_value=info):
            for status, want in ((404, BROKEN), (410, BROKEN), (206, HEALTHY), (500, UNKNOWN), (0, UNKNOWN)):
                with mock.patch.object(mount_proof, 'link_status', return_value=status):
                    self.assertEqual(dl.mount_check('X', '0')[0], want, status)
            self.assertEqual(dl.mount_check('X', '1'), (BROKEN, 'missing_provider_link'))
            self.assertEqual(dl.mount_check('X', '5'), (BROKEN, 'missing_provider_link'))

    def test_highway_404(self):
        from debrid.highway import client as hwc
        from debrid.highway.exceptions import HighwayAPIError
        hw = hwc.HighwayProvider.__new__(hwc.HighwayProvider)
        with mock.patch.object(type(hw), 'api_key', new_callable=lambda: KEY):
            with mock.patch.object(hwc.hw, 'get_file', side_effect=HighwayAPIError('HighWay request failed: 404 not_found: gone')):
                self.assertEqual(hw.mount_check('12-a', '3'), (BROKEN, 'hoster_unavailable'))
                with self.assertRaises(MountFileGone):
                    hw.mount_link('12-a', '3')
            with mock.patch.object(hwc.hw, 'get_file', side_effect=HighwayAPIError('HighWay request failed: 400 bad: x')):
                self.assertEqual(hw.mount_check('12-a', '3')[0], UNKNOWN)


class FakeProvider:
    supports_mount_links = True

    def __init__(self, state):
        self.state = state

    def mount_check(self, tid, fid):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


FakeProvider.__name__ = 'RealDebridProvider'


class CheckRouting(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        p = mock.patch.object(md, '_cache_path', return_value=os.path.join(self._dir.name, 'links.json'))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._dir.cleanup)
        md._links = None
        self.addCleanup(setattr, md, '_links', None)

    def test_routes_to_the_provider_and_never_raises(self):
        with mock.patch.object(md, 'providers', return_value=[FakeProvider((HEALTHY, ''))]):
            self.assertEqual(md.check('rd:T:1'), (HEALTHY, ''))
            self.assertEqual(md.check('tb:T:1')[0], UNKNOWN, 'no such provider configured')
            self.assertEqual(md.check('garbage')[0], UNKNOWN)
        with mock.patch.object(md, 'providers', return_value=[FakeProvider(RuntimeError('boom'))]):
            self.assertEqual(md.check('rd:T:1')[0], UNKNOWN)

    def test_broken_file_loses_its_cached_link(self):
        md._load_links()['rd:T:1'] = {'url': 'https://old', 'expires': 9e12, 'minted': 0}
        with mock.patch.object(md, 'providers', return_value=[FakeProvider((BROKEN, 'hoster_unavailable'))]):
            self.assertEqual(md.check('rd:T:1'), (BROKEN, 'hoster_unavailable'))
        self.assertNotIn('rd:T:1', md._load_links())


class EngineCallbackRoutes(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_mount_routes import mr
        self.mr = mr

    def call(self, fn, body):
        from flask import Flask
        from utilities import builtin_engine
        with mock.patch.object(builtin_engine, 'current_token', return_value='tok'), \
                Flask(__name__).test_request_context(json=body, headers={'Authorization': 'Bearer tok'},
                                                     environ_base={'REMOTE_ADDR': '127.0.0.1'}):
            r = fn()
        return (r[1], r[0].get_json()) if isinstance(r, tuple) else (200, r.get_json())

    def test_gone_file_is_410_with_reason(self):
        with mock.patch.object(md, 'link', side_effect=MountFileGone('hoster_unavailable', 'RD error 24')):
            self.assertEqual(self.call(self.mr.internal_link, {'ref': 'rd:T:1'}),
                             (410, {'dead': True, 'reason': 'hoster_unavailable', 'error': 'RD error 24'}))
        with mock.patch.object(md, 'link', side_effect=RuntimeError('timeout')):
            self.assertEqual(self.call(self.mr.internal_link, {'ref': 'rd:T:1'})[0], 502)

    def test_check_endpoint(self):
        with mock.patch.object(md, 'check', return_value=(BROKEN, 'hoster_unavailable')):
            self.assertEqual(self.call(self.mr.internal_check, {'ref': 'rd:T:1'}),
                             (200, {'state': 'broken', 'reason': 'hoster_unavailable'}))


class BuiltinClientDebridRepair(unittest.TestCase):
    JOBS = [{'id': 'rd-T1', 'protocol': 'debrid', 'hash': 'abc', 'name': 'Show', 'path': '/mnt/clid/__all__/Show',
             'status': 'ready', 'health': {'status': 'broken', 'broken_files': [
                 {'file_name': 'e1.mkv', 'reason': 'hoster_unavailable'}]}},
            {'id': 'u-1', 'protocol': '', 'name': 'Movie', 'path': '/mnt/clid/__all__/Movie', 'status': 'ready',
             'health': {'status': 'broken', 'broken_files': [{'file_name': 'm.mkv', 'reason': 'usenet_segment_missing'}]}}]

    def client(self, calls, delete_code=204):
        from usenet.builtin_client import BuiltinClient
        c = BuiltinClient.__new__(BuiltinClient)
        c.enabled, c.base_url = True, 'http://e'

        def call(method, path, **kw):
            calls.append((method, path))
            if method == 'DELETE':
                return delete_code, None
            return 200, self.JOBS
        c._call = call
        return c

    def test_records_carry_protocol_and_torrent_hash(self):
        out = {e['entry_name']: e for e in self.client([])._broken_entries()}
        self.assertEqual((out['Show']['protocol'], out['Show']['info_hash'], out['Show']['failure_reason']),
                         ('torrent', 'abc', 'hoster_unavailable'))
        self.assertEqual((out['Movie']['protocol'], out['Movie']['info_hash']), ('nzb', 'u-1'))

    def test_delete_removes_from_the_account_first(self):
        class P:
            supports_mount_links = True
            removed = []

            def remove_torrent(self, tid, removal_reason=None):
                self.removed.append(tid)
                return True
        P.__name__ = 'RealDebridProvider'
        calls = []
        with mock.patch.object(md, 'providers', return_value=[P()]):
            self.assertEqual(self.client(calls).delete_mount_torrent('ABC'), 200)
        self.assertEqual(P.removed, ['T1'])
        self.assertIn(('DELETE', '/jobs/rd-T1'), calls)
        self.assertEqual(self.client([]).delete_mount_torrent('nope'), 404)

    def test_failed_account_removal_keeps_it_on_the_mount(self):
        class P:
            supports_mount_links = True

            def remove_torrent(self, tid, removal_reason=None):
                return False
        P.__name__ = 'RealDebridProvider'
        calls = []
        with mock.patch.object(md, 'providers', return_value=[P()]):
            self.assertEqual(self.client(calls).delete_mount_torrent('abc'), 502)
        self.assertNotIn(('DELETE', '/jobs/rd-T1'), calls)

    def test_debrid_repair_talks_to_the_engine_when_it_holds_debrid(self):
        from usenet import debrid_repair_engine as dre
        from usenet.builtin_client import BuiltinClient
        with mock.patch.object(md, 'engine_holds_debrid', return_value=True):
            c = dre._client()
        self.assertIsInstance(c, BuiltinClient)
        self.assertTrue(c.enabled)
        with mock.patch.object(md, 'engine_holds_debrid', return_value=False), \
                mock.patch('usenet.get_usenet_client', return_value='climount'):
            self.assertEqual(dre._client(), 'climount')


if __name__ == '__main__':
    unittest.main()
