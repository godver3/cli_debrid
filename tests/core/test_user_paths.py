"""App files live under USER_CONFIG / USER_DB_CONTENT / USER_LOGS, never a hardcoded /user/....

On Windows those point at the app data folder, so a hardcoded '/user/config/...' path
writes to C:\\user\\config instead. Paths must come from utilities.paths (or the
env vars) when they're used.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HARDCODED = re.compile(r"""['"]/user/(config|db_content|logs)\b""")
# The fallbacks themselves, and one legacy location the nzbdav migration script also tries.
ALLOWED = {
    ('main.py', "os.environ.setdefault("),
    ('windows_wrapper.py', "os.environ.setdefault("),
    ('usenet/nzbdav_migrate.py', "'/user/config/config.json',"),
    ('utilities/deletion_manager.py', "'cache_files':"),  # docstring example
}


class NoHardcodedUserPathsTest(unittest.TestCase):
    def test_no_hardcoded_user_paths(self):
        offenders = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in ('tests', 'phalanx_db_hyperswarm', 'node_modules', '.git', '__pycache__')]
            for name in filenames:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, ROOT)
                with open(path, encoding='utf-8', errors='ignore') as f:
                    for lineno, line in enumerate(f, 1):
                        if not HARDCODED.search(line) or 'environ.get(' in line or 'getenv(' in line:
                            continue
                        if any(rel == r and marker in line for r, marker in ALLOWED):
                            continue
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], 'use utilities.paths instead of a hardcoded /user/... path')

    def test_paths_follow_the_env_when_called(self):
        from utilities import paths
        old = os.environ.get('USER_CONFIG')
        try:
            os.environ['USER_CONFIG'] = os.path.join('C:', 'AppData', 'cli_debrid', 'config')
            self.assertEqual(paths.config_dir('a', 'b.json'),
                             os.path.join('C:', 'AppData', 'cli_debrid', 'config', 'a', 'b.json'))
        finally:
            if old is None:
                os.environ.pop('USER_CONFIG', None)
            else:
                os.environ['USER_CONFIG'] = old


if __name__ == '__main__':
    unittest.main()
