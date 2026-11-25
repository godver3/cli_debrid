"""Plex mode stores <mount>/<typed folder>/... instead of __all__: the typed
folder must be one that exists on the mount (cli_mount's movies/shows or the
built-in mount's Movies / TV Shows)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: F401
from queues.runner.plex_tasks import _typed_mount_path


class TypedMountPathTests(unittest.TestCase):
    def _mount(self, tops, holder):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d)
        for t in ['__all__'] + tops:
            os.makedirs(os.path.join(d, t, 'Rel'))
        for t in ['__all__', holder]:
            open(os.path.join(d, t, 'Rel', 'f.mkv'), 'w').close()
        return d

    def test_builtin_default_folders(self):
        d = self._mount(['Movies', 'TV Shows'], 'TV Shows')
        self.assertEqual(_typed_mount_path(f'{d}/__all__/Rel/f.mkv', 'episode'), f'{d}/TV Shows/Rel/f.mkv')
        d = self._mount(['Movies', 'TV Shows'], 'Movies')
        self.assertEqual(_typed_mount_path(f'{d}/__all__/Rel/f.mkv', 'movie'), f'{d}/Movies/Rel/f.mkv')

    def test_climount_lowercase_and_custom_folder(self):
        d = self._mount(['movies', 'shows', 'ufc'], 'shows')
        self.assertEqual(_typed_mount_path(f'{d}/__all__/Rel/f.mkv', 'episode'), f'{d}/shows/Rel/f.mkv')
        d = self._mount(['movies', 'shows', 'ufc'], 'ufc')
        self.assertEqual(_typed_mount_path(f'{d}/__all__/Rel/f.mkv', 'movie'), f'{d}/ufc/Rel/f.mkv')

    def test_never_picks_the_other_type_and_falls_back(self):
        d = self._mount(['Movies', 'TV Shows'], 'TV Shows')
        self.assertEqual(_typed_mount_path(f'{d}/__all__/Rel/f.mkv', 'movie'), f'{d}/movies/Rel/f.mkv')
        self.assertEqual(_typed_mount_path('/nope/__all__/Rel/f.mkv', 'episode'), '/nope/shows/Rel/f.mkv')


if __name__ == '__main__':
    unittest.main()
