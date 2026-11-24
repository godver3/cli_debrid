#!/usr/bin/env python3
"""Mount page (routes/mount_routes.py): secrets never reach the browser,
blank password fields keep the saved ones, and bad input is rejected
before anything is written."""

import tempfile
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import importlib.util
import types

from flask import Flask

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    """Load routes/mount_routes.py alone: importing the routes package pulls
    in the whole app. admin_required is stubbed to a pass-through."""
    models = types.ModuleType('routes.models')
    models.admin_required = lambda f: f
    with mock.patch.dict(sys.modules, {'routes.models': models}):
        spec = importlib.util.spec_from_file_location('mount_routes_under_test',
                                                      os.path.join(_ROOT, 'routes', 'mount_routes.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


mr = _load()

SERVERS = [{'host': 'news.a', 'port': 563, 'ssl': True, 'username': 'ua', 'password': 'pa', 'connections': 10},
           {'host': 'news.b', 'port': 563, 'ssl': True, 'username': 'ub', 'password': 'pb', 'connections': 4},
           {'host': 'block.c', 'port': 119, 'ssl': False, 'username': 'uc', 'password': 'pc', 'connections': 2,
            'backup': True}]


class MountRoutesTests(unittest.TestCase):
    def setUp(self):
        self.store = {'Usenet Provider': {'enabled': True, 'provider': 'builtin'},
                      'Built-in Mount': {'api_token': 'tok', 'read_ahead_mb': 16, 'servers': json.dumps(SERVERS)}}

        def get(section, key=None, default=None):
            sec = self.store.get(section, {})
            return sec if key is None else sec.get(key, default)

        def put(section, key, value):
            self.store.setdefault(section, {})[key] = value
        for target, fn in (('get_setting', get), ('set_setting', put)):
            p = mock.patch.object(mr, target, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        s = mock.patch.object(mr, '_sync_in_background')
        self.sync = s.start()
        self.addCleanup(s.stop)

    def save(self, payload):
        with Flask(__name__).test_request_context(json=payload):
            resp = mr.save_settings()
        r = resp[0] if isinstance(resp, tuple) else resp
        return (resp[1] if isinstance(resp, tuple) else 200), r.get_json()

    def test_public_settings_hide_secrets(self):
        pub = mr._public_settings()
        self.assertEqual((pub['api_token'], pub['api_token_set']), ('', True))
        self.assertTrue(all(s['password'] == '' and s['password_set'] for s in pub['servers']))
        self.assertNotIn('pb', json.dumps(pub))

    def test_blank_passwords_keep_saved_ones_after_reorder(self):
        rows = [dict(s, password='') for s in reversed(SERVERS)]  # moved around on the page
        code, body = self.save({'api_token': '', 'read_ahead_mb': 32, 'servers': rows})
        self.assertEqual(code, 200, body)
        bm = self.store['Built-in Mount']
        self.assertEqual((bm['api_token'], bm['read_ahead_mb']), ('tok', 32))
        saved = json.loads(bm['servers'])
        self.assertEqual([(s['host'], s['password']) for s in saved], [('block.c', 'pc'), ('news.b', 'pb'), ('news.a', 'pa')])
        self.sync.assert_called_once()

    def test_plan_end_is_saved_but_never_reaches_the_engine(self):
        rows = [dict(SERVERS[0], password='', expires='2026-12-31', auto_renew=True), dict(SERVERS[1], password='')]
        code, body = self.save({'servers': rows})
        self.assertEqual(code, 200, body)
        saved = json.loads(self.store['Built-in Mount']['servers'])
        self.assertEqual([(s['expires'], s['auto_renew']) for s in saved], [('2026-12-31', True), ('', False)])
        self.assertEqual(mr._public_settings()['servers'][0]['expires'], '2026-12-31')
        from utilities.builtin_engine import servers
        self.assertNotIn('expires', json.dumps(servers({'servers': saved})))

    def test_server_on_off_is_saved_and_password_kept(self):
        rows = [dict(SERVERS[0], password='', enabled=False)] + [dict(s, password='') for s in SERVERS[1:]]
        code, body = self.save({'servers': rows})
        self.assertEqual(code, 200, body)
        saved = json.loads(self.store['Built-in Mount']['servers'])
        self.assertEqual([(s['host'], s['enabled'], s['password']) for s in saved][0], ('news.a', False, 'pa'))
        self.assertTrue(all(s['enabled'] for s in saved[1:]), 'rows without the flag stay on')
        self.assertFalse(mr._public_settings()['servers'][0]['enabled'])
        self.sync.assert_called_once()

    def test_bad_plan_end_is_rejected(self):
        before = self.store['Built-in Mount']['servers']
        code, body = self.save({'servers': [dict(SERVERS[0], expires='31/12/2026')]})
        self.assertEqual(code, 400, body)
        self.assertIn('plan end', body['error'])
        self.assertEqual(self.store['Built-in Mount']['servers'], before)

    def test_new_password_and_removed_row(self):
        code, _ = self.save({'servers': [dict(SERVERS[2], password='new'), {'host': ''}]})
        self.assertEqual(code, 200)
        saved = json.loads(self.store['Built-in Mount']['servers'])
        self.assertEqual([(s['host'], s['password'], s['backup']) for s in saved], [('block.c', 'new', True)])

    def test_bad_input_writes_nothing(self):
        before = json.dumps(self.store)
        for payload in ({'read_ahead_mb': 'lots'}, {'mount_path': 'relative/path'}, {'umask': '9'},
                        {'import_sample_percent': 150}, {'servers': [{'host': 'x', 'port': 'abc'}]}):
            code, body = self.save(payload)
            self.assertEqual(code, 400, payload)
            self.assertFalse(body['success'])
        self.assertEqual(json.dumps(self.store), before)
        self.sync.assert_not_called()

    def test_queue_actions_go_to_the_engine(self):
        calls = []

        class FakeClient:
            def remove_nzb_exact(self, i):
                calls.append(('del', i))
                return i != 'stuck'

            def _call(self, method, path, timeout=0, **kw):
                calls.append((method, path))
                return (202, {}) if path.endswith('/health') else (200, {})
        with mock.patch.object(mr, '_engine', return_value=FakeClient()):
            with Flask(__name__).test_request_context(json={'ids': ['a', 'stuck', '']}):
                body = mr.jobs_delete().get_json()
            self.assertEqual((body['deleted'], body['failed'], body['success']), (1, ['stuck'], False))
            with Flask(__name__).test_request_context(method='POST'):
                ok = mr.job_action('j1', 'check')
                self.assertEqual((ok[1], ok[0].get_json()['success']), (200, True))
                resp = mr.job_action('j1', 'rm -rf')
            self.assertEqual(resp[1], 404)
        self.assertIn(('POST', '/jobs/j1/health'), calls)
        self.assertNotIn(('del', ''), calls)

    def test_internal_link_only_for_the_local_engine(self):
        from utilities import builtin_engine, mount_debrid

        def call(auth, addr='127.0.0.1'):
            with Flask(__name__).test_request_context(json={'ref': 'rd:A:1'}, headers={'Authorization': auth},
                                                      environ_base={'REMOTE_ADDR': addr}):
                r = mr.internal_link()
            return (r[1], r[0].get_json()) if isinstance(r, tuple) else (200, r.get_json())
        with mock.patch.object(builtin_engine, 'current_token', return_value='tok'), \
                mock.patch.object(mount_debrid, 'link', return_value=('https://cdn/x', 600)):
            self.assertEqual(call('Bearer tok'), (200, {'url': 'https://cdn/x', 'expires_in': 600}))
            self.assertEqual(call('Bearer nope')[0], 403)
            self.assertEqual(call('Bearer tok', addr='192.168.1.5')[0], 403, 'never from the network')
        with mock.patch.object(builtin_engine, 'current_token', return_value=''):
            self.assertEqual(call('Bearer ')[0], 403, 'no token configured: closed')

    def test_unchanged_save_does_not_resync(self):
        code, body = self.save({'read_ahead_mb': 16})
        self.assertEqual((code, body['changed']), (200, []))
        self.sync.assert_not_called()


    def _post(self, fn, payload=None):
        with Flask(__name__).test_request_context(json=payload or {}):
            resp = fn()
        r = resp[0] if isinstance(resp, tuple) else resp
        return (resp[1] if isinstance(resp, tuple) else 200), r.get_json()

    def _switch_ready(self, debrid_needed, debrid_status):
        self.store['Usenet Provider']['provider'] = 'climount'
        self.store['File Management'] = {'original_files_path': '/mnt/climount/__all__'}
        return mock.patch.multiple(mr, _debrid_needed=mock.Mock(return_value=debrid_needed),
                                   _debrid_status=mock.Mock(return_value=debrid_status))

    def test_switch_waits_for_the_debrid_mirror(self):
        with self._switch_ready(True, {'synced_at': 1.0, 'complete': False, 'pending': 120}), \
                mock.patch.object(mr, '_fuse_mounted', return_value=False):
            code, body = self._post(mr.migrate_switch)
        self.assertEqual(code, 409)
        self.assertIn('120 debrid torrent(s)', body['error'])
        self.assertEqual(self.store['Usenet Provider']['provider'], 'climount', 'nothing switched')
        with self._switch_ready(True, {'synced_at': 0, 'complete': False}), \
                mock.patch.object(mr, '_fuse_mounted', return_value=False):
            self.assertEqual(self._post(mr.migrate_switch)[0], 409, 'never listed yet')
        with self._switch_ready(True, {'synced_at': 1.0, 'complete': True}), \
                mock.patch.object(mr, '_fuse_mounted', return_value=False):
            code, body = self._post(mr.migrate_switch)
        self.assertEqual((code, self.store['Usenet Provider']['provider']), (200, 'builtin'))
        self.assertEqual(self.store['Built-in Mount']['mount_path'], '/mnt/climount')

    def test_switch_explains_a_mount_left_in_the_container(self):
        with self._switch_ready(False, {}), mock.patch.object(mr, '_fuse_mounted', return_value=True):
            with mock.patch.object(mr, '_dead_mount', return_value=False):
                code, body = self._post(mr.migrate_switch)
            self.assertEqual(code, 409)
            self.assertIn('stop the cli_mount container first', body['error'])
            with mock.patch.object(mr, '_dead_mount', return_value=True):
                code, body = self._post(mr.migrate_switch)
            self.assertEqual(code, 409)
            self.assertIn('recreate', body['error'])
        self.assertEqual(self.store['Usenet Provider']['provider'], 'climount', 'nothing switched')

    def test_dead_mount_is_enotconn_only(self):
        import errno
        with mock.patch.object(mr.os, 'scandir', side_effect=OSError(errno.ENOTCONN, 'Transport endpoint is not connected')):
            self.assertTrue(mr._dead_mount('/mnt/x'))
        with mock.patch.object(mr.os, 'scandir', side_effect=OSError(errno.ENOENT, 'gone')):
            self.assertFalse(mr._dead_mount('/mnt/x'))
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(mr._dead_mount(d))

    def test_switch_without_debrid_does_not_wait(self):
        with self._switch_ready(False, {}), mock.patch.object(mr, '_fuse_mounted', return_value=False):
            self.assertEqual(self._post(mr.migrate_switch)[0], 200)

    def test_switch_back_needs_the_program_stopped_and_keeps_the_engine_up(self):
        self.store['Built-in Mount'].update(mount_path='/mnt/climount', pre_migration_mount_path='/mnt/clid')
        with mock.patch.object(mr, '_program_running', return_value=True):
            code, body = self._post(mr.migrate_rollback)
        self.assertEqual(code, 409)
        self.assertEqual(self.store['Usenet Provider']['provider'], 'builtin')
        with mock.patch.object(mr, '_program_running', return_value=False):
            code, body = self._post(mr.migrate_rollback)
        self.assertEqual(code, 200)
        self.assertEqual(self.store['Usenet Provider']['provider'], 'climount')
        self.assertEqual(self.store['Built-in Mount']['mount_path'], '/mnt/clid')
        self.assertTrue(self.store['Built-in Mount']['run_alongside'], 'engine stays up for Copy back')

    def test_copy_back_only_after_switching_back(self):
        code, body = self._post(mr.migrate_export)
        self.assertEqual(code, 409)
        self.store['Usenet Provider']['provider'] = 'climount'
        from utilities import builtin_migrate as bm
        with mock.patch.object(bm, 'start_export', return_value='abc') as st, \
                mock.patch.object(mr, '_engine'), mock.patch.object(mr, '_climount_source'):
            code, body = self._post(mr.migrate_export)
        self.assertEqual((code, body['job']), (200, 'abc'))
        st.assert_called_once()

    def test_import_needs_the_engine_and_syncs_inside_the_import(self):
        from utilities import builtin_engine as be, builtin_migrate as bm
        self.store['Usenet Provider']['provider'] = 'climount'
        with mock.patch.object(be, 'wanted', return_value=False), \
                mock.patch.object(bm, 'start_import') as st:
            code, body = self._post(mr.migrate_import, {'limit': 0})
        self.assertEqual(code, 400)
        self.assertIn('Start alongside', body['error'])
        st.assert_not_called()
        # Running alongside but it can't start (no servers): say so at once.
        with mock.patch.object(be, 'wanted', return_value=True), \
                mock.patch.object(be, 'problems', return_value=['no usenet server set (Mount page → Usenet servers)']), \
                mock.patch.object(bm, 'start_import') as st:
            code, body = self._post(mr.migrate_import, {'limit': 0})
        self.assertEqual(code, 400)
        self.assertIn("can't start: no usenet server set", body['error'])
        st.assert_not_called()
        # Copied settings: the sync runs in the import thread (prepare), so
        # the import waits for an engine restart instead of racing it.
        with mock.patch.object(be, 'wanted', return_value=True), mock.patch.object(mr, '_engine_why_down', return_value=''), \
                mock.patch.object(bm, 'climount_repair_settings', return_value={'plex_prewarm': True}), \
                mock.patch.object(bm, 'start_import', return_value='job1') as st, \
                mock.patch.object(mr, '_engine'), mock.patch.object(mr, '_climount_source'), \
                mock.patch.object(mr, '_import_space', return_value=None):
            code, body = self._post(mr.migrate_import, {'limit': 0})
        self.assertEqual((code, body['job']), (200, 'job1'))
        self.assertIs(st.call_args.kwargs['prepare'], mr._sync_now)
        self.assertTrue(callable(st.call_args.kwargs['why_down']), 'Import can say why the engine is down')
        self.sync.assert_not_called()

    def test_status_counts_only_jobs_cli_mount_lists(self):
        # cli_mount's meta folder keeps maps of removed jobs (a user had 1093
        # maps for 226 jobs): the page counts what Import will import.
        from utilities import builtin_engine as be
        with tempfile.TemporaryDirectory() as d:
            mdir = os.path.join(d, 'usenet', 'meta')
            os.makedirs(mdir)
            for i in range(5):
                with open(os.path.join(mdir, f'j{i}.meta'), 'wb') as f:
                    f.write(b'\x0a' + b'x' * 100)
            self.store['Usenet Provider'].update(provider='climount', data_path=d)

            class Source:
                def iter_library_nzbs(self):
                    return iter([{'info_hash': 'j1'}, {'info_hash': 'j3'}])
            with mock.patch.object(mr, '_climount_source', return_value=Source()), \
                    mock.patch.object(mr, '_import_space', return_value={'data_dir': d, 'cache_dir': '', 'cache_left': 0}), \
                    mock.patch.object(be, 'state', return_value='RUNNING'), \
                    mock.patch.object(be, 'problems', return_value=[]), \
                    mock.patch.object(mr, '_debrid_needed', return_value=False), \
                    mock.patch.object(mr, '_debrid_status', return_value={}), \
                    Flask(__name__).test_request_context():
                body = mr.migrate_status().get_json()
        self.assertEqual((body['metas'], body['listed'], body['space']['jobs']), (5, 2, 2))


if __name__ == '__main__':
    unittest.main()


class FuseMountedTests(unittest.TestCase):
    """Switch must tell cli_mount's FUSE mount apart from Docker's bind mount
    of the same folder (os.path.ismount is True for both)."""

    MOUNTINFO = (
        "5920 5477 259:1 /decypharr_root/mnt /media/x/mnt rw,relatime shared:106 - ext4 /dev/nvme0n1p1 rw\n"
        "6001 5920 0:99 / /mnt/clid rw,nosuid,nodev - fuse.clid clid rw,user_id=1000\n"
        "6002 5920 0:98 / /media/my\\040mount rw - fuse.decypharr decypharr rw\n"
    )

    def _check(self, path):
        with mock.patch('builtins.open', mock.mock_open(read_data=self.MOUNTINFO)), \
                mock.patch.object(mr.os.path, 'realpath', side_effect=lambda p: p):
            return mr._fuse_mounted(path)

    def test_bind_mount_is_not_cli_mount(self):
        self.assertFalse(self._check('/media/x/mnt'))

    def test_fuse_mount_is_seen(self):
        self.assertTrue(self._check('/mnt/clid'))
        self.assertTrue(self._check('/media/my mount/'))

    def test_no_mountinfo_falls_back_to_ismount(self):
        with mock.patch('builtins.open', side_effect=OSError), \
                mock.patch.object(mr.os.path, 'ismount', return_value=True):
            self.assertTrue(mr._fuse_mounted('/anything'))


class EngineCallbacksPublicTests(unittest.TestCase):
    def test_engine_callbacks_skip_the_login_gate(self):
        """The engine has no login session; with the user system on, the
        login gate answered its link requests with 401 and every debrid read
        failed. The routes check the engine token + localhost themselves."""
        from routes import models
        self.assertIn('mount.internal_link', models._PUBLIC_ENDPOINTS)
        self.assertIn('mount.internal_check', models._PUBLIC_ENDPOINTS)
