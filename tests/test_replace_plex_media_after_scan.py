"""remove_replaced_plex_media_after_scan waits for Plex to scan in the replacement before
removing the old version, so Plex keeps the item (addedAt, watch state) instead of
re-adding the replacement as a brand-new "recently added" item."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))



def _plex_functions():
    # Other test modules replace `utilities` (and submodules) with stubs in sys.modules.
    import importlib
    for name in ('utilities', 'utilities.settings', 'utilities.plex_functions', 'database'):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, '__file__', None):
            sys.modules.pop(name, None)
    # plex_functions -> database -> routes -> scraper -> plex_functions is circular unless
    # database is imported first, as the app does.
    importlib.import_module('database')
    return importlib.import_module('utilities.plex_functions')

OLD = '/sym/Movies/Spider-Man (2026)/Spider-Man (2026) - tt1 - 1080p - (TURG).mkv'
NEW = '/sym/Movies/Spider-Man (2026)/Spider-Man (2026) - tt1 - 1080p - (GL0P).mkv'


def _movie(*files):
    return SimpleNamespace(media=[SimpleNamespace(parts=[SimpleNamespace(file=f)]) for f in files])


class _FakeSection:
    type = 'movie'

    def __init__(self, scans):
        self.scans = list(scans)  # what each successive search returns

    def search(self, title=None):
        return [self.scans.pop(0) if len(self.scans) > 1 else self.scans[0]]


class RemoveReplacedPlexMediaTests(unittest.TestCase):
    def _run(self, section, timeout=30):
        pf = _plex_functions()
        plex = SimpleNamespace(library=SimpleNamespace(sections=lambda: [section]))
        settings = {('File Management', 'file_collection_management'): 'Symlinked/Local',
                    ('File Management', 'plex_url_for_symlink'): 'http://plex:32400',
                    ('File Management', 'plex_token_for_symlink'): 'tok'}
        clock = {'t': 0.0}
        with mock.patch.object(pf, 'get_setting', lambda s, k, default=None: settings.get((s, k), default)), \
             mock.patch.object(pf.plexapi.server, 'PlexServer', return_value=plex), \
             mock.patch.object(pf, 'remove_file_from_plex', return_value=True) as remove, \
             mock.patch.object(pf.time, 'time', lambda: clock['t']), \
             mock.patch.object(pf.time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s)):
            ok = pf.remove_replaced_plex_media_after_scan('Spider-Man', NEW, [OLD], timeout=timeout, interval=10)
        return ok, remove, clock['t']

    def test_waits_for_replacement_then_removes_old_version(self):
        section = _FakeSection([_movie(OLD), _movie(OLD), _movie(OLD, NEW)])
        ok, remove, elapsed = self._run(section)
        self.assertTrue(ok)
        remove.assert_called_once_with('Spider-Man', OLD, None)
        self.assertEqual(elapsed, 20)  # removed only once the third lookup saw the new file

    def test_timeout_still_removes_old_version(self):
        ok, remove, elapsed = self._run(_FakeSection([_movie(OLD)]), timeout=30)
        remove.assert_called_once_with('Spider-Man', OLD, None)
        self.assertGreaterEqual(elapsed, 30)


if __name__ == '__main__':
    unittest.main()
