"""Anime packs numbered 01-39 straight through, for a show in TVDB seasons.

Revolutionary Girl Utena (tt0205410) is 4 TVDB seasons (13/11/9/6). Packs
name the files by absolute number only: "[Exiled-Destiny]_..._Ep14_(...).mkv",
"[GHOST] ... - 14 - [...]", "[Some-Stuffs] Revolutionary Girl Utena 14 (BD
720p)". Season 1 matched (absolute == episode). S2E1 (absolute 14) picked
only the files numbered 1 as candidates, rejected them, and every S2-S4 item
was blacklisted after the cached torrents were added and removed (Discord
report, 2026-11-26).
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

COUNTS = {1: 13, 2: 11, 3: 9, 4: 6}
EXILED = [f'/[Exiled-Destiny]_Revolutionary_Girl_Utena_Ep{i:02d}_(ABCD{i:04X}).mkv' for i in range(1, 40)]
GHOST = [f'/[GHOST][1080p] Revolutionary Girl Utena - {i:02d} - [BD HEVC 10bit Dual Audio AC3][7FAE{i:04X}].mkv'
         for i in range(1, 40)]
SINGLE = ['/[Some-Stuffs] Revolutionary Girl Utena 14 (BD 720p) [F03F20BB].mkv']


def _real_parse_title():
    saved = sys.modules.pop('PTT', None)
    try:
        return importlib.import_module('PTT').parse_title
    finally:
        if saved is not None:
            sys.modules['PTT'] = saved


def _absolute(season, episode):
    return sum(COUNTS[s] for s in COUNTS if s < season) + episode


class LaterSeasonAbsolutePackTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(anime_utils, 'cached_stored_absolute_episode',
                                    side_effect=lambda imdb, s, e: _absolute(s, e)),
                  mock.patch.object(anime_utils, '_show_season_counts', return_value=dict(COUNTS)),
                  mock.patch.object(mm, 'parse_title', _real_parse_title())):
            p.start()
            self.addCleanup(p.stop)
        self.m = mm.MediaMatcher()
        self.m._get_season_episode_counts_cached = lambda tmdb_id: dict(COUNTS)

    def _item(self, season, episode):
        return {'id': 198885, 'type': 'episode', 'title': 'Revolutionary Girl Utena',
                'series_title': 'Revolutionary Girl Utena', 'imdb_id': 'tt0205410', 'tmdb_id': '1042',
                'season_number': season, 'episode_number': episode, 'genres': '["anime"]', 'version': 'Anime'}

    def _match(self, files, season, episode):
        parsed = [p for p in (self.m._parse_file_info({'path': f, 'bytes': 1}) for f in files) if p]
        with mock.patch('utilities.settings.get_setting', return_value='Symlinked/Local'):
            hit = self.m.find_best_match_from_parsed(parsed, self._item(season, episode))
        return hit[0] if hit else None

    def test_season_one_still_matches_its_own_numbers(self):
        self.assertIn('_Ep05_', self._match(EXILED, 1, 5))

    def test_later_seasons_match_by_absolute_number(self):
        for files, fmt in ((EXILED, '_Ep{:02d}_'), (GHOST, ' - {:02d} - ')):
            for season, episode in ((2, 1), (2, 11), (3, 1), (4, 6)):
                with self.subTest(files=files[0][:20], season=season, episode=episode):
                    self.assertIn(fmt.format(_absolute(season, episode)), self._match(files, season, episode))

    def test_later_season_never_takes_the_file_with_its_relative_number(self):
        # Only Ep01 in the torrent: S2E1 is absolute 14, so no match
        self.assertIsNone(self._match(EXILED[:1], 2, 1))

    def test_names_the_parser_reads_no_episode_from(self):
        # PTT gives no episode for these; the filename fallback reads 14
        self.assertIn('Utena 14 (BD', self._match(SINGLE, 2, 1))
        kuchi = [f'/{i:02d} Shoujo Kakumei Utena (1997) [BD 720p Hi10p 5.1 AAC][dual-audio][kuchikirukia].mkv'
                 for i in range(1, 40)]
        self.assertTrue(self._match(kuchi, 1, 5).startswith('05 Shoujo'))
        self.assertTrue(self._match(kuchi, 2, 1).startswith('14 Shoujo'))
        self.assertTrue(self._match(kuchi, 4, 6).startswith('39 Shoujo'))
        self.assertIsNone(self._match(SINGLE, 2, 2))

    def test_related_items_match_non_delimited_fallback_episodes(self):
        kuchi = [f'/{i:02d} Shoujo Kakumei Utena (1997) [BD 720p Hi10p 5.1 AAC][dual-audio][kuchikirukia].mkv'
                 for i in range(1, 40)]
        parsed = [p for p in (self.m._parse_file_info({'path': f, 'bytes': 1}) for f in kuchi) if p]
        items = [dict(self._item(s, e), id=s * 100 + e)
                 for s, count in COUNTS.items() for e in range(1, count + 1)]
        def _setting(section, key, default=None):
            return 'Symlinked/Local' if key == 'file_collection_management' else default

        with mock.patch('utilities.settings.get_setting', side_effect=_setting):
            related = self.m.find_related_items(parsed, [], items, self._item(2, 1),
                                                torrent_title='Shoujo Kakumei Utena (1997) [BD 720p]')
        by_ep = {(i['season_number'], i['episode_number']): fn for i, fn in related}
        self.assertEqual(len(by_ep), 39)
        self.assertTrue(by_ep[(1, 5)].startswith('05 Shoujo'))
        self.assertTrue(by_ep[(2, 1)].startswith('14 Shoujo'))
        self.assertTrue(by_ep[(4, 6)].startswith('39 Shoujo'))
