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


class SomPackMatchTests(unittest.TestCase):
    FILES = (['/Extras/Box Art/Box 1 Scans/DISC1.png', '/Extras/Nicktoons Promo.mkv',
              '/Extras/Dragon Ball Kai NCOP 1 - Dragon Soul.mkv',
              '/Extras/Dragon Ball Kai NCED 1 - Yeah! Break! Care! Break!.mkv']
             + ['/' + SOM.format(n) for n in range(1, 99)])

    def _match(self, episode):
        m = mm.MediaMatcher()
        patcher = mock.patch.object(mm, 'parse_title', _real_parse_title())
        patcher.start()
        self.addCleanup(patcher.stop)
        parsed = [p for p in (m._parse_file_info({'path': f, 'bytes': 1}) for f in self.FILES) if p]
        item = {'type': 'episode', 'title': 'Dragon Ball Kai', 'season_number': 1,
                'episode_number': episode, 'genres': '["anime"]', 'tmdb_id': '61709'}
        with mock.patch('utilities.settings.get_setting', return_value='Symlinked/Local'):
            return m.find_best_match_from_parsed(parsed, item)

    def test_matches_episode_file(self):
        for ep in (1, 50, 98):
            result = self._match(ep)
            self.assertIsNotNone(result, ep)
            self.assertIn('.{:03d}.'.format(ep), result[0])


if __name__ == '__main__':
    unittest.main()
