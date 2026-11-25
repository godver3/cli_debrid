#!/usr/bin/env python3
"""Mount page speed tests (utilities/mount_speedtest.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import mount_speedtest as mst


class FakeClient:
    def __init__(self, jobs=(), reply=(200, {})):
        self.jobs, self.reply, self.calls = list(jobs), reply, []

    def _debrid_jobs(self):
        return self.jobs

    def _call(self, method, path, timeout=0, **kw):
        self.calls.append((method, path, kw.get('json')))
        return self.reply


class RD:
    def mount_files(self, tid):
        if tid == 'empty':
            return None
        return 'T', [{'name': 'small.nfo', 'size': 10, 'file_id': '1'}, {'name': 'big.mkv', 'size': 9000, 'file_id': '2'}]


class Resp:
    status_code = 206

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        for _ in range(4):
            yield b'x' * n


class SpeedTestTests(unittest.TestCase):
    def test_usenet_goes_to_engine(self):
        c = FakeClient(reply=(200, {'provider': 'news', 'mb_per_second': 9.5, 'latency_ms': 80}))
        out = mst.usenet(c, 'news')
        self.assertTrue(out['ok'])
        self.assertEqual(c.calls, [('POST', '/speedtest', {'provider': 'news'})])
        c.reply = (409, {'error': 'no usenet jobs'})
        self.assertEqual(mst.usenet(c, 'news'), {'ok': False, 'provider': 'news', 'error': 'no usenet jobs'})

    def test_debrid_reads_biggest_file_of_newest_torrent(self):
        asked, got = [], []
        c = FakeClient(jobs=[{'id': 'rd-old', 'created_at': '2026-01-01'}, {'id': 'rd-empty', 'created_at': '2026-11-02'},
                             {'id': 'tb-x', 'created_at': '2026-11-03'}])

        def link(ref):
            asked.append(ref)
            return 'https://cdn/f', 600

        def get(url, **kw):
            got.append(kw['headers']['Range'])
            return Resp()
        out = mst.debrid(c, 'rd', lambda: [RD()], lambda p: 'rd', link, get)
        self.assertTrue(out['ok'], out)
        self.assertEqual(asked, ['rd:old:2'], 'newest with files first (rd-empty had none), largest file')
        self.assertEqual(got, [f'bytes=0-{mst.DEBRID_BYTES - 1}'])
        self.assertEqual(out['bytes'], 4 << 16)
        self.assertEqual(out['file'], 'big.mkv')

    def test_debrid_without_provider_or_torrents(self):
        self.assertFalse(mst.debrid(FakeClient(), 'rd', lambda: [], lambda p: 'rd', None)['ok'])
        out = mst.debrid(FakeClient(), 'rd', lambda: [RD()], lambda p: 'rd', None)
        self.assertIn('no torrents', out['error'])


if __name__ == '__main__':
    unittest.main()
