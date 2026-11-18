"""Built-in mount: allowed extensions, deobfuscate mode, virtual folders,
and the new-install default backend."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  (app import order; keeps later tests' imports working)
from utilities import builtin_engine as be


class FileSettingsTests(unittest.TestCase):
    def test_allowed_extensions(self):
        self.assertEqual(be.allowed_extensions({}), ['mkv', 'mp4', 'm4v', 'avi', 'mov', 'mpg', 'mpeg'])
        self.assertEqual(be.allowed_extensions({'allowed_extensions': '.MKV, mp4;srt mkv'}), ['mkv', 'mp4', 'srt'])
        self.assertEqual(be.allowed_extensions({'allowed_extensions': ''}), [])  # empty = everything

    def test_virtual_folders(self):
        rows = [{'name': 'shows', 'filters': [{'type': 'regex', 'value': r'S\d\dE\d\d'}, {'type': '', 'value': ''}]},
                {'name': 'big', 'filters': [{'type': 'size_gt', 'value': '20GB'}]}]
        self.assertEqual(be.virtual_folders({'virtual_folders': json.dumps(rows)}),
                         {'shows': {'regex': r'S\d\dE\d\d'}, 'big': {'size_gt': '20GB'}})
        for bad in ([{'name': '__all__', 'filters': [{'type': 'regex', 'value': 'x'}]}],
                    [{'name': 'a', 'filters': [{'type': 'regex', 'value': '('}]}],
                    [{'name': 'a', 'filters': [{'type': 'nope', 'value': 'x'}]}],
                    [{'name': 'a/b', 'filters': [{'type': 'regex', 'value': 'x'}]}],
                    [{'name': 'a', 'filters': []}]):
            with self.assertRaises(ValueError):
                be.virtual_folders({'virtual_folders': bad}, strict=True)
            self.assertEqual(be.virtual_folders({'virtual_folders': bad}), {})

    def test_engine_config_carries_them(self):
        bm = {'servers': json.dumps([{'host': 'news.example.com', 'username': 'u', 'password': 'p', 'connections': 10}]),
              'deobfuscate_mode': 'off', 'virtual_folders': json.dumps([{'name': 'm', 'filters': [{'type': 'protocol', 'value': 'debrid'}]}])}
        with mock.patch.object(be, 'resolve_token', return_value='t'):
            cfg = be.build_config({'Built-in Mount': bm})
        self.assertEqual(cfg['deobfuscate_mode'], '')
        self.assertEqual(cfg['virtual_folders'], {'m': {'protocol': 'debrid'}})
        self.assertIn('mkv', cfg['allowed_extensions'])
        with mock.patch.object(be, 'resolve_token', return_value='t'):
            self.assertEqual(be.build_config({'Built-in Mount': {}})['deobfuscate_mode'], 'season_ep')


class InstallDefaultTests(unittest.TestCase):
    """New installs default to the built-in mount; existing configs keep cli_mount."""

    def _run(self, existing):
        from utilities import settings
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'config.json')
            if existing is not None:
                with open(path, 'w') as f:
                    json.dump(existing, f)
            settings.invalidate_config_cache()
            try:
                with mock.patch.object(settings, 'get_config_file_path', return_value=path):
                    settings.ensure_settings_file()
            finally:
                settings.invalidate_config_cache()  # don't leave this temp config cached for later tests
            with open(path) as f:
                return json.load(f)

    def test_new_install_gets_builtin(self):
        self.assertEqual(self._run(None)['Usenet Provider']['provider'], 'builtin')

    def test_existing_config_without_key_keeps_climount(self):
        self.assertEqual(self._run({'Usenet Provider': {'enabled': True}})['Usenet Provider']['provider'], 'climount')

    def test_existing_choice_untouched(self):
        self.assertEqual(self._run({'Usenet Provider': {'enabled': True, 'provider': 'nzbdav'}})['Usenet Provider']['provider'], 'nzbdav')

    def test_new_install_gets_movies_and_tv_folders(self):
        vf = be.virtual_folders(self._run(None)['Built-in Mount'])
        self.assertEqual(sorted(vf), ['Movies', 'TV Shows'])
        self.assertEqual(sorted(vf['TV Shows']), ['files_regex', 'regex'])
        self.assertEqual(sorted(vf['Movies']), ['not_files_regex', 'not_regex'])

    def test_existing_config_gets_no_folders(self):
        self.assertEqual(self._run({'Built-in Mount': {'mount_path': '/mnt/x'}})['Built-in Mount']['virtual_folders'], '')


class DefaultRoutingTests(unittest.TestCase):
    def test_patterns_compile_and_match_docs_and_go_testdata(self):
        import re
        from utilities.content_routing import DEFAULT_VIRTUAL_FOLDERS, TV_NAME_REGEX, TV_FILES_REGEX
        tv = re.compile(TV_NAME_REGEX)
        self.assertTrue(tv.search('Show.S01E05.1080p') and tv.search('[G] Anime - 12 [1080p]'))
        self.assertFalse(tv.search('The.Matrix.1999.1080p.BluRay'))
        self.assertTrue(re.compile(TV_FILES_REGEX).search('Show.S03E02.mkv'))
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, 'engine/internal/engine/testdata/default_virtual_folders.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f), {v['name']: {x['type']: x['value'] for x in v['filters']}
                                            for v in DEFAULT_VIRTUAL_FOLDERS})
        docs = os.path.join(os.path.dirname(root), 'cli_docs', 'docs', 'integrations', 'cli_mount.md')
        if os.path.exists(docs):  # sibling checkout only
            with open(docs, encoding='utf-8') as f:
                text = f.read()
            self.assertIn(TV_NAME_REGEX, text)
            self.assertIn(TV_FILES_REGEX, text)


if __name__ == '__main__':
    unittest.main()
