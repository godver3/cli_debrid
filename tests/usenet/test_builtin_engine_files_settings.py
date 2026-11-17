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
                return json.load(f).get('Usenet Provider', {}).get('provider')

    def test_new_install_gets_builtin(self):
        self.assertEqual(self._run(None), 'builtin')

    def test_existing_config_without_key_keeps_climount(self):
        self.assertEqual(self._run({'Usenet Provider': {'enabled': True}}), 'climount')

    def test_existing_choice_untouched(self):
        self.assertEqual(self._run({'Usenet Provider': {'enabled': True, 'provider': 'nzbdav'}}), 'nzbdav')


if __name__ == '__main__':
    unittest.main()
