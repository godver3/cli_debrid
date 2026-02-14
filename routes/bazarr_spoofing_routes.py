"""
Bazarr Spoofing Routes - Radarr/Sonarr API emulation for Bazarr subtitle integration.

This module implements the Radarr/Sonarr v3 API endpoints that Bazarr uses to discover
movies and TV shows. When enabled, Bazarr can connect to cli_debrid as if it were
Radarr or Sonarr, allowing automatic subtitle downloads for collected media.
"""

from flask import Blueprint, jsonify, request, Response
from functools import wraps
import logging
import os
import hashlib
import secrets
import time
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from utilities.settings import get_setting, set_setting
from database.core import get_db_connection

# Create blueprint - mounted at root to handle /api/v3/* and /signalr/* paths
bazarr_bp = Blueprint('bazarr', __name__)

# Process start time for system status
PROCESS_START_TIME = datetime.now(timezone.utc)

# ============================================================================
# Authentication
# ============================================================================

def is_symlinked_mode() -> bool:
    """Check if file collection management is set to Symlinked/Local mode."""
    return get_setting('File Management', 'file_collection_management', 'Plex') == 'Symlinked/Local'


def require_bazarr_auth(f):
    """Decorator to require API key authentication for Bazarr endpoints."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Bazarr integration only works in Symlinked/Local mode
        if not is_symlinked_mode():
            return jsonify({'error': 'Bazarr integration requires Symlinked/Local file management mode'}), 404

        # Check if spoofing is enabled
        if not get_setting('Bazarr Integration', 'enabled', False):
            return jsonify({'error': 'Bazarr integration is not enabled'}), 404

        # Get API key from header or query param
        api_key = request.headers.get('X-Api-Key') or request.args.get('apikey')

        if not api_key:
            logging.warning(f"Bazarr auth failed: missing API key for {request.path}")
            return jsonify({'error': 'Unauthorized'}), 401

        configured_key = get_setting('Bazarr Integration', 'api_key', '')
        if not configured_key or api_key != configured_key:
            logging.warning(f"Bazarr auth failed: invalid API key for {request.path}")
            return jsonify({'error': 'Unauthorized'}), 401

        return f(*args, **kwargs)
    return decorated_function


def generate_api_key():
    """Generate a random 32-character API key."""
    return secrets.token_hex(16)


# ============================================================================
# Helper Functions - Database Queries
# ============================================================================

def get_collected_movies() -> List[Dict[str, Any]]:
    """Get all collected movies from the database."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, year, file_path,
                   location_on_disk, genres, runtime, collected_at, version
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'movie'
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
            ORDER BY title
        """)

        movies = []
        for row in cursor.fetchall():
            movies.append({
                'id': row[0],
                'imdb_id': row[1],
                'tmdb_id': row[2],
                'title': row[3],
                'year': row[4],
                'file_path': row[5],
                'location_on_disk': row[6],
                'genres': row[7],
                'runtime': row[8],
                'collected_at': row[9],
                'version': row[10]
            })
        return movies
    finally:
        conn.close()


def get_collected_series() -> List[Dict[str, Any]]:
    """Get all collected TV series from the database (grouped by show)."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Get unique shows with their metadata
        cursor.execute("""
            SELECT DISTINCT
                COALESCE(imdb_id, tmdb_id) as show_id,
                imdb_id, tmdb_id, title, year, genres, runtime
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'episode'
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
            GROUP BY COALESCE(imdb_id, tmdb_id), title
            ORDER BY title
        """)

        series_list = []
        for row in cursor.fetchall():
            series_list.append({
                'show_id': row[0],
                'imdb_id': row[1],
                'tmdb_id': row[2],
                'title': row[3],
                'year': row[4],
                'genres': row[5],
                'runtime': row[6]
            })
        return series_list
    finally:
        conn.close()


def get_episodes_for_series(show_id: str) -> List[Dict[str, Any]]:
    """Get all collected episodes for a specific series."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, episode_title, year,
                   season_number, episode_number, file_path,
                   location_on_disk, collected_at, version
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'episode'
            AND (imdb_id = ? OR tmdb_id = ?)
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
            ORDER BY season_number, episode_number
        """, (show_id, show_id))

        episodes = []
        for row in cursor.fetchall():
            episodes.append({
                'id': row[0],
                'imdb_id': row[1],
                'tmdb_id': row[2],
                'title': row[3],
                'episode_title': row[4],
                'year': row[5],
                'season_number': row[6],
                'episode_number': row[7],
                'file_path': row[8],
                'location_on_disk': row[9],
                'collected_at': row[10],
                'version': row[11]
            })
        return episodes
    finally:
        conn.close()


def get_movie_by_id(movie_id: int) -> Optional[Dict[str, Any]]:
    """Get a specific movie by its ID."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, year, file_path,
                   location_on_disk, genres, runtime, collected_at, version
            FROM media_items
            WHERE (id = ? OR tmdb_id = ? OR tmdb_id = ?)
            AND state = 'Collected'
            AND type = 'movie'
            LIMIT 1
        """, (movie_id, str(movie_id), movie_id))

        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'imdb_id': row[1],
                'tmdb_id': row[2],
                'title': row[3],
                'year': row[4],
                'file_path': row[5],
                'location_on_disk': row[6],
                'genres': row[7],
                'runtime': row[8],
                'collected_at': row[9],
                'version': row[10]
            }
        return None
    finally:
        conn.close()


def get_series_by_id(series_id: int) -> Optional[Dict[str, Any]]:
    """Get a specific series by its ID."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                COALESCE(imdb_id, tmdb_id) as show_id,
                imdb_id, tmdb_id, title, year, genres, runtime
            FROM media_items
            WHERE (id = ? OR tmdb_id = ? OR tmdb_id = ? OR imdb_id = ?)
            AND state = 'Collected'
            AND type = 'episode'
            LIMIT 1
        """, (series_id, str(series_id), series_id, str(series_id)))

        row = cursor.fetchone()
        if row:
            return {
                'show_id': row[0],
                'imdb_id': row[1],
                'tmdb_id': row[2],
                'title': row[3],
                'year': row[4],
                'genres': row[5],
                'runtime': row[6]
            }
        return None
    finally:
        conn.close()


# ============================================================================
# Helper Functions - Resource Mapping
# ============================================================================

def detect_quality_from_version(version: str) -> Dict[str, Any]:
    """Detect quality information from the version string."""
    version_lower = (version or '').lower()

    # Resolution detection
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

    # Source detection
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


def generate_unique_id(base_id: Any, prefix: str = '') -> int:
    """Generate a unique integer ID from various input types.

    Always incorporates the prefix to ensure different entity types
    (episode, episodefile, series, movie) get different IDs even with
    the same base_id.
    """
    # Always use hash to ensure prefix is incorporated
    hash_input = f"{prefix}{base_id}".encode()
    return int(hashlib.md5(hash_input).hexdigest()[:8], 16)


def generate_tvdb_id(tmdb_id: Any, title: str, episode_info: Optional[Dict[str, Any]] = None) -> int:
    """Generate a fake but consistent tvdbId for Bazarr compatibility.

    Args:
        tmdb_id: The TMDB ID of the series/episode
        title: The title of the series
        episode_info: Optional dict with 'season' and 'episode' keys for episode-specific IDs

    Returns:
        A consistent integer ID derived from the input parameters
    """
    base = f"tvdb{tmdb_id}{title}"
    if episode_info:
        base += f"S{episode_info.get('season', 0)}E{episode_info.get('episode', 0)}"
    return int(hashlib.md5(base.encode()).hexdigest()[:8], 16)


def get_file_path(item: Dict[str, Any]) -> str:
    """Get the best available file path for an item."""
    return item.get('location_on_disk') or item.get('file_path') or ''


def get_file_size(file_path: str) -> int:
    """Get file size in bytes, returns 0 if file doesn't exist."""
    try:
        if file_path and os.path.exists(file_path):
            return os.path.getsize(file_path)
    except Exception:
        pass
    # Return a reasonable default size (1GB for movies, 500MB for episodes)
    return 1073741824  # 1GB default


def parse_genres(genres_str: str) -> List[str]:
    """Parse genres from database string format."""
    if not genres_str:
        return []
    try:
        return json.loads(genres_str)
    except (json.JSONDecodeError, TypeError):
        return [g.strip() for g in genres_str.split(',') if g.strip()]


def create_movie_resource(item: Dict[str, Any]) -> Dict[str, Any]:
    """Create a Radarr-compatible MovieResource from database item."""
    tmdb_id = int(item.get('tmdb_id') or 0) if item.get('tmdb_id') else 0
    movie_id = tmdb_id or generate_unique_id(item.get('id'), 'movie')
    file_path = get_file_path(item)
    file_size = get_file_size(file_path)

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

    # Build movie file object
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


def create_series_resource(item: Dict[str, Any], episodes: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a Sonarr-compatible SeriesResource from database item."""
    tmdb_id = int(item.get('tmdb_id') or 0) if item.get('tmdb_id') else 0
    series_id = tmdb_id or generate_unique_id(item.get('show_id'), 'series')
    title = item.get('title', 'Unknown')

    # Generate consistent tvdbId for Bazarr compatibility
    tvdb_id = generate_tvdb_id(tmdb_id or item.get('show_id'), title)

    # Build seasons from episodes with complete statistics
    seasons = []
    total_size_on_disk = 0
    if episodes:
        season_numbers = set(ep.get('season_number', 0) for ep in episodes)
        for season_num in sorted(season_numbers):
            season_episodes = [ep for ep in episodes if ep.get('season_number') == season_num]
            # Calculate size for this season
            season_size = sum(get_file_size(get_file_path(ep)) for ep in season_episodes)
            total_size_on_disk += season_size
            seasons.append({
                'seasonNumber': season_num,
                'monitored': True,
                'statistics': {
                    'episodeFileCount': len(season_episodes),
                    'episodeCount': len(season_episodes),
                    'totalEpisodeCount': len(season_episodes),
                    'percentOfEpisodes': 100.0,
                    'sizeOnDisk': season_size,
                    'previousAiring': None,
                    'nextAiring': None
                }
            })

    # Get series path from episodes - handle various folder structures
    series_path = ''
    root_folder_path = '/tv'
    if episodes:
        # Collect all episode paths to find common series folder
        episode_paths = [get_file_path(ep) for ep in episodes if get_file_path(ep)]
        if episode_paths:
            first_path = episode_paths[0]
            # Check if there's a season folder structure
            parent_dir = os.path.dirname(first_path)
            parent_name = os.path.basename(parent_dir).lower()

            # Check if parent is a season folder (e.g., "Season 1", "S01", "Specials")
            is_season_folder = (
                parent_name.startswith('season') or
                parent_name.startswith('s0') or
                parent_name.startswith('s1') or
                parent_name == 'specials'
            )

            if is_season_folder:
                # Go up two levels: file -> season folder -> series folder
                series_path = os.path.dirname(parent_dir)
            else:
                # Episodes are directly in series folder
                series_path = parent_dir

            # Root folder is one level above series folder
            root_folder_path = os.path.dirname(series_path) if series_path else '/tv'

    return {
        'id': series_id,
        'title': title,
        'alternateTitles': [],
        'sortTitle': title,
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
        'tvdbId': tvdb_id,
        'tmdbId': tmdb_id,
        'tvRageId': 0,
        'tvMazeId': 0,
        'firstAired': '',
        'lastAired': None,
        'nextAiring': None,
        'previousAiring': None,
        'lastInfoSync': datetime.now(timezone.utc).isoformat() + 'Z',
        'seriesType': 'standard',
        'cleanTitle': title.lower().replace(' ', ''),
        'imdbId': item.get('imdb_id') or '',
        'titleSlug': title.lower().replace(' ', '-'),
        'rootFolderPath': root_folder_path,
        'genres': parse_genres(item.get('genres', '')),
        'tags': [],
        'added': datetime.now(timezone.utc).isoformat() + 'Z',
        'statistics': {
            'seasonCount': len(seasons),
            'episodeFileCount': len(episodes) if episodes else 0,
            'episodeCount': len(episodes) if episodes else 0,
            'totalEpisodeCount': len(episodes) if episodes else 0,
            'sizeOnDisk': total_size_on_disk,
            'percentOfEpisodes': 100.0
        }
    }


def create_episode_resource(item: Dict[str, Any], series_id: int, series_title: str = None) -> Dict[str, Any]:
    """Create a Sonarr-compatible EpisodeResource from database item.

    Args:
        item: The episode data from database
        series_id: The series ID this episode belongs to
        series_title: The series title (optional, uses item title if not provided)
    """
    episode_id = generate_unique_id(item.get('id'), 'episode')
    # Episode file ID should be separate from episode ID
    episode_file_id = generate_unique_id(item.get('id'), 'episodefile')

    season_num = item.get('season_number') or 0
    episode_num = item.get('episode_number') or 0
    title = item.get('title', 'Unknown')
    tmdb_id = item.get('tmdb_id') or item.get('imdb_id')

    # Generate consistent tvdbId for this episode
    tvdb_id = generate_tvdb_id(tmdb_id, title, {'season': season_num, 'episode': episode_num})

    collected_at = item.get('collected_at')
    if isinstance(collected_at, str):
        try:
            air_date = datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        except ValueError:
            air_date = datetime.now(timezone.utc)
    else:
        air_date = datetime.now(timezone.utc)

    # Get quality and language info for root level
    quality = detect_quality_from_version(item.get('version', ''))
    languages = [{'id': 1, 'name': 'English'}]

    # Build episode file object for inclusion
    file_path = get_file_path(item)
    file_size = get_file_size(file_path)
    episode_file = {
        'id': episode_file_id,
        'seriesId': series_id,
        'seasonNumber': season_num,
        'relativePath': os.path.basename(file_path) if file_path else '',
        'path': file_path,
        'size': file_size,
        'dateAdded': air_date.isoformat() + 'Z',
        'quality': quality,
        'languages': languages,
        'sceneName': '',
        'releaseGroup': ''
    }

    return {
        'id': episode_id,
        'seriesId': series_id,
        'tvdbId': tvdb_id,
        'episodeFileId': episode_file_id,
        'seasonNumber': season_num,
        'episodeNumber': episode_num,
        'title': item.get('episode_title') or f"Episode {episode_num}",
        'airDate': air_date.strftime('%Y-%m-%d'),
        'airDateUtc': air_date.isoformat() + 'Z',
        'overview': '',
        'hasFile': True,
        'monitored': True,
        'absoluteEpisodeNumber': episode_num,
        'unverifiedSceneNumbering': False,
        'grabbed': False,
        # Add language and quality at root level for Bazarr
        'languages': languages,
        'quality': quality,
        # Series sub-object for Bazarr compatibility (matches CineSync)
        'series': {
            'id': series_id,
            'title': series_title or title
        },
        # Include episode file data
        'episodeFile': episode_file
    }


def create_episode_file_resource(item: Dict[str, Any], series_id: int) -> Dict[str, Any]:
    """Create a Sonarr-compatible EpisodeFileResource from database item."""
    episode_file_id = generate_unique_id(item.get('id'), 'episodefile')
    file_path = get_file_path(item)
    file_size = get_file_size(file_path)
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


# ============================================================================
# System Endpoints
# ============================================================================

@bazarr_bp.route('/api/v3/system/status', methods=['GET'])
@bazarr_bp.route('/api/v3/system/status/', methods=['GET'])
@require_bazarr_auth
def system_status():
    """Return system status in Radarr/Sonarr format."""
    service_type = get_setting('Bazarr Integration', 'service_type', 'auto')
    version = get_setting('Bazarr Integration', 'spoofed_version', '5.14.0.9383')

    # Determine app name based on service type
    if service_type == 'sonarr':
        app_name = 'Sonarr'
    else:
        app_name = 'Radarr'

    import platform

    return jsonify({
        'appName': app_name,
        'version': version,
        'buildTime': PROCESS_START_TIME.isoformat() + 'Z',
        'appGuid': secrets.token_hex(16),
        'instanceName': 'cli_debrid',
        'isDebug': False,
        'isProduction': True,
        'isAdmin': True,
        'isUserInteractive': False,
        'startupPath': '/app',
        'appData': '/config',
        'osName': platform.system(),
        'osVersion': platform.release(),
        'isMonoRuntime': False,
        'isMono': False,
        'isLinux': platform.system() == 'Linux',
        'isOsx': platform.system() == 'Darwin',
        'isWindows': platform.system() == 'Windows',
        'mode': 'production',
        'branch': 'master',
        'authentication': 'external',
        'sqliteVersion': '3.40.1',
        'migrationVersion': 209,
        'urlBase': '',
        'runtimeVersion': '6.0.16',
        'runtimeName': '.NET 6.0',
        'startTime': PROCESS_START_TIME.isoformat() + 'Z',
        'packageVersion': version,
        'packageAuthor': 'cli_debrid',
        'packageUpdateMechanism': 'docker'
    })


@bazarr_bp.route('/api/v3/health', methods=['GET'])
@bazarr_bp.route('/api/v3/health/', methods=['GET'])
@require_bazarr_auth
def health():
    """Return health status."""
    return jsonify([])


@bazarr_bp.route('/api/v3/rootfolder', methods=['GET'])
@bazarr_bp.route('/api/v3/rootfolder/', methods=['GET'])
@require_bazarr_auth
def root_folder():
    """Return root folders."""
    # Get paths from settings
    symlink_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')

    folders = [
        {'id': 1, 'path': os.path.join(symlink_path, 'Movies')},
        {'id': 2, 'path': os.path.join(symlink_path, 'TV Shows')}
    ]

    return jsonify(folders)


@bazarr_bp.route('/api/v3/qualityprofile', methods=['GET'])
@bazarr_bp.route('/api/v3/qualityprofile/', methods=['GET'])
@require_bazarr_auth
def quality_profile():
    """Return quality profiles."""
    return jsonify([
        {'id': 1, 'name': 'Any'},
        {'id': 2, 'name': 'HD-1080p'},
        {'id': 3, 'name': 'Ultra-HD'}
    ])


@bazarr_bp.route('/api/v3/language', methods=['GET'])
@bazarr_bp.route('/api/v3/language/', methods=['GET'])
@require_bazarr_auth
def language():
    """Return available languages."""
    return jsonify([
        {'id': 1, 'name': 'English'},
        {'id': 2, 'name': 'French'},
        {'id': 3, 'name': 'Spanish'},
        {'id': 4, 'name': 'German'},
        {'id': 5, 'name': 'Italian'},
        {'id': 6, 'name': 'Portuguese'},
        {'id': 7, 'name': 'Dutch'},
        {'id': 8, 'name': 'Japanese'},
        {'id': 9, 'name': 'Korean'},
        {'id': 10, 'name': 'Chinese'}
    ])


@bazarr_bp.route('/api/v3/languageprofile', methods=['GET'])
@bazarr_bp.route('/api/v3/languageprofile/', methods=['GET'])
@require_bazarr_auth
def language_profile():
    """Return language profiles (Sonarr)."""
    return jsonify([
        {'id': 1, 'name': 'English'},
        {'id': 2, 'name': 'Any'}
    ])


@bazarr_bp.route('/api/v3/tag', methods=['GET'])
@bazarr_bp.route('/api/v3/tag/', methods=['GET'])
@require_bazarr_auth
def tags():
    """Return tags."""
    return jsonify([])


# ============================================================================
# Radarr Movie Endpoints
# ============================================================================

@bazarr_bp.route('/api/v3/movie', methods=['GET'])
@bazarr_bp.route('/api/v3/movie/', methods=['GET'])
@require_bazarr_auth
def get_movies():
    """Return all collected movies in Radarr format."""
    service_type = get_setting('Bazarr Integration', 'service_type', 'auto')
    if service_type == 'sonarr':
        return jsonify([])

    try:
        movies = get_collected_movies()
        return jsonify([create_movie_resource(m) for m in movies])
    except Exception as e:
        logging.error(f"Error getting movies for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


@bazarr_bp.route('/api/v3/movie/<int:movie_id>', methods=['GET'])
@require_bazarr_auth
def get_movie(movie_id: int):
    """Return a specific movie by ID."""
    try:
        movie = get_movie_by_id(movie_id)
        if movie:
            return jsonify(create_movie_resource(movie))
        return jsonify({'error': 'Movie not found'}), 404
    except Exception as e:
        logging.error(f"Error getting movie {movie_id} for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


@bazarr_bp.route('/api/v3/moviefile', methods=['GET'])
@bazarr_bp.route('/api/v3/moviefile/', methods=['GET'])
@require_bazarr_auth
def get_movie_files():
    """Return all movie files."""
    movie_id = request.args.get('movieId', type=int)

    try:
        if movie_id:
            movie = get_movie_by_id(movie_id)
            if movie:
                resource = create_movie_resource(movie)
                return jsonify([resource.get('movieFile')])
            return jsonify([])

        # Return all movie files
        movies = get_collected_movies()
        movie_files = []
        for m in movies:
            resource = create_movie_resource(m)
            if resource.get('movieFile'):
                movie_files.append(resource['movieFile'])
        return jsonify(movie_files)
    except Exception as e:
        logging.error(f"Error getting movie files for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


@bazarr_bp.route('/api/v3/moviefile/<int:file_id>', methods=['GET'])
@require_bazarr_auth
def get_movie_file(file_id: int):
    """Return a specific movie file by ID."""
    try:
        movies = get_collected_movies()
        for m in movies:
            resource = create_movie_resource(m)
            if resource.get('movieFile', {}).get('id') == file_id:
                return jsonify(resource['movieFile'])
        return jsonify({'error': 'Movie file not found'}), 404
    except Exception as e:
        logging.error(f"Error getting movie file {file_id} for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Sonarr Series/Episode Endpoints
# ============================================================================

@bazarr_bp.route('/api/v3/series', methods=['GET'])
@bazarr_bp.route('/api/v3/series/', methods=['GET'])
@require_bazarr_auth
def get_series():
    """Return all collected series in Sonarr format."""
    service_type = get_setting('Bazarr Integration', 'service_type', 'auto')
    if service_type == 'radarr':
        return jsonify([])

    try:
        series_list = get_collected_series()
        result = []
        for s in series_list:
            episodes = get_episodes_for_series(s.get('show_id', ''))
            result.append(create_series_resource(s, episodes))
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error getting series for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


@bazarr_bp.route('/api/v3/series/<int:series_id>', methods=['GET'])
@require_bazarr_auth
def get_series_by_id_route(series_id: int):
    """Return a specific series by ID."""
    try:
        series = get_series_by_id(series_id)
        if series:
            episodes = get_episodes_for_series(series.get('show_id', ''))
            return jsonify(create_series_resource(series, episodes))
        return jsonify({'error': 'Series not found'}), 404
    except Exception as e:
        logging.error(f"Error getting series {series_id} for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


@bazarr_bp.route('/api/v3/episode', methods=['GET'])
@bazarr_bp.route('/api/v3/episode/', methods=['GET'])
@require_bazarr_auth
def get_episodes():
    """Return episodes for a series."""
    series_id = request.args.get('seriesId', type=int)

    if not series_id:
        return jsonify([])

    try:
        series = get_series_by_id(series_id)
        if not series:
            return jsonify([])

        series_title = series.get('title', 'Unknown')
        episodes = get_episodes_for_series(series.get('show_id', ''))
        return jsonify([create_episode_resource(ep, series_id, series_title) for ep in episodes])
    except Exception as e:
        logging.error(f"Error getting episodes for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


@bazarr_bp.route('/api/v3/episodefile', methods=['GET'])
@bazarr_bp.route('/api/v3/episodefile/', methods=['GET'])
@bazarr_bp.route('/api/v3/episodeFile', methods=['GET'])
@bazarr_bp.route('/api/v3/episodeFile/', methods=['GET'])
@require_bazarr_auth
def get_episode_files():
    """Return episode files for a series."""
    series_id = request.args.get('seriesId', type=int)

    if not series_id:
        return jsonify([])

    try:
        series = get_series_by_id(series_id)
        if not series:
            return jsonify([])

        episodes = get_episodes_for_series(series.get('show_id', ''))
        return jsonify([create_episode_file_resource(ep, series_id) for ep in episodes])
    except Exception as e:
        logging.error(f"Error getting episode files for Bazarr: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Additional Required Endpoints
# ============================================================================

@bazarr_bp.route('/api/v3/notification', methods=['GET'])
@bazarr_bp.route('/api/v3/notification/', methods=['GET'])
@require_bazarr_auth
def notifications():
    """Return notification settings."""
    return jsonify([])


@bazarr_bp.route('/api/v3/downloadclient', methods=['GET'])
@bazarr_bp.route('/api/v3/downloadclient/', methods=['GET'])
@require_bazarr_auth
def download_clients():
    """Return download client settings."""
    return jsonify([])


@bazarr_bp.route('/api/v3/indexer', methods=['GET'])
@bazarr_bp.route('/api/v3/indexer/', methods=['GET'])
@require_bazarr_auth
def indexers():
    """Return indexer settings."""
    return jsonify([])


@bazarr_bp.route('/api/v3/importlist', methods=['GET'])
@bazarr_bp.route('/api/v3/importlist/', methods=['GET'])
@require_bazarr_auth
def import_lists():
    """Return import list settings."""
    return jsonify([])


@bazarr_bp.route('/api/v3/queue', methods=['GET'])
@bazarr_bp.route('/api/v3/queue/', methods=['GET'])
@require_bazarr_auth
def queue():
    """Return download queue."""
    return jsonify({
        'page': 1,
        'pageSize': 20,
        'sortKey': 'timeleft',
        'sortDirection': 'ascending',
        'totalRecords': 0,
        'records': []
    })


@bazarr_bp.route('/api/v3/command', methods=['GET', 'POST'])
@bazarr_bp.route('/api/v3/command/', methods=['GET', 'POST'])
@require_bazarr_auth
def command():
    """Handle command requests."""
    if request.method == 'POST':
        return jsonify({
            'id': 1,
            'name': 'Command',
            'status': 'completed',
            'queued': datetime.now(timezone.utc).isoformat() + 'Z',
            'ended': datetime.now(timezone.utc).isoformat() + 'Z'
        })
    return jsonify([])


@bazarr_bp.route('/api/v3/config/host', methods=['GET'])
@bazarr_bp.route('/api/v3/config/host/', methods=['GET'])
@require_bazarr_auth
def config_host():
    """Return host configuration."""
    api_key = get_setting('Bazarr Integration', 'api_key', '')

    return jsonify({
        'bindAddress': '*',
        'port': 5000,
        'sslPort': 443,
        'enableSsl': False,
        'launchBrowser': False,
        'authenticationMethod': 'none',
        'analyticsEnabled': False,
        'username': '',
        'password': '',
        'logLevel': 'info',
        'consoleLogLevel': 'info',
        'branch': 'master',
        'apiKey': api_key,
        'sslCertPath': '',
        'sslCertPassword': '',
        'urlBase': '',
        'updateAutomatically': False,
        'updateMechanism': 'docker',
        'updateScriptPath': '',
        'proxyEnabled': False,
        'proxyType': 'http',
        'proxyHostname': '',
        'proxyPort': 8080,
        'proxyUsername': '',
        'proxyPassword': '',
        'proxyBypassFilter': '',
        'proxyBypassLocalAddresses': True
    })


@bazarr_bp.route('/api', methods=['GET'])
@require_bazarr_auth
def api_info():
    """Return API version info."""
    version = get_setting('Bazarr Integration', 'spoofed_version', '5.14.0.9383')
    return jsonify({
        'current': version,
        'version': version
    })


# ============================================================================
# SignalR Endpoints
# ============================================================================

def check_signalr_auth() -> Optional[tuple]:
    """
    Check SignalR authentication - lighter check that returns error tuple or None.

    Returns:
        None if authentication passes, or (response, status_code) tuple on failure
    """
    # Bazarr integration only works in Symlinked/Local mode
    if not is_symlinked_mode():
        return jsonify({'error': 'Not found'}), 404

    if not get_setting('Bazarr Integration', 'enabled', False):
        return jsonify({'error': 'Not found'}), 404

    # Get API key from header or query param
    api_key = request.headers.get('X-Api-Key') or request.args.get('apikey')

    if not api_key:
        logging.warning(f"SignalR auth failed: missing API key for {request.path}")
        return jsonify({'error': 'Unauthorized'}), 401

    configured_key = get_setting('Bazarr Integration', 'api_key', '')
    if not configured_key or api_key != configured_key:
        logging.warning(f"SignalR auth failed: invalid API key for {request.path}")
        return jsonify({'error': 'Unauthorized'}), 401

    return None


@bazarr_bp.route('/signalr/messages/negotiate', methods=['GET', 'POST'])
@bazarr_bp.route('/signalr/negotiate', methods=['GET', 'POST'])
def signalr_negotiate():
    """Handle SignalR negotiation."""
    auth_error = check_signalr_auth()
    if auth_error:
        return auth_error

    connection_id = f"cli_debrid-{int(time.time() * 1000)}"

    return jsonify({
        'connectionId': connection_id,
        'connectionToken': connection_id,
        'negotiateVersion': 1,
        'availableTransports': [
            {
                'transport': 'WebSockets',
                'transferFormats': ['Text', 'Binary']
            },
            {
                'transport': 'ServerSentEvents',
                'transferFormats': ['Text']
            },
            {
                'transport': 'LongPolling',
                'transferFormats': ['Text', 'Binary']
            }
        ]
    })


@bazarr_bp.route('/signalr/messages', methods=['GET'])
@bazarr_bp.route('/signalr', methods=['GET'])
def signalr_messages():
    """Handle SignalR message stream (WebSocket or Server-Sent Events fallback)."""
    auth_error = check_signalr_auth()
    if auth_error:
        return auth_error

    # Check if this is a WebSocket upgrade request
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return handle_signalr_websocket()

    # Fall back to Server-Sent Events
    return handle_signalr_sse()


def handle_signalr_websocket():
    """Handle SignalR WebSocket connection (matches CineSync implementation)."""
    try:
        from simple_websocket import Server, ConnectionClosed
    except ImportError:
        # simple-websocket not available, fall back to SSE
        logging.debug("[SignalR] WebSocket requested but simple-websocket not installed, using SSE")
        return handle_signalr_sse()

    try:
        ws = Server.accept(request.environ)
    except Exception as e:
        logging.error(f"[SignalR] WebSocket accept failed: {e}")
        return handle_signalr_sse()

    connection_id = f"ws-{int(time.time() * 1000)}"

    # Register this connection for event broadcasting
    from routes.bazarr_signalr import register_connection, unregister_connection, get_pending_events
    event_queue = register_connection(connection_id)

    try:
        # Send handshake response (matches CineSync: {"error":null}\x1e)
        ws.send(json.dumps({"error": None}) + '\x1e')

        # Send version message
        version = get_setting('Bazarr Integration', 'spoofed_version', '5.14.0.9383')
        version_msg = {
            'type': 1,
            'target': 'receiveMessage',
            'arguments': [{'name': 'version', 'body': {'version': version}}]
        }
        ws.send(json.dumps(version_msg) + '\x1e')

        logging.info(f"[SignalR] WebSocket connection established: {connection_id}")

        last_ping = time.time()
        while True:
            # Check for incoming messages (non-blocking with short timeout)
            try:
                data = ws.receive(timeout=0.1)
                if data:
                    # Handle ping/pong from client
                    try:
                        msg = json.loads(data.rstrip('\x1e'))
                        if msg.get('type') == 6:  # Ping
                            ws.send(json.dumps({'type': 6}) + '\x1e')
                    except (json.JSONDecodeError, AttributeError):
                        pass
            except Exception:
                pass

            # Check for events to broadcast
            events = get_pending_events(connection_id, timeout=0.1)
            for event in events:
                ws.send(json.dumps(event) + '\x1e')

            # Send periodic ping (every 10 seconds like CineSync)
            if time.time() - last_ping > 10:
                ws.send(json.dumps({'type': 6}) + '\x1e')
                last_ping = time.time()

    except ConnectionClosed:
        logging.info(f"[SignalR] WebSocket connection closed: {connection_id}")
    except Exception as e:
        logging.error(f"[SignalR] WebSocket error: {e}")
    finally:
        unregister_connection(connection_id)

    return ''


def handle_signalr_sse():
    """Handle SignalR Server-Sent Events connection."""
    connection_id = f"sse-{int(time.time() * 1000)}"

    # Register this connection for event broadcasting
    from routes.bazarr_signalr import register_connection, unregister_connection, get_pending_events

    def generate():
        event_queue = register_connection(connection_id)

        try:
            # Send handshake response
            yield f'data: {json.dumps({"error": None})}\x1e\n\n'

            # Send version message
            version = get_setting('Bazarr Integration', 'spoofed_version', '5.14.0.9383')
            version_msg = {
                'type': 1,
                'target': 'receiveMessage',
                'arguments': [{'name': 'version', 'body': {'version': version}}]
            }
            yield f'data: {json.dumps(version_msg)}\x1e\n\n'

            logging.info(f"[SignalR] SSE connection established: {connection_id}")

            last_ping = time.time()
            while True:
                # Check for events to broadcast
                events = get_pending_events(connection_id, timeout=1.0)
                for event in events:
                    yield f'data: {json.dumps(event)}\x1e\n\n'

                # Send periodic ping (every 15 seconds)
                if time.time() - last_ping > 15:
                    yield f'data: {json.dumps({"type": 6})}\x1e\n\n'
                    last_ping = time.time()

        except GeneratorExit:
            logging.info(f"[SignalR] SSE connection closed: {connection_id}")
        finally:
            unregister_connection(connection_id)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        }
    )


# ============================================================================
# Settings API Endpoints
# ============================================================================

@bazarr_bp.route('/settings/api/bazarr/generate_key', methods=['POST'])
def generate_bazarr_key():
    """Generate a new API key for Bazarr integration."""
    new_key = generate_api_key()
    set_setting('Bazarr Integration', 'api_key', new_key)
    return jsonify({'success': True, 'api_key': new_key})


@bazarr_bp.route('/settings/api/bazarr/test', methods=['GET'])
def test_bazarr_connection():
    """Test that Bazarr spoofing is working."""
    symlinked_mode = is_symlinked_mode()
    enabled = get_setting('Bazarr Integration', 'enabled', False)
    api_key = get_setting('Bazarr Integration', 'api_key', '')
    service_type = get_setting('Bazarr Integration', 'service_type', 'auto')
    version = get_setting('Bazarr Integration', 'spoofed_version', '5.14.0.9383')

    # Count available media
    try:
        movies = get_collected_movies()
        series = get_collected_series()
        movie_count = len(movies)
        series_count = len(series)
    except Exception:
        movie_count = 0
        series_count = 0

    # Determine status
    if not symlinked_mode:
        status = 'unavailable'
        status_message = 'Bazarr integration requires Symlinked/Local file management mode'
    elif not enabled:
        status = 'disabled'
        status_message = 'Bazarr integration is disabled'
    elif not api_key:
        status = 'not_configured'
        status_message = 'API key not configured'
    else:
        status = 'ready'
        status_message = 'Bazarr integration is ready'

    return jsonify({
        'symlinked_mode': symlinked_mode,
        'enabled': enabled,
        'has_api_key': bool(api_key),
        'service_type': service_type,
        'version': version,
        'movie_count': movie_count,
        'series_count': series_count,
        'status': status,
        'status_message': status_message
    })
