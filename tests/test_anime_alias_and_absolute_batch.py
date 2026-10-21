"""Anime search alias choice and absolute-numbered batches.

- The romaji alias searched for an anime is the plain-ASCII spelling release
  groups use ("Sousou no Frieren", not "Sōsō no Furīren"), and never an arc or
  sequel name ("Bleach: Sennen Kessen-hen" for Bleach season 13).
- A batch numbered by absolute episode with no season in the name
  ("[Erai-raws] Bleach - 001 ~ 366 [BATCH]") is kept for a later season it
  holds, instead of being read as season 1 and rejected.
"""
import os
import sys
import unittest
from unittest import mock
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.functions import anime_utils
from scraper.functions.anime_utils import pick_romanized_alias, seasons_in_absolute_batch
from scraper.functions.common import detect_season_episode_info
from scraper.functions.filter_results import filter_results

# Japan-sourced alias lists as cli_battery returned them (2026-10-20)
FRIEREN = ['Sōsō no Furīren', 'Sōsō no Frieren', 'Sousou no Frieren', '葬送のフリーレン', 'そうそうのフリーレン']
BLEACH = ['BLEACH', 'BLEACH - ブリーチ -', 'BLEACH 千年血戦篇', 'Bleach: Sennen Kessen-hen', 'ブリーチ',
          'Bleach - Sennen Kessen Hen', 'Bleach - Sennen Kessen Hen - Ketsubetsu Tan', 'Bleach Sennen Kessen Hen',
          'Burīchi: Sennen Kessen-hen', 'Bleach - Sennen Kessen-hen', 'BLEACH Sennen Kessen-hen: Kakahjuu-tan']
TITAN = ['進撃の巨人', 'Shingeki no Kyojin', 'SNK', 'Shingeki no Kyojin The Final Season',
         'Shingeki no Kyojin: Kuinaki Sentaku', 'Shingeki no Kyojin Lost Girls']
DEMON_SLAYER = ['鬼滅の刃', 'Kimetsu no Yaiba', 'kny', 'Kimetsu no Yaiba: Hashira Geiko-hen',
                'Kimetsu no Yaiba: Mugen Ressha-hen', 'Kimetsu no Yaiba Katanakaji no Sato Hen']
ONE_PIECE = ['ワンピース', 'Wan Pisu', 'ONE PIECE FAN LETTER', 'ONE PIECE']


class PickRomanizedAliasTests(unittest.TestCase):
    def test_ascii_spelling_preferred_over_macrons(self):
        self.assertEqual(pick_romanized_alias(FRIEREN, "Frieren: Beyond Journey's End"), 'Sousou no Frieren')

    def test_accented_alias_used_when_no_ascii_one(self):
        self.assertEqual(pick_romanized_alias(['Sōsō no Furīren', '葬送のフリーレン'], 'Frieren'), 'Sōsō no Furīren')

    def test_arc_names_skipped(self):
        # "BLEACH" is the title; everything else is the Thousand-Year Blood War arc
        self.assertIsNone(pick_romanized_alias(BLEACH, 'BLEACH'))

    def test_base_name_kept_and_its_sequels_skipped(self):
        self.assertEqual(pick_romanized_alias(TITAN, 'Attack on Titan'), 'Shingeki no Kyojin')
        self.assertEqual(pick_romanized_alias(DEMON_SLAYER, 'Demon Slayer: Kimetsu no Yaiba'), 'Kimetsu no Yaiba')
        self.assertEqual(pick_romanized_alias(ONE_PIECE, 'One Piece'), 'Wan Pisu')

    def test_already_tried_alias_skipped(self):
        self.assertEqual(pick_romanized_alias(FRIEREN, 'Frieren', {'sousou no frieren'}), 'Sōsō no Furīren')


# Bleach-like: S13 is absolute 230-265, seasons 1-16 are 366 episodes, then the TYBW season
BLEACH_COUNTS = {1: 20, 2: 21, 3: 22, 4: 32, 5: 19, 6: 15, 7: 15, 8: 16, 9: 22,
                 10: 17, 11: 13, 12: 17, 13: 36, 14: 51, 15: 22, 16: 28, 17: 52}


class AbsoluteBatchTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(anime_utils, 'cached_stored_absolute_episode', return_value=None),
            mock.patch.object(anime_utils, '_show_season_counts', return_value=dict(BLEACH_COUNTS)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_seasons_covered(self):
        self.assertEqual(seasons_in_absolute_batch(range(1, 367), BLEACH_COUNTS), list(range(1, 17)))
        self.assertEqual(seasons_in_absolute_batch(range(230, 266), BLEACH_COUNTS), [13])
        self.assertEqual(seasons_in_absolute_batch(range(230, 260), BLEACH_COUNTS), [])

    def _run(self, title, episodes, size_gb, season=13, genres=('anime',), named_seasons=()):
        named_seasons = list(named_seasons)
        parsed = {'seasons': named_seasons, 'episodes': list(episodes)}
        result = {
            'title': title, 'original_title': title, 'size': size_gb,
            'scraper_type': 'Generic', 'scraper_instance': 'test',
            'additional_metadata': {'filename': title, 'bingeGroup': None},
            'parsed_info': {
                'title': 'Bleach', 'original_title': title, 'year': 2004,
                'resolution': '1080p', 'seasons': named_seasons, 'episodes': list(episodes),
                'season_episode_info': detect_season_episode_info(parsed),
            },
        }
        direct_api = Mock()
        direct_api.get_show_aliases.return_value = ({}, None)
        direct_api.get_show_metadata.return_value = ({}, None)
        direct_api.get_show_seasons.return_value = ({s: {'episode_count': c} for s, c in BLEACH_COUNTS.items()}, None)
        version = {
            'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
            'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
            'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0,
        }
        kept, _ = filter_results(
            results=[result], tmdb_id='1', title='Bleach', year=2004, content_type='episode',
            season=season, episode=1, multi=True, version_settings=version, runtime=24,
            episode_count=BLEACH_COUNTS[season], season_episode_counts={}, genres=list(genres),
            matching_aliases=['Bleach'], imdb_id='tt0434665', direct_api=direct_api)
        return kept

    def test_whole_run_batch_kept_for_later_season(self):
        kept = self._run('[Erai-raws] Bleach - 001 ~ 366 [1080p][MultiSub] [BATCH]', range(1, 367), 300.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['parsed_info']['season_episode_info']['seasons'], list(range(1, 17)))
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 366)

    def test_single_season_absolute_range_kept(self):
        kept = self._run('[Group] Bleach - 230-265 [1080p] [Batch]', range(230, 266), 30.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['parsed_info']['season_episode_info']['season_pack'], '13')
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 36)

    def test_named_season_with_absolute_episodes_kept(self):
        kept = self._run('Bleach S13 Ep. 230-265 (Eng. Sub) (1080p)', range(230, 266), 30.0, named_seasons=[13])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['parsed_info']['season_episode_info']['season_pack'], '13')

    def test_named_season_with_its_own_numbering_untouched(self):
        # S13 01-36 is an ordinary season pack; the absolute path must not touch it
        kept = self._run('Bleach S13 01-36 (1080p)', range(1, 37), 30.0, named_seasons=[13])
        self.assertEqual(len(kept), 1)
        self.assertNotIn('absolute_batch', kept[0]['parsed_info']['season_episode_info'])

    def test_overlapping_range_sized_by_its_own_episodes(self):
        kept = self._run('[Group] Bleach 221-302 [Batch]', range(221, 303), 80.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['parsed_info']['season_episode_info']['seasons'], [13])
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 82)

    def test_range_short_of_the_season_still_rejected(self):
        self.assertEqual(self._run('[Group] Bleach - 001-251 [1080p] [Batch]', range(1, 252), 200.0), [])

    def test_not_anime_unchanged(self):
        self.assertEqual(self._run('[Erai-raws] Bleach - 001 ~ 366 [BATCH]', range(1, 367), 300.0,
                                   genres=('animation',)), [])


if __name__ == '__main__':
    unittest.main()
