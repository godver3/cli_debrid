"""A TV release named with the show's premiere year fits every season.

Sonarr-style names carry the premiere year ("JUJUTSU KAISEN (2020) S03E04"),
but the filter only allowed it within 5 years of the season's air year, so
Jujutsu Kaisen S3 (2026) dropped every such release. A remake or revival has a
different premiere year and must still be rejected.
"""
import os
import sys
import unittest
from unittest import mock
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scraper.functions.common import detect_season_episode_info
from scraper.functions.filter_results import filter_results, tv_release_year_matches


class TvReleaseYearMatchesTests(unittest.TestCase):
    def test_premiere_year_fits_a_late_season(self):
        self.assertTrue(tv_release_year_matches(2020, 2020, 2026))
        self.assertTrue(tv_release_year_matches(1993, 1993, 2004))

    def test_season_air_year_within_one(self):
        self.assertTrue(tv_release_year_matches(2026, 2020, 2026))
        self.assertTrue(tv_release_year_matches(2025, 2020, 2026))

    def test_unrelated_year_rejected(self):
        self.assertFalse(tv_release_year_matches(2015, 2020, 2026))

    def test_remake_years_rejected_both_ways(self):
        # Want the 2023 revival, release is the 1993 show (and the reverse)
        self.assertFalse(tv_release_year_matches(1993, 2023, 2024))
        self.assertFalse(tv_release_year_matches(2023, 1993, 1995))


class FilterTvYearTests(unittest.TestCase):
    """Show premiered 2020; season 3 aired 2026."""

    def _run(self, title, parsed_year):
        result = {
            'title': title, 'original_title': title, 'size': 1.2,
            'scraper_type': 'Generic', 'scraper_instance': 'test',
            'additional_metadata': {'filename': title, 'bingeGroup': None},
            'parsed_info': {
                'title': 'Test Show', 'original_title': title, 'year': parsed_year,
                'resolution': '1080p', 'seasons': [3], 'episodes': [4],
                'season_episode_info': detect_season_episode_info({'seasons': [3], 'episodes': [4]}),
            },
        }
        direct_api = Mock()
        direct_api.get_show_aliases.return_value = ({}, None)
        direct_api.get_show_metadata.return_value = ({}, None)
        version = {
            'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
            'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
            'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0,
        }
        with mock.patch('database.database_reading.get_season_year', return_value=2026):
            kept, _ = filter_results(
                results=[result], tmdb_id='1', title='Test Show', year=2020, content_type='episode',
                season=3, episode=4, multi=False, version_settings=version, runtime=24,
                episode_count=12, season_episode_counts={3: 12}, genres=['drama'],
                matching_aliases=['Test Show'], imdb_id='tt1', direct_api=direct_api)
        return kept

    def test_premiere_year_release_kept_six_years_on(self):
        self.assertEqual(len(self._run('Test Show (2020) S03E04 1080p WEB-DL', 2020)), 1)

    def test_premiere_year_in_a_year_list_kept(self):
        # Only 2020 can match: 2021 is neither the premiere nor near 2026
        self.assertEqual(len(self._run('Test Show (2020-2021) S03E04 1080p WEB-DL', [2020, 2021])), 1)

    def test_season_air_year_release_kept(self):
        self.assertEqual(len(self._run('Test Show 2026 S03E04 1080p WEB-DL', 2026)), 1)

    def test_other_year_release_rejected(self):
        self.assertEqual(self._run('Test Show (2015) S03E04 1080p WEB-DL', 2015), [])


if __name__ == '__main__':
    unittest.main()
