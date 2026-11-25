"""A year-like number in the title is not a release year.

"Blade Runner 2099" (2026) lost every 1080p release: the parser read 2099 from
the name as the release year, and 2099 doesn't fit the 2026 season. Years that
are part of the wanted title are now ignored by the year checks; any other year
in the release name is still checked.
"""
import os
import sys
import unittest
from unittest import mock
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scraper.functions.common import detect_season_episode_info
from scraper.functions.filter_results import filter_results, release_years_outside_title

VERSION = {
    'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
    'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
    'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0,
}


class ReleaseYearsOutsideTitleTests(unittest.TestCase):
    def test_title_year_dropped(self):
        self.assertEqual(release_years_outside_title(2099, 'Blade Runner 2099 S01E03 1080p', 'Blade Runner 2099'), [])

    def test_other_year_kept(self):
        self.assertEqual(release_years_outside_title([2099, 2026], 'x', 'Blade Runner 2099'), [2026])

    def test_no_title_year_unchanged(self):
        self.assertEqual(release_years_outside_title(2015, 'x', 'Test Show'), [2015])
        self.assertEqual(release_years_outside_title(None, 'x', 'Test Show'), [])

    def test_fallback_skips_title_year(self):
        self.assertEqual(release_years_outside_title(
            None, 'Blade Runner 2099 S01E03 1080p', 'Blade Runner 2099', extract_fallback=True), [])
        self.assertEqual(release_years_outside_title(
            None, '1923 2025 S02E01 1080p', '1923', extract_fallback=True), [2025])

    def test_fallback_off_by_default(self):
        self.assertEqual(release_years_outside_title(None, 'Test Show 2015 S01E01', 'Test Show'), [])


def _result(title, parsed_title, parsed_year, seasons=None, episodes=None):
    se = {'seasons': seasons or [], 'episodes': episodes or []}
    return {
        'title': title, 'original_title': title, 'size': 3.4,
        'scraper_type': 'Generic', 'scraper_instance': 'test',
        'additional_metadata': {'filename': title, 'bingeGroup': None},
        'parsed_info': {
            'title': parsed_title, 'original_title': title, 'year': parsed_year,
            'resolution': '1080p', **se,
            'season_episode_info': detect_season_episode_info(se),
        },
    }


def _direct_api():
    api = Mock()
    api.get_show_aliases.return_value = ({}, None)
    api.get_show_metadata.return_value = ({}, None)
    api.get_movie_aliases.return_value = ({}, None)
    api.get_movie_metadata.return_value = ({}, None)
    return api


class FilterTitleYearTvTests(unittest.TestCase):
    """Blade Runner 2099 premiered and aired S1 in 2026."""

    def _run(self, title, parsed_year):
        result = _result(title, 'Blade Runner 2099', parsed_year, seasons=[1], episodes=[3])
        with mock.patch('database.database_reading.get_season_year', return_value=2026):
            kept, _ = filter_results(
                results=[result], tmdb_id='171802', title='Blade Runner 2099', year=2026,
                content_type='episode', season=1, episode=3, multi=False, version_settings=VERSION,
                runtime=50, episode_count=8, season_episode_counts={1: 8}, genres=['drama'],
                matching_aliases=['Blade Runner 2099'], imdb_id='tt18224594', direct_api=_direct_api())
        return kept

    def test_title_year_release_kept(self):
        self.assertEqual(len(self._run('Blade Runner 2099 S01E03 1080p AMZN WEB-DL DDP5 1 Atmos H 264-FLUX', 2099)), 1)

    def test_title_year_unparsed_release_kept(self):
        self.assertEqual(len(self._run('Blade Runner 2099 S01E03 1080p WEB H264-CAKES', None)), 1)

    def test_wrong_extra_year_still_rejected(self):
        self.assertEqual(self._run('Blade Runner 2099 2015 S01E03 1080p WEB H264', [2099, 2015]), [])


class FilterTitleYearMovieTests(unittest.TestCase):
    """2001: A Space Odyssey (1968)."""

    def _run(self, title, parsed_year):
        result = _result(title, '2001 A Space Odyssey', parsed_year)
        kept, _ = filter_results(
            results=[result], tmdb_id='62', title='2001 A Space Odyssey', year=1968,
            content_type='movie', season=None, episode=None, multi=False, version_settings=VERSION,
            runtime=149, episode_count=0, season_episode_counts={}, genres=['science fiction'],
            matching_aliases=['2001 A Space Odyssey'], imdb_id='tt0062622', direct_api=_direct_api())
        return kept

    def test_title_year_only_kept(self):
        self.assertEqual(len(self._run('2001 A Space Odyssey 1080p BluRay x264', 2001)), 1)

    def test_real_year_still_checked(self):
        self.assertEqual(len(self._run('2001 A Space Odyssey 1968 1080p BluRay x264', [2001, 1968])), 1)
        self.assertEqual(self._run('2001 A Space Odyssey 1990 1080p BluRay x264', [2001, 1990]), [])


if __name__ == '__main__':
    unittest.main()
