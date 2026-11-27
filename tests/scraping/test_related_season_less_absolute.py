"""Related items for files that name no season ("Show - 0053 - Title").

Such a file is one episode, never episode N of every season. Its season is the
one the pack's title states; else the absolute number over the show's season
counts decides; episode 0 is no regular episode.
"""
import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  -- importing queues.media_matcher first is circular
from queues import media_matcher as mm
from scraper.functions import anime_utils

COUNTS = {1: 80, 2: 52, 3: 40, 4: 52}


def _real_parse_title():
    saved = sys.modules.pop('PTT', None)
    try:
        return importlib.import_module('PTT').parse_title
    finally:
        if saved is not None:
            sys.modules['PTT'] = saved


class RelatedSeasonLessTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(mm, 'parse_title', _real_parse_title()),
                  mock.patch.object(anime_utils, 'cached_stored_absolute_episode', return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.counts = dict(COUNTS)
        self.m = mm.MediaMatcher(relaxed_matching=True)
        self.m._get_season_episode_counts_cached = lambda tmdb_id: self.counts

    def _item(self, season, episode, genres='["animation"]'):
        return {'id': season * 1000 + episode, 'type': 'episode', 'title': 'Pokémon', 'series_title': 'Pokémon',
                'imdb_id': 'tt0168366', 'season_number': season, 'episode_number': episode,
                'genres': genres, 'tmdb_id': '1', 'version': '1080p'}

    def _related(self, title, names, original, items):
        parsed = [p for p in (self.m._parse_file_info({'path': f'/{title}/{n}', 'bytes': 1}) for n in names) if p]
        self.assertEqual(len(parsed), len(names))
        with mock.patch('utilities.settings.get_setting', return_value=False):
            related = self.m.find_related_items(parsed, [], items, original, torrent_title=title)
        return sorted((i['season_number'], i['episode_number']) for i, _ in related)

    def test_pack_season_in_title_matches_only_that_season(self):
        title = '[Koten_Gars] Pokemon - US Season 1 [BD][Hi10][1080p][FLAC][CC] (English Dubbed)'
        names = [f'[Koten_Gars] Pokémon - {n:04d} - Ep [BD][Hi10][1080p][FLAC] [261E8F44].mkv' for n in range(51, 56)]
        names.append('[Koten_Gars] Pokémon - 0000 - A sneak peek at Pokémon! [VHS][480p].mkv')
        items = [self._item(s, e) for s in (1, 2, 3, 4) for e in (0, 51, 52, 53, 54, 55) if e <= COUNTS[s]]
        for genres in ('["animation"]', '["anime"]'):
            items = [dict(i, genres=genres) for i in items]
            self.assertEqual(self._related(title, names, self._item(1, 51, genres), items),
                             [(1, e) for e in range(51, 56)], genres)

    def test_normal_season_episode_pack_unchanged(self):
        title = 'Show.S02.1080p.WEB'
        names = [f'Show.S02E{e:02d}.1080p.WEB.mkv' for e in (1, 2, 3)]
        items = [self._item(s, e, '[]') for s in (1, 2, 3) for e in (1, 2, 3, 4)]
        self.assertEqual(self._related(title, names, self._item(2, 1, '[]'), items), [(2, 1), (2, 2), (2, 3)])

    def test_absolute_pack_without_season_in_title_maps_by_counts(self):
        # Files 0081-0083 are S2E1-E3 (S1 has 80); nothing in the title says so
        title = '[Grp] Pokemon - Complete [BD][1080p]'
        names = [f'[Grp] Pokémon - {n:04d} - Ep [BD][1080p].mkv' for n in (81, 82, 83)]
        items = [self._item(s, e, '["anime"]') for s in (1, 2, 3) for e in (1, 2, 3, 81, 82, 83) if e <= COUNTS[s]]
        self.assertEqual(self._related(title, names, self._item(2, 1, '["anime"]'), items), [(2, 1), (2, 2), (2, 3)])

    def test_season_less_pack_for_a_later_season_stays_in_that_season(self):
        # "Show - 05" files of a pack for the original item's S02: relative numbers
        title = '[Grp] Pokemon [BD][1080p]'
        names = [f'[Grp] Pokémon - {n:02d} - Ep [BD][1080p].mkv' for n in (5, 6)]
        items = [self._item(s, e) for s in (1, 2, 3) for e in (5, 6)]
        self.assertEqual(self._related(title, names, self._item(2, 5), items), [(2, 5), (2, 6)])

    def test_without_counts_only_pack_season_matches(self):
        self.counts = None
        title = '[Grp] Pokemon - Season 2 [BD]'
        names = ['[Grp] Pokémon - 0053 - Ep [BD].mkv']
        items = [self._item(s, 53) for s in (1, 2, 3)]
        items[1]['season_number'] = 2
        self.assertEqual(self._related(title, names, self._item(2, 1), items), [(2, 53)])

    def test_season_titled_pack_with_absolute_range_maps_as_absolute(self):
        # S1=12, S2=24: "Season 2" pack with files 13..36 is absolute (file 14 is S2E2, not S2E14)
        self.counts = {1: 12, 2: 24}
        title = '[Grp] Show - Season 2 [BD]'
        names = [f'[Grp] Show - {n:02d} - Ep [BD].mkv' for n in range(13, 37)]
        parsed = [p for p in (self.m._parse_file_info({'path': f'/{title}/{n}', 'bytes': 1}) for n in names) if p]
        ctx = self.m._seasonless_context(parsed, self._item(2, 1, '["animation"]'), title_season=2, pack_season=2)
        file_14 = parsed[1]  # "Show - 14 - Ep [BD].mkv"
        self.assertTrue(self.m._seasonless_file_fits(file_14, self._item(2, 2, '["animation"]'), None, ctx))
        self.assertFalse(self.m._seasonless_file_fits(file_14, self._item(2, 14, '["animation"]'), None, ctx))
        items = [self._item(2, e, '["anime"]') for e in range(1, 25)]
        with mock.patch('utilities.settings.get_setting', return_value=False):
            related = self.m.find_related_items(parsed, [], items, self._item(2, 1, '["anime"]'), torrent_title=title)
        by_ep = {(i['season_number'], i['episode_number']): fn for i, fn in related}
        self.assertEqual(len(by_ep), 24)
        self.assertIn(' - 14 - ', by_ep[(2, 2)])
        self.assertIn(' - 26 - ', by_ep[(2, 14)])
        self.assertIn(' - 36 - ', by_ep[(2, 24)])


if __name__ == '__main__':
    unittest.main()
