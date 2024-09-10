import unittest
from unittest.mock import patch, MagicMock
from scraper.scraper import *
import logging

class TestScraper(unittest.TestCase):

    def setUp(self):
        # Set up any necessary test fixtures
        pass

    def test_romanize_japanese(self):
        # Test the romanize_japanese function
        japanese_text = "こんにちは世界"
        expected_result = "konnichiha sekai"
        self.assertEqual(romanize_japanese(japanese_text), expected_result)

    @patch('scraper.scraper.logging.getLogger')
    @patch('scraper.scraper.logging.FileHandler')
    def test_setup_scraper_logger(self, mock_file_handler, mock_get_logger):
        # Test the setup_scraper_logger function
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger

        result = setup_scraper_logger()

        self.assertEqual(result, mock_logger)
        mock_logger.setLevel.assert_called_once_with(logging.DEBUG)
        mock_logger.addHandler.assert_called_once()

    def test_normalize_title(self):
        # Test the normalize_title function
        title = "The.Quick.Brown.Fox.2023.1080p.WEB-DL.H264-GROUP"
        expected_result = "the quick brown fox group"
        normalized = normalize_title(title)
        print(f"Normalized title: '{normalized}'")
        self.assertEqual(normalized, expected_result)

    @patch('scraper.scraper.get_setting')
    @patch('scraper.scraper.ScraperManager')
    def test_scrape(self, mock_scraper_manager, mock_get_setting):
        # Test the scrape function
        mock_get_setting.return_value = {}
        mock_scraper_manager.return_value.scrape_all.return_value = []

        result, filtered_out = scrape("tt1234567", "123456", "Test Movie", 2023, "movie", "default")

        self.assertEqual(result, [])
        self.assertIsNone(filtered_out)

    def test_filter_results(self):
        # Test the filter_results function
        results = [
            {
                "title": "Test Movie 1080p",
                "size": "5 GB",
                "parsed_info": {
                    "resolution": "1080p",
                    "title": "Test Movie",
                    "year": [2023],
                    "screen_size": "1080p"
                }
            },
            {
                "title": "Test Movie 720p",
                "size": "3 GB",
                "parsed_info": {
                    "resolution": "720p",
                    "title": "Test Movie",
                    "year": [2023],
                    "screen_size": "720p"
                }
            },
            {
                "title": "Wrong Movie 1080p",
                "size": "4 GB",
                "parsed_info": {
                    "resolution": "1080p",
                    "title": "Wrong Movie",
                    "year": [2023],
                    "screen_size": "1080p"
                }
            }
        ]
        filtered, pre_filtered = filter_results(
            results, "123456", "Test Movie", 2023, "movie", None, None, False,
            {"min_size_gb": 0, "max_size_gb": 10}, 120, 1, {}, []
        )
        print(f"Filtered results: {filtered}")
        print(f"Pre-filtered results: {pre_filtered}")
        self.assertEqual(len(filtered), 2)
        self.assertEqual(len(pre_filtered), 2)

    def test_deduplicate_results(self):
        # Test the deduplicate_results function
        results = [
            {"magnet": "magnet1", "title": "Movie 1", "size": "5.0"},
            {"magnet": "magnet1", "title": "Movie 1", "size": "5.0"},
            {"magnet": "magnet2", "title": "Movie 2", "size": "4.0"},
            {"title": "Movie 3", "size": "3.0"},
            {"title": "Movie 3", "size": "3.0"}
        ]
        deduplicated = deduplicate_results(results)
        self.assertEqual(len(deduplicated), 3)

    def test_detect_hdr(self):
        parsed_info_hdr = {"other": ["HDR"]}
        parsed_info_no_hdr = {"other": ["SDR"]}
        parsed_info_dv = {"title": "Movie Title DOLBY VISION"}
        
        self.assertTrue(detect_hdr(parsed_info_hdr))
        self.assertFalse(detect_hdr(parsed_info_no_hdr))
        self.assertTrue(detect_hdr(parsed_info_dv))

    def test_trim_magnet(self):
        magnet_with_dn = "magnet:?xt=urn:btih:1234567890&dn=test.file"
        magnet_without_dn = "magnet:?xt=urn:btih:1234567890"
        
        self.assertEqual(trim_magnet(magnet_with_dn), "magnet:?xt=urn:btih:1234567890")
        self.assertEqual(trim_magnet(magnet_without_dn), "magnet:?xt=urn:btih:1234567890")

    def test_compare_resolutions(self):
        self.assertGreater(compare_resolutions("1080p", "720p"), 0)
        self.assertEqual(compare_resolutions("1080p", "1080p"), 0)
        self.assertLess(compare_resolutions("720p", "1080p"), 0)

    def test_round_size(self):
        self.assertEqual(round_size("5.6"), 6)
        self.assertEqual(round_size("5.4"), 5)
        self.assertEqual(round_size("invalid"), 0)

    def test_parse_torrent_info(self):
        title = "Movie.Title.2023.1080p.BluRay.x264-GROUP"
        parsed = parse_torrent_info(title)
        self.assertEqual(parsed.get("title"), "Movie Title")
        self.assertEqual(parsed.get("year"), [2023])
        self.assertEqual(parsed.get("screen_size"), "1080p")

    def test_preprocess_title(self):
        # Test removal of specific terms
        title = "Movie.Title.2023.1080p.WEB-DL.x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertEqual(preprocessed, "Movie.Title.2023.1080p.x264-GROUP")
        self.assertNotIn("WEB-DL", preprocessed)

        # Test case insensitivity
        title = "Movie.Title.2023.720p.BluRay.x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertEqual(preprocessed, "Movie.Title.2023.720p.x264-GROUP")
        self.assertNotIn("BluRay", preprocessed)

        # Test multiple terms
        title = "Movie.Title.2023.1080p.WEB-DL.WEBRip.BluRay.DVDRip.x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertEqual(preprocessed, "Movie.Title.2023.1080p.x264-GROUP")
        self.assertNotIn("WEB-DL", preprocessed)
        self.assertNotIn("WEBRip", preprocessed)
        self.assertNotIn("BluRay", preprocessed)
        self.assertNotIn("DVDRip", preprocessed)

        # Test no terms to remove
        title = "Movie.Title.2023.1080p.x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertEqual(preprocessed, title)

        # Test with spaces instead of periods
        title = "Movie Title 2023 1080p WEB-DL x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertEqual(preprocessed, "Movie Title 2023 1080p x264-GROUP")
        self.assertNotIn("WEB-DL", preprocessed)

        # Test that periods are not removed
        title = "The.Movie.Title.2023.1080p.WEB-DL.x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertIn(".", preprocessed)
        self.assertEqual(preprocessed, "The.Movie.Title.2023.1080p.x264-GROUP")

        # Test removal of multiple adjacent terms
        title = "Movie.Title.2023.1080p.WEB-DL.BluRay.x264-GROUP"
        preprocessed = preprocess_title(title)
        self.assertEqual(preprocessed, "Movie.Title.2023.1080p.x264-GROUP")
        self.assertNotIn("..", preprocessed)

    def test_detect_season_episode_info(self):
        # Test single episode
        parsed_info = {'season': 1, 'episode': 5}
        result = detect_season_episode_info(parsed_info)
        self.assertEqual(result['season_pack'], 'N/A')
        self.assertEqual(result['seasons'], [1])
        self.assertEqual(result['episodes'], [5])
        self.assertFalse(result['multi_episode'])

        # Test multi-episode
        parsed_info = {'season': 2, 'episode': [6, 7]}
        result = detect_season_episode_info(parsed_info)
        self.assertEqual(result['season_pack'], '2')
        self.assertEqual(result['seasons'], [2])
        self.assertEqual(result['episodes'], [6, 7])
        self.assertTrue(result['multi_episode'])

        # Test season pack
        parsed_info = {'season': [1, 2, 3]}
        result = detect_season_episode_info(parsed_info)
        self.assertEqual(result['season_pack'], '1,2,3')
        self.assertEqual(result['seasons'], [1, 2, 3])
        self.assertEqual(result['episodes'], [])
        self.assertFalse(result['multi_episode'])

        # Test no season or episode info
        parsed_info = {}
        result = detect_season_episode_info(parsed_info)
        self.assertEqual(result['season_pack'], 'Unknown')
        self.assertEqual(result['seasons'], [])
        self.assertEqual(result['episodes'], [])
        self.assertFalse(result['multi_episode'])

        # Test string input
        result = detect_season_episode_info("Show.S01E05.1080p")
        self.assertEqual(result['season_pack'], 'N/A')
        self.assertEqual(result['seasons'], [1])
        self.assertEqual(result['episodes'], [5])
        self.assertFalse(result['multi_episode'])

    def test_rank_result_key(self):
        result = {
            "title": "Movie.2023.1080p.BluRay",
            "size": 5.0,
            "seeders": 100,
            "parsed_info": {"screen_size": "1080p"},
            "bitrate": 5000  # Add this line
        }
        rank = rank_result_key(result, [], "Movie", 2023, None, None, False, "movie", {})
        self.assertIsInstance(rank, tuple)
        self.assertEqual(len(rank), 4)  # Assuming it returns a 4-tuple

    class TestImprovedTitleSimilarity(unittest.TestCase):

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_non_anime(self, mock_logging):
            query_title = "The Matrix"
            result = {
                'parsed_info': {
                    'title': 'The Matrix Reloaded'
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=False)
            
            self.assertGreater(similarity, 0.5)
            self.assertLess(similarity, 1.0)
            mock_logging.debug.assert_called()

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_anime(self, mock_logging):
            query_title = "Attack on Titan"
            result = {
                'parsed_info': {
                    'title': 'Shingeki no Kyojin',
                    'alternative_title': ['Attack on Titan', 'AoT']
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=True)
            
            self.assertEqual(similarity, 1.0)
            mock_logging.debug.assert_called()

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_partial_match(self, mock_logging):
            query_title = "The Lord of the Rings"
            result = {
                'parsed_info': {
                    'title': 'The Lord of the Rings: The Fellowship of the Ring'
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=False)
            
            self.assertGreater(similarity, 0.5)
            self.assertLess(similarity, 1.0)
            mock_logging.debug.assert_called()

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_no_match(self, mock_logging):
            query_title = "Star Wars"
            result = {
                'parsed_info': {
                    'title': 'Star Trek'
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=False)
            
            self.assertLess(similarity, 0.5)
            mock_logging.debug.assert_called()

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_exact_match(self, mock_logging):
            query_title = "Inception"
            result = {
                'parsed_info': {
                    'title': 'Inception'
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=False)
            
            self.assertEqual(similarity, 1.0)
            mock_logging.debug.assert_called()

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_anime_no_match(self, mock_logging):
            query_title = "One Piece"
            result = {
                'parsed_info': {
                    'title': 'Naruto',
                    'alternative_title': ['ナルト']
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=True)
            
            self.assertLess(similarity, 0.5)
            mock_logging.debug.assert_called()

        @patch('scraper.scraper.logging')
        def test_improved_title_similarity_with_special_characters(self, mock_logging):
            query_title = "Amélie"
            result = {
                'parsed_info': {
                    'title': 'Amelie'
                }
            }
            
            similarity = improved_title_similarity(query_title, result, is_anime=False)
            
            self.assertGreater(similarity, 0.8)
            mock_logging.debug.assert_called()

if __name__ == '__main__':
    unittest.main()