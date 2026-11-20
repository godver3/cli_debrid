#!/usr/bin/env python3
"""Debrid streaming through the built-in mount (utilities/mount_debrid.py and
each provider's mount_files/mount_link methods)."""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import mount_debrid as md


class FakeProvider:
    supports_mount_links = True

    def __init__(self, torrents, files):
        self.torrents, self.files, self.asked = torrents, files, []

    def list_active_torrents(self):
        return self.torrents

    def mount_files(self, tid):
        self.asked.append(tid)
        return self.files.get(tid)

    def mount_link(self, tid, fid):
        return f'https://cdn/{tid}/{fid}', 600


FakeProvider.__name__ = 'RealDebridProvider'


class FakeEngine:
    def __init__(self, jobs):
        self.jobs, self.calls = jobs, []

    def _call(self, method, path, timeout=0, **kw):
        self.calls.append((method, path, kw.get('json')))
        if method == 'GET':
            return 200, self.jobs
        return (201, {}) if method == 'POST' else (204, None)


class IsolatedLinkCache(unittest.TestCase):
    """Each test gets its own on-disk link cache."""

    def setUp(self):
        md._missing.clear()
        md._skipped.clear()
        md._mass_warned.clear()
        self._dir = tempfile.TemporaryDirectory()
        path = os.path.join(self._dir.name, 'mount_links.json')
        self._patch = mock.patch.object(md, '_cache_path', return_value=path)
        self._patch.start()
        md._links = None

    def tearDown(self):
        self._patch.stop()
        md._links = None
        self._dir.cleanup()


class MountDebridTests(IsolatedLinkCache):
    def test_unique_names(self):
        out = md.unique_names([{'name': 'Pack/S01/e1.mkv', 'file_id': '1'}, {'name': 'Pack/Sample/sample.mkv', 'file_id': '2'},
                               {'name': 'Pack/Extras/sample.mkv', 'file_id': '3'}])
        self.assertEqual([f['name'] for f in out], ['e1.mkv', 'Pack - Sample - sample.mkv', 'Pack - Extras - sample.mkv'])

    def test_sync_adds_finished_and_removes_gone(self):
        p = FakeProvider([{'id': 'A', 'status': 'downloaded', 'hash': 'AABB'}, {'id': 'B', 'status': 'downloading'},
                          {'id': 'C', 'status': 'downloaded'}],
                         {'A': ('Movie.2024', [{'name': 'Movie.2024/Movie.2024.mkv', 'size': 10, 'file_id': '1'},
                                               {'name': 'empty.txt', 'size': 0, 'file_id': '2'}])})
        eng = FakeEngine([{'id': 'rd-OLD', 'protocol': 'debrid'}, {'id': 'rd-C', 'protocol': 'debrid'},
                          {'id': 'usenetjob', 'protocol': ''}])
        out = md.sync_once(eng, [p])
        self.assertEqual(out, {'added': 1, 'removed': 0, 'skipped': 0, 'errors': 0, 'pending': 0, 'held_removals': 0},
                         'one listing without a torrent is not enough to remove it')
        self.assertEqual(p.asked, ['A'], 'unfinished and already-mounted torrents are not asked')
        self.assertNotIn(('DELETE', '/jobs/rd-OLD', None), eng.calls)
        eng.jobs.append({'id': 'rd-A', 'protocol': 'debrid'})
        out = md.sync_once(eng, [p])
        self.assertEqual((out['added'], out['removed']), (0, 0), 'two passes, but not missing long enough yet')
        with mock.patch.object(md.time, 'time', return_value=time.time() + md.MIN_MISS_SECONDS + 1):
            out = md.sync_once(eng, [p])
        self.assertEqual((out['added'], out['removed']), (0, 1))
        post = [c for c in eng.calls if c[0] == 'POST'][0]
        self.assertEqual(post[1:], ('/debrid/jobs', {'id': 'rd-A', 'name': 'Movie.2024', 'provider': 'rd', 'hash': 'aabb', 'files': [
            {'name': 'Movie.2024.mkv', 'size': 10, 'ref': 'rd:A:1'}]}))
        self.assertIn(('DELETE', '/jobs/rd-OLD', None), eng.calls)
        self.assertNotIn(('DELETE', '/jobs/usenetjob', None), eng.calls)

    def test_empty_listing_never_empties_the_mount(self):
        eng = FakeEngine([{'id': 'rd-A', 'protocol': 'debrid'}])
        md.sync_once(eng, [FakeProvider([], {})])
        self.assertFalse([c for c in eng.calls if c[0] == 'DELETE'])

    def test_failed_listing_page_removes_nothing(self):
        class Strict(FakeProvider):
            def list_active_torrents(self, strict=False):
                if strict:
                    raise RuntimeError('page 2: 429')
                return self.torrents[:1]
        Strict.__name__ = 'RealDebridProvider'
        eng = FakeEngine([{'id': f'rd-{i}', 'protocol': 'debrid'} for i in range(5)])
        p = Strict([{'id': '0', 'status': 'downloaded'}], {})
        for _ in range(3):
            out = md.sync_once(eng, [p])
        self.assertEqual(out['errors'], 1)
        self.assertFalse([c for c in eng.calls if c[0] == 'DELETE'])
        self.assertFalse(md.status()['complete'])

    def test_back_on_the_account_resets_the_miss_count(self):
        eng = FakeEngine([{'id': 'rd-A', 'protocol': 'debrid'}, {'id': 'rd-B', 'protocol': 'debrid'}])
        with_a = FakeProvider([{'id': 'A', 'status': 'downloaded'}, {'id': 'B', 'status': 'downloaded'}], {})
        without_a = FakeProvider([{'id': 'B', 'status': 'downloaded'}], {})
        for p in (without_a, with_a, without_a):
            md.sync_once(eng, [p])
        self.assertFalse([c for c in eng.calls if c[0] == 'DELETE'], 'misses must be in a row')

    def test_mass_removal_is_held(self):
        eng = FakeEngine([{'id': f'rd-{i}', 'protocol': 'debrid'} for i in range(100)])
        p = FakeProvider([{'id': str(i), 'status': 'downloaded'} for i in range(50)], {})  # half the account vanished
        with mock.patch.object(md.time, 'time', return_value=1000.0):
            md.sync_once(eng, [p])
        with mock.patch.object(md.time, 'time', return_value=1000.0 + md.MIN_MISS_SECONDS):
            out = md.sync_once(eng, [p])
        self.assertEqual((out['removed'], out['held_removals']), (0, 50))
        with mock.patch.object(md.time, 'time', return_value=1000.0 + md.MASS_HOLD_SECONDS + 1):
            out = md.sync_once(eng, [p])
        self.assertEqual(out['removed'], 50, 'still missing after the hold: removed')

    def test_backlog_is_reported_and_budgeted(self):
        torrents = [{'id': str(i), 'status': 'downloaded'} for i in range(7)]
        files = {str(i): (f'T{i}', [{'name': 'a.mkv', 'size': 1, 'file_id': '1'}]) for i in range(7)}
        p = FakeProvider(torrents, files)
        eng = FakeEngine([])
        out = md.sync_once(eng, [p], budget=5)
        self.assertEqual((out['added'], out['pending']), (5, 2))
        self.assertFalse(md.status()['complete'])
        eng.jobs = [{'id': f'rd-{i}', 'protocol': 'debrid'} for i in range(5)]
        out = md.sync_once(eng, [p], budget=5)
        self.assertEqual((out['added'], out['pending']), (2, 0))
        self.assertTrue(md.status()['complete'])

    def test_declined_torrents_are_not_asked_every_pass(self):
        p = FakeProvider([{'id': 'X', 'status': 'downloaded'}], {})  # mount_files -> None (RD archive link)
        eng = FakeEngine([])
        md.sync_once(eng, [p])
        md.sync_once(eng, [p])
        self.assertEqual(p.asked, ['X'])
        self.assertTrue(md.status()['complete'], 'a declined torrent is not pending')

    def test_link_routes_to_provider(self):
        with mock.patch.object(md, 'providers', return_value=[FakeProvider([], {})]):
            self.assertEqual(md.link('rd:A:7'), ('https://cdn/A/7', 600))
            with self.assertRaises(ValueError):
                md.link('tb:A:7')
            with self.assertRaises(ValueError):
                md.link('garbage')


class LinkCacheTests(IsolatedLinkCache):
    """Links are kept on disk until they expire: a restart must not mint a
    new link for every file (AllDebrid suspends accounts for that)."""

    def _provider(self):
        p = FakeProvider([], {})
        p.minted = 0

        def mint(tid, fid):
            p.minted += 1
            return f'https://cdn/{tid}/{fid}/{p.minted}', 3600
        p.mount_link = mint
        return p

    def test_cached_across_restart(self):
        p = self._provider()
        with mock.patch.object(md, 'providers', return_value=[p]):
            url, ttl = md.link('rd:A:1')
            md._links = None  # "restart": reload from disk
            self.assertEqual(md.link('rd:A:1')[0], url)
            self.assertEqual(p.minted, 1)
            self.assertGreater(ttl, 3500)

    def test_concurrent_requests_mint_once(self):
        import threading
        p = self._provider()
        orig = p.mount_link

        def slow(tid, fid):
            time.sleep(0.2)
            return orig(tid, fid)
        p.mount_link = slow
        out = []
        with mock.patch.object(md, 'providers', return_value=[p]):
            ts = [threading.Thread(target=lambda: out.append(md.link('rd:A:1')[0])) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(p.minted, 1)
        self.assertEqual(len(set(out)), 1)

    def test_refresh_mints_unless_link_is_brand_new(self):
        p = self._provider()
        with mock.patch.object(md, 'providers', return_value=[p]):
            first = md.link('rd:A:1')[0]
            self.assertEqual(md.link('rd:A:1', refresh=True)[0], first, 'refresh loop guard')
            md._links['rd:A:1']['minted'] -= md.REFRESH_MIN_AGE + 1
            self.assertNotEqual(md.link('rd:A:1', refresh=True)[0], first)
            self.assertEqual(p.minted, 2)

    def test_expired_links_are_reminted_and_pruned(self):
        p = self._provider()
        with mock.patch.object(md, 'providers', return_value=[p]):
            md.link('rd:A:1')
            md._links['rd:A:1']['expires'] = time.time() + 30  # about to expire
            md.link('rd:A:1')
            self.assertEqual(p.minted, 2)
            md.link('rd:B:1')
            md.forget_links('rd:A:')
            self.assertEqual(sorted(md._links), ['rd:B:1'])

    def test_alldebrid_ref_keeps_colons_of_its_link(self):
        p = self._provider()
        p.__class__ = type('AllDebridProvider', (FakeProvider,), {})
        with mock.patch.object(md, 'providers', return_value=[p]):
            self.assertEqual(md.link('ad:42:https://alldebrid.com/f/abc')[0], 'https://cdn/42/https://alldebrid.com/f/abc/1')


class SelectionTests(unittest.TestCase):
    def test_debrid_only_runs_the_engine(self):
        from utilities import builtin_engine as be
        none = {'Usenet Provider': {'enabled': False}, 'Built-in Mount': {}}
        debrid_only = {'Usenet Provider': {'enabled': False}, 'Built-in Mount': {'enabled': True}}
        usenet = {'Usenet Provider': {'enabled': True, 'provider': 'builtin'}, 'Built-in Mount': {}}
        self.assertFalse(be.selected(none))
        self.assertTrue(be.selected(debrid_only))
        self.assertTrue(be.wanted(debrid_only))
        self.assertTrue(be.selected(usenet))
        self.assertFalse(be.usenet_selected(debrid_only))
        with mock.patch.object(be, 'host_problems', return_value=[]):
            self.assertEqual(be.problems(debrid_only), [], 'no news server needed for debrid alone')
            self.assertTrue(any('usenet server' in p for p in be.problems(usenet)))

    def test_picking_the_builtin_mount_always_streams_debrid(self):
        """No separate debrid-streaming switch: an old config's stored
        debrid_streaming=False no longer turns the mirror off."""
        cfgs = {
            'usenet builtin': {'Usenet Provider': {'enabled': True, 'provider': 'builtin'},
                               'Built-in Mount': {'debrid_streaming': False}},
            'debrid only': {'Usenet Provider': {'enabled': False}, 'Built-in Mount': {'enabled': True}},
            'staged next to cli_mount': {'Usenet Provider': {'enabled': True, 'provider': 'climount'},
                                         'Built-in Mount': {'run_alongside': True}},
        }
        for name, cfg in cfgs.items():
            get = lambda section, *a, **k: cfg.get(section)
            self.assertTrue(md._enabled(get), name)
        get = lambda section, *a, **k: cfgs['usenet builtin'].get(section)
        self.assertTrue(md.engine_holds_debrid(get))
        off = {'Usenet Provider': {'enabled': True, 'provider': 'climount'}, 'Built-in Mount': {}}
        self.assertFalse(md._enabled(lambda section, *a, **k: off.get(section)), 'cli_mount users are left alone')
        with mock.patch.object(md, 'providers', return_value=[object()]):
            self.assertTrue(md.needed(get))

    def test_mount_page_has_no_debrid_switches(self):
        # Read from the source: importing routes alone hits an unrelated
        # circular import (debrid <-> database.statistics).
        import ast
        src = os.path.join(os.path.dirname(__file__), '..', '..', 'routes', 'mount_routes.py')
        tree = ast.parse(open(src).read())
        groups = next(ast.literal_eval(n.value) for n in tree.body
                      if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'GROUPS')
        keys = {k for _, ks in groups for k in ks}
        self.assertFalse(keys & {'enabled', 'debrid_streaming'})

    def test_webdav_needs_a_login(self):
        from utilities import builtin_engine as be
        self.assertEqual(be.webdav_config({'webdav_enabled': True}), {'webdav_listen': ''})
        self.assertEqual(be.webdav_config({'webdav_enabled': False, 'webdav_username': 'u', 'webdav_password': 'p'}),
                         {'webdav_listen': ''})
        self.assertEqual(be.webdav_config({'webdav_enabled': True, 'webdav_username': 'u', 'webdav_password': 'p',
                                           'webdav_port': '9000'}),
                         {'webdav_listen': '0.0.0.0:9000', 'webdav_user': 'u', 'webdav_password': 'p'})


class ProviderLinkTests(unittest.TestCase):
    def test_real_debrid_matches_links_to_selected_files(self):
        import database  # noqa: F401 - app import order
        from debrid.real_debrid import client as rdc
        info = {'status': 'downloaded', 'filename': 'Show.S01', 'links': ['L2', 'L5'],
                'files': [{'id': 5, 'path': '/Show.S01/e2.mkv', 'bytes': 20, 'selected': 1},
                          {'id': 1, 'path': '/Show.S01/nfo.txt', 'bytes': 1, 'selected': 0},
                          {'id': 2, 'path': '/Show.S01/e1.mkv', 'bytes': 10, 'selected': 1}]}
        calls = []

        def req(method, endpoint, key, data=None, **kw):
            calls.append((method, endpoint, data))
            return {'download': 'https://dl/' + data['link']} if endpoint == '/unrestrict/link' else info
        rd = rdc.RealDebridProvider.__new__(rdc.RealDebridProvider)
        from debrid.mount_proof import MountFileGone
        with mock.patch.object(rdc, 'make_request', side_effect=req), \
                mock.patch.object(rd, '_mount_rd_post', side_effect=lambda ep, data, api_key=None: (200, req('POST', ep, 'k', data))), \
                mock.patch.object(type(rd), 'api_key', new_callable=mock.PropertyMock, return_value='k'):
            folder, files = rd.mount_files('T')
            self.assertEqual((folder, [(f['file_id'], f['size']) for f in files]), ('Show.S01', [('2', 10), ('5', 20)]))
            self.assertEqual(rd.mount_link('T', '5'), ('https://dl/L5', 43200))
            with self.assertRaises(MountFileGone) as gone:
                rd.mount_link('T', '1')  # not selected: the torrent has no link for it
            self.assertEqual(gone.exception.reason, 'missing_provider_link')
            info['links'] = ['ONE.rar']
            self.assertIsNone(rd.mount_files('T'), 'files packed into one link are not streamable')

    def test_alldebrid_file_ref_is_the_locker_link(self):
        import database  # noqa: F401
        from debrid.alldebrid import client as adc
        ad = adc.AllDebridProvider.__new__(adc.AllDebridProvider)
        info = {'status': 'downloaded', 'filename': 'Movie.2024',
                'files': [{'path': 'Movie.2024/m.mkv', 'bytes': 9, 'link': 'https://alldebrid.com/f/X1'},
                          {'path': 'Movie.2024/no-link.nfo', 'bytes': 1, 'link': ''}]}
        calls = []

        def req(method, endpoint, key, **kw):
            calls.append((endpoint, kw.get('params')))
            return {'status': 'success', 'data': {'link': 'https://cdn.ad/m.mkv'}}
        with mock.patch.object(ad, 'get_torrent_info', return_value=info), \
                mock.patch.object(adc, 'make_request', side_effect=req), \
                mock.patch.object(type(ad), 'api_key', new_callable=mock.PropertyMock, return_value='k'):
            self.assertEqual(ad.mount_files('7'), ('Movie.2024', [{'name': 'Movie.2024/m.mkv', 'size': 9,
                                                                   'file_id': 'https://alldebrid.com/f/X1'}]))
            self.assertEqual(ad.mount_link('7', 'https://alldebrid.com/f/X1'), ('https://cdn.ad/m.mkv', ad.MOUNT_LINK_TTL))
            self.assertEqual(calls, [('/v4/link/unlock', {'link': 'https://alldebrid.com/f/X1'})])
            info['status'] = 'downloading'
            self.assertIsNone(ad.mount_files('7'))

    def test_premiumize_walks_season_folders_and_remints(self):
        from debrid.premiumize import client as pmc
        pm = pmc.PremiumizeProvider.__new__(pmc.PremiumizeProvider)
        transfers = [{'id': 'T', 'name': 'Show.S01-S02', 'status': 'finished', 'folder_id': 'F'}]
        folders = {'F': [{'type': 'folder', 'id': 'S1', 'name': 'S01'}, {'type': 'file', 'id': 'i0', 'name': 'x.nfo', 'size': 1}],
                   'S1': [{'type': 'file', 'id': 'i1', 'name': 'e1.mkv', 'size': 10}]}

        def req(method, endpoint, params=None, **kw):
            if endpoint == '/folder/list':
                return {'status': 'success', 'content': folders[params['id']]}
            if endpoint == '/item/details':
                return {'id': params['id'], 'link': f"https://pm/{params['id']}"}
            raise AssertionError(endpoint)
        with mock.patch.object(pm, '_list_transfers_raw', return_value=transfers), mock.patch.object(pm, '_req', side_effect=req):
            folder, files = pm.mount_files('T')
            self.assertEqual(folder, 'Show.S01-S02')
            self.assertEqual(files, [{'name': 'S01/e1.mkv', 'size': 10, 'file_id': 'i1'},
                                     {'name': 'x.nfo', 'size': 1, 'file_id': 'i0'}])
            self.assertEqual(pm.mount_link('T', 'i1'), ('https://pm/i1', pm.MOUNT_LINK_TTL))
            transfers[0]['status'] = 'running'
            self.assertIsNone(pm.mount_files('T'))

    def test_debridlink_uses_file_index(self):
        from debrid.debridlink import client as dlc
        dl = dlc.DebridLinkProvider.__new__(dlc.DebridLinkProvider)
        info = {'status': 'downloaded', 'filename': 'Movie', 'files': [
            {'path': 'Movie/a.mkv', 'bytes': 5, 'link': 'https://dl/a'}, {'path': 'Movie/b.mkv', 'bytes': 6, 'link': 'https://dl/b'}]}
        with mock.patch.object(dl, 'get_torrent_info', return_value=info):
            self.assertEqual(dl.mount_files('X')[1][1], {'name': 'Movie/b.mkv', 'size': 6, 'file_id': '1'})
            self.assertEqual(dl.mount_link('X', '1'), ('https://dl/b', dl.MOUNT_LINK_TTL))
            with self.assertRaises(ValueError):
                dl.mount_link('X', '9')

    def test_highway_signs_one_file(self):
        from debrid.highway import client as hwc
        hw = hwc.HighwayProvider.__new__(hwc.HighwayProvider)
        info = {'status': 'downloaded', 'filename': 'Movie', 'files': [
            {'id': 3, 'path': 'Movie/m.mkv', 'bytes': 7}, {'id': 4, 'path': 'Movie/p.mkv', 'bytes': 7, 'partial': True}]}
        with mock.patch.object(hw, 'get_torrent_info', return_value=info), \
                mock.patch.object(hwc.hw, 'get_file', return_value={'file': {'download_url': 'https://hw/m'}}) as gf, \
                mock.patch.object(type(hw), 'api_key', new_callable=mock.PropertyMock, return_value='k'):
            self.assertEqual(hw.mount_files('12-abc'), ('Movie', [{'name': 'Movie/m.mkv', 'size': 7, 'file_id': '3'}]))
            self.assertEqual(hw.mount_link('12-abc', '3'), ('https://hw/m', hw.MOUNT_LINK_TTL))
            gf.assert_called_once_with('k', '12', '3')

    def test_every_provider_opts_in(self):
        import database  # noqa: F401
        from debrid.alldebrid.client import AllDebridProvider
        from debrid.debridlink.client import DebridLinkProvider
        from debrid.highway.client import HighwayProvider
        from debrid.premiumize.client import PremiumizeProvider
        from debrid.real_debrid.client import RealDebridProvider
        from debrid.torbox.client import TorboxProvider
        keys = set()
        for cls in (AllDebridProvider, DebridLinkProvider, HighwayProvider, PremiumizeProvider, RealDebridProvider, TorboxProvider):
            self.assertTrue(cls.supports_mount_links, cls.__name__)
            keys.add(md.provider_key(cls.__new__(cls)))
        self.assertEqual(keys, {'ad', 'dl', 'hw', 'pm', 'rd', 'tb'})


if __name__ == '__main__':
    unittest.main()
