"""Fallback episode extraction when PTT finds no episode.

SoM's Dragon Ball Kai BD pack names files 'Dragon.Ball.Kai.2009.001.1080p...':
PTT reads the year but no episode, and the fallback only looked at the first
standalone number (2009, rejected), so a cached complete pack matched nothing.
"""
import importlib
import sys
import unittest
from unittest import mock

import database  # noqa: F401  -- importing queues.media_matcher first is circular
from queues import media_matcher as mm


def _real_parse_title():
    # Other tests may leave a stub PTT in sys.modules; this test needs the real parser.
    saved = sys.modules.pop('PTT', None)
    try:
        return importlib.import_module('PTT').parse_title
    finally:
        if saved is not None:
            sys.modules['PTT'] = saved


SOM = 'Dragon.Ball.Kai.2009.{:03d}.1080p.BD.Dual-Audio.FLAC2.0.Hi10P.x264-SoM.mkv'


class ExtractEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.m = mm.MediaMatcher()

    def test_year_before_absolute_episode(self):
        self.assertIsNone(self.m._extract_episode_from_filename(SOM.format(1)))
        self.assertEqual(self.m._extract_delimited_episode(SOM.format(1)), 1)
        self.assertEqual(self.m._extract_delimited_episode(SOM.format(98)), 98)

    def test_existing_behaviour_kept(self):
        self.assertEqual(self.m._extract_episode_from_filename('999 1.mp4'), 999)
        self.assertEqual(self.m._extract_episode_from_filename('ep1.mp4'), 1)

    def test_no_episode_from_tokens(self):
        for name in ('Show.2018.1080p.DDP5.1.x265.mkv',):
            self.assertIsNone(self.m._extract_delimited_episode(name), name)

    def test_version_suffix(self):
        self.assertEqual(self.m._extract_delimited_episode('Show.2009.012v2.1080p.mkv'), 12)

    def test_hash_names_yield_nothing(self):
        for name in ('wr2NGtIEy5o.mkv', '6068c0eed89b4aecac47da471434828b.mkv'):
            self.assertIsNone(self.m._extract_delimited_episode(name), name)


class _PackMatchMixin:
    FILES = ()
    TITLE = 'Dragon Ball Kai'

    def _match(self, episode, files=None):
        m = mm.MediaMatcher()
        patcher = mock.patch.object(mm, 'parse_title', _real_parse_title())
        patcher.start()
        self.addCleanup(patcher.stop)
        files = self.FILES if files is None else files
        parsed = [p for p in (m._parse_file_info({'path': f, 'bytes': 1}) for f in files) if p]
        item = {'type': 'episode', 'title': self.TITLE, 'season_number': 1,
                'episode_number': episode, 'genres': '["anime"]', 'tmdb_id': '61709'}
        with mock.patch('utilities.settings.get_setting', return_value='Symlinked/Local'):
            return m.find_best_match_from_parsed(parsed, item)


class SomPackMatchTests(_PackMatchMixin, unittest.TestCase):
    FILES = (['/Extras/Box Art/Box 1 Scans/DISC1.png', '/Extras/Nicktoons Promo.mkv',
              '/Extras/Dragon Ball Kai NCOP 1 - Dragon Soul.mkv',
              '/Extras/Dragon Ball Kai NCED 1 - Yeah! Break! Care! Break!.mkv']
             + ['/' + SOM.format(n) for n in range(1, 99)])

    def test_matches_episode_file(self):
        for ep in (1, 50, 98):
            result = self._match(ep)
            self.assertIsNotNone(result, ep)
            self.assertIn('.{:03d}.'.format(ep), result[0])

    def test_stray_numbered_file_does_not_win(self):
        files = ['/Dragon.Ball.Kai.Movie.1.WEB.mkv'] + list(self.FILES)
        result = self._match(1, files)
        self.assertIsNotNone(result)
        self.assertIn('.001.', result[0])

    def test_single_file_release(self):
        result = self._match(5, ['/' + SOM.format(5)])
        self.assertIsNotNone(result)
        self.assertIn('.005.', result[0])


class StrayNumberedFileTests(_PackMatchMixin, unittest.TestCase):
    """A pack whose episodes PTT reads, missing one episode, plus a stray
    numbered non-episode file. The stray file must not fill the gap."""
    TITLE = 'Frieren'
    FILES = (['/Frieren.S01E{:02d}.1080p.WEB.x264.mkv'.format(n) for n in range(1, 11) if n != 2]
             + ['/Frieren.2023.Movie.2.WEB.mkv'])

    def test_missing_episode_not_filled_by_stray_file(self):
        self.assertIsNone(self._match(2))

    def test_present_episode_still_matches(self):
        result = self._match(3)
        self.assertIsNotNone(result)
        self.assertIn('S01E03', result[0])


class IndexablePrefixTests(unittest.TestCase):
    @staticmethod
    def _pf(prefix=None, episodes=None, special=False):
        info = {'is_anime_special_content': special}
        if episodes:
            info['episodes'] = episodes
        if prefix is not None:
            info.update(fallback_episode=1, fallback_episode_prefix=prefix, fallback_episode_delimited=True)
        return {'parsed_info': info}

    def test_main_run_kept_small_groups_dropped(self):
        files = [self._pf('show 2009') for _ in range(20)] + [self._pf('show movie')] * 2
        self.assertEqual(mm.MediaMatcher._indexable_delimited_prefixes(files), {'show 2009'})

    def test_stray_next_to_ptt_episodes_dropped(self):
        files = [self._pf(episodes=[n]) for n in range(1, 11)] + [self._pf('show part')] * 2
        self.assertEqual(mm.MediaMatcher._indexable_delimited_prefixes(files), set())

    def test_two_comparable_runs_kept(self):
        files = [self._pf('show s1 2009')] * 98 + [self._pf('show s2 2014')] * 61
        self.assertEqual(mm.MediaMatcher._indexable_delimited_prefixes(files),
                         {'show s1 2009', 'show s2 2014'})

    def test_lone_file_only_when_only_video(self):
        self.assertEqual(mm.MediaMatcher._indexable_delimited_prefixes([self._pf('show 2009')]), {'show 2009'})
        files = [self._pf('show 2009'), self._pf(special=True), self._pf(episodes=[1])]
        self.assertEqual(mm.MediaMatcher._indexable_delimited_prefixes(files), set())


if __name__ == '__main__':
    unittest.main()


class PaddedAbsoluteMatchTests(unittest.TestCase):
    """Anime S02E01 matched by a padded absolute number ("E099") in a file name.

    This path ("Pattern 4" in _check_match) once referenced an unassigned
    variable; the NameError was swallowed by the surrounding try, so padded
    absolute names silently never matched.
    """

    def test_padded_absolute_in_filename(self):
        m = mm.MediaMatcher()
        parsed_file = {'path': '/Show E099 [1080p].mkv',
                       'parsed_info': {'seasons': [], 'episodes': [], 'original_filename': 'Show E099 [1080p].mkv',
                                       'is_anime_special_content': False}}
        item = {'type': 'episode', 'title': 'Show', 'season_number': 2, 'episode_number': 1,
                'genres': ['anime'], 'tmdb_id': '1'}
        with mock.patch.object(m, '_get_season_episode_counts_cached', return_value={1: 98, 2: 61}):
            self.assertTrue(m._check_match(parsed_file, item, use_relaxed_matching=True))
