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
    # the old locations migrate_legacy_user_dirs copies from
    ('utilities/paths.py', "('/user/config', config_dir,"),
    ('utilities/paths.py', "('/user/db_content', db_content_dir,"),
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


def test_legacy_folders_are_copied_once(tmp_path, monkeypatch):
    from utilities import paths
    legacy, new = tmp_path / 'legacy', tmp_path / 'appdata'
    (legacy / 'poster_backups').mkdir(parents=True)
    (legacy / 'poster_backups' / 'orig.jpg').write_text('poster')
    (legacy / 'badge_assets').mkdir()
    (legacy / 'badge_assets' / 'old.png').write_text('old')
    (new / 'badge_assets').mkdir(parents=True)
    (new / 'badge_assets' / 'current.png').write_text('in use')
    monkeypatch.setenv('USER_CONFIG', str(new))
    monkeypatch.setattr(paths, '_LEGACY_FOLDERS', ((str(legacy), paths.config_dir, ('poster_backups', 'badge_assets', 'overlay_assets')),))

    paths.migrate_legacy_user_dirs()

    assert (new / 'poster_backups' / 'orig.jpg').read_text() == 'poster'
    assert (legacy / 'poster_backups' / 'orig.jpg').exists()  # copied, not moved
    assert sorted(p.name for p in (new / 'badge_assets').iterdir()) == ['current.png']  # in use: left alone
    assert not (new / 'overlay_assets').exists()


def test_legacy_migration_does_nothing_when_the_folders_are_the_same(tmp_path, monkeypatch):
    from utilities import paths
    (tmp_path / 'poster_backups').mkdir()
    monkeypatch.setenv('USER_CONFIG', str(tmp_path))
    monkeypatch.setattr(paths, '_LEGACY_FOLDERS', ((str(tmp_path), paths.config_dir, ('poster_backups',)),))
    paths.migrate_legacy_user_dirs()  # must not try to copy a folder onto itself
