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

SERVERS = [{'host': 'news.b', 'port': 563, 'ssl': True, 'username': 'ub', 'password': 'pb', 'connections': 4},
           {'host': 'block.c', 'port': 119, 'ssl': False, 'username': 'uc', 'password': 'pc', 'connections': 2,
            'backup': True}]


class MountRoutesTests(unittest.TestCase):
    def setUp(self):
        self.store = {'Usenet Provider': {'enabled': True, 'provider': 'builtin'},
                      'Built-in Mount': {'primary_host': 'news.a', 'primary_password': 'pa', 'api_token': 'tok',
                                         'primary_connections': 10, 'additional_servers': json.dumps(SERVERS)}}

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
        self.assertEqual((pub['primary_password'], pub['primary_password_set']), ('', True))
        self.assertEqual((pub['api_token'], pub['api_token_set']), ('', True))
        self.assertTrue(all(s['password'] == '' and s['password_set'] for s in pub['additional_servers']))
        self.assertNotIn('pb', json.dumps(pub))

    def test_blank_passwords_keep_saved_ones(self):
        rows = [dict(s, password='') for s in SERVERS]
        code, body = self.save({'primary_password': '', 'primary_connections': 12, 'additional_servers': rows})
        self.assertEqual(code, 200, body)
        bm = self.store['Built-in Mount']
        self.assertEqual(bm['primary_password'], 'pa')
        self.assertEqual(bm['primary_connections'], 12)
        self.assertEqual([s['password'] for s in json.loads(bm['additional_servers'])], ['pb', 'pc'])
        self.sync.assert_called_once()

    def test_new_password_and_removed_row(self):
        code, _ = self.save({'additional_servers': [dict(SERVERS[1], password='new'), {'host': ''}]})
        self.assertEqual(code, 200)
        saved = json.loads(self.store['Built-in Mount']['additional_servers'])
        self.assertEqual([(s['host'], s['password'], s['backup']) for s in saved], [('block.c', 'new', True)])

    def test_bad_input_writes_nothing(self):
        before = json.dumps(self.store)
        for payload in ({'primary_connections': 'lots'}, {'mount_path': 'relative/path'},
                        {'additional_servers': [{'host': 'x', 'port': 'abc'}]}):
            code, body = self.save(payload)
            self.assertEqual(code, 400, payload)
            self.assertFalse(body['success'])
        self.assertEqual(json.dumps(self.store), before)
        self.sync.assert_not_called()

    def test_unchanged_save_does_not_resync(self):
        code, body = self.save({'primary_connections': 10})
        self.assertEqual((code, body['changed']), (200, []))
        self.sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
