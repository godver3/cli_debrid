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
        self.assertEqual(r['result']['serverInfo'], {'name': 'clyde', 'version': '0.7.89'})
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


class HelpAndReportTests(unittest.TestCase):
    def test_help_topics_and_page(self):
        out = mcp.tool_get_help()
        self.assertIn('queues', out['topics'])
        page = mcp.tool_get_help('Queues')
        self.assertTrue(page['text'].strip())
        self.assertIn('did_you_mean', mcp.tool_get_help('queue'))
        self.assertIn('error', mcp.tool_get_help('../../etc/passwd'))

    def test_issue_report_bundle(self):
        with mock.patch.object(mcp, 'tool_get_overview', return_value={'version': '0.7.89', 'library_mode': 'Symlinked/Local',
                                                                         'media_server': 'plex', 'debrid_provider': 'RealDebrid'}), \
                mock.patch('utilities.ai_context.get_diagnostic_settings_snapshot', return_value='App version: 0.7.89'), \
                mock.patch.object(mcp, 'tool_search_logs', return_value={'entries': [{'message': 'boom'}]}) as logs, \
                mock.patch.object(mcp, 'tool_mount_status', return_value={'built_in_mount': True}), \
                mock.patch.object(mcp, 'tool_lookup_item', return_value={'items': []}) as item:
            out = mcp.tool_prepare_issue_report('Friends S10 plays the wrong episode', 'Friends')
        self.assertIn('Friends S10 plays the wrong episode', out['draft'])
        self.assertIn('Clyde 0.7.89, Symlinked/Local', out['draft'])
        self.assertEqual(out['settings_snapshot'], 'App version: 0.7.89')
        self.assertEqual({c.args[2] for c in logs.call_args_list}, {'app', 'mount'})
        item.assert_called_once_with('Friends', limit=5)
        self.assertTrue(any('Share' in t for t in out['tips']))


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


class NewFeatureTests(unittest.TestCase):
    """Settings outside the schema, the debrid chain and the external scan (0.9.3)."""

    def test_extra_sections_listed_and_masked(self):
        cfg = {'NFO': {'mode': 'changed', 'snapshot_id': 3, 'tvdb_api_key': 'abc'}}
        with mock.patch.object(mcp, '_schema', return_value={'Scraping': {}}), \
                mock.patch.object(mcp, '_get_setting', side_effect=lambda s, *a: cfg.get(s, {})):
            self.assertIn('NFO', mcp.tool_list_settings()['sections'])
            out = mcp.tool_list_settings('nfo')
        self.assertEqual(out['section'], 'NFO')
        self.assertIn('NFO Files', out['edited_on'])
        rows = {r['setting']: r['value'] for r in out['settings']}
        self.assertEqual(rows['mode'], 'changed')
        self.assertEqual(rows['tvdb_api_key'], mcp.MASK_SET)

    def test_fallback_providers_shown_with_keys_masked(self):
        schema = {'Debrid Provider': {'provider': {'type': 'string', 'default': 'RealDebrid'}}}
        cur = {'provider': 'RealDebrid', 'fallback_providers': [
            {'provider': 'Torbox', 'api_key': 'SECRETKEY', 'stream_only': True}]}
        with mock.patch.object(mcp, '_schema', return_value=schema), \
                mock.patch.object(mcp, '_get_setting', return_value=cur):
            row = mcp.tool_explain_setting('Debrid Provider', 'fallback_providers')
            text = mcp.call_tool('list_settings', {'section': 'Debrid Provider'})['content'][0]['text']
        self.assertEqual(row['value'], [{'provider': 'Torbox', 'api_key': mcp.MASK_SET, 'stream_only': True}])
        self.assertIn('streaming', row['description'].lower())
        self.assertNotIn('SECRETKEY', text)

    def test_debrid_chain_splits_streaming_only(self):
        import types
        rd = types.SimpleNamespace(PROVIDER_NAME='Real-Debrid', stream_only=False)
        tb = types.SimpleNamespace(PROVIDER_NAME='Torbox', stream_only=True)
        fake = types.ModuleType('debrid')
        fake.get_debrid_providers = lambda: [rd, tb]
        fake.get_grab_providers = lambda: [rd]
        with mock.patch.dict(sys.modules, {'debrid': fake}):
            self.assertEqual(mcp._debrid_chain(), {'all': ['Real-Debrid', 'Torbox'],
                                                   'adds_new_releases': ['Real-Debrid'],
                                                   'streaming_only': ['Torbox']})

    def test_external_scan_status_reads_state(self):
        import tempfile
        from utilities import external_mount_scan as ems
        state = {ems.META_KEY: {'last_run': 100},
                 'Old.Show.S01': {'baseline': True},
                 'New.Movie.2024.1080p': {'imported': True, 'first_seen': 50},
                 'Empty.Folder': {'no_media': True, 'first_seen': 60},
                 'Broken.Release': {'given_up': True, 'attempts': 3, 'first_seen': 40}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            with open(path, 'w') as f:
                json.dump(state, f)
            with mock.patch.object(ems, '_state_path', return_value=path), \
                    mock.patch.object(mcp, '_get_setting', return_value={'scan_mount_for_external_adds': True}):
                out = mcp.tool_external_scan_status()
        self.assertTrue(out['enabled'])
        self.assertEqual(out['last_run'], 100)
        self.assertEqual(out['counts'], {'baseline': 1, 'imported': 1, 'waiting_for_media': 1, 'given_up': 1})
        self.assertEqual([r['folder'] for r in out['recent']], ['Empty.Folder', 'New.Movie.2024.1080p', 'Broken.Release'])

    def test_external_scan_status_without_state_file(self):
        from utilities import external_mount_scan as ems
        with mock.patch.object(ems, '_state_path', return_value='/nonexistent/state.json'), \
                mock.patch.object(mcp, '_get_setting', return_value={}):
            out = mcp.tool_external_scan_status()
        self.assertFalse(out['enabled'])
        self.assertEqual(out['counts'], {})
        self.assertNotIn('error', out)
