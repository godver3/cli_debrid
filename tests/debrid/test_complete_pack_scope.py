"""'Complete' in a release name only means the whole series without a season.

PTT sets complete=True for "S13 COMPLETE" and "Season 3 Complete" too, and both
detect_season_episode_info and filter_results then treated those season packs
as complete-series packs: the size per episode was divided by every episode of
the show (AHS: ~145 instead of 10) and good packs failed the size check.
"""
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scraper.functions.common import detect_season_episode_info
from scraper.functions.filter_results import filter_results


class DetectSeasonEpisodeInfoTests(unittest.TestCase):
    def test_named_season_stays_season_pack(self):
        info = detect_season_episode_info({'complete': True, 'seasons': [13], 'episodes': []})
        self.assertEqual(info['season_pack'], '13')
        self.assertEqual(info['seasons'], [13])

    def test_several_named_seasons(self):
        info = detect_season_episode_info({'complete': True, 'seasons': [1, 2, 3, 4], 'episodes': []})
        self.assertEqual(info['season_pack'], '1,2,3,4')

    def test_no_season_is_complete_series(self):
        info = detect_season_episode_info({'complete': True, 'seasons': [], 'episodes': []})
        self.assertEqual(info['season_pack'], 'Complete')


class FilterCompletePackScopeTests(unittest.TestCase):
    COUNTS = {1: 12, 2: 10, 3: 12}

    def _run(self, title, seasons, size_gb):
        result = {
            'title': title, 'original_title': title, 'size': size_gb,
            'scraper_type': 'Generic', 'scraper_instance': 'test',
            'additional_metadata': {'filename': title, 'bingeGroup': None},
            'parsed_info': {
                'title': 'Test Show', 'original_title': title, 'year': 2020,
                'resolution': '1080p', 'seasons': seasons, 'episodes': [],
                'season_episode_info': detect_season_episode_info(
                    {'seasons': seasons, 'episodes': [], 'complete': True}),
            },
        }
        direct_api = Mock()
        direct_api.get_show_aliases.return_value = ({}, None)
        direct_api.get_show_metadata.return_value = ({}, None)
        direct_api.get_show_seasons.return_value = ({}, None)
        version = {
            'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
            'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
            'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0,
        }
        kept, _ = filter_results(
            results=[result], tmdb_id='1', title='Test Show', year=2020, content_type='episode',
            season=2, episode=1, multi=True, version_settings=version, runtime=45,
            episode_count=10, season_episode_counts=self.COUNTS, genres=['drama'],
            matching_aliases=['Test Show'], imdb_id='tt1', direct_api=direct_api)
        return kept

    def test_season_complete_counts_that_season_only(self):
        kept = self._run('Test.Show.S02.COMPLETE.1080p.WEB.x264', [2], 20.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 10)

    def test_named_multi_season_complete_pack_kept(self):
        # "S01-S03 COMPLETE": a complete pack that names its seasons, sized by them
        kept = self._run('Test.Show.S01-S03.COMPLETE.1080p.WEB.x264', [1, 2, 3], 60.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 34)

    def test_named_multi_season_complete_pack_sized_by_its_seasons(self):
        kept = self._run('Test.Show.Season.1-2.Complete.1080p.WEB.x264', [1, 2], 40.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 22)

    def test_multi_season_pack_without_complete_still_rejected(self):
        self.assertEqual(self._run('Test.Show.S01-S03.1080p.WEB.x264', [1, 2, 3], 60.0), [])

    def test_incomplete_multi_season_pack_still_rejected(self):
        self.assertEqual(self._run('Test.Show.S01-S03.INCOMPLETE.1080p.WEB.x264', [1, 2, 3], 60.0), [])

    def test_complete_series_counts_every_season(self):
        kept = self._run('Test.Show.COMPLETE.SERIES.1080p.WEB.x264', [], 60.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 34)


if __name__ == '__main__':
    unittest.main()
