"""A Plex partial scan of a removed folder does nothing: scan the nearest
folder that still exists, never above the section location."""
from types import SimpleNamespace

import database  # noqa: F401 - initialize database before plex_functions to avoid circular import

from utilities.plex_functions.scan_cleanup import _nearest_existing_scan_paths


def _sections(*locs):
    return [SimpleNamespace(locations=list(locs))]


def test_removed_show_folder_scans_section_root(tmp_path):
    tv = tmp_path / 'TV Shows'
    tv.mkdir()
    gone = [str(tv / 'Detectorists (2014)' / 'Season 01'), str(tv / 'Detectorists (2014)' / 'Season 02')]
    assert _nearest_existing_scan_paths(gone, _sections(str(tv))) == [str(tv)]


def test_existing_season_folder_kept(tmp_path):
    tv = tmp_path / 'TV Shows'
    s1 = tv / 'Show' / 'Season 01'
    s1.mkdir(parents=True)
    other = tv / 'Show' / 'Season 02'  # removed
    assert _nearest_existing_scan_paths([str(s1), str(other)], _sections(str(tv))) == [str(tv / 'Show')]  # covers Season 01


def test_parent_covers_children_and_unknown_path_untouched(tmp_path):
    tv = tmp_path / 'TV Shows'
    (tv / 'A').mkdir(parents=True)
    paths = [str(tv / 'A' / 'gone1'), str(tv / 'A' / 'gone2'), '/elsewhere/x']
    assert _nearest_existing_scan_paths(paths, _sections(str(tv))) == [str(tv / 'A'), '/elsewhere/x']


def test_trash_waits_for_scan_then_empties(monkeypatch):
    from utilities.plex_functions import scan_cleanup as sc
    states = iter([True, True, False])

    class Sec:
        key, type, title = 1, 'show', 'TV Shows'
        refreshing = True
        emptied = 0

        def reload(self):
            self.refreshing = next(states, False)

        def emptyTrash(self):
            Sec.emptied += 1

    monkeypatch.setattr(sc.time, 'sleep', lambda s: None)
    assert sc._wait_for_scans([Sec()], timeout_s=60) is True
    sc._empty_trash_after_scans([Sec()], 'show')
    assert Sec.emptied == 1


def test_wait_times_out_while_still_scanning(monkeypatch):
    from utilities.plex_functions import scan_cleanup as sc

    class Busy:
        refreshing = True

        def reload(self):
            pass

    t = iter(range(0, 1000, 5))
    monkeypatch.setattr(sc.time, 'monotonic', lambda: next(t))
    monkeypatch.setattr(sc.time, 'sleep', lambda s: None)
    assert sc._wait_for_scans([Busy()], timeout_s=10) is False
