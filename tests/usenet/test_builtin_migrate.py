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
    """Records POSTs; GET /status (the readiness check) answers 200.
    reachable(method, path, call_no) -> False makes a call fail like a
    refused connection."""
    def __init__(self, code=201, reachable=None):
        self.code, self.posts, self.reachable, self.calls, self.last_error = code, [], reachable, 0, ''

    def _call(self, method, path, timeout=0, **kw):
        self.calls += 1
        if self.reachable and not self.reachable(method, path, self.calls):
            self.last_error = 'Connection refused'
            return None, None
        if method == 'GET':
            return 200, {}
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
            outcome, msg, _ = bm.import_one(FakeEngine(reachable=lambda *a: False), entry, d)
            self.assertEqual(outcome, 'unreachable')
            self.assertIn('Connection refused', msg, 'the real reason, not just "unreachable"')

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

    def test_climount_mount_root_in_direct_mount(self):
        def getter(mode, original, mounted):
            values = {('File Management', 'file_collection_management'): mode,
                      ('File Management', 'original_files_path'): original,
                      ('Plex', 'mounted_file_location'): mounted}
            return lambda s, k, d='': values.get((s, k), d)
        # Direct Mount: its own path wins, even over a stale original files path.
        self.assertEqual(bm.climount_mount_root(getter('Plex', '/mnt/zurg/__all__', '/mnt/decypharr/__all__')),
                         '/mnt/decypharr')
        # Onboarding hides the original files path in Direct Mount, so it's often blank.
        self.assertEqual(bm.climount_mount_root(getter('Plex', '', '/mnt/decypharr/__all__')), '/mnt/decypharr')
        # Symlinked/Local: the original files path, else the Direct Mount one.
        self.assertEqual(bm.climount_mount_root(getter('Symlinked/Local', '/mnt/cm/__all__', '/mnt/x/__all__')), '/mnt/cm')
        self.assertEqual(bm.climount_mount_root(getter('Symlinked/Local', '', '/mnt/x/__all__')), '/mnt/x')
        self.assertEqual(bm.climount_mount_root(getter('Plex', '', '')), '')


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

    def _run(self, free, limit=0, n=3, eng=None, prepare=None, why_down=None, wait=0.2):
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
            eng = eng or FakeEngine()
            with unittest.mock.patch('shutil.disk_usage', side_effect=free), \
                    unittest.mock.patch.object(bm, 'ENGINE_WAIT', wait), unittest.mock.patch.object(bm, 'ENGINE_POLL', 0.01):
                jid = bm.start_import(Source(), eng, d, limit=limit, space={'data_dir': d}, prepare=prepare,
                                       why_down=why_down)
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

    def test_engine_down_stops_once_with_a_clear_error(self):
        # Engine not running: one clear error, not a "failed" line per job.
        st, eng = self._run(lambda p: self._usage(50 << 30), eng=FakeEngine(reachable=lambda *a: False))
        self.assertEqual((st['status'], st['failed'], st['done']), ('error', 0, 0))
        self.assertIn("engine isn't answering (Connection refused)", st['error'])
        self.assertIn('Start alongside', st['error'])
        self.assertEqual(eng.posts, [])

    def test_engine_that_cannot_start_ends_the_wait_with_the_reason(self):
        # No 90 s blind wait: the engine's own reason ends it at once.
        st, eng = self._run(lambda p: self._usage(50 << 30), eng=FakeEngine(reachable=lambda *a: False),
                            why_down=lambda: "it can't start: no usenet server set", wait=60)
        self.assertEqual((st['status'], st['total'], st['failed']), ('error', 3, 0))
        self.assertIn("isn't running: it can't start: no usenet server set", st['error'])
        self.assertEqual(eng.calls, 1, 'one readiness check, no 60 s wait')

    def test_waits_for_the_engine_after_the_settings_sync(self):
        # The sync (prepare) restarts the engine: the import waits for it
        # instead of racing through every job while it's down.
        state = {'synced': False, 'up_after': None}

        def prepare():
            state['synced'] = True
            state['up_after'] = eng.calls + 3   # down for the next 3 calls

        def reachable(method, path, n):
            return state['up_after'] is not None and n > state['up_after']
        eng = FakeEngine(reachable=reachable)
        st, _ = self._run(lambda p: self._usage(50 << 30), eng=eng, prepare=prepare)
        self.assertTrue(state['synced'])
        self.assertEqual((st['status'], st['imported'], st['failed']), ('complete', 3, 0))

    def test_engine_restart_mid_run_retries_the_job(self):
        # 2nd import POST hits a restart: wait, retry it, carry on.
        def reachable(method, path, n):
            return not (method == 'POST' and n == 3)
        st, eng = self._run(lambda p: self._usage(50 << 30), eng=FakeEngine(reachable=reachable))
        self.assertEqual((st['status'], st['imported'], st['failed']), ('complete', 3, 0))
        self.assertEqual(len(eng.posts), 3)

    def test_engine_gone_mid_run_stops_instead_of_failing_the_rest(self):
        def reachable(method, path, n):
            return n <= 2   # readiness check + first import, then gone
        st, eng = self._run(lambda p: self._usage(50 << 30), n=5, eng=FakeEngine(reachable=reachable))
        self.assertEqual((st['status'], st['imported'], st['failed']), ('error', 1, 0))
        self.assertIn('Stopped after 1 job(s) (1 imported)', st['error'])

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


class BackboneTests(unittest.TestCase):
    """cli_mount's per-server backbones carry over to the built-in servers."""

    def test_backbones_read_and_applied_by_host(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'config.json'), 'w') as f:
                json.dump({'usenet': {'providers': [
                    {'host': 'News.Newshosting.com', 'backbone': 'omicron'},
                    {'host': 'news.easynews.com', 'backbone': ' omicron '},
                    {'host': 'news.tweaknews.eu'}]}}, f)
            got = bm.climount_repair_settings(d)
        bb = got[bm.BACKBONES_KEY]
        self.assertEqual(bb, {'news.newshosting.com': 'omicron', 'news.easynews.com': 'omicron'})
        servers = [{'host': 'news.newshosting.com'}, {'host': 'news.easynews.com', 'backbone': 'mine'},
                   {'host': 'news.tweaknews.eu'}]
        out, changed = bm.apply_backbones(servers, bb)
        self.assertEqual(changed, 1)
        self.assertEqual([s.get('backbone') for s in out], ['omicron', 'mine', None])
        self.assertNotIn('backbone', servers[0])  # input not modified

    def test_engine_config_passes_backbone(self):
        from utilities.builtin_engine import servers
        got = servers({'servers': [{'host': 'a', 'backbone': ' omicron '}, {'host': 'b'}]})
        self.assertEqual([s['backbone'] for s in got], ['omicron', ''])


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _fixture(name):
    with open(os.path.join(FIXTURES, name), 'rb') as f:
        return f.read()


def _expected_segs(n, size, data_start, prefix):
    """The segment map the fixture generator wrote (last segment a third)."""
    out, off = [], 0
    for i in range(n):
        b = size // 3 if i == n - 1 else size
        out.append({'number': i + 1, 'id': f'{prefix}-{i + 1:04d}@news.example', 'start': off,
                    'end': off + b - 1, 'data_start': data_start})
        off += b
    return out


class UpstreamV2MetaTests(unittest.TestCase):
    """Upstream decypharr rewrites every .meta into its zstd "v2" format
    (pkg/usenet/nzbcodec.go). The fixtures were written by upstream's own
    encodeNZBV2: a 300-segment file, a deleted file, a file with no
    articles, and an encrypted RAR member."""

    def test_plain_job(self):
        m = bm.parse_meta(_fixture('v2_plain.meta'))
        self.assertEqual((m['id'], m['name']), ('a1b2c3d4e5f6', 'Some.Movie.2026.1080p.WEB-DL-GRP'))
        self.assertEqual([(f['name'], f['size']) for f in m['files']],
                         [('Some.Movie.2026.1080p.WEB-DL-GRP.mkv', 300 * 716800), ('Extras.mkv', 4 * 716800)],
                         'deleted sample.mkv left out')
        self.assertEqual(m['files'][0]['segments'], _expected_segs(300, 716800, 0, 'main'))
        self.assertEqual(m['files'][1]['segments'], _expected_segs(4, 716800, 3, 'ex'),
                         'columns of a later file start after the earlier files\' segments')
        self.assertNotIn('key', m['files'][0])
        self.assertEqual(m['skipped'], [('empty.nfo', 'no articles')])

    def test_encrypted_member_keeps_key_iv_and_plaintext_size(self):
        m = bm.parse_meta(_fixture('v2_encrypted.meta'))
        f = m['files'][0]
        self.assertEqual((f['name'], f['size'], f['key'], f['iv']),
                         ('Some.Movie.2026.1080p.WEB-DL-GRP.mkv', 1234567, b'0123456789abcdef' * 2, b'ivivivivivivivi!'))
        self.assertEqual(f['segments'], _expected_segs(5, 716800, 77, 'enc'))

    def test_truncated_file_is_an_error_not_a_crash(self):
        data = _fixture('v2_plain.meta')
        with self.assertRaises(Exception):
            bm.parse_meta(data[:40])

    def test_import_one_from_v2(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a1b2c3d4e5f6.meta'), 'wb') as f:
                f.write(_fixture('v2_plain.meta'))
            with open(os.path.join(d, 'broken.meta'), 'wb') as f:
                f.write(_fixture('v2_plain.meta')[:40])
            eng = FakeEngine()
            self.assertEqual(bm.import_one(eng, {'name': 'Some Movie', 'info_hash': 'a1b2c3d4e5f6'}, d),
                             ('imported', '', 1))
            body = eng.posts[0][2]
            self.assertEqual((body['id'], len(body['files']), len(body['files'][0]['segments'])),
                             ('a1b2c3d4e5f6', 2, 300))
            outcome, msg, _ = bm.import_one(eng, {'name': 'Broken', 'info_hash': 'broken'}, d)
            self.assertEqual(outcome, 'failed')
            self.assertIn('Broken: unreadable metadata', msg)

    def test_space_estimate_counts_v2_at_protobuf_size(self):
        with tempfile.TemporaryDirectory() as d:
            v2 = _fixture('v2_plain.meta')
            with open(os.path.join(d, 'new.meta'), 'wb') as f:
                f.write(v2)
            with open(os.path.join(d, 'old.meta'), 'wb') as f:
                f.write(META)
            self.assertEqual(bm.meta_sizes(d), {'new': len(v2) * bm.V2_SIZE_FACTOR, 'old': len(META)})
