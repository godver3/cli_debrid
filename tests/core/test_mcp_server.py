#!/usr/bin/env python3
"""Read-only MCP endpoint (utilities/mcp_server.py, routes/mcp_routes.py)."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import mcp_server as mcp

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_routes():
    """routes/mcp_routes.py alone (the routes package pulls in the whole app)."""
    import importlib.util
    import types
    models = types.ModuleType('routes.models')
    models.admin_required = lambda f: f
    with mock.patch.dict(sys.modules, {'routes.models': models}):
        spec = importlib.util.spec_from_file_location('mcp_routes_under_test', os.path.join(_ROOT, 'routes', 'mcp_routes.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class ProtocolTests(unittest.TestCase):
    def test_initialize_negotiates_version(self):
        r = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't'}}}, '0.7.89')
        self.assertEqual(r['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(r['result']['serverInfo'], {'name': 'cli_debrid', 'version': '0.7.89'})
        self.assertIn('tools', r['result']['capabilities'])
        r = mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {'protocolVersion': '1999-01-01'}})
        self.assertEqual(r['result']['protocolVersion'], mcp.PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_reply_and_unknown_methods_error(self):
        self.assertIsNone(mcp.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        self.assertEqual(mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'nope'})['error']['code'], -32601)
        self.assertEqual(mcp.handle({'id': 4})['error']['code'], -32600)

    def test_tools_are_listed_read_only(self):
        tools = mcp.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'})['result']['tools']
        names = {t['name'] for t in tools}
        self.assertTrue({'get_overview', 'explain_setting', 'search_logs', 'lookup_item', 'mount_status'} <= names)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools))
        self.assertTrue(all(t['inputSchema']['type'] == 'object' for t in tools))

    def test_unknown_tool_and_bad_arguments(self):
        r = mcp.call_tool('drop_database', {})
        self.assertTrue(r['isError'])
        with mock.patch.dict(mcp.TOOLS, {'boom': {'fn': lambda: 1 / 0, 'description': '', 'schema': mcp._obj({})}}):
            self.assertTrue(mcp.call_tool('boom', {})['isError'])


class RedactionTests(unittest.TestCase):
    def test_secrets_masked_in_dicts_lists_and_json_strings(self):
        servers = json.dumps([{'host': 'news.x', 'username': 'me', 'password': 'hunter2'}])
        out = mcp.redact({'Debrid Provider': {'provider': 'RealDebrid', 'api_key': 'SECRET'},
                          'fallback_providers': [{'provider': 'TB', 'api_key': 'S2'}],
                          'servers': servers, 'plex_token': ''})
        text = json.dumps(out)
        for secret in ('SECRET', 'S2', 'hunter2', '"me"'):
            self.assertNotIn(secret, text)
        self.assertEqual(out['Debrid Provider']['provider'], 'RealDebrid')
        self.assertEqual(out['servers'][0]['host'], 'news.x')
        self.assertEqual(out['plex_token'], mcp.MASK_UNSET)

    def test_tool_output_goes_through_the_log_scrubber(self):
        with mock.patch.dict(mcp.TOOLS, {'t': {'fn': lambda: {'msg': 'url?apikey=abc'}, 'description': '',
                                               'schema': mcp._obj({})}}), \
                mock.patch.object(mcp, '_scrub', side_effect=lambda s: s.replace('abc', '[REDACTED]')) as scrub:
            text = mcp.call_tool('t', {})['content'][0]['text']
        scrub.assert_called_once()
        self.assertNotIn('abc', text)

    def test_explain_setting_masks_sensitive_schema_keys(self):
        schema = {'Built-in Mount': {'webdav_password': {'type': 'string', 'default': '', 'sensitive': True,
                                                         'description': 'pw'},
                                     'read_ahead_mb': {'type': 'integer', 'default': 16, 'description': ['Read', 'ahead']}}}
        cur = {'webdav_password': 'topsecret', 'read_ahead_mb': 32}
        with mock.patch.object(mcp, '_schema', return_value=schema), \
                mock.patch.object(mcp, '_get_setting', return_value=cur):
            pw = mcp.tool_explain_setting('built-in mount', 'webdav_password')
            ra = mcp.tool_explain_setting('Built-in Mount', 'READ_AHEAD_MB')
        self.assertEqual(pw['value'], mcp.MASK_SET)
        self.assertNotIn('default', pw)
        self.assertEqual((ra['value'], ra['default'], ra['description']), (32, 16, 'Read ahead'))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        mr = _load_routes()
        app = Flask(__name__)
        app.register_blueprint(mr.mcp_bp, url_prefix='/mcp')
        self.client = app.test_client()
        self.cfg = {'enabled': True, 'token': 'tok'}
        p = mock.patch.object(mr, 'get_setting', side_effect=lambda s, k, d=None: self.cfg.get(k, d))
        p.start()
        self.addCleanup(p.stop)

    def post(self, body, token='tok'):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return self.client.post('/mcp', data=json.dumps(body), headers=headers, content_type='application/json')

    def test_token_required_even_without_user_system(self):
        ping = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        self.assertEqual(self.post(ping, token=None).status_code, 401)
        self.assertEqual(self.post(ping, token='wrong').status_code, 401)
        r = self.post(ping)
        self.assertEqual((r.status_code, r.get_json()['result']), (200, {}))
        self.cfg['token'] = ''
        self.assertEqual(self.post(ping, token='').status_code, 401, 'no token configured: closed')

    def test_off_by_default(self):
        self.cfg['enabled'] = False
        self.assertEqual(self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).status_code, 404)

    def test_notification_is_202_batch_and_get(self):
        self.assertEqual(self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code, 202)
        r = self.post([{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, {'jsonrpc': '2.0', 'method': 'notifications/x'}])
        self.assertEqual(len(r.get_json()), 1)
        self.assertEqual(self.client.get('/mcp').status_code, 405)
        bad = self.client.post('/mcp', data='{oops', headers={'Authorization': 'Bearer tok'})
        self.assertEqual(bad.get_json()['error']['code'], -32700)

    def test_mcp_endpoint_bypasses_login_gate(self):
        with open(os.path.join(_ROOT, 'routes', 'models.py')) as f:
            self.assertIn("'mcp.endpoint'", f.read().split('_PUBLIC_ENDPOINTS = frozenset(')[1].split('})')[0])


if __name__ == '__main__':
    unittest.main()
