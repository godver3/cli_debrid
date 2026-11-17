#!/usr/bin/env python3
"""Debrid streaming through the built-in mount (utilities/mount_debrid.py and
the RD/TorBox mount_files/mount_link methods)."""

import os
import sys
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


class MountDebridTests(unittest.TestCase):
    def test_unique_names(self):
        out = md.unique_names([{'name': 'Pack/S01/e1.mkv', 'file_id': '1'}, {'name': 'Pack/Sample/sample.mkv', 'file_id': '2'},
                               {'name': 'Pack/Extras/sample.mkv', 'file_id': '3'}])
        self.assertEqual([f['name'] for f in out], ['e1.mkv', 'Pack - Sample - sample.mkv', 'Pack - Extras - sample.mkv'])

    def test_sync_adds_finished_and_removes_gone(self):
        p = FakeProvider([{'id': 'A', 'status': 'downloaded'}, {'id': 'B', 'status': 'downloading'},
                          {'id': 'C', 'status': 'downloaded'}],
                         {'A': ('Movie.2024', [{'name': 'Movie.2024/Movie.2024.mkv', 'size': 10, 'file_id': '1'},
                                               {'name': 'empty.txt', 'size': 0, 'file_id': '2'}])})
        eng = FakeEngine([{'id': 'rd-OLD', 'protocol': 'debrid'}, {'id': 'rd-C', 'protocol': 'debrid'},
                          {'id': 'usenetjob', 'protocol': ''}])
        out = md.sync_once(eng, [p])
        self.assertEqual(out, {'added': 1, 'removed': 1, 'skipped': 0, 'errors': 0})
        self.assertEqual(p.asked, ['A'], 'unfinished and already-mounted torrents are not asked')
        post = [c for c in eng.calls if c[0] == 'POST'][0]
        self.assertEqual(post[1:], ('/debrid/jobs', {'id': 'rd-A', 'name': 'Movie.2024', 'provider': 'rd', 'files': [
            {'name': 'Movie.2024.mkv', 'size': 10, 'ref': 'rd:A:1'}]}))
        self.assertIn(('DELETE', '/jobs/rd-OLD', None), eng.calls)
        self.assertNotIn(('DELETE', '/jobs/usenetjob', None), eng.calls)

    def test_empty_listing_never_empties_the_mount(self):
        eng = FakeEngine([{'id': 'rd-A', 'protocol': 'debrid'}])
        md.sync_once(eng, [FakeProvider([], {})])
        self.assertFalse([c for c in eng.calls if c[0] == 'DELETE'])

    def test_link_routes_to_provider(self):
        with mock.patch.object(md, 'providers', return_value=[FakeProvider([], {})]):
            self.assertEqual(md.link('rd:A:7'), ('https://cdn/A/7', 600))
            with self.assertRaises(ValueError):
                md.link('tb:A:7')
            with self.assertRaises(ValueError):
                md.link('garbage')


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
        with mock.patch.object(rdc, 'make_request', side_effect=req), \
                mock.patch.object(type(rd), 'api_key', new_callable=mock.PropertyMock, return_value='k'):
            folder, files = rd.mount_files('T')
            self.assertEqual((folder, [(f['file_id'], f['size']) for f in files]), ('Show.S01', [('2', 10), ('5', 20)]))
            self.assertEqual(rd.mount_link('T', '5'), ('https://dl/L5', 43200))
            with self.assertRaises(ValueError):
                rd.mount_link('T', '1')  # not selected
            info['links'] = ['ONE.rar']
            self.assertIsNone(rd.mount_files('T'), 'files packed into one link are not streamable')


if __name__ == '__main__':
    unittest.main()
