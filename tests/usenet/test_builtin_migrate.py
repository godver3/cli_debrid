#!/usr/bin/env python3
"""Migration from cli_mount (utilities/builtin_migrate.py): cli_mount's
NZBProto metadata is decoded and imported with the same id/folder/files."""

import base64
import os
import sys
import tempfile
import unittest
import unittest.mock

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


def nzbfile(name, segs, size, ftype='rar', deleted=False, encrypted=False, key=b'', iv=b''):
    b = fld(2, name) + fld(4, size) + fld(8, ftype) + b''.join(fld(6, s) for s in segs)
    b += (fld(13, key) if key else b'') + (fld(14, iv) if iv else b'')
    return b + (fld(10, 1) if deleted else b'') + (fld(15, 1) if encrypted else b'')


META = (fld(1, '95af6933-9d46-487a-96a1-efac7d2b4113') + fld(2, 'Veep.S06E01') +
        fld(9, nzbfile('Veep.S06E01.mkv', [seg(2, 'b@x', 1000, 1999), seg(1, 'a@x', 0, 999, 176)], 2000)) +
        fld(9, nzbfile('old.nfo', [seg(1, 'c@x', 0, 9)], 10, 'media', deleted=True)) +
        fld(9, nzbfile('secret.mkv', [seg(1, 'd@x', 0, 9)], 10, encrypted=True)) +
        fld(9, nzbfile('locked.mkv', [seg(1, 'e@x', 0, 31)], 27, encrypted=True, key=b'K' * 32, iv=b'I' * 16)))


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
        self.assertEqual([f['name'] for f in m['files']], ['Veep.S06E01.mkv', 'locked.mkv'],
                         'deleted files and encrypted ones without a key left out')
        locked = m['files'][1]
        self.assertEqual((locked['key'], locked['iv'], locked['size']), (b'K' * 32, b'I' * 16, 27))
        segs = m['files'][0]['segments']
        self.assertEqual([(s['id'], s['start'], s['end'], s['data_start']) for s in segs],
                         [('a@x', 0, 999, 176), ('b@x', 1000, 1999, 0)], 'sorted by file offset')
        self.assertEqual(m['skipped'], [('secret.mkv', "encrypted archive without a usable key in cli_mount's metadata")])

    def test_parse_meta_duplicate_name_keeps_largest(self):
        meta = (fld(1, 'x') + fld(2, 'Deep.Blue.Sea') +
                fld(9, nzbfile('dbs.mkv', [seg(1, 'a@x', 0, 99)], 100)) +
                fld(9, nzbfile('dbs.mkv', [seg(1, 'b@x', 0, 9999)], 10000)) +
                fld(9, nzbfile('dbs.sample.mkv', [seg(1, 'c@x', 0, 9)], 10)))
        m = bm.parse_meta(meta)
        self.assertEqual([(f['name'], f['size']) for f in m['files']], [('dbs.mkv', 10000), ('dbs.sample.mkv', 10)])
        self.assertEqual(len(m['skipped']), 1)
        self.assertIn('duplicate name', m['skipped'][0][1])

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
            self.assertNotIn('key', body['files'][0])
            self.assertEqual({k: body['files'][1][k] for k in ('name', 'key', 'iv', 'size')},
                             {'name': 'locked.mkv', 'key': base64.b64encode(b'K' * 32).decode(),
                              'iv': base64.b64encode(b'I' * 16).decode(), 'size': 27})
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


class _Resp:
    def __init__(self, code, body=None, content=b''):
        self.status_code, self._body, self.content = code, body, content
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body


class _Engine:
    base_url = 'http://engine'

    def __init__(self, jobs):
        self.jobs = jobs

    def _headers(self):
        return {}

    def _call(self, method, path, **kw):
        return 200, self.jobs


class _Target:
    base_url = 'http://climount'

    def __init__(self, have):
        self.have = have

    def _headers(self):
        return {}

    def iter_library_nzbs(self):
        return [{'info_hash': h} for h in self.have]


class CopyBackTests(unittest.TestCase):
    JID = '95af6933-9d46-487a-96a1-efac7d2b4113'

    def _run(self, jobs, have, post):
        from unittest import mock
        import requests
        sent = []

        def fake_post(url, headers=None, files=None, timeout=0):
            sent.append(files)
            return post(files)
        with mock.patch.object(requests, 'get', return_value=_Resp(200, content=b'<nzb/>')), \
                mock.patch.object(requests, 'post', side_effect=fake_post), \
                mock.patch.object(bm.time, 'sleep'):
            jid = bm.start_export(_Engine(jobs), _Target(have))
            for _ in range(200):
                if bm.status(jid).get('complete'):
                    break
                import time as _t
                _t.sleep(0.01)
        return bm.status(jid), sent

    def test_copies_only_usenet_jobs_cli_mount_lacks_with_same_id_and_folder(self):
        jobs = [{'id': self.JID, 'name': 'Movie', 'folder': 'Movie (2)', 'category': 'movies', 'protocol': ''},
                {'id': 'old-1', 'name': 'Old', 'protocol': ''},
                {'id': 'rd-9', 'name': 'Torrent', 'protocol': 'debrid'}]
        st, sent = self._run(jobs, {'old-1'}, lambda f: _Resp(200, [{'id': f['id'][1], 'status': 'started'}]))
        self.assertEqual((st['status'], st['imported'], st['already'], st['failed']), ('complete', 1, 1, 0))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['id'], (None, self.JID))
        self.assertEqual(sent[0]['nzbFiles'][0], 'Movie (2).nzb', 'the folder name, not the job name')
        self.assertEqual(sent[0]['downloadFolder'], (None, 'movies'))

    def test_stops_when_cli_mount_ignores_the_id(self):
        jobs = [{'id': self.JID, 'name': 'A', 'protocol': ''},
                {'id': '0f0f0f0f-0000-4000-8000-000000000002', 'name': 'B', 'protocol': ''}]
        st, sent = self._run(jobs, set(), lambda f: _Resp(200, [{'id': 'something-else'}]))
        self.assertEqual(st['status'], 'error')
        self.assertIn("doesn't keep job ids", st['error'])
        self.assertEqual(len(sent), 1, 'stopped after the first mismatch')

    def test_rejections_are_failures_not_aborts(self):
        jobs = [{'id': self.JID, 'name': 'A', 'protocol': ''}]
        st, _ = self._run(jobs, set(), lambda f: _Resp(200, [{'status': 'error', 'error': 'a job with id x already exists'}]))
        self.assertEqual((st['status'], st['failed']), ('complete', 1))
        self.assertIn('already exists', '\n'.join(st['log']))


class CompareTests(unittest.TestCase):
    def test_debrid_folders_are_checked_by_name_and_size_without_reading(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for root in (a, b):
                os.makedirs(os.path.join(root, 'Torrent'))
                with open(os.path.join(root, 'Torrent', 'x.mkv'), 'wb') as f:
                    f.write(b'z' * 10)
            os.makedirs(os.path.join(b, 'Renamed'))
            open(os.path.join(b, 'Renamed', 'y.mkv'), 'wb').close()
            eng = _Engine([{'id': 'rd-1', 'name': 'Torrent', 'protocol': 'debrid'},
                           {'id': 'rd-2', 'name': 'Renamed', 'protocol': 'debrid'}])
            with mock.patch.object(bm, '_digest', side_effect=AssertionError('debrid files must not be read')):
                out = bm.compare(eng, a, b)
        rows = {r['folder']: r for r in out['rows']}
        self.assertTrue(rows['Torrent']['ok'])
        self.assertFalse(rows['Renamed']['ok'])
        self.assertIn('not in cli_mount under this name', rows['Renamed']['detail'])


class RepairSettingsTests(unittest.TestCase):
    def test_copies_cli_mounts_repair_standards(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'config'))
            with open(os.path.join(d, 'config', 'config.json'), 'w') as f:
                json.dump({'repair': {'media_probe_usenet': True},
                           'usenet': {'availability_sample_percent': 10, 'import_availability_sample_percent': 0}}, f)
            self.assertEqual(bm.climount_repair_settings(d), {'media_probe': True, 'health_sample_percent': 10})
            self.assertEqual(bm.climount_repair_settings(os.path.join(d, 'nowhere')), {})


if __name__ == '__main__':
    unittest.main()


class SpaceTests(unittest.TestCase):
    """A big library must not fill the disk cli_debrid's database is on."""

    def _usage(self, free, total=100 << 30):
        from collections import namedtuple
        return namedtuple('U', 'total used free')(total, total - free, free)

    def test_plan_counts_factor_margin_and_cache(self):
        sizes = {'a': 1 << 30, 'b': 1 << 30, 'c': 1 << 30}
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch('shutil.disk_usage', return_value=self._usage(10 << 30)):
            p = bm.space_plan(['a', 'b', 'c'], sizes, os.path.join(d, 'engine'))  # dir not created yet
            self.assertEqual(p['needed'], int(3 * 1.5 * (1 << 30)))
            self.assertEqual(p['margin'], 5 << 30)  # 5% of 100 GB > 2 GB floor
            self.assertEqual((p['room'], p['fits'], p['fits_jobs']), (5 << 30, True, 3))
            # The disk cache on the same disk may still grow 2 GB: only 2 jobs fit.
            p = bm.space_plan(['a', 'b', 'c'], sizes, d, cache_dir=os.path.join(d, 'cache'), cache_left=2 << 30)
            self.assertEqual((p['cache_reserved'], p['fits'], p['fits_jobs']), (2 << 30, False, 2))

    def _run(self, free, limit=0, n=3):
        with tempfile.TemporaryDirectory() as d:
            mdir = os.path.join(d, 'usenet', 'meta')
            os.makedirs(mdir)
            entries = []
            for i in range(n):
                jid = f'job{i}'
                with open(os.path.join(mdir, jid + '.meta'), 'wb') as f:
                    f.write(META)
                entries.append({'name': f'Folder {i}', 'info_hash': jid})

            class Source:
                def iter_library_nzbs(self):
                    return iter(entries)
            eng = FakeEngine()
            with unittest.mock.patch('shutil.disk_usage', side_effect=free):
                jid = bm.start_import(Source(), eng, d, limit=limit, space={'data_dir': d})
                for _ in range(200):
                    if bm.status(jid).get('complete'):
                        break
                    import time
                    time.sleep(0.01)
            return bm.status(jid), eng

    def test_refuses_before_importing_when_it_does_not_fit(self):
        st, eng = self._run(lambda p: self._usage(0))
        self.assertEqual(st['status'], 'error')
        self.assertIn('Not enough disk space', st['error'])
        self.assertIn('import at most 0 job(s)', st['error'])
        self.assertEqual(eng.posts, [], 'nothing imported')

    def test_imports_when_it_fits_and_honours_the_job_count(self):
        st, eng = self._run(lambda p: self._usage(50 << 30), limit=2)
        self.assertEqual((st['status'], st['imported'], len(eng.posts)), ('complete', 2, 2))

    def test_stops_midway_when_the_disk_fills(self):
        calls = {'n': 0}

        def usage(p):
            calls['n'] += 1
            return self._usage(50 << 30 if calls['n'] == 1 else 0)
        with unittest.mock.patch.object(bm, 'SPACE_RECHECK_EVERY', 2):
            st, eng = self._run(usage, n=5)
        self.assertEqual(st['status'], 'error')
        self.assertIn('Stopped after 2 job(s)', st['error'])
        self.assertEqual(len(eng.posts), 2, 'jobs imported before the stop are kept')

    def test_copies_plex_protection_and_prewarm(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'config.json'), 'w') as f:
                json.dump({'usenet': {'interactive_pool_reserve_enabled': True, 'interactive_pool_reserve_percent': 11,
                                      'interactive_pool_reserve_min': 6, 'interactive_pool_reserve_max': 100},
                           'mount': {'dfs': {'prewarm_next_episode': True, 'prewarm_max_size': '512MB',
                                             'plex_url': 'http://10.0.0.2:32400/', 'plex_token': 'tok'}}}, f)
            self.assertEqual(bm.climount_repair_settings(d), {
                'plex_playback_protection': True, 'playback_reserve_percent': 11, 'playback_reserve_min': 6,
                'playback_reserve_max': 100, 'plex_prewarm': True, 'prewarm_max_mb': 512,
                'plex_url': 'http://10.0.0.2:32400', 'plex_token': 'tok'})

    def test_protection_off_turns_reserve_off(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'config.json'), 'w') as f:
                json.dump({'usenet': {'interactive_pool_reserve_enabled': False, 'interactive_pool_reserve_percent': 15}}, f)
            self.assertEqual(bm.climount_repair_settings(d),
                             {'plex_playback_protection': False, 'playback_reserve_percent': 0})

    def test_size_strings(self):
        self.assertEqual([bm._size_mb(x) for x in ('8MB', '1GB', '512KB', 'junk', 1 << 21)], [8, 1024, 0, None, 2])

    def test_copies_debrid_streaming(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'config.json'), 'w') as f:
                json.dump({'mount': {'dfs': {'chunk_size': '8MB', 'read_ahead_size': '32MB'}},
                           'debrids': [{'provider': 'realdebrid', 'api_key': 'main', 'download_api_keys': ['main', 'x2'],
                                        'torrents_refresh_interval': '10s', 'auto_expire_links_after': '3d'}]}, f)
            self.assertEqual(bm.climount_repair_settings(d), {
                'debrid_chunk_mb': 8, 'debrid_read_ahead_mb': 32, 'debrid_sync_seconds': 10,
                'debrid_link_lifetime_hours': 72, 'debrid_download_api_keys': 'x2'})

    def test_durations(self):
        self.assertEqual([bm._seconds(x) for x in ('10s', '5m', '3d', '1h30m', '', 'junk')], [10, 300, 259200, 5400, None, None])
