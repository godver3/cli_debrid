"""Absolute episode numbers for anime.

Every absolute computation used to sum all seasons before the target,
including season 0. cli_battery stores specials, so season-pack searches and
the matcher's absolute checks were shifted by the number of specials. The
provider's stored absolute (TVDB absoluteNumber / Trakt number_abs) is now
preferred wherever the S/E are the show's own numbering.
"""
import importlib
import sys
import unittest
from unittest import mock


def _fresh(*prefixes):
    """Import real modules even if other tests left stubs in sys.modules."""
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + '.') for p in prefixes):
            del sys.modules[name]
    return patcher


class _AnimeUtilsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(_fresh('scraper').stop)
        cls.au = importlib.import_module('scraper.functions.anime_utils')


class AbsoluteFromCountsTests(_AnimeUtilsTest):
    def test_specials_not_counted(self):
        counts = {0: 5, 1: 12, 2: 12}
        self.assertEqual(self.au.absolute_from_counts(3, 1, counts), 25)
        self.assertEqual(self.au.absolute_from_counts(1, 4, counts), 4)

    def test_movie_season_still_counted(self):
        # TVDB counts Initial D's one-episode "Third Stage" season (abs 42)
        counts = {1: 26, 2: 13, 3: 1, 4: 24}
        self.assertEqual(self.au.absolute_from_counts(3, 1, counts), 40)
        self.assertEqual(self.au.absolute_from_counts(4, 1, counts), 41)

    def test_no_counts(self):
        self.assertIsNone(self.au.absolute_from_counts(2, 1, {}))
        self.assertIsNone(self.au.absolute_from_counts(2, None, {1: 3}))

    def test_stored_absolute_preferred_only_with_imdb(self):
        counts = {0: 3, 1: 98}
        with mock.patch.object(self.au, 'cached_stored_absolute_episode', return_value=150) as stored:
            self.assertEqual(self.au.absolute_episode_for(2, 1, counts, imdb_id='tt1'), 150)
            stored.assert_called_once_with('tt1', 2, 1)
            # Scene-mapped numbers: no imdb_id, arithmetic only
            self.assertEqual(self.au.absolute_episode_for(2, 1, counts), 99)

    def test_falls_back_when_nothing_stored(self):
        with mock.patch.object(self.au, 'cached_stored_absolute_episode', return_value=None):
            self.assertEqual(self.au.absolute_episode_for(2, 1, {0: 3, 1: 98}, imdb_id='tt1'), 99)

    def test_season_counts_from_show_seasons(self):
        seasons = {0: {'episode_count': 4}, 1: {'episode_count': 10}, 'x': {}, 2: None}
        self.assertEqual(self.au.season_counts_from_show_seasons(seasons), {0: 4, 1: 10})


class ResolveAbsoluteEpisodeTests(_AnimeUtilsTest):
    """The absolute used to build anime search formats, including season packs."""

    def _patch(self, stored=None, show_counts=None):
        p1 = mock.patch.object(self.au, 'stored_absolute_episode', return_value=stored)
        p2 = mock.patch.object(self.au, '_show_season_counts', return_value=show_counts or {})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_stored_absolute_first(self):
        self._patch(stored=99, show_counts={0: 7, 1: 98})
        self.assertEqual(self.au.resolve_absolute_episode(2, 1, {1: 98}, imdb_id='tt1'), 99)

    def test_show_season_list_skips_specials(self):
        self._patch(show_counts={0: 7, 1: 98, 2: 61})
        self.assertEqual(self.au.resolve_absolute_episode(2, 1, {}, imdb_id='tt1'), 99)

    def test_xem_before_arithmetic(self):
        self._patch(show_counts={1: 98})
        xem = [{'tvdb': {'season': 2, 'episode': 3, 'absolute': 101}, 'scene': {}}]
        self.assertEqual(self.au.resolve_absolute_episode(2, 3, {1: 98}, xem_mapping=xem, imdb_id='tt1'), 101)

    def test_counts_then_episode_fallback(self):
        self._patch()
        self.assertEqual(self.au.resolve_absolute_episode(2, 3, {0: 2, 1: 12}), 15)
        self.assertEqual(self.au.resolve_absolute_episode(2, 3, {}), 3)


if __name__ == '__main__':
    unittest.main()
