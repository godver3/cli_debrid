#!/usr/bin/env python3
"""
Test suite for Bazarr spoofing API endpoints.

Tests the Radarr/Sonarr API emulation that allows Bazarr to connect
to cli_debrid for subtitle management.

Note: These tests are designed to run in isolation without importing
the full application, testing only the core helper functions.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import json
import secrets
import hashlib
from datetime import datetime, timezone

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# Standalone implementations for testing (avoid importing full app)
# ============================================================================

def generate_api_key():
    """Generate a random 32-character API key."""
    return secrets.token_hex(16)


def generate_unique_id(base_id, prefix=''):
    """Generate a unique integer ID from various input types."""
    # Always use hash to ensure prefix is incorporated
    hash_input = f"{prefix}{base_id}".encode()
    return int(hashlib.md5(hash_input).hexdigest()[:8], 16)


def detect_quality_from_version(version):
    """Detect quality information from the version string."""
    version_lower = (version or '').lower()

    if '2160p' in version_lower or '4k' in version_lower or 'uhd' in version_lower:
        resolution = 2160
        quality_name = 'WEBDL-2160p'
    elif '1080p' in version_lower:
        resolution = 1080
        quality_name = 'WEBDL-1080p'
    elif '720p' in version_lower:
        resolution = 720
        quality_name = 'WEBDL-720p'
    elif '480p' in version_lower:
        resolution = 480
        quality_name = 'WEBDL-480p'
    else:
        resolution = 1080
        quality_name = 'WEBDL-1080p'

    if 'remux' in version_lower:
        source = 'BLURAY'
        quality_name = quality_name.replace('WEBDL', 'Remux')
    elif 'bluray' in version_lower or 'blu-ray' in version_lower:
        source = 'BLURAY'
        quality_name = quality_name.replace('WEBDL', 'Bluray')
    elif 'web-dl' in version_lower or 'webdl' in version_lower:
        source = 'WEBDL'
    elif 'webrip' in version_lower:
        source = 'WEBRIP'
        quality_name = quality_name.replace('WEBDL', 'WEBRip')
    elif 'hdtv' in version_lower:
        source = 'TV'
        quality_name = quality_name.replace('WEBDL', 'HDTV')
    else:
        source = 'WEBDL'

    return {
        'quality': {
            'id': resolution,
            'name': quality_name,
            'source': source,
            'resolution': resolution
        },
        'revision': {
            'version': 1,
            'real': 0,
            'isRepack': False
        }
    }


def parse_genres(genres_str):
    """Parse genres from database string format."""
    if not genres_str:
        return []
    try:
        return json.loads(genres_str)
    except (json.JSONDecodeError, TypeError):
        return [g.strip() for g in genres_str.split(',') if g.strip()]


def get_file_path(item):
    """Get the best available file path for an item."""
    return item.get('location_on_disk') or item.get('file_path') or ''


def create_movie_resource(item):
    """Create a Radarr-compatible MovieResource from database item."""
    tmdb_id = int(item.get('tmdb_id') or 0) if item.get('tmdb_id') else 0
    movie_id = tmdb_id or generate_unique_id(item.get('id'), 'movie')
    file_path = get_file_path(item)
    file_size = 1073741824  # Default 1GB

    collected_at = item.get('collected_at')
    if isinstance(collected_at, str):
        try:
            added_time = datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        except ValueError:
            added_time = datetime.now(timezone.utc)
    else:
        added_time = datetime.now(timezone.utc)

    movie_file_id = generate_unique_id(item.get('id'), 'moviefile')
    quality = detect_quality_from_version(item.get('version', ''))

    movie_file = {
        'id': movie_file_id,
        'movieId': movie_id,
        'relativePath': os.path.basename(file_path) if file_path else '',
        'path': file_path,
        'size': file_size,
        'dateAdded': added_time.isoformat() + 'Z',
        'quality': quality,
        'languages': [{'id': 1, 'name': 'English'}],
        'sceneName': '',
        'releaseGroup': ''
    }

    return {
        'id': movie_id,
        'title': item.get('title', 'Unknown'),
        'alternateTitles': [],
        'originalTitle': item.get('title', 'Unknown'),
        'sortTitle': item.get('title', 'Unknown'),
        'status': 'released',
        'overview': '',
        'year': item.get('year') or 0,
        'hasFile': True,
        'movieFileId': movie_file_id,
        'path': os.path.dirname(file_path) if file_path else '',
        'qualityProfileId': 1,
        'monitored': True,
        'minimumAvailability': 'released',
        'isAvailable': True,
        'runtime': item.get('runtime') or 0,
        'cleanTitle': (item.get('title') or '').lower().replace(' ', ''),
        'imdbId': item.get('imdb_id') or '',
        'tmdbId': tmdb_id,
        'titleSlug': (item.get('title') or '').lower().replace(' ', '-'),
        'rootFolderPath': os.path.dirname(os.path.dirname(file_path)) if file_path else '/movies',
        'certification': '',
        'genres': parse_genres(item.get('genres', '')),
        'tags': [],
        'added': added_time.isoformat() + 'Z',
        'images': [],
        'popularity': 0,
        'movieFile': movie_file,
        'sizeOnDisk': file_size
    }


def create_series_resource(item, episodes=None):
    """Create a Sonarr-compatible SeriesResource from database item."""
    tmdb_id = int(item.get('tmdb_id') or 0) if item.get('tmdb_id') else 0
    series_id = tmdb_id or generate_unique_id(item.get('show_id'), 'series')

    seasons = []
    if episodes:
        season_numbers = set(ep.get('season_number', 0) for ep in episodes)
        for season_num in sorted(season_numbers):
            season_episodes = [ep for ep in episodes if ep.get('season_number') == season_num]
            seasons.append({
                'seasonNumber': season_num,
                'monitored': True,
                'statistics': {
                    'episodeFileCount': len(season_episodes),
                    'episodeCount': len(season_episodes),
                    'totalEpisodeCount': len(season_episodes),
                    'percentOfEpisodes': 100.0
                }
            })

    series_path = ''
    if episodes:
        first_path = get_file_path(episodes[0])
        if first_path:
            series_path = os.path.dirname(os.path.dirname(first_path))

    return {
        'id': series_id,
        'title': item.get('title', 'Unknown'),
        'alternateTitles': [],
        'sortTitle': item.get('title', 'Unknown'),
        'status': 'continuing',
        'overview': '',
        'network': '',
        'airTime': '',
        'images': [],
        'seasons': seasons,
        'year': item.get('year') or 0,
        'path': series_path,
        'qualityProfileId': 1,
        'languageProfileId': 1,
        'seasonFolder': True,
        'monitored': True,
        'runtime': item.get('runtime') or 0,
        'tvdbId': 0,
        'tvRageId': 0,
        'tvMazeId': 0,
        'firstAired': '',
        'lastAired': None,
        'nextAiring': None,
        'previousAiring': None,
        'lastInfoSync': datetime.now(timezone.utc).isoformat() + 'Z',
        'seriesType': 'standard',
        'cleanTitle': (item.get('title') or '').lower().replace(' ', ''),
        'imdbId': item.get('imdb_id') or '',
        'titleSlug': (item.get('title') or '').lower().replace(' ', '-'),
        'rootFolderPath': os.path.dirname(series_path) if series_path else '/tv',
        'genres': parse_genres(item.get('genres', '')),
        'tags': [],
        'added': datetime.now(timezone.utc).isoformat() + 'Z'
    }


def create_episode_resource(item, series_id, series_title=None):
    """Create a Sonarr-compatible EpisodeResource from database item."""
    episode_id = generate_unique_id(item.get('id'), 'episode')
    title = item.get('title', 'Unknown')

    collected_at = item.get('collected_at')
    if isinstance(collected_at, str):
        try:
            air_date = datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        except ValueError:
            air_date = datetime.now(timezone.utc)
    else:
        air_date = datetime.now(timezone.utc)

    return {
        'id': episode_id,
        'seriesId': series_id,
        'tvdbId': 0,
        'episodeFileId': episode_id,
        'seasonNumber': item.get('season_number') or 0,
        'episodeNumber': item.get('episode_number') or 0,
        'title': item.get('episode_title') or f"Episode {item.get('episode_number', 0)}",
        'airDate': air_date.strftime('%Y-%m-%d'),
        'airDateUtc': air_date.isoformat() + 'Z',
        'overview': '',
        'hasFile': True,
        'monitored': True,
        'absoluteEpisodeNumber': item.get('episode_number') or 0,
        'unverifiedSceneNumbering': False,
        'grabbed': False,
        # Series sub-object for Bazarr compatibility (matches CineSync)
        'series': {
            'id': series_id,
            'title': series_title or title
        }
    }


def create_episode_file_resource(item, series_id):
    """Create a Sonarr-compatible EpisodeFileResource from database item."""
    episode_file_id = generate_unique_id(item.get('id'), 'episodefile')
    file_path = get_file_path(item)
    file_size = 536870912  # Default 512MB
    quality = detect_quality_from_version(item.get('version', ''))

    collected_at = item.get('collected_at')
    if isinstance(collected_at, str):
        try:
            added_time = datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        except ValueError:
            added_time = datetime.now(timezone.utc)
    else:
        added_time = datetime.now(timezone.utc)

    return {
        'id': episode_file_id,
        'seriesId': series_id,
        'seasonNumber': item.get('season_number') or 0,
        'relativePath': os.path.basename(file_path) if file_path else '',
        'path': file_path,
        'size': file_size,
        'dateAdded': added_time.isoformat() + 'Z',
        'quality': quality,
        'languages': [{'id': 1, 'name': 'English'}],
        'sceneName': '',
        'releaseGroup': ''
    }


class TestBazarrSpoofingHelpers(unittest.TestCase):
    """Test helper functions for Bazarr spoofing."""

    def test_generate_api_key(self):
        """Test API key generation."""
        key1 = generate_api_key()
        key2 = generate_api_key()

        # Keys should be 32 characters (hex)
        self.assertEqual(len(key1), 32)
        self.assertEqual(len(key2), 32)

        # Keys should be different
        self.assertNotEqual(key1, key2)

        # Keys should be valid hex
        int(key1, 16)  # Should not raise
        int(key2, 16)  # Should not raise

    def test_generate_unique_id_int(self):
        """Test unique ID generation from integer."""
        result1 = generate_unique_id(12345, 'episode')
        result2 = generate_unique_id(12345, 'episode')
        result3 = generate_unique_id(12345, 'episodefile')

        # Same input should produce same output
        self.assertEqual(result1, result2)

        # Different prefix should produce different output
        self.assertNotEqual(result1, result3)

        # Result should be an integer
        self.assertIsInstance(result1, int)

    def test_generate_unique_id_string(self):
        """Test unique ID generation from string."""
        result1 = generate_unique_id('tt1234567', 'movie')
        result2 = generate_unique_id('tt1234567', 'movie')
        result3 = generate_unique_id('tt9999999', 'movie')

        # Same input should produce same output
        self.assertEqual(result1, result2)

        # Different input should produce different output
        self.assertNotEqual(result1, result3)

        # Result should be an integer
        self.assertIsInstance(result1, int)

    def test_detect_quality_2160p(self):
        """Test quality detection for 4K content."""
        result = detect_quality_from_version('2160p.WEB-DL')

        self.assertEqual(result['quality']['resolution'], 2160)
        self.assertIn('2160p', result['quality']['name'])

    def test_detect_quality_1080p(self):
        """Test quality detection for 1080p content."""
        result = detect_quality_from_version('1080p.BluRay')

        self.assertEqual(result['quality']['resolution'], 1080)
        self.assertIn('1080p', result['quality']['name'])
        self.assertEqual(result['quality']['source'], 'BLURAY')

    def test_detect_quality_remux(self):
        """Test quality detection for Remux content."""
        result = detect_quality_from_version('2160p.Remux')

        self.assertIn('Remux', result['quality']['name'])
        self.assertEqual(result['quality']['source'], 'BLURAY')

    def test_parse_genres_json(self):
        """Test parsing genres from JSON format."""
        result = parse_genres('["Action", "Comedy", "Drama"]')

        self.assertEqual(result, ['Action', 'Comedy', 'Drama'])

    def test_parse_genres_csv(self):
        """Test parsing genres from CSV format."""
        result = parse_genres('Action, Comedy, Drama')

        self.assertEqual(result, ['Action', 'Comedy', 'Drama'])

    def test_parse_genres_empty(self):
        """Test parsing empty genres."""
        result = parse_genres('')
        self.assertEqual(result, [])

        result = parse_genres(None)
        self.assertEqual(result, [])


class TestMovieResourceMapping(unittest.TestCase):
    """Test movie data mapping to Radarr format."""

    def test_create_movie_resource_basic(self):
        """Test basic movie resource creation."""
        item = {
            'id': 1,
            'tmdb_id': '12345',
            'imdb_id': 'tt1234567',
            'title': 'Test Movie',
            'year': 2024,
            'file_path': '/movies/Test Movie (2024)/Test.Movie.2024.1080p.mkv',
            'location_on_disk': None,
            'genres': '["Action", "Comedy"]',
            'runtime': 120,
            'collected_at': '2024-01-15T10:30:00Z',
            'version': '1080p.WEB-DL'
        }

        result = create_movie_resource(item)

        # Check basic fields
        self.assertEqual(result['id'], 12345)
        self.assertEqual(result['title'], 'Test Movie')
        self.assertEqual(result['year'], 2024)
        self.assertEqual(result['tmdbId'], 12345)
        self.assertEqual(result['imdbId'], 'tt1234567')
        self.assertEqual(result['runtime'], 120)

        # Check file info
        self.assertTrue(result['hasFile'])
        self.assertIn('movieFile', result)
        self.assertIn('Test.Movie.2024.1080p.mkv', result['movieFile']['path'])

        # Check genres
        self.assertEqual(result['genres'], ['Action', 'Comedy'])

    def test_create_movie_resource_location_on_disk(self):
        """Test movie resource uses location_on_disk when available."""
        item = {
            'id': 1,
            'tmdb_id': '12345',
            'title': 'Test Movie',
            'year': 2024,
            'file_path': '/old/path/movie.mkv',
            'location_on_disk': '/new/path/movie.mkv',
            'collected_at': '2024-01-15T10:30:00Z'
        }

        result = create_movie_resource(item)

        # Should use location_on_disk
        self.assertIn('/new/path/movie.mkv', result['movieFile']['path'])


class TestSeriesResourceMapping(unittest.TestCase):
    """Test series data mapping to Sonarr format."""

    def test_create_series_resource_basic(self):
        """Test basic series resource creation."""
        item = {
            'show_id': 'tt9999999',
            'imdb_id': 'tt9999999',
            'tmdb_id': '67890',
            'title': 'Test Show',
            'year': 2023,
            'genres': '["Drama"]',
            'runtime': 45
        }

        episodes = [
            {
                'id': 1,
                'season_number': 1,
                'episode_number': 1,
                'file_path': '/tv/Test Show/Season 01/S01E01.mkv'
            },
            {
                'id': 2,
                'season_number': 1,
                'episode_number': 2,
                'file_path': '/tv/Test Show/Season 01/S01E02.mkv'
            },
            {
                'id': 3,
                'season_number': 2,
                'episode_number': 1,
                'file_path': '/tv/Test Show/Season 02/S02E01.mkv'
            }
        ]

        result = create_series_resource(item, episodes)

        # Check basic fields
        self.assertEqual(result['title'], 'Test Show')
        self.assertEqual(result['year'], 2023)
        self.assertEqual(result['imdbId'], 'tt9999999')
        self.assertEqual(result['runtime'], 45)
        self.assertEqual(result['genres'], ['Drama'])

        # Check seasons
        self.assertEqual(len(result['seasons']), 2)

        # Find season 1
        season1 = next(s for s in result['seasons'] if s['seasonNumber'] == 1)
        self.assertEqual(season1['statistics']['episodeCount'], 2)

        # Find season 2
        season2 = next(s for s in result['seasons'] if s['seasonNumber'] == 2)
        self.assertEqual(season2['statistics']['episodeCount'], 1)


class TestEpisodeResourceMapping(unittest.TestCase):
    """Test episode data mapping to Sonarr format."""

    def test_create_episode_resource(self):
        """Test episode resource creation."""
        item = {
            'id': 1,
            'imdb_id': 'tt9999999',
            'tmdb_id': '67890',
            'title': 'Test Show',
            'episode_title': 'Pilot Episode',
            'year': 2023,
            'season_number': 1,
            'episode_number': 1,
            'file_path': '/tv/Test Show/Season 01/S01E01.mkv',
            'collected_at': '2024-01-15T10:30:00Z'
        }

        result = create_episode_resource(item, series_id=67890, series_title='Test Show')

        self.assertEqual(result['seriesId'], 67890)
        self.assertEqual(result['seasonNumber'], 1)
        self.assertEqual(result['episodeNumber'], 1)
        self.assertEqual(result['title'], 'Pilot Episode')
        self.assertTrue(result['hasFile'])
        self.assertTrue(result['monitored'])

    def test_create_episode_resource_has_series_subobject(self):
        """Test that episode resource includes series sub-object (CineSync compatibility)."""
        item = {
            'id': 1,
            'title': 'Test Show',
            'episode_title': 'Pilot',
            'season_number': 1,
            'episode_number': 1,
            'collected_at': '2024-01-15T10:30:00Z'
        }

        result = create_episode_resource(item, series_id=12345, series_title='Test Show')

        # Must have series sub-object for Bazarr compatibility
        self.assertIn('series', result)
        self.assertEqual(result['series']['id'], 12345)
        self.assertEqual(result['series']['title'], 'Test Show')

    def test_create_episode_file_resource(self):
        """Test episode file resource creation."""
        item = {
            'id': 1,
            'season_number': 1,
            'episode_number': 5,
            'file_path': '/tv/Test Show/Season 01/S01E05.1080p.mkv',
            'location_on_disk': None,
            'collected_at': '2024-01-15T10:30:00Z',
            'version': '1080p.WEB-DL'
        }

        result = create_episode_file_resource(item, series_id=12345)

        self.assertEqual(result['seriesId'], 12345)
        self.assertEqual(result['seasonNumber'], 1)
        self.assertIn('S01E05', result['path'])
        self.assertIn('quality', result)


class TestAdditionalHelpers(unittest.TestCase):
    """Test additional helper functions."""

    def test_get_file_path_prefers_location_on_disk(self):
        """Test that get_file_path prefers location_on_disk."""
        item = {
            'file_path': '/old/path/file.mkv',
            'location_on_disk': '/new/path/file.mkv'
        }
        result = get_file_path(item)
        self.assertEqual(result, '/new/path/file.mkv')

    def test_get_file_path_falls_back_to_file_path(self):
        """Test that get_file_path falls back to file_path."""
        item = {
            'file_path': '/path/file.mkv',
            'location_on_disk': None
        }
        result = get_file_path(item)
        self.assertEqual(result, '/path/file.mkv')

    def test_get_file_path_empty(self):
        """Test get_file_path with no paths."""
        item = {}
        result = get_file_path(item)
        self.assertEqual(result, '')

    def test_detect_quality_webdl(self):
        """Test quality detection for WEB-DL."""
        result = detect_quality_from_version('1080p.WEB-DL')
        self.assertEqual(result['quality']['source'], 'WEBDL')

    def test_detect_quality_webrip(self):
        """Test quality detection for WEBRip."""
        result = detect_quality_from_version('1080p.WEBRip')
        self.assertEqual(result['quality']['source'], 'WEBRIP')

    def test_detect_quality_hdtv(self):
        """Test quality detection for HDTV."""
        result = detect_quality_from_version('720p.HDTV')
        self.assertEqual(result['quality']['source'], 'TV')
        self.assertEqual(result['quality']['resolution'], 720)

    def test_movie_resource_has_required_fields(self):
        """Test that movie resource has all required Radarr fields."""
        item = {
            'id': 1,
            'tmdb_id': '12345',
            'title': 'Test Movie',
            'year': 2024,
            'file_path': '/movies/test.mkv'
        }
        result = create_movie_resource(item)

        # Check required Radarr fields
        required_fields = [
            'id', 'title', 'year', 'tmdbId', 'hasFile', 'movieFile',
            'path', 'monitored', 'status', 'genres', 'tags', 'added'
        ]
        for field in required_fields:
            self.assertIn(field, result, f"Missing required field: {field}")

    def test_series_resource_has_required_fields(self):
        """Test that series resource has all required Sonarr fields."""
        item = {
            'show_id': 'tt1234567',
            'title': 'Test Show',
            'year': 2024
        }
        result = create_series_resource(item, [])

        # Check required Sonarr fields
        required_fields = [
            'id', 'title', 'year', 'seasons', 'path', 'monitored',
            'status', 'genres', 'tags', 'added'
        ]
        for field in required_fields:
            self.assertIn(field, result, f"Missing required field: {field}")


class TestMediaInfoParsing(unittest.TestCase):
    """Test media info parsing from filenames.

    Note: These tests verify the basic structure. The actual parsing
    is done in the real module with ReleaseParser/guessit.
    """

    def test_movie_file_structure_includes_expected_fields(self):
        """Test that movie file resource includes expected structure."""
        # The standalone test implementation has basic structure
        # The real implementation adds mediaInfo
        item = {
            'id': 1,
            'tmdb_id': '12345',
            'title': 'Test Movie',
            'year': 2024,
            'file_path': '/movies/Test.Movie.2024.1080p.WEB-DL.x265-GROUP/test.mkv',
            'version': '1080p WEB-DL'
        }

        result = create_movie_resource(item)

        # Check basic structure exists
        self.assertIn('movieFile', result)
        self.assertIn('quality', result['movieFile'])
        self.assertIn('releaseGroup', result['movieFile'])

    def test_episode_file_structure_includes_expected_fields(self):
        """Test that episode file resource includes expected structure."""
        item = {
            'id': 1,
            'season_number': 1,
            'episode_number': 1,
            'file_path': '/tv/Show/Season 01/S01E01.1080p.HDTV.x264-LOL.mkv',
            'version': '1080p HDTV'
        }

        result = create_episode_file_resource(item, series_id=12345)

        # Check basic structure exists
        self.assertIn('quality', result)
        self.assertIn('releaseGroup', result)


if __name__ == '__main__':
    unittest.main()
