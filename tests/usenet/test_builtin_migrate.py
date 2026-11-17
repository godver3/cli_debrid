#!/usr/bin/env python3
"""Migration from cli_mount (utilities/builtin_migrate.py): cli_mount's
NZBProto metadata is decoded and imported with the same id/folder/files."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import builtin_migrate as bm


def _v(n):
    out = b''
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def fld(num, val):
    if isinstance(val, int):
        return _v(num << 3) + _v(val)
    if isinstance(val, str):
        val = val.encode()
    return _v(num << 3 | 2) + _v(len(val)) + val


def seg(number, mid, start, end, data_start=0):
    return fld(1, number) + fld(2, mid) + fld(3, end - start + 1) + fld(4, start) + fld(5, end) + (
        fld(7, data_start) if data_start else b'')


def nzbfile(name, segs, size, ftype='rar', deleted=False, encrypted=False):
    b = fld(2, name) + fld(4, size) + fld(8, ftype) + b''.join(fld(6, s) for s in segs)
    return b + (fld(10, 1) if deleted else b'') + (fld(15, 1) if encrypted else b'')


META = (fld(1, '95af6933-9d46-487a-96a1-efac7d2b4113') + fld(2, 'Veep.S06E01') +
        fld(9, nzbfile('Veep.S06E01.mkv', [seg(2, 'b@x', 1000, 1999), seg(1, 'a@x', 0, 999, 176)], 2000)) +
        fld(9, nzbfile('old.nfo', [seg(1, 'c@x', 0, 9)], 10, 'media', deleted=True)) +
        fld(9, nzbfile('secret.mkv', [seg(1, 'd@x', 0, 9)], 10, encrypted=True)))


class FakeEngine:
    def __init__(self, code=201):
        self.code, self.posts = code, []

    def _call(self, method, path, timeout=0, **kw):
        self.posts.append((method, path, kw.get('json')))
        return self.code, ({'error': 'boom'} if self.code >= 400 else {})


class MigrateTests(unittest.TestCase):
    def test_parse_meta(self):
        m = bm.parse_meta(META)
        self.assertEqual((m['id'], m['name']), ('95af6933-9d46-487a-96a1-efac7d2b4113', 'Veep.S06E01'))
        self.assertEqual([f['name'] for f in m['files']], ['Veep.S06E01.mkv'], 'deleted and encrypted files left out')
        segs = m['files'][0]['segments']
        self.assertEqual([(s['id'], s['start'], s['end'], s['data_start']) for s in segs],
                         [('a@x', 0, 999, 176), ('b@x', 1000, 1999, 0)], 'sorted by file offset')
        self.assertEqual(m['skipped'], [('secret.mkv', 'encrypted archive (not supported by the built-in mount)')])

    def test_import_one_keeps_id_folder_and_reports(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, '95af6933-9d46-487a-96a1-efac7d2b4113.meta'), 'wb') as f:
                f.write(META)
            entry = {'name': 'Veep S06E01 (renamed folder)', 'info_hash': '95af6933-9d46-487a-96a1-efac7d2b4113'}
            eng = FakeEngine()
            self.assertEqual(bm.import_one(eng, entry, d), ('imported', '', 1))
            method, path, body = eng.posts[0]
            self.assertEqual((method, path, body['id'], body['name']),
                             ('POST', '/jobs/import', entry['info_hash'], 'Veep S06E01 (renamed folder)'))
            self.assertEqual(body['files'][0]['segments'][0], {'id': 'a@x', 'start': 0, 'end': 999, 'data_start': 176})
            self.assertEqual(bm.import_one(FakeEngine(409), entry, d)[0], 'already')
            self.assertEqual(bm.import_one(FakeEngine(400), entry, d)[0], 'failed')
            outcome, msg, _ = bm.import_one(FakeEngine(), {'name': 'X', 'info_hash': 'missing'}, d)
            self.assertEqual(outcome, 'failed')
            self.assertIn('no cli_mount metadata', msg)

    def test_compare_identical_and_different(self):
        class Jobs:
            def _call(self, *a, **k):
                return 200, [{'name': 'A'}, {'name': 'B'}]
        with tempfile.TemporaryDirectory() as d:
            for root in ('cm', 'bi'):
                for folder in ('A', 'B'):
                    os.makedirs(os.path.join(d, root, folder))
                    data = os.urandom(0) + bytes(range(256)) * 4000
                    if root == 'bi' and folder == 'B':
                        data = data[:-1] + b'X'
                    with open(os.path.join(d, root, folder, 'f.mkv'), 'wb') as f:
                        f.write(data)
            out = bm.compare(Jobs(), os.path.join(d, 'cm'), os.path.join(d, 'bi'), 5)
        self.assertEqual((out['checked'], out['identical']), (2, 1))
        self.assertEqual({r['folder']: r['detail'] for r in out['rows']}, {'A': 'identical', 'B': 'bytes differ'})

    def test_climount_mount_root(self):
        get = lambda s, k, d='': '/media/x/mnt/__all__/' if (s, k) == ('File Management', 'original_files_path') else d
        self.assertEqual(bm.climount_mount_root(get), '/media/x/mnt')


if __name__ == '__main__':
    unittest.main()
