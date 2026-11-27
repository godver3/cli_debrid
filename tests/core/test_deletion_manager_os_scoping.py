#!/usr/bin/env python3
"""
Regression test for a live bug: DeletionManager.delete_single_item() raised
"cannot access local variable 'os' where it is not associated with a value"
on every call in Symlinked/Local mode, aborting before it ever unlinked the
old symlink or original file.

Root cause: a redundant `import os` deep inside delete_single_item() (used
only for one os.path.dirname() call in a Plex-removal fallback branch) made
Python treat `os` as a local variable for the ENTIRE function body - so the
symlink/original-file deletion code earlier in the same function, which
uses the module-level `os` (imported at the top of the file), raised
UnboundLocalError before that local import statement ever executed.

This is a source-shape test (no heavy imports/instantiation needed, and the
real module can't even be imported standalone in this environment - see
utilities/deletion_manager.py's database.core -> cli_battery -> sqlalchemy
chain) that fails if a local `import os` (or `os = ...` assignment) is ever
reintroduced inside delete_single_item(), since `os` is already imported at
module scope and any local rebinding anywhere in the function poisons every
earlier reference to it in that same function.
"""

import unittest
import os
import re


class TestDeletionManagerOsScoping(unittest.TestCase):
    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             'utilities', 'deletion_manager.py')
        with open(path) as f:
            self.src = f.read()

    def _method_body(self, method_name):
        """Extract a method's source (from its `def` line to the next same-or-lesser-indented `def`)."""
        start_match = re.search(rf'^(    )def {re.escape(method_name)}\(', self.src, re.MULTILINE)
        self.assertIsNotNone(start_match, f"Could not find method {method_name!r}")
        start = start_match.start()
        next_def = re.search(r'^    def [a-zA-Z_]', self.src[start_match.end():], re.MULTILINE)
        end = start_match.end() + next_def.start() if next_def else len(self.src)
        return self.src[start:end]

    def test_module_level_os_import_exists(self):
        self.assertRegex(self.src, r'(?m)^import os$', "utilities/deletion_manager.py must import os at module scope")

    def test_delete_single_item_has_no_local_os_rebinding(self):
        body = self._method_body('delete_single_item')
        # Any `import os` or `os = ...` inside this function body would shadow
        # the module-level `os` for the whole function and reproduce the bug.
        self.assertNotRegex(body, r'\bimport os\b',
                             "delete_single_item() must not locally re-import os - it shadows the "
                             "module-level import for the WHOLE function, breaking every earlier os.* "
                             "call (symlink/original-file deletion) with UnboundLocalError")
        self.assertNotRegex(body, r'(?<![.\w])os\s*=(?!=)',
                             "delete_single_item() must not locally rebind the name 'os'")

    def test_delete_single_item_still_uses_os_for_symlink_and_file_deletion(self):
        """Sanity check the extraction above actually captured the real method body."""
        body = self._method_body('delete_single_item')
        self.assertIn('os.unlink', body)
        self.assertIn('os.remove', body)


if __name__ == '__main__':
    unittest.main()


def test_deleting_a_symlink_takes_its_subtitles(tmp_path, monkeypatch):
    """Live 2026-11-22: deleting a movie left 'Movie.en.srt' behind, so its
    folder never emptied. Only the deleted file's own Clyde-written subtitles go."""
    import os
    from database import subtitle_history
    from utilities.deletion_manager import remove_sidecar_subtitles
    db_dir = tmp_path / 'db'
    db_dir.mkdir()
    monkeypatch.setenv('USER_DB_CONTENT', str(db_dir))
    monkeypatch.setattr(subtitle_history, '_ready', False)
    folder = tmp_path / 'Plan 9 from Outer Space (1959)'
    folder.mkdir()
    video = folder / 'Plan 9 (1959) - 1080p.mkv'
    os.symlink('/nonexistent/target.mkv', video)
    for name in ('Plan 9 (1959) - 1080p.en.srt', 'Plan 9 (1959) - 1080p.eng.forced.ass',
                 'Plan 9 (1959) - 720p.en.srt', 'Plan 9 (1959) - 1080p.nfo', 'poster.jpg'):
        (folder / name).write_text('x')
    for name in ('Plan 9 (1959) - 1080p.en.srt', 'Plan 9 (1959) - 1080p.eng.forced.ass',
                 'Plan 9 (1959) - 720p.en.srt'):
        subtitle_history.record(1, 'Plan 9', 'en', 'subdl', 900, 971, [], '', 'srt',
                                'sidecar', str(folder / name), 'auto')
    os.unlink(video)
    removed = sorted(os.path.basename(p) for p in remove_sidecar_subtitles(str(video)))
    assert removed == ['Plan 9 (1959) - 1080p.en.srt', 'Plan 9 (1959) - 1080p.eng.forced.ass']
    assert sorted(os.listdir(folder)) == ['Plan 9 (1959) - 1080p.nfo', 'Plan 9 (1959) - 720p.en.srt', 'poster.jpg']


def test_subtitles_of_a_longer_named_version_stay(tmp_path, monkeypatch):
    """'Movie.2020.1080p.REMUX.en.srt' starts with 'Movie.2020.1080p.' but
    belongs to the REMUX version still in the folder."""
    import os
    from database import subtitle_history
    from utilities.deletion_manager import remove_sidecar_subtitles
    db_dir = tmp_path / 'db'
    db_dir.mkdir()
    monkeypatch.setenv('USER_DB_CONTENT', str(db_dir))
    monkeypatch.setattr(subtitle_history, '_ready', False)
    folder = tmp_path / 'Movie (2020)'
    folder.mkdir()
    video = folder / 'Movie.2020.1080p.mkv'
    os.symlink('/nonexistent/a.mkv', folder / 'Movie.2020.1080p.REMUX.mkv')
    for name in ('Movie.2020.1080p.en.srt', 'Movie.2020.1080p.REMUX.en.srt'):
        (folder / name).write_text('x')
        subtitle_history.record(1, 'Movie', 'en', 'subdl', 900, 971, [], '', 'srt',
                                'sidecar', str(folder / name), 'auto')
    removed = [os.path.basename(p) for p in remove_sidecar_subtitles(str(video))]
    assert removed == ['Movie.2020.1080p.en.srt']
    assert os.path.exists(folder / 'Movie.2020.1080p.REMUX.en.srt')
