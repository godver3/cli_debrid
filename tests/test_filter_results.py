import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import logging

# Add the project root to the path so we can import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.functions.filter_results import filter_results
from scraper.functions.ptt_parser import parse_with_ptt


class TestFilterResultsAnimeXEM(unittest.TestCase):
    """Test cases for filter_results function with focus on anime and XEM mappings."""

    def setUp(self):
        """Set up test fixtures."""
        # Configure logging to avoid noise during tests
        logging.basicConfig(level=logging.ERROR)
        
        # Common test data
        self.tmdb_id = "12345"
        self.title = "Test Anime"
        self.year = 2020
        self.content_type = "episode"
        self.season = 2
        self.episode = 5
        self.multi = False
        self.runtime = 24  # 24 minutes per episode
        self.episode_count = 12
        self.season_episode_counts = {1: 12, 2: 12, 3: 12}
        self.genres = ["anime", "action"]
        self.matching_aliases = ["Test Anime", "Test Anime TV"]
        self.imdb_id = "tt12345678"
        
        # Version settings for testing
        self.version_settings = {
            'resolution_wanted': '<=',
            'max_resolution': '1080p',
            'min_size_gb': 0.1,
            'max_size_gb': 10.0,
            'filter_in': [],
            'filter_out': [],
            'enable_hdr': False,
            'similarity_threshold': 0.8,
            'similarity_threshold_anime': 0.8,
            'min_bitrate_mbps': 0.0,
            'max_bitrate_mbps': 100.0
        }
        
        # Mock DirectAPI
        self.mock_direct_api = Mock()
        self.mock_direct_api.get_show_aliases.return_value = ({}, None)
        self.mock_direct_api.get_show_metadata.return_value = ({}, None)
        self.mock_direct_api.get_show_seasons.return_value = ({}, None)

    def create_mock_result(self, title, size_gb=1.0, parsed_info=None, scraper_type="Generic"):
        """Helper method to create a mock result with given parameters."""
        if parsed_info is None:
            parsed_info = {
                'title': 'Test Anime',
                'original_title': title,
                'year': 2020,
                'resolution': '1080p',
                'source': 'WEB-DL',
                'audio': 'AAC',
                'codec': 'H.264',
                'group': 'SubsPlease',
                'seasons': [2],
                'episodes': [5],
                'season_episode_info': {
                    'season_pack': 'N/A',
                    'seasons': [2],
                    'episodes': [5]
                }
            }
        
        return {
            'title': title,
            'original_title': title,
            'size': size_gb,
            'parsed_info': parsed_info,
            'scraper_type': scraper_type,
            'scraper_instance': 'test_instance',
            'additional_metadata': {
                'filename': title,
                'bingeGroup': None
            }
        }

    def test_anime_absolute_episode_format(self):
        """Test anime with absolute episode format (e.g., '125' for episode 125)."""
        # Create a result with absolute episode format
        # Episode 125 would be S02E05 in XEM mapping (assuming 12 episodes per season)
        # Absolute episode 125 = (2-1)*12 + 5 = 17, but let's test with actual absolute number
        
        results = [
            self.create_mock_result(
                "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [],
                    'episodes': [125],  # Absolute episode number
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [],
                        'episodes': [125]
                    }
                }
            )
        ]
        
        # Add target_abs_episode to the result to simulate XEM mapping
        results[0]['target_abs_episode'] = 125
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api,
            original_episode=125  # Original absolute episode number
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime with absolute episode format")
        self.assertEqual(filtered_results[0]['original_title'], "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease")

    def test_anime_regular_season_episode_format(self):
        """Test anime with regular season/episode format (e.g., 'S02E05')."""
        results = [
            self.create_mock_result(
                "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [2],
                    'episodes': [5],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [2],
                        'episodes': [5]
                    }
                }
            )
        ]
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime with regular S/E format")

    def test_anime_combined_format(self):
        """Test anime with combined format (e.g., 'S15E520' where 520 is absolute episode)."""
        results = [
            self.create_mock_result(
                "Test.Anime.S15E520.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S15E520.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [15],
                    'episodes': [520],  # Absolute episode number in S/E format
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [15],
                        'episodes': [520]
                    }
                }
            )
        ]
        
        # Add target_abs_episode to simulate XEM mapping
        results[0]['target_abs_episode'] = 520
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=15,  # Season 15
            episode=520,  # Episode 520 (absolute)
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts={**self.season_episode_counts, 15: 12},  # Add season 15
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api,
            original_episode=520
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime with combined S/E format containing absolute episode")

    def test_anime_xem_mapping_absolute_fallback(self):
        """Test anime XEM mapping with absolute episode fallback when S/E doesn't match."""
        # Create a result where PTT parses S02E05 but we're looking for S02E05 (XEM mapped)
        # but the torrent actually contains absolute episode 125
        results = [
            self.create_mock_result(
                "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [],  # No season info
                    'episodes': [125],  # Absolute episode
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [],
                        'episodes': [125]
                    }
                }
            )
        ]
        
        # Add target_abs_episode to simulate XEM mapping
        results[0]['target_abs_episode'] = 125
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,  # Looking for E05 (XEM mapped)
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api,
            original_episode=125  # Original absolute episode
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime with absolute episode fallback")

    def test_anime_xem_mapping_original_episode_fallback(self):
        """Test anime XEM mapping with original episode fallback."""
        # Create a result where PTT parses S02E05 but we're looking for S02E05 (XEM mapped)
        # but the torrent actually contains original episode 125
        results = [
            self.create_mock_result(
                "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [2],
                    'episodes': [5],  # PTT parsed E05
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [2],
                        'episodes': [5]
                    }
                }
            )
        ]
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,  # Looking for E05 (XEM mapped)
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api,
            original_episode=125  # Original episode was 125
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime with original episode fallback")

    def test_anime_season_pack_detection(self):
        """Test anime season pack detection and filtering."""
        results = [
            self.create_mock_result(
                "Test.Anime.S02.Complete.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=15.0,  # Large size indicating pack
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S02.Complete.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [2],
                    'episodes': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],  # All episodes
                    'season_episode_info': {
                        'season_pack': 'S2',
                        'seasons': [2],
                        'episodes': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
                    }
                }
            )
        ]
        
        # Test in multi mode (should accept season pack)
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=True,  # Multi mode
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime season pack in multi mode")
        
        # Test in single mode (should reject season pack)
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=False,  # Single mode
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 0, "Should reject anime season pack in single mode")

    def test_anime_heuristic_pack_detection(self):
        """Test anime heuristic pack detection for titles without explicit pack indicators."""
        results = [
            self.create_mock_result(
                "Test.Anime.Batch.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=8.0,  # Large size
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.Batch.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [],
                    'episodes': [],  # No explicit episodes
                    'season_episode_info': {
                        'season_pack': 'Unknown',
                        'seasons': [],
                        'episodes': []
                    }
                }
            )
        ]
        
        # Test in multi mode with pack wantedness check disabled
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=True,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api,
            check_pack_wantedness=False
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime heuristic pack in multi mode")

    def test_anime_similarity_threshold(self):
        """Test anime similarity threshold enforcement."""
        # Create a result with slightly different title
        results = [
            self.create_mock_result(
                "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [2],
                    'episodes': [5],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [2],
                        'episodes': [5]
                    }
                }
            )
        ]
        
        # Test with high similarity threshold
        high_threshold_settings = self.version_settings.copy()
        high_threshold_settings['similarity_threshold_anime'] = 0.95
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title="Completely Different Anime",  # Very different title
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=self.multi,
            version_settings=high_threshold_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=[],
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 0, "Should reject anime with low similarity score")

    def test_anime_sanity_check(self):
        """Test anime sanity check for false matches."""
        # Create a result with high fuzzy similarity but low character overlap
        results = [
            self.create_mock_result(
                "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [2],
                    'episodes': [5],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [2],
                        'episodes': [5]
                    }
                }
            )
        ]
        
        # Test with a title that has high fuzzy similarity but low character overlap
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title="Test Anime",  # Same title but different content
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        # This should pass the sanity check since it's the same title
        self.assertEqual(len(filtered_results), 1, "Should pass anime sanity check with same title")

    def test_anime_no_season_info_leniency(self):
        """Test anime leniency for titles without season info."""
        results = [
            self.create_mock_result(
                "Test.Anime.E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [],  # No season info
                    'episodes': [5],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [],
                        'episodes': [5]
                    }
                }
            )
        ]
        
        # Test for S1 (should be lenient)
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=1,  # Season 1
            episode=5,
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 1, "Should be lenient for S1 with no season info")
        
        # Test for S2+ (should be more restrictive)
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=2,  # Season 2
            episode=5,
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 0, "Should be restrictive for S2+ with no season info")

    def test_anime_absolute_episode_calculation(self):
        """Test anime absolute episode calculation for XEM mapping."""
        # Test the calculation: abs_target = sum of episodes in previous seasons + current episode
        # For S02E05: abs_target = 12 (S1) + 5 (S2E05) = 17
        
        results = [
            self.create_mock_result(
                "Test.Anime.17.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.17.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [],
                    'episodes': [17],  # Absolute episode 17
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [],
                        'episodes': [17]
                    }
                }
            )
        ]
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=2,  # Season 2
            episode=5,  # Episode 5
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api
        )
        
        self.assertEqual(len(filtered_results), 1, "Should accept anime with correct absolute episode calculation")

    def test_anime_multiple_formats_mixed(self):
        """Test anime with multiple different episode formats in the same result set."""
        results = [
            # Absolute format
            self.create_mock_result(
                "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.125.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [],
                    'episodes': [125],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [],
                        'episodes': [125]
                    }
                }
            ),
            # Regular S/E format
            self.create_mock_result(
                "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S02E05.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [2],
                    'episodes': [5],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [2],
                        'episodes': [5]
                    }
                }
            ),
            # Combined format
            self.create_mock_result(
                "Test.Anime.S15E520.1080p.WEB-DL.AAC.H.264-SubsPlease",
                size_gb=1.5,
                parsed_info={
                    'title': 'Test Anime',
                    'original_title': "Test.Anime.S15E520.1080p.WEB-DL.AAC.H.264-SubsPlease",
                    'year': 2020,
                    'resolution': '1080p',
                    'source': 'WEB-DL',
                    'audio': 'AAC',
                    'codec': 'H.264',
                    'group': 'SubsPlease',
                    'seasons': [15],
                    'episodes': [520],
                    'season_episode_info': {
                        'season_pack': 'N/A',
                        'seasons': [15],
                        'episodes': [520]
                    }
                }
            )
        ]
        
        # Add target_abs_episode to first and third results
        results[0]['target_abs_episode'] = 125
        results[2]['target_abs_episode'] = 520
        
        filtered_results, pre_size_filtered = filter_results(
            results=results,
            tmdb_id=self.tmdb_id,
            title=self.title,
            year=self.year,
            content_type=self.content_type,
            season=self.season,
            episode=self.episode,
            multi=self.multi,
            version_settings=self.version_settings,
            runtime=self.runtime,
            episode_count=self.episode_count,
            season_episode_counts=self.season_episode_counts,
            genres=self.genres,
            matching_aliases=self.matching_aliases,
            imdb_id=self.imdb_id,
            direct_api=self.mock_direct_api,
            original_episode=125
        )
        
        # Should accept the first two results (absolute and regular S/E format)
        # The third result is for a different season/episode combination
        self.assertEqual(len(filtered_results), 2, "Should accept multiple anime formats in mixed result set")


if __name__ == '__main__':
    unittest.main()
