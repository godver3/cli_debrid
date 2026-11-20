#!/usr/bin/env python3
"""
BuiltinClient translates the built-in engine's API into CliMountClient's
return shapes, and utilities/builtin_engine.py runs the engine only when it
is the chosen backend. The engine itself is tested in engine/ (Go).
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from usenet import base


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.content = b'' if body is None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError('no body')
        return self._body


JOB = {'id': 'j1', 'name': 'Show.S01E01', 'status': 'ready', 'path': '/mnt/clid/__all__/Show.S01E01',
       'cli_ids': {'Show.S01E01.mkv': 42}, 'tags': ['a'],
       'entries': [{'name': 'Show.S01E01.mkv', 'size': 1000}, {'name': 'Show.S01E01.nfo', 'size': 5}],
       'health': {'status': 'broken', 'broken_files': [
           {'file_name': 'Show.S01E01.mkv', 'reason': 'usenet_segment_missing', 'via': 'playback'}]}}


class FakeEngine:
    """Routes (method, path) to canned engine responses and records calls."""

    def __init__(self):
        self.calls = []
        self.routes = {
            ('GET', '/jobs'): _Resp(200, [JOB, {'id': 'j2', 'name': 'Queued.Thing', 'status': 'queued', 'path': ''}]),
            ('GET', '/jobs/j1'): _Resp(200, JOB),
            ('GET', '/jobs/gone'): _Resp(404, {'error': 'not found'}),
            ('GET', '/health'): _Resp(200, [JOB]),
            ('GET', '/stats'): _Resp(200, {'pools': [{'name': 'p', 'max': 20, 'open': 5, 'idle': 2},
                                                     {'name': 'b', 'max': 5, 'open': 0, 'idle': 0}],
                                            'overview': {'content': {'bytes': 300, 'usenet_bytes': 200, 'debrid_bytes': 100}}}),
            ('GET', '/changes'): _Resp(200, {'now': 1, 'jobs': [JOB, {'id': 'j2', 'status': 'queued'}], 'deleted': []}),
            ('POST', '/jobs'): _Resp(201, {'id': 'new1', 'status': 'queued'}),
            ('PATCH', '/jobs/j1'): _Resp(200, JOB),
            ('DELETE', '/jobs/j1'): _Resp(204),
            ('DELETE', '/jobs/gone'): _Resp(404, {'error': 'not found'}),
        }

    def request(self, method, url, headers=None, timeout=None, **kw):
        path = url.split('8283', 1)[1]
        self.calls.append((method, path, kw))
        return self.routes.get((method, path), _Resp(500, {'error': 'unrouted'}))


class BuiltinClientTests(unittest.TestCase):
    def setUp(self):
        settings = {
            ('Usenet Provider',): {'enabled': True, 'provider': 'builtin'},
            ('Built-in Mount',): {'api_port': 8283, 'api_token': 'tok'},
        }
        p = mock.patch('usenet.builtin_client.get_setting',
                       side_effect=lambda *a, **k: settings.get(a[:1], k.get('default', a[2] if len(a) > 2 else None))
                       if len(a) == 1 else ('Plex' if a == ('File Management', 'file_collection_management', 'Plex') else None))
        p.start()
        self.addCleanup(p.stop)
        self.engine = FakeEngine()
        r = mock.patch('usenet.builtin_client.requests.request', side_effect=self.engine.request)
        r.start()
        self.addCleanup(r.stop)
        from usenet.builtin_client import BuiltinClient
        self.c = BuiltinClient()

    def test_contract_and_capabilities(self):
        for name in [n for n, v in vars(base.UsenetBackend).items() if callable(v) and not n.startswith('_')]:
            self.assertTrue(callable(getattr(self.c, name, None)), name)
        self.assertTrue(base.supports(self.c, base.REPAIR_API))
        self.assertFalse(base.supports(self.c, base.SIDECAR))
        self.assertEqual(self.c.base_url, 'http://127.0.0.1:8283')

    def test_submit_and_status_shapes(self):
        self.assertEqual(self.c.add_nzb_content('<nzb/>', title='Show.S01E01', category='tv'), 'new1')
        method, path, kw = self.engine.calls[0]
        self.assertEqual((method, path), ('POST', '/jobs'))
        self.assertEqual(kw['params'], {'name': 'Show.S01E01', 'category': 'tv'})
        st = self.c.get_job_status('j1')
        self.assertEqual((st['state'], st['progress'], st['name']), ('completed', 100, 'Show.S01E01'))
        self.assertTrue(st['raw'])
        gone = self.c.get_job_status('gone')
        self.assertEqual(gone['raw'], {}, 'empty raw is the "not found" signal callers use')

    def test_file_lookup_reuses_cli_mount_selection(self):
        self.assertEqual(self.c.get_nzb_file_info('Show.S01E01', season=1, episode=1), ('Show.S01E01', 'Show.S01E01.mkv'))
        self.assertEqual(self.c.get_nzb_folder_all_files('Show.S01E01'), ('Show.S01E01', ['Show.S01E01.mkv']))

    def test_deletes_treat_404_as_gone(self):
        self.assertTrue(self.c.remove_nzb_exact('j1'))
        self.assertTrue(self.c.remove_nzb_exact('gone'))
        self.assertEqual(self.c.delete_queue_job('j1'), 200)

    def test_tags_merge_and_ids(self):
        self.assertTrue(self.c.push_tags('j1', 'a,b'))
        patch = [c for c in self.engine.calls if c[0] == 'PATCH'][-1]
        self.assertEqual(patch[2]['json'], {'tags': ['a', 'b']})
        self.assertTrue(self.c.register_cli_ids('j1', {'x.mkv': '7'}))
        self.assertEqual([c for c in self.engine.calls if c[0] == 'PATCH'][-1][2]['json'], {'cli_ids': {'x.mkv': 7}})

    def test_queue_listing_shape(self):
        jobs = list(self.c.iter_queue_jobs(search='show'))
        self.assertEqual(jobs, [{'name': 'Show.S01E01', 'info_hash': 'j1', 'state': 'completed', 'status': 'ready',
                                 'progress': 1.0, 'original_filename': 'Show.S01E01'}])
        self.assertEqual(self.c.find_job_hash_by_name('Show.S01E01'), 'j1')
        self.assertEqual([e['name'] for e in self.c.iter_library_nzbs()], ['Show.S01E01'])

    def test_broken_items_match_cli_mount_shape(self):
        items = self.c.fetch_broken_items()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it['entry_name'], it['file_name'], it['info_hash'], it['cli_debrid_id'],
                          it['failure_reason'], it['protocol'], it['hash_is_authoritative']),
                         ('Show.S01E01', 'Show.S01E01.mkv', 'j1', 42, 'usenet_segment_missing', 'nzb', True))
        broken = self.c.list_repair_health(status='broken')
        self.assertEqual(broken[0]['broken_files'][0]['reason'], 'usenet_segment_missing')

    def test_sync_feed_shape(self):
        status, page = self.c.fetch_sync_changes_page(1, 500, since_ts=100)
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls[-1][2]['params'], {'since': 100_000})  # seconds -> ms
        self.assertFalse(page['has_more'])
        self.assertEqual(len(page['changes']), 1, 'only ready jobs are mount entries')
        ch = page['changes'][0]
        self.assertEqual((ch['provider_id'], ch['folder_name'], ch['protocol'], ch['cli_debrid_ids']),
                         ('nzb:j1', 'Show.S01E01', 'nzb', {'Show.S01E01.mkv': 42}))
        self.assertEqual(self.c.fetch_sync_changes_page(2, 500)[1]['changes'], [])

    def _with_debrid_job(self):
        dj = {'id': 'rd-LQL6O4QJXWDRK', 'name': 'The Rings Of Power Season 2', 'status': 'ready',
              'protocol': 'debrid', 'provider': 'rd', 'hash': 'ABCDEF0123456789ABCDEF0123456789ABCDEF01',
              'path': '/mnt/clid/__all__/The Rings Of Power Season 2',
              'entries': [{'name': 'LotR The Rings Of Power S02E05.mp4', 'size': 2000}]}
        self.engine.routes[('GET', '/jobs')] = _Resp(200, [JOB, dj])
        self.engine.routes[('GET', '/changes')] = _Resp(200, {'now': 1, 'jobs': [JOB, dj], 'deleted': []})
        return dj

    def test_sync_feed_reports_debrid_jobs_as_torrents(self):
        """2026-11-18 test stack: debrid jobs went out as protocol nzb with
        provider_id "nzb:rd-<id>", and climount_sync wrote that onto the
        debrid items' filled_by_torrent_id (and nulled their magnet)."""
        self._with_debrid_job()
        changes = {c['folder_name']: c for c in self.c.fetch_sync_changes_page(1, 500)[1]['changes']}
        tor = changes['The Rings Of Power Season 2']
        self.assertEqual((tor['protocol'], tor['provider_id'], tor['info_hash']),
                         ('torrent', 'LQL6O4QJXWDRK', 'abcdef0123456789abcdef0123456789abcdef01'))
        self.assertEqual((changes['Show.S01E01']['protocol'], changes['Show.S01E01']['provider_id']), ('nzb', 'nzb:j1'))

        from usenet.climount_sync import _build_update
        sets, params = _build_update(tor)
        self.assertIn('LQL6O4QJXWDRK', params)
        self.assertFalse(any('nzb:' in str(p) for p in params))
        self.assertFalse(any('filled_by_magnet' in s for s in sets), 'a debrid item keeps its magnet')

    def test_usenet_listings_leave_out_debrid_jobs(self):
        self._with_debrid_job()
        self.assertEqual([j['info_hash'] for j in self.c.iter_queue_jobs()], ['j1'])
        self.assertEqual(self.c.find_job_hash_by_name('The Rings Of Power Season 2'), '')
        self.assertEqual([e['name'] for e in self.c.iter_library_nzbs()], ['Show.S01E01'])
        self.assertIsNone(self.c._find_nzb_folder('theringsofpowerseason2', 'The Rings Of Power Season 2'))
        self.assertFalse(self.c.trigger_health_check('The Rings Of Power Season 2'))
        self.assertTrue(self.c.remove_nzb('', entry_name='The Rings Of Power Season 2'))
        self.assertFalse([c for c in self.engine.calls if c[0] in ('DELETE', 'POST')],
                         'usenet paths never touch the debrid job')
        # Debrid repair still reaches it by folder.
        self.engine.routes[('DELETE', '/jobs/rd-LQL6O4QJXWDRK/health')] = _Resp(204)
        self.assertEqual(self.c.delete_repair_health('The Rings Of Power Season 2'), 200)

    def test_debug_stats_shape(self):
        st = self.c.get_debug_stats()
        self.assertEqual(st['usenet']['pool'], {'active': 3, 'idle': 2, 'max_connections': 25})
        self.assertEqual(st['repair']['health']['broken'], 1)
        # Library size on the stats page: the mount's content, not the debrid account.
        self.assertEqual(st['content'], {'total_size': 300, 'usenet_size': 200, 'debrid_size': 100})

    def test_generated_token_read_per_call(self):
        """The client often exists before startup sync writes engine.json."""
        self.c._configured_token = ''
        tokens = iter(['', 'gen1', 'gen2'])
        with mock.patch('utilities.builtin_engine.current_token', side_effect=lambda: next(tokens)):
            self.assertNotIn('Authorization', self.c._headers())
            self.assertEqual(self.c._headers()['Authorization'], 'Bearer gen1')
            self.assertEqual(self.c._headers()['Authorization'], 'Bearer gen2')

    def test_engine_log_in_cli_mount_format(self):
        import tempfile
        from usenet import builtin_client as bc
        self.assertEqual(bc.format_engine_log_line(
            'time=2026-11-17T09:46:37.927-06:00 level=WARN msg="import failed" job=abc err="job: x \\"y\\""'),
            '2026-11-17 09:46:37.927 | WARN | [mount] import failed job=abc err="job: x \\"y\\""')
        self.assertEqual(bc.format_engine_log_line('panic: boom'), 'panic: boom', 'non-slog lines pass through')
        self.assertEqual(bc.format_engine_log_line(
            'time=2026-11-20T17:59:30.174-06:00 level=DEBUG msg="debrid link" component=debrid '
            'file="Show S01/Show.S01E02.mkv" ref=rd:PQ:2 took=1.6s'),
            '2026-11-20 17:59:30.174 | DEBUG | [mount] [Show S01/Show.S01E02.mkv] debrid link component=debrid ref=rd:PQ:2 took=1.6s',
            'the file a line is about goes first')
        self.assertIn('[mount] read failed job=j',
                      bc.format_engine_log_line('time=2026-11-20T17:59:30.174Z level=WARN msg="read failed" job=j file= x=1'),
                      'empty file= is dropped')
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'mount_engine.log'), 'w') as f:
                f.write('time=2026-11-17T09:00:00Z level=INFO msg=mounted path=/mnt/clid\n\n'
                        'time=2026-11-17T09:00:01Z level=WARN msg="slow"\n')
            with mock.patch.dict(os.environ, {'USER_LOGS': d}):
                self.assertEqual(self.c.fetch_logs()['total'], 2)
                self.assertEqual(self.c.fetch_logs(level='warning')['lines'], ['2026-11-17 09:00:01Z | WARN | [mount] slow'])
                self.assertEqual(self.c.fetch_logs(lines=1)['lines'][0].split(' | ')[1], 'WARN')
        with mock.patch.dict(os.environ, {'USER_LOGS': '/nonexistent'}):
            self.assertEqual(self.c.fetch_logs(), {'lines': [], 'total': 0})
        self.assertTrue(base.supports(self.c, base.LOGS))

    def test_unreachable_engine(self):
        with mock.patch('usenet.builtin_client.requests.request', side_effect=ConnectionError('refused')):
            ok, err = self.c.check_connectivity()
            self.assertFalse(ok)
            self.assertIn('not running', err)
            self.assertIsNone(self.c.get_job_status('j1'))


class EngineManagerTests(unittest.TestCase):
    def setUp(self):
        from utilities import builtin_engine
        self.be = builtin_engine
        self.tmp = self.enterContext(__import__('tempfile').TemporaryDirectory())
        env = mock.patch.dict(os.environ, {'USER_CONFIG': self.tmp, 'USER_DB_CONTENT': self.tmp})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.state = 'STOPPED'

        def ctl(*args):
            self.calls.append(args)
            return f'mount_engine   {self.state}'
        p = mock.patch.object(builtin_engine, '_supervisorctl', side_effect=ctl)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(builtin_engine, 'problems', return_value=[])
        self.problems = q.start()
        self.addCleanup(q.stop)

    def settings(self, provider='builtin'):
        return {'Usenet Provider': {'enabled': True, 'provider': provider},
                'Built-in Mount': {'mount_path': '/mnt/clid/', 'cache_size_gb': 2,
                                   'servers': json.dumps([{'host': 'news.x', 'password': 'pw', 'connections': 30}])}}

    def test_config(self):
        cfg = self.be.build_config(self.settings())
        self.assertEqual(cfg['mount_path'], '/mnt/clid')
        self.assertEqual(cfg['listen'], '127.0.0.1:8283')
        self.assertEqual(cfg['cache_bytes'], 2 << 30)
        self.assertEqual(len(cfg['providers']), 1)
        self.assertEqual(cfg['providers'][0]['max_conns'], 30)
        self.assertTrue(cfg['token'])
        # cli_mount-style streaming settings map onto engine fields.
        self.assertEqual((cfg['read_ahead_mb'], cfg['max_conns_per_file'], cfg['mem_cache_bytes']), (16, 15, 512 << 20))
        self.assertEqual((cfg['import_timeout_seconds'], cfg['idle_timeout_seconds'], cfg['import_sample_percent']),
                         (300, 300, 1))
        s = self.settings()
        s['Built-in Mount'].update(umask='027', uid='1000', gid='', cache_expiry_hours=2, health_sample_percent=150)
        with mock.patch.dict(os.environ, {'PGID': '1001'}):
            cfg = self.be.build_config(s)
        self.assertEqual((cfg['umask'], cfg['uid'], cfg['gid']), (0o027, 1000, 1001))
        self.assertEqual((cfg['cache_expiry_seconds'], cfg['health_sample_percent']), (7200, 100))

    def test_servers_list(self):
        s = self.settings()
        s['Built-in Mount']['servers'] = json.dumps([
            {'host': 'block.c', 'port': 119, 'ssl': False, 'backup': True},
            {'host': 'news.a', 'username': 'u', 'password': 'p', 'connections': 30},
            {'host': 'news.b', 'connections': 4}])
        ps = self.be.build_config(s)['providers']
        self.assertEqual([(p['host'], p['port'], p['tls'], p['max_conns'], p['backup']) for p in ps],
                         [('block.c', 119, False, 10, True), ('news.a', 563, True, 30, False),
                          ('news.b', 563, True, 4, False)], 'order kept; the engine itself asks backups last')
        # warm connections (not on backups) and the playback reserve (>= 1, none on 1-conn servers)
        self.assertEqual([(p['min_idle'], p['reserve']) for p in ps], [(0, 1), (2, 3), (2, 1)])
        s['Built-in Mount'].update(warm_connections=0, playback_reserve_percent=0)
        self.assertEqual({(p['min_idle'], p['reserve']) for p in self.be.build_config(s)['providers']}, {(0, 0)})
        s['Built-in Mount']['servers'] = ''
        for bad in ('{not json', '{"host": "x"}', '[{"port": 1}]', '[{"host": "x", "port": "abc"}]'):
            s['Built-in Mount']['servers'] = bad
            with self.assertRaises(ValueError):
                self.be.servers(s['Built-in Mount'])

    def test_user_stop_survives_sync_until_start(self):
        self.state = 'RUNNING'
        self.assertEqual(self.be.user_stop(), 'stopped')
        self.state = 'STOPPED'
        self.calls.clear()
        self.assertEqual(self.be.sync(self.settings()), 'stopped by user', 'startup/settings save keeps it stopped')
        self.assertNotIn(('start', 'mount_engine'), self.calls)
        self.assertTrue(os.path.exists(self.be.config_path()), 'config still written while stopped')
        self.assertEqual(self.be.user_start(self.settings()), 'started')
        self.assertFalse(self.be.stopped_by_user())

    def test_startup_with_autostart_overrides_a_manual_stop(self):
        self.state = 'RUNNING'
        self.be.user_stop()
        self.state = 'STOPPED'
        s = self.settings()
        self.assertEqual(self.be.startup(s), 'stopped by user', 'autostart off keeps a manual Stop')
        self.assertTrue(self.be.stopped_by_user())
        s['Debug'] = {'auto_run_program': True}
        self.calls.clear()
        self.assertEqual(self.be.startup(s), 'autostart: started')
        self.assertIn(('start', 'mount_engine'), self.calls)
        self.assertFalse(self.be.stopped_by_user())

    def test_startup_never_starts_for_an_external_mount(self):
        self.be.user_stop()
        s = self.settings(provider='climount')
        s['Debug'] = {'auto_run_program': True}
        self.assertEqual(self.be.startup(s), 'not selected')
        self.assertNotIn(('start', 'mount_engine'), self.calls)
        self.assertTrue(self.be.stopped_by_user(), 'flag left alone when the mount is not in use')

    def test_wait_until_mounted(self):
        s = self.settings()
        self.state = 'RUNNING'
        seen = iter([False, False, True])
        with mock.patch.object(self.be, 'fuse_mounted', side_effect=lambda p: next(seen)), \
                mock.patch('time.sleep'):
            self.assertTrue(self.be.wait_until_mounted(s, timeout=60))
        # Not started yet (container start: the program comes up first) is waited for.
        self.state = 'STOPPED'
        seen = iter([False, False, True])
        with mock.patch.object(self.be, 'fuse_mounted', side_effect=lambda p: next(seen)), \
                mock.patch('time.sleep'):
            self.assertTrue(self.be.wait_until_mounted(s, timeout=60))
        with mock.patch.object(self.be, 'fuse_mounted', return_value=False) as fm:
            self.state = 'FATAL'
            self.assertFalse(self.be.wait_until_mounted(s, timeout=60), 'a crashed mount is not waited for')
            self.assertEqual(fm.call_count, 2)
            fm.reset_mock()
            self.state = 'STOPPED'
            self.be.user_stop()
            self.assertFalse(self.be.wait_until_mounted(s, timeout=60), 'stopped by hand: no wait')
            self.assertEqual(fm.call_count, 2)
            fm.reset_mock()
            self.assertTrue(self.be.wait_until_mounted(self.settings(provider='climount')), 'external mount: no wait')
            fm.assert_not_called()

    def test_sync_reports_problems_instead_of_starting(self):
        self.problems.return_value = ['/dev/fuse is not available']
        self.assertTrue(self.be.sync(self.settings()).startswith('cannot start'))
        self.assertNotIn(('start', 'mount_engine'), self.calls)


class WindowsProcessTests(unittest.TestCase):
    """Windows has no supervisord: the engine is a detached process with a
    pid file, stopped through its /shutdown API."""

    def setUp(self):
        from utilities import builtin_engine
        self.be = builtin_engine
        self.tmp = self.enterContext(__import__('tempfile').TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {'USER_CONFIG': self.tmp, 'USER_LOGS': self.tmp,
                                                       'USER_DB_CONTENT': self.tmp}))
        self.enterContext(mock.patch.object(builtin_engine, 'IS_WINDOWS', True))

    def test_start_detached_and_state_from_pid(self):
        popen = mock.Mock(return_value=mock.Mock(pid=4242))
        with mock.patch.object(self.be.subprocess, 'Popen', popen), mock.patch.object(self.be, '_proc', return_value=None):
            self.assertEqual(self.be.control('start'), 'started')
        args, kw = popen.call_args
        self.assertEqual(args[0][1:], ['--config', self.be.config_path()])
        self.assertIs(kw['stdin'], self.be.subprocess.DEVNULL)
        self.assertEqual(open(os.path.join(self.tmp, 'engine.pid')).read(), '4242')
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'mount_engine.log')))
        with mock.patch.object(self.be, '_proc', return_value=object()):
            self.assertEqual(self.be.state(), 'RUNNING')
            self.assertEqual(self.be.control('start'), 'already running')
        with mock.patch.object(self.be, '_proc', return_value=None):
            self.assertEqual(self.be.state(), 'STOPPED')

    def test_stop_asks_the_engine_then_waits(self):
        with open(self.be.config_path(), 'w') as f:
            json.dump({'listen': '127.0.0.1:8283', 'token': 'tok'}, f)
        proc = mock.Mock()
        opened = []
        with mock.patch.object(self.be, '_proc', return_value=proc), \
                mock.patch('urllib.request.urlopen', side_effect=lambda r, timeout: opened.append(r) or mock.Mock()):
            self.assertEqual(self.be.control('stop'), 'stopped')
        self.assertEqual((opened[0].full_url, opened[0].get_method(), opened[0].headers['Authorization']),
                         ('http://127.0.0.1:8283/shutdown', 'POST', 'Bearer tok'))
        proc.wait.assert_called_once_with(timeout=20)
        proc.terminate.assert_not_called()

    def test_windows_paths_and_problems(self):
        self.assertEqual(self.be.mount_path({'mount_path': '/mnt/clid'}), 'M:', 'Linux default becomes a drive')
        self.assertEqual(self.be.mount_path({'mount_path': 'C:\\clid\\'}), 'C:\\clid')
        with mock.patch.object(self.be, '_WINFSP_DLLS', ['/nonexistent/winfsp-x64.dll']), \
                mock.patch.object(self.be, 'ENGINE_BIN', '/nonexistent/clid-engine.exe'):
            probs = ' '.join(self.be.problems({'Built-in Mount': {'servers': '[{"host": "h"}]'}}))
        self.assertIn('WinFsp', probs)
        self.assertNotIn('/dev/fuse', probs)


class FactoryTests(unittest.TestCase):
    def test_builtin_routing_and_configured(self):
        cfg = {'enabled': True, 'provider': 'builtin', 'url': ''}
        import usenet

        def fake(*a, **k):
            if a == ('Usenet Provider',):
                return cfg
            if len(a) >= 2 and a[0] == 'Usenet Provider':
                return cfg.get(a[1], a[2] if len(a) > 2 else None)
            return None
        with mock.patch('usenet.get_setting', side_effect=fake):
            self.assertTrue(usenet.usenet_configured())
            with mock.patch('usenet.builtin_client.get_builtin_client', return_value='BUILTIN'):
                self.assertEqual(usenet.get_usenet_client(), 'BUILTIN')
            self.assertEqual(usenet.get_usenet_provider_display_name(), 'Built-in mount')
            cfg['provider'] = 'climount'
            self.assertFalse(usenet.usenet_configured(), 'external backends still need a URL')


if __name__ == '__main__':
    unittest.main()
