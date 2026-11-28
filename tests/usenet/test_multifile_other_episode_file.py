"""Symlink-mode multi-file scan: a pack file that belongs to another episode is
not an alternate version of this one.

2026-11-26: Pokémon's SRS pack (aired order) was assigned to the show in
tvdb:dvd order. Checking S11E19 "Crossing Paths", the scan found
Pokemon.S11E19...Kidnapped.Riolu.Part.1 (item 38560, Clyde's S11E29), called
it another version of S11E19, linked it under S11E19 and repointed 38560's
location_on_disk at that link: 26 items and 44 stray links in Plex.
"""
import sqlite3

from tests.usenet.test_prefer_largest_nzb_source import lls  # noqa: F401  (shared fixture)

SHOW = 'tt0168366'
RIOLU = 'Pokemon.S11E19.Pokemon.Ranger.and.the.Kidnapped.Riolu.Part.1.DUBBED.480p.WEBRip.AAC.2.0.x264-SRS.mkv'
CROSSING = 'Pokemon.S11E21.Crossing.Paths.DUBBED.480p.WEBRip.AAC.2.0.x264-SRS.mkv'


def _db(*rows):
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, type TEXT, imdb_id TEXT, "
                 "season_number INTEGER, episode_number INTEGER, filled_by_file TEXT)")
    conn.executemany("INSERT INTO media_items VALUES (?, ?, ?, ?, ?, ?)", rows)
    return conn


def _item(id_, season, episode):
    return {'id': id_, 'type': 'episode', 'imdb_id': SHOW, 'season_number': season, 'episode_number': episode}


def test_file_assigned_to_another_episode_is_not_a_second_version(lls):
    conn = _db((38560, 'episode', SHOW, 11, 29, RIOLU), (38562, 'episode', SHOW, 11, 19, CROSSING))
    # Checking S11E19: the Riolu file parses as S11E19 but is 38560's (S11E29)
    assert lls._other_episode_owning_file(conn, _item(38562, 11, 19), RIOLU) == (38560, 11, 29)


def test_file_owned_by_nobody_or_by_the_same_episode_is_still_a_version(lls):
    proper = 'Pokemon.S11E19.Crossing.Paths.PROPER.mkv'
    conn = _db((38562, 'episode', SHOW, 11, 19, CROSSING), (40000, 'episode', SHOW, 11, 19, proper))
    item = _item(38562, 11, 19)
    assert lls._other_episode_owning_file(conn, item, 'Pokemon.S11E19.Crossing.Paths.REPACK.mkv') is None
    # Another row of the same episode (an earlier alternate version) is not "another episode"
    assert lls._other_episode_owning_file(conn, item, proper) is None


def test_same_file_name_in_another_show_does_not_count(lls):
    conn = _db((1, 'episode', 'tt9999999', 3, 4, RIOLU))
    assert lls._other_episode_owning_file(conn, _item(38562, 11, 19), RIOLU) is None


def test_the_item_itself_is_never_its_own_owner(lls):
    # Same file on the item's own row (renumbered since): not "another episode"
    conn = _db((38562, 'episode', SHOW, 11, 21, CROSSING))
    assert lls._other_episode_owning_file(conn, _item(38562, 11, 19), CROSSING) is None
