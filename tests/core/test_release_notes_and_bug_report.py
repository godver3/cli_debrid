"""Release notes from GitHub releases, and the bug report's setup summary."""
import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401  (app import order)
from routes import base_routes
from utilities import bug_report

FOOTER = '\n\n---\n\n<!-- release-notes -->\n<!-- discord-posted:dev -->\nVersion 0.8.6 (dev branch), commit abc\n\n## Windows install\n...'


def _rel(tag, body, prerelease=True, draft=False):
    return {'tag_name': tag, 'name': f'Release {tag[1:]}', 'body': body, 'prerelease': prerelease,
            'draft': draft, 'published_at': '2026-11-22T20:00:00Z', 'html_url': f'https://x/{tag}'}


class ReleaseNotesTest(unittest.TestCase):
    def test_only_releases_with_notes_and_footer_stripped(self):
        releases = [_rel('v0.8.6', '- New thing' + FOOTER),
                    _rel('v0.8.5', 'Version 0.8.5 (dev branch)\n\nThis release was automatically created')]
        out = base_routes.release_notes_from_releases(releases, 'dev')
        self.assertEqual([r['version'] for r in out], ['0.8.6'])
        self.assertEqual(out[0]['body'], '- New thing')

    def test_main_skips_prereleases(self):
        releases = [_rel('v0.8.7', '- dev only' + FOOTER),
                    _rel('v0.8.6', '- stable' + FOOTER.replace(':dev', ':main'), prerelease=False)]
        self.assertEqual([r['version'] for r in base_routes.release_notes_from_releases(releases, 'main')], ['0.8.6'])
        self.assertEqual(len(base_routes.release_notes_from_releases(releases, 'dev')), 2)

    def test_drafts_and_limit(self):
        releases = [_rel('v1.0.0', '- d' + FOOTER, draft=True)] + [_rel(f'v0.{i}.0', '- n' + FOOTER) for i in range(20)]
        out = base_routes.release_notes_from_releases(releases, 'dev', limit=3)
        self.assertEqual(len(out), 3)
        self.assertNotIn('1.0.0', [r['version'] for r in out])


def _settings(values):
    return lambda section, key=None, default=None: (values.get(section, default) if key is None
                                                     else values.get((section, key), default))


class BugReportContextTest(unittest.TestCase):
    def _ctx(self, values, builtin=False, climount_version='2.5.24'):
        get = _settings(values)
        client = mock.Mock(base_url='http://cm:8282')
        client._headers.return_value = {}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {'version': climount_version}
        with mock.patch.object(bug_report, 'get_setting', get), \
             mock.patch('utilities.silo_functions.get_setting', get), \
             mock.patch('utilities.builtin_engine.selected', lambda s=None: builtin), \
             mock.patch('utilities.builtin_engine.alongside', lambda s=None: False), \
             mock.patch('usenet.get_usenet_client', lambda: client), \
             mock.patch('usenet.get_usenet_provider_display_name', lambda: 'cli_mount'), \
             mock.patch('requests.get', lambda *a, **k: resp), \
             mock.patch.object(bug_report, '_debrid', lambda: 'Real-Debrid'):
            ctx = bug_report.build_context()
        return ctx, {r['label']: r['value'] for r in ctx['rows']}

    def test_external_climount_with_version(self):
        ctx, rows = self._ctx({'Usenet Provider': {'enabled': True, 'provider': 'climount',
                                                   'url': 'http://secret-host:8282', 'api_key': 'k'},
                               ('File Management', 'file_collection_management'): 'Plex'})
        self.assertEqual(ctx['mount_kind'], 'external')
        self.assertEqual(rows['Mount'], 'External cli_mount (2.5.24)')
        self.assertEqual(rows['Media server'], 'Plex')
        self.assertNotIn('secret-host', str(ctx))

    def test_builtin_mount(self):
        ctx, rows = self._ctx({'Usenet Provider': {'enabled': True, 'provider': 'builtin'},
                               ('File Management', 'file_collection_management'): 'Plex'}, builtin=True)
        self.assertEqual(ctx['mount_kind'], 'builtin')
        self.assertEqual(rows['Mount'], 'Built-in mount')

    def test_symlink_silo_no_mount(self):
        ctx, rows = self._ctx({'Usenet Provider': {'enabled': False},
                               ('File Management', 'file_collection_management'): 'Symlinked/Local',
                               ('File Management', 'media_server_type'): 'silo'})
        self.assertEqual(ctx['mount_kind'], '')
        self.assertEqual(rows['Media server'], 'Silo')
        self.assertEqual(rows['Mount'], 'None')


class EngineLogUploadTest(unittest.TestCase):
    def test_engine_lines_are_tailed_and_scrubbed(self):
        from routes import log_viewer_routes as lv
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'mount_engine.log')
            with open(path, 'w') as f:
                f.write(''.join(f'line {i} apikey=abc123\n' for i in range(10)))
            with mock.patch('usenet.builtin_client.engine_log_path', lambda: path), \
                 mock.patch('usenet.builtin_client.format_engine_log_line', lambda l: l), \
                 mock.patch('utilities.log_redaction.scrub', lambda l: l.replace('abc123', '***')):
                lines = lv._engine_log_for_upload(max_lines=3)
        self.assertEqual(lines, ['line 7 apikey=***', 'line 8 apikey=***', 'line 9 apikey=***'])

    def test_missing_engine_log(self):
        from routes import log_viewer_routes as lv
        with mock.patch('usenet.builtin_client.engine_log_path', lambda: '/nonexistent/mount_engine.log'):
            self.assertEqual(lv._engine_log_for_upload(), [])


if __name__ == '__main__':
    unittest.main()
