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


def test_deleting_a_symlink_takes_its_subtitles(tmp_path):
    """Live 2026-11-22: deleting a movie left 'Movie.en.srt' behind, so its
    folder never emptied. Only the deleted file's own subtitles go."""
    import os
    from utilities.deletion_manager import remove_sidecar_subtitles
    folder = tmp_path / 'Plan 9 from Outer Space (1959)'
    folder.mkdir()
    video = folder / 'Plan 9 (1959) - 1080p.mkv'
    os.symlink('/nonexistent/target.mkv', video)
    for name in ('Plan 9 (1959) - 1080p.en.srt', 'Plan 9 (1959) - 1080p.eng.forced.ass',
                 'Plan 9 (1959) - 720p.en.srt', 'Plan 9 (1959) - 1080p.nfo', 'poster.jpg'):
        (folder / name).write_text('x')
    os.unlink(video)
    removed = sorted(os.path.basename(p) for p in remove_sidecar_subtitles(str(video)))
    assert removed == ['Plan 9 (1959) - 1080p.en.srt', 'Plan 9 (1959) - 1080p.eng.forced.ass']
    assert sorted(os.listdir(folder)) == ['Plan 9 (1959) - 1080p.nfo', 'Plan 9 (1959) - 720p.en.srt', 'poster.jpg']


def test_orphaned_subtitles_of_replaced_files_are_removed(tmp_path):
    """Live 2026-11-22 (Possession): subtitles named after earlier, replaced
    files stayed in the folder and Plex picked one of them."""
    import os
    from utilities.deletion_manager import remove_orphan_subtitles
    root = tmp_path / 'symlinks'
    folder = root / 'Movies' / 'Possession (1981)'
    folder.mkdir(parents=True)
    os.symlink('/nonexistent/x.mkv', folder / 'Possession - (DiVULGED.mkv')
    for n in ('Possession - (DiVULGED.en.srt', 'Possession - (38166f.en.srt', 'Possession - (a1pr7h.en.hi.srt', 'poster.jpg'):
        (folder / n).write_text('x')
    removed = sorted(os.path.basename(p) for p in remove_orphan_subtitles(str(folder), str(root)))
    assert removed == ['Possession - (38166f.en.srt', 'Possession - (a1pr7h.en.hi.srt']
    assert sorted(os.listdir(folder)) == ['Possession - (DiVULGED.en.srt', 'Possession - (DiVULGED.mkv', 'poster.jpg']
    # never outside the library, never the library root itself, never a folder without videos
    assert remove_orphan_subtitles(str(folder), str(tmp_path / 'other')) == []
    assert remove_orphan_subtitles(str(root), str(root)) == []
    empty = root / 'Movies' / 'Only Subs'
    empty.mkdir()
    (empty / 'x.en.srt').write_text('x')
    assert remove_orphan_subtitles(str(empty), str(root)) == []
