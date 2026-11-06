"""Symlink-mode backfill must read the real symlink root setting.

get_and_add_all_collected_from_plex(backfill=True) has a filesystem-first path
that avoids tens of thousands of Plex API calls. In Symlinked/Local mode it was
gated on get_setting('File Management', 'symlinked_directory'), a key that
doesn't exist, so it always fell back to the Plex API.
"""
import ast
import os
import unittest

from utilities.settings_schema import SETTINGS_SCHEMA

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _function_source(path, name):
    with open(os.path.join(ROOT, path), encoding='utf-8') as f:
        src = f.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in {path}')


class BackfillSymlinkSettingKeyTest(unittest.TestCase):
    def test_every_file_management_key_it_reads_exists(self):
        fn = _function_source('queues/run_program.py', 'get_and_add_all_collected_from_plex')
        read = [
            call.args[1].value for call in ast.walk(fn)
            if isinstance(call, ast.Call) and getattr(call.func, 'id', None) == 'get_setting'
            and len(call.args) >= 2 and all(isinstance(a, ast.Constant) for a in call.args[:2])
            and call.args[0].value == 'File Management'
        ]
        self.assertIn('symlinked_files_path', read)
        missing = [k for k in read if k not in SETTINGS_SCHEMA['File Management']]
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
