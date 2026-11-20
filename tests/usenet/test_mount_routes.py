#!/usr/bin/env python3
"""Mount page (routes/mount_routes.py): secrets never reach the browser,
blank password fields keep the saved ones, and bad input is rejected
before anything is written."""

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
