#!/usr/bin/env python3
"""A broken file inside a season pack must not take the whole torrent with it.

Replacing one flagged episode used to call db_items[0] and DELETE the torrent.
For a complete-series pack that reset an unrelated episode and broke every
sibling symlink.
"""

import importlib.util
import sqlite3
import sys
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HASH = '6643f623b8ef54d91bcc9a64a4c930cb9f46ab73'
PACK = 'The Office (2005-2013) Complete Series - Superfan Episodes'


def _load():
    spec = importlib.util.spec_from_file_location(
        'debrid_repair_engine_under_test',
        PROJECT_ROOT / 'usenet' / 'debrid_repair_engine.py',
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eng = _load()


def _ep(item_id, season, episode, magnet=None, state='Collected'):
    return {
        'id': item_id,
        'title': 'The Office (US)',
        'type': 'episode',
        'state': state,
        'season_number': season,
        'episode_number': episode,
        'filled_by_magnet': magnet if magnet is not None else f'magnet:?xt=urn:btih:{HASH}',
        'filled_by_torrent_id': 'WW3GF6P2SY2U6',
        'debrid_folder_name': PACK,
        'location_on_disk': f'/symlinks/S{season:02d}E{episode:02d}.mkv',
    }


class TestSelectBrokenDbItems(unittest.TestCase):
    def test_cli_debrid_id_picks_the_flagged_episode_not_the_first_row(self):
        rows = [_ep(23794, 9, 23), _ep(23651, 4, 1), _ep(23600, 2, 1)]
        broken = [{'file_name': 'S04E01 Fun Run Part 1 (Extended Cut).mkv', 'cli_debrid_id': 23651}]
        items, status = eng.select_broken_db_items(rows, broken, HASH)
        self.assertEqual(status, 'ok')
        self.assertEqual([i['id'] for i in items], [23651])

    def test_filename_season_episode_matches_when_id_is_missing(self):
        rows = [_ep(23794, 9, 23), _ep(23651, 4, 1)]
        broken = [{'file_name': 'S04E01 Fun Run Part 1 (Extended Cut).mkv'}]
        items, status = eng.select_broken_db_items(rows, broken, HASH)
        self.assertEqual(status, 'ok')
        self.assertEqual([i['id'] for i in items], [23651])

    def test_pack_without_a_broken_file_is_unresolved(self):
        rows = [_ep(1, 1, 1), _ep(2, 1, 2)]
        items, status = eng.select_broken_db_items(rows, [], HASH)
        self.assertEqual(status, 'unresolved')
        self.assertEqual(items, [])

    def test_single_item_without_a_broken_file_is_that_item(self):
        rows = [_ep(9, 1, 1)]
        items, status = eng.select_broken_db_items(rows, None, HASH)
        self.assertEqual(status, 'ok')
        self.assertEqual([i['id'] for i in items], [9])

    def test_stale_cli_debrid_id_on_a_different_magnet_is_not_used(self):
        rows = [
            _ep(23794, 9, 23, magnet='magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),
            _ep(23651, 4, 1),
        ]
        broken = [{'file_name': 'S04E01 Fun Run.mkv', 'cli_debrid_id': 23794}]
        items, status = eng.select_broken_db_items(rows, broken, HASH)
        self.assertEqual(status, 'ok')
        self.assertEqual([i['id'] for i in items], [23651])


class TestCountTorrentSiblings(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute(
            """CREATE TABLE media_items (
                id INTEGER PRIMARY KEY,
                state TEXT,
                filled_by_magnet TEXT,
                filled_by_torrent_id TEXT,
                debrid_folder_name TEXT
            )"""
        )

    def _add(self, item_id, state, magnet, torrent_id, folder=PACK):
        self.conn.execute(
            'INSERT INTO media_items VALUES (?, ?, ?, ?, ?)',
            (item_id, state, magnet, torrent_id, folder),
        )

    def test_other_episodes_on_the_same_hash_count(self):
        self._add(23651, 'Collected', f'magnet:?xt=urn:btih:{HASH}', 'RD1')
        self._add(23600, 'Collected', f'magnet:?xt=urn:btih:{HASH}', 'RD1')
        self._add(23794, 'Checking', f'magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'RD2')
        self.assertEqual(eng.count_torrent_siblings(self.conn, HASH, PACK, [23651]), 1)

    def test_nzb_replacement_with_a_stale_folder_name_does_not_count(self):
        self._add(23651, 'Collected', f'magnet:?xt=urn:btih:{HASH}', 'RD1')
        self._add(23600, 'Collected', '', 'nzb:dead-job', PACK)
        self.assertEqual(eng.count_torrent_siblings(self.conn, HASH, PACK, [23651]), 0)

    def test_empty_magnet_on_the_same_folder_still_counts(self):
        self._add(23651, 'Collected', f'magnet:?xt=urn:btih:{HASH}', 'RD1')
        self._add(23600, 'Collected', '', 'RD1', PACK)
        self.assertEqual(eng.count_torrent_siblings(self.conn, HASH, PACK, [23651]), 1)


class TestReplaceEntryKeepsSharedTorrent(unittest.TestCase):
    def setUp(self):
        self.moved = []
        self.deleted = []
        self.rows = []
        self.siblings = 0
        self._saved = []
        self._orig = {
            '_find_db_items_by_entry_name': eng._find_db_items_by_entry_name,
            '_delete_from_climount': eng._delete_from_climount,
            '_delete_from_plex': eng._delete_from_plex,
            '_unlink_item_symlink': eng._unlink_item_symlink,
            '_torrent_sibling_count': eng._torrent_sibling_count,
            '_fetch_media_item': eng._fetch_media_item,
        }
        eng._find_db_items_by_entry_name = lambda *a, **k: self.rows
        eng._delete_from_climount = lambda *a, **k: self.deleted.append(a) or True
        eng._delete_from_plex = lambda item: True
        eng._unlink_item_symlink = lambda item: None
        eng._torrent_sibling_count = lambda *a, **k: self.siblings
        eng._fetch_media_item = lambda item_id: None
        self._patch('database.nzb_repair_activity', 'log_repair_activity', lambda **k: None)
        self._patch('usenet.repair_engine', '_junk_nzb_source_reason', lambda *a, **k: None)
        self._patch(
            'routes.debug_routes', 'move_item_to_wanted',
            lambda item_id, title: self.moved.append(item_id),
        )

    def _patch(self, modname, attr, value):
        created = modname not in sys.modules
        if created:
            sys.modules[modname] = types.ModuleType(modname)
        module = sys.modules[modname]
        had = hasattr(module, attr)
        old = getattr(module, attr, None)
        setattr(module, attr, value)
        self._saved.append((modname, attr, old, had, created))

    def tearDown(self):
        for name, original in self._orig.items():
            setattr(eng, name, original)
        for modname, attr, old, had, created in reversed(self._saved):
            if created:
                sys.modules.pop(modname, None)
            elif had:
                setattr(sys.modules[modname], attr, old)
            else:
                delattr(sys.modules[modname], attr)

    def test_office_pack_resets_only_the_flagged_episode_and_keeps_the_torrent(self):
        self.rows = [_ep(23794, 9, 23), _ep(23651, 4, 1), _ep(23600, 2, 1)]
        self.siblings = 2
        result = eng.replace_entry(PACK, HASH, broken_files=[{
            'file_name': 'S04E01 Fun Run Part 1 (Extended Cut).mkv',
            'cli_debrid_id': 23651,
            'info_hash': HASH,
        }])
        self.assertEqual(result['outcome'], 'replaced')
        self.assertEqual(self.moved, [23651])
        self.assertFalse(result['torrent_deleted'])
        self.assertEqual(self.deleted, [])

    def test_single_file_torrent_is_still_deleted(self):
        self.rows = [_ep(9, 1, 1)]
        self.siblings = 0
        result = eng.replace_entry('Movie.mkv', HASH, broken_files=[{
            'file_name': 'Movie.mkv',
            'cli_debrid_id': 9,
        }])
        self.assertEqual(result['outcome'], 'replaced')
        self.assertTrue(result['torrent_deleted'])
        self.assertEqual(len(self.deleted), 1)

    def _replace_single(self, mode):
        plex_deleted, unlinked = [], []
        eng._delete_from_plex = lambda item: plex_deleted.append(item['id']) or True
        eng._unlink_item_symlink = lambda item: unlinked.append(item['id'])
        self._patch('utilities.settings', 'get_setting',
                    lambda section, key, default=None: mode if key == 'file_collection_management' else default)
        self.rows = [_ep(9, 1, 1)]
        eng.replace_entry('Movie.mkv', HASH, broken_files=[{'file_name': 'Movie.mkv', 'cli_debrid_id': 9}])
        return plex_deleted, unlinked

    def test_symlink_mode_keeps_the_plex_item_and_unlinks_the_symlink(self):
        # Deleting the Plex item before the replacement exists made it come back as
        # "recently added"; the dead version is cleaned up after re-collection instead.
        plex_deleted, unlinked = self._replace_single('Symlinked/Local')
        self.assertEqual(plex_deleted, [])
        self.assertEqual(unlinked, [9])

    def test_plex_mode_still_deletes_from_plex(self):
        plex_deleted, unlinked = self._replace_single('Plex')
        self.assertEqual(plex_deleted, [9])

    def test_unidentified_pack_is_not_deleted_or_reset(self):
        self.rows = [_ep(1, 1, 1), _ep(2, 1, 2)]
        self.siblings = 0
        result = eng.replace_entry(PACK, HASH, broken_files=None)
        self.assertEqual(result['outcome'], 'ambiguous')
        self.assertEqual(self.moved, [])
        self.assertEqual(self.deleted, [])


class EntryIsEpisodeTests(unittest.TestCase):
    """Plex cleanup/scan after a repair picks shows/ vs movies/ from the entry name."""

    def test_separated_and_ep_prefixed_markers_are_episodes(self):
        for name in ('Lost S01 EP01 1080p BluRay DTS x264-CtrlHD', 'Show.S01.E01.1080p',
                     'Show.S01E01.1080p', 'Show.S01E101.WEB-DL'):
            self.assertTrue(eng._entry_is_episode(name), name)

    def test_movie_is_not_an_episode(self):
        self.assertFalse(eng._entry_is_episode('Parasite.2019.1080p.BluRay.x264'))

    def test_both_plex_paths_use_the_helper(self):
        src = (PROJECT_ROOT / 'usenet' / 'debrid_repair_engine.py').read_text()
        self.assertEqual(src.count('is_episode = _entry_is_episode(entry_name)'), 2)
        self.assertNotIn(r"[Ss]\d{1,2}[Ee]\d{1,2}'", src)

    def test_pattern_matches_shared_season_pack_marker(self):
        spec = importlib.util.spec_from_file_location(
            'debrid_common_utils_under_test', PROJECT_ROOT / 'debrid' / 'common' / 'utils.py')
        utils = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(utils)
        self.assertEqual(eng._EPISODE_MARKER_RE.pattern, utils._SEASON_PACK_EPISODE_RE.pattern)


if __name__ == '__main__':
    unittest.main()
