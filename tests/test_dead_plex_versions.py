"""Repairs leave the old file behind in Plex as a dead second version.

Repairs and replacements put the new file in the same folder under a new name (the
default symlink template includes {original_filename}), so Plex attaches it to the
existing item as a second version. The old version's symlink is left dangling (or was
already unlinked) and, with Plex's "empty trash automatically" off, stays as an
unavailable version. Live: Jim Gaffigan: Obsessed (playback repair) and four
Supernatural / The Walking Dead episodes (debrid repair).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _real(module_name):
    # Other test modules replace `utilities`/`database` with stubs in sys.modules.
    import importlib
    for name in ('utilities', 'utilities.settings', 'utilities.plex_functions',
                 'utilities.post_processing', 'database'):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, '__file__', None):
            sys.modules.pop(name, None)
    importlib.import_module('database')  # plex_functions <-> database import cycle
    return importlib.import_module(module_name)


class _Media:
    _next = 0

    def __init__(self, *files):
        _Media._next += 1
        self.id = _Media._next
        self.parts = [SimpleNamespace(file=f) for f in files]
        self.deleted = False

    def delete(self):
        self.deleted = True


class DeadVersionSweepTests(unittest.TestCase):
    def setUp(self):
        self.pf = _real('utilities.plex_functions')
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, 'Jim Gaffigan_ Obsessed (2014)')
        os.makedirs(self.folder)
        target = os.path.join(self.tmp, 'mount-new.mkv')
        open(target, 'w').close()
        self.new = os.path.join(self.folder, 'Jim (2014) - 1080p - (GPRS).mkv')
        os.symlink(target, self.new)
        self.old = os.path.join(self.folder, 'Jim (2014) - 1080p - (sadpanda).mkv')
        os.symlink(os.path.join(self.tmp, 'gone.mkv'), self.old)  # dangling

    def _sweep(self, owner_media, new_media):
        owner = SimpleNamespace(media=owner_media)
        with mock.patch.object(self.pf, '_file_management_plex', return_value=object()), \
             mock.patch.object(self.pf, '_wait_for_plex_file', return_value=(owner, new_media)):
            return self.pf.remove_dead_plex_versions_after_scan('Jim Gaffigan: Obsessed', self.new)

    def test_dead_version_and_dangling_symlink_removed(self):
        new_m, old_m = _Media(self.new), _Media(self.old)
        self.assertEqual(self._sweep([old_m, new_m], new_m), 1)
        self.assertTrue(old_m.deleted)
        self.assertFalse(new_m.deleted)
        self.assertFalse(os.path.lexists(self.old))
        self.assertTrue(os.path.exists(self.new))

    def test_live_duplicate_is_never_removed(self):
        dup_target = os.path.join(self.tmp, 'dup.mkv')
        open(dup_target, 'w').close()
        dup = os.path.join(self.folder, 'Jim (2014) - 1080p - (other).mkv')
        os.symlink(dup_target, dup)
        new_m, dup_m = _Media(self.new), _Media(dup)
        self.assertEqual(self._sweep([dup_m, new_m], new_m), 0)
        self.assertFalse(dup_m.deleted)

    def test_dead_version_in_another_folder_is_left_alone(self):
        new_m = _Media(self.new)
        elsewhere = _Media('/other/library/Jim (2014)/Jim - 2160p.mkv')
        self.assertEqual(self._sweep([elsewhere, new_m], new_m), 0)
        self.assertFalse(elsewhere.deleted)

    def test_unresolved_new_file_skips_everything(self):
        # Mount down: the new symlink doesn't resolve, so nothing may be judged dead.
        os.unlink(self.new)
        os.symlink(os.path.join(self.tmp, 'also-gone.mkv'), self.new)
        with mock.patch.object(self.pf, '_file_management_plex') as plex:
            self.assertEqual(self.pf.remove_dead_plex_versions_after_scan('Jim', self.new), 0)
            plex.assert_not_called()


class DeadVersionTriggerTests(unittest.TestCase):
    def setUp(self):
        self.pp = _real('utilities.post_processing')

    def test_recollection_detected(self):
        now = datetime.now()
        self.assertTrue(self.pp._is_recollection(
            {'original_collected_at': str(now - timedelta(hours=10)), 'collected_at': str(now)}))
        self.assertFalse(self.pp._is_recollection({'original_collected_at': str(now), 'collected_at': str(now)}))
        self.assertFalse(self.pp._is_recollection({}))

    def test_dangling_sibling_detected(self):
        folder = tempfile.mkdtemp()
        item = os.path.join(folder, 'new.mkv')
        open(item, 'w').close()
        self.assertFalse(self.pp._has_dangling_symlink_sibling(item))
        os.symlink(os.path.join(folder, 'gone.mkv'), os.path.join(folder, 'old.mkv'))
        self.assertTrue(self.pp._has_dangling_symlink_sibling(item))


if __name__ == '__main__':
    unittest.main()
