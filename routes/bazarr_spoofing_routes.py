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
from utilities.release_parser import ReleaseParser
from database.core import get_db_connection

# Create blueprint - mounted at root to handle /api/v3/* and /signalr/* paths
bazarr_bp = Blueprint('bazarr', __name__)

# Process start time for system status
PROCESS_START_TIME = datetime.now(timezone.utc)

# ============================================================================
# Authentication
# ============================================================================

def is_symlinked_mode() -> bool:
    """Check if file collection management is Symlinked/Local or Plex mode (both support Bazarr)."""
    mode = get_setting('File Management', 'file_collection_management', 'Plex')
    return mode in ('Symlinked/Local', 'Plex')


@bazarr_bp.before_request
def log_bazarr_request():
    logging.debug(f"[Bazarr] {request.method} {request.path} args={dict(request.args)} json={request.get_json(silent=True)}")


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


def _get_stable_app_guid() -> str:
    """Return a stable app GUID derived from the configured API key.
    Must be the same on every request — Bazarr uses this to identify the
    Radarr/Sonarr instance and associates its media library with it.
    A random GUID per request causes Bazarr to lose all media associations."""
    import hashlib
    api_key = get_setting('Bazarr Integration', 'api_key', 'cli_debrid_bazarr')
    return hashlib.md5(f'cli_debrid_bazarr_{api_key}'.encode()).hexdigest()


# ============================================================================
# Helper Functions - Database Queries
# ============================================================================

def get_collected_movies() -> List[Dict[str, Any]]:
    """Get all collected movies from the database."""
    conn = get_db_connection()
    conn.execute('PRAGMA query_only = ON')
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, year, file_path,
                   location_on_disk, genres, runtime, collected_at, version,
                   filled_by_file, resolution, size
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'movie'
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
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
                'version': row[10],
                'filled_by_file': row[11],
                'resolution': row[12],
                'size': row[13]
            })
        return movies
    finally:
        conn.close()


def get_collected_series() -> List[Dict[str, Any]]:
    """Get all collected TV series from the database (grouped by show)."""
    conn = get_db_connection()
    conn.execute('PRAGMA query_only = ON')
    try:
        cursor = conn.cursor()
        # NULLIF so empty-string imdb_id falls through to tmdb (matches normalize_show_id)
        cursor.execute("""
            SELECT DISTINCT
                COALESCE(NULLIF(imdb_id, ''), NULLIF(CAST(tmdb_id AS TEXT), '')) as show_id,
                imdb_id, tmdb_id, title, year, genres, runtime
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'episode'
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
            GROUP BY COALESCE(NULLIF(imdb_id, ''), NULLIF(CAST(tmdb_id AS TEXT), '')), title
        """)

        series_list = []
        for row in cursor.fetchall():
            imdb_id, tmdb_id = row[1], row[2]
            series_list.append({
                'show_id': normalize_show_id(imdb_id, tmdb_id) or row[0],
                'imdb_id': imdb_id,
                'tmdb_id': tmdb_id,
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
    conn.execute('PRAGMA query_only = ON')
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, episode_title, year,
                   season_number, episode_number, file_path,
                   location_on_disk, collected_at, version,
                   filled_by_file, resolution, size
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'episode'
            AND (imdb_id = ? OR tmdb_id = ?)
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
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
                'version': row[11],
                'filled_by_file': row[12],
                'resolution': row[13],
                'size': row[14]
            })
        return episodes
    finally:
        conn.close()


def get_all_collected_episodes() -> List[Dict[str, Any]]:
    """Get all collected episodes in one query for bulk series listing."""
    conn = get_db_connection()
    conn.execute('PRAGMA query_only = ON')
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, episode_title, year,
                   season_number, episode_number, file_path,
                   location_on_disk, collected_at, version,
                   filled_by_file, resolution, size
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'episode'
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
        """)
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
                'version': row[11],
                'filled_by_file': row[12],
                'resolution': row[13],
                'size': row[14]
            })
        return episodes
    finally:
        conn.close()


def _movie_row_to_dict(row) -> Dict[str, Any]:
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
        'version': row[10],
        'filled_by_file': row[11],
        'resolution': row[12]
    }


def get_movie_by_id(movie_id: int) -> Optional[Dict[str, Any]]:
    """Get a specific movie by its Radarr id (TMDB, else hashed media id).

    Prefer tmdb_id match — never match media_items.id first, which collides
    with TMDB ids and returns the wrong title.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, imdb_id, tmdb_id, title, year, file_path,
                   location_on_disk, genres, runtime, collected_at, version,
                   filled_by_file, resolution
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'movie'
            AND (tmdb_id = ? OR tmdb_id = ?)
            LIMIT 1
        """, (str(movie_id), movie_id))

        row = cursor.fetchone()
        if row:
            return _movie_row_to_dict(row)
    finally:
        conn.close()

    # Fallback: hashed movie ids when TMDB is missing
    for movie in get_collected_movies():
        try:
            tmdb_id = int(movie.get('tmdb_id') or 0)
        except (ValueError, TypeError):
            tmdb_id = 0
        resolved = tmdb_id or generate_unique_id(movie.get('id'), 'movie')
        if resolved == movie_id:
            return movie
    return None


def get_series_by_id(series_id: int) -> Optional[Dict[str, Any]]:
    """Resolve a Sonarr series id to show metadata.

    Prefer tmdb_id match (not media_items.id — PK collisions with TMDB break
    Bazarr FK inserts). Fall back to scanning collected shows for hashed
    sonarr_series_id matches when TMDB is absent.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                COALESCE(NULLIF(imdb_id, ''), NULLIF(CAST(tmdb_id AS TEXT), '')) as show_id,
                imdb_id, tmdb_id, title, year, genres, runtime
            FROM media_items
            WHERE state = 'Collected'
            AND type = 'episode'
            AND (tmdb_id = ? OR tmdb_id = ?)
            AND (file_path IS NOT NULL OR location_on_disk IS NOT NULL)
            LIMIT 1
        """, (str(series_id), series_id))

        row = cursor.fetchone()
        if row:
            imdb_id, tmdb_id = row[1], row[2]
            return {
                'show_id': normalize_show_id(imdb_id, tmdb_id) or row[0],
                'imdb_id': imdb_id,
                'tmdb_id': tmdb_id,
                'title': row[3],
                'year': row[4],
                'genres': row[5],
                'runtime': row[6]
            }
    finally:
        conn.close()

    for series in get_collected_series():
        if sonarr_series_id(series) == series_id:
            result = dict(series)
            result['show_id'] = (
                normalize_show_id(result.get('imdb_id'), result.get('tmdb_id'))
                or result.get('show_id')
            )
            return result
    return None


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


def normalize_show_id(imdb_id: Any = None, tmdb_id: Any = None) -> str:
    """Normalize show identity for grouping and lookups.

    Blank/whitespace strings are treated as missing. Prefer IMDB, then TMDB —
    matching SQL COALESCE(NULLIF(imdb_id, ''), NULLIF(tmdb_id, '')).
    """
    def _clean(value: Any) -> str:
        if value is None:
            return ''
        text = str(value).strip()
        return text if text else ''

    return _clean(imdb_id) or _clean(tmdb_id)


def sonarr_series_id(item: Dict[str, Any]) -> int:
    """Stable Sonarr series id used by every HTTP and SignalR path.

    Use integer TMDB when present; otherwise a deterministic hash of show_id.
    """
    try:
        tmdb_id = int(item.get('tmdb_id') or 0)
    except (ValueError, TypeError):
        tmdb_id = 0
    if tmdb_id:
        return tmdb_id
    show_id = item.get('show_id') or normalize_show_id(item.get('imdb_id'), item.get('tmdb_id'))
    return generate_unique_id(show_id, 'series')


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


def _get_mount_base() -> str:
    """No remapping — return empty so paths are passed through as-is from DB."""
    return ''


def _remap_plex_path(path: str) -> str:
    """Replace the /debrid prefix in DB paths with the actual mount path."""
    if not path:
        return path
    mount_base = _get_mount_base()
    if not mount_base:
        return path
    # DB paths look like /debrid/movies/... or /debrid/shows/...
    # Split off /debrid and replace with mount_base
    parts = path.split('/', 3)  # ['', 'debrid', 'movies'|'shows', 'rest']
    if len(parts) >= 3 and parts[1] == 'debrid':
        return mount_base + '/' + '/'.join(parts[2:])
    return path


def get_file_path(item: Dict[str, Any]) -> str:
    """Get the full file path for an item.

    location_on_disk is always a complete file path in both modes:
    - Symlinked/Local: full path to the renamed symlink file
    - Plex: /debrid/movies/Folder/file.mkv or similar

    In Symlinked/Local mode the symlink filename differs from filled_by_file (the original
    source name), so appending filled_by_file would produce a non-existent path.
    Use location_on_disk directly when present; only fall back to filled_by_file when absent.
    """
    location = (item.get('location_on_disk') or item.get('file_path') or '').rstrip('/')
    filled_by_file = (item.get('filled_by_file') or '').strip()

    if location:
        full_path = location
    else:
        full_path = filled_by_file

    return _remap_plex_path(full_path)


def get_file_size(file_path: str, db_size: int = None) -> int:
    """Get file size in bytes. Uses DB size when available to avoid FUSE stat calls."""
    if db_size:
        return db_size
    try:
        if file_path and os.path.exists(file_path):
            return os.path.getsize(file_path)
    except Exception:
        pass
    return 1073741824  # 1GB default


def parse_genres(genres_str: str) -> List[str]:
    """Parse genres from database string format."""
    if not genres_str:
        return []
    try:
        return json.loads(genres_str)
    except (json.JSONDecodeError, TypeError):
        return [g.strip() for g in genres_str.split(',') if g.strip()]


def parse_media_info(filename: str, version: str = None) -> Dict[str, Any]:
    """
    Parse media info (codec, audio, release group) from filename.

    Returns dict with:
        - video_codec: str (e.g., 'x265', 'x264', 'HEVC')
        - audio_codec: str (e.g., 'AAC', 'DTS', 'TrueHD')
        - release_group: str (e.g., 'SPARKS', 'YTS')
        - resolution: str (e.g., '1080p', '2160p')
    """
    result = {
        'video_codec': '',
        'audio_codec': '',
        'release_group': '',
        'resolution': ''
    }

    # Try parsing filename first, then version string
    parse_target = filename or version or ''
    if not parse_target:
        return result

    try:
        # Use ReleaseParser to extract info
        parsed = ReleaseParser.parse_with_guessit(parse_target)
        if not parsed or parsed.get('codec') is None:
            # Fall back to regex parsing
            parsed = ReleaseParser.parse_with_regex(parse_target)

        # Map parsed values
        if parsed.get('codec'):
            result['video_codec'] = str(parsed['codec'])
        if parsed.get('audio'):
            result['audio_codec'] = str(parsed['audio'])
        if parsed.get('resolution'):
            result['resolution'] = str(parsed['resolution'])

        # Get release group
        release_group = ReleaseParser.extract_release_group(parse_target)
        if release_group:
            result['release_group'] = release_group

    except Exception as e:
        logging.debug(f"Error parsing media info from '{parse_target}': {e}")

    return result


def create_media_info(parsed: Dict[str, Any], file_path: str = '') -> Dict[str, Any]:
    """Create Radarr/Sonarr mediaInfo object from parsed data."""
    # Map codec names to Radarr/Sonarr format
    video_codec = parsed.get('video_codec', '')
    audio_codec = parsed.get('audio_codec', '')

    # Get resolution as integer
    resolution_str = parsed.get('resolution', '')
    resolution_int = 0
    if '2160' in resolution_str or '4k' in resolution_str.lower():
        resolution_int = 2160
    elif '1080' in resolution_str:
        resolution_int = 1080
    elif '720' in resolution_str:
        resolution_int = 720
    elif '480' in resolution_str:
        resolution_int = 480

    return {
        'audioBitrate': 0,
        'audioChannels': 2.0,
        'audioCodec': audio_codec,
        'audioLanguages': 'English',
        'audioStreamCount': 1,
        'videoBitDepth': 10 if 'x265' in video_codec.lower() or 'hevc' in video_codec.lower() else 8,
        'videoBitrate': 0,
        'videoCodec': video_codec,
        'videoFps': 23.976,
        'videoDynamicRange': 'HDR' if 'HDR' in video_codec.upper() else 'SDR',
        'videoDynamicRangeType': '',
        'resolution': f'{resolution_int}p' if resolution_int else '',
        'runTime': '0:00:00',
        'scanType': 'Progressive',
        'subtitles': ''
    }


def create_movie_resource(item: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    """Create a Radarr-compatible MovieResource from database item."""
    try:
        tmdb_id = int(item.get('tmdb_id') or 0)
    except (ValueError, TypeError):
        tmdb_id = 0
    movie_id = tmdb_id or generate_unique_id(item.get('id'), 'movie')
    file_path = get_file_path(item)
    file_size = get_file_size(file_path, item.get('size'))

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
    filename = item.get('filled_by_file', '') or os.path.basename(file_path)

    # Skip expensive media info parsing for list endpoint
    if full:
        media_info_parsed = parse_media_info(filename, item.get('version', ''))
        media_info = create_media_info(media_info_parsed, file_path)
        db_resolution = item.get('resolution', '')
        if db_resolution:
            media_info['resolution'] = db_resolution
        release_group = media_info_parsed.get('release_group', '')
    else:
        media_info = {'subtitles': ''}
        release_group = ''

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
        'sceneName': filename,
        'releaseGroup': release_group,
        'mediaInfo': media_info
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
        'path': os.path.dirname(file_path),
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
    try:
        tmdb_id = int(item.get('tmdb_id') or 0)
    except (ValueError, TypeError):
        tmdb_id = 0
    series_id = sonarr_series_id(item)
    title = item.get('title', 'Unknown')

    # Generate consistent tvdbId for Bazarr compatibility
    tvdb_id = generate_tvdb_id(tmdb_id or item.get('show_id'), title)

    # Build seasons from episodes with complete statistics
    seasons = []
    total_size_on_disk = 0
    if episodes:
        season_numbers = set(ep.get('season_number') or 0 for ep in episodes)
        for season_num in sorted(season_numbers):
            season_episodes = [ep for ep in episodes if ep.get('season_number') == season_num]
            # Calculate size for this season
            season_size = sum(get_file_size(get_file_path(ep), ep.get('size')) for ep in season_episodes)
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
    file_size = get_file_size(file_path, item.get('size'))

    # Parse media info from filename
    filename = item.get('filled_by_file', '') or os.path.basename(file_path)
    media_info_parsed = parse_media_info(filename, item.get('version', ''))
    media_info = create_media_info(media_info_parsed, file_path)

    # Use database resolution if available
    db_resolution = item.get('resolution', '')
    if db_resolution:
        media_info['resolution'] = db_resolution

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
        'sceneName': filename,
        'releaseGroup': media_info_parsed.get('release_group', ''),
        'mediaInfo': media_info
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
    file_size = get_file_size(file_path, item.get('size'))
    quality = detect_quality_from_version(item.get('version', ''))

    collected_at = item.get('collected_at')
    if isinstance(collected_at, str):
        try:
            added_time = datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        except ValueError:
            added_time = datetime.now(timezone.utc)
    else:
        added_time = datetime.now(timezone.utc)

    # Parse media info from filename
    filename = item.get('filled_by_file', '') or os.path.basename(file_path)
    media_info_parsed = parse_media_info(filename, item.get('version', ''))
    media_info = create_media_info(media_info_parsed, file_path)

    # Use database resolution if available
    db_resolution = item.get('resolution', '')
    if db_resolution:
        media_info['resolution'] = db_resolution

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
        'sceneName': filename,
        'releaseGroup': media_info_parsed.get('release_group', ''),
        'mediaInfo': media_info
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
        'appGuid': _get_stable_app_guid(),
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
    mode = get_setting('File Management', 'file_collection_management', 'Plex')
    if mode == 'Plex':
        mount_path = ''
        # Try to read mount path from cli_mount config.json
        try:
            from utilities.settings import load_config
            import json as _json
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            if data_path:
                dc_config_path = os.path.join(data_path, 'config.json')
                with open(dc_config_path, 'r') as _f:
                    dc_cfg = _json.load(_f)
                mount_path = (dc_cfg.get('mount', {}).get('mount_path') or '').strip()
        except Exception:
            pass
        # Fallback: derive mount base from CLI's mounted_file_location setting
        if not mount_path:
            try:
                mounted = get_setting('Usenet Provider', 'mounted_file_location', '/debrid/__all__')
                # Strip trailing path components like /__all__ or /content to get the mount base
                import re as _re
                mount_path = _re.sub(r'/(__all__|content|movies|shows)$', '', mounted.rstrip('/'))
            except Exception:
                mount_path = '/debrid'
        folders = [
            {'id': 1, 'path': os.path.join(mount_path, 'movies')},
            {'id': 2, 'path': os.path.join(mount_path, 'shows')}
        ]
    else:
        symlink_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
        movies_folder = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_folder = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
        folders = [
            {'id': 1, 'path': os.path.join(symlink_path, movies_folder)},
            {'id': 2, 'path': os.path.join(symlink_path, tv_folder)}
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
            return jsonify(create_movie_resource(movie, full=True))
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
        # Load all episodes in one query and group by normalized show_id
        all_episodes = get_all_collected_episodes()
        ep_map = {}
        for ep in all_episodes:
            key = normalize_show_id(ep.get('imdb_id'), ep.get('tmdb_id'))
            if key:
                ep_map.setdefault(key, []).append(ep)

        result = []
        for s in series_list:
            show_id = normalize_show_id(s.get('imdb_id'), s.get('tmdb_id')) or s.get('show_id', '')
            episodes = ep_map.get(show_id, [])
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


@bazarr_bp.route('/api/v3/filesystem', methods=['GET'])
@bazarr_bp.route('/api/v3/filesystem/', methods=['GET'])
@require_bazarr_auth
def filesystem():
    """Return filesystem directory listing for path browsing."""
    path = request.args.get('path', '/')
    include_files = request.args.get('includeFiles', 'false').lower() == 'true'
    try:
        if not path or path in ('', '/'):
            # Return top-level directories
            dirs = [{'path': '/mnt', 'name': 'mnt', 'lastModified': '2024-01-01T00:00:00Z'}]
        else:
            dirs = []
            files = []
            if os.path.isdir(path):
                for entry in sorted(os.scandir(path), key=lambda e: e.name):
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append({'path': entry.path, 'name': entry.name, 'lastModified': '2024-01-01T00:00:00Z'})
                        elif include_files and entry.is_file(follow_symlinks=False):
                            files.append({'path': entry.path, 'name': entry.name, 'lastModified': '2024-01-01T00:00:00Z', 'size': entry.stat().st_size})
                    except Exception:
                        continue
        return jsonify({'directories': dirs, 'files': files if include_files else []})
    except Exception as e:
        return jsonify({'directories': [], 'files': [], 'error': str(e)})


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
    if not is_symlinked_mode():
        return jsonify({'error': 'Not found'}), 404

    if not get_setting('Bazarr Integration', 'enabled', False):
        return jsonify({'error': 'Not found'}), 404

    # Get API key from header, query param, or negotiate token
    api_key = (request.headers.get('X-Api-Key')
               or request.args.get('apikey')
               or request.args.get('access_token'))

    configured_key = get_setting('Bazarr Integration', 'api_key', '')

    logging.debug(f"[SignalR] auth check path={request.path} has_key={bool(api_key)} headers={dict(request.headers)} args={dict(request.args)}")

    if not api_key:
        logging.warning(f"[SignalR] auth failed: missing API key for {request.path}")
        return jsonify({'error': 'Unauthorized'}), 401

    if not configured_key or api_key != configured_key:
        logging.warning(f"[SignalR] auth failed: invalid API key for {request.path}")
        return jsonify({'error': 'Unauthorized'}), 401

    return None


@bazarr_bp.route('/signalr/messages/negotiate', methods=['GET', 'POST'])
@bazarr_bp.route('/signalr/negotiate', methods=['GET', 'POST'])
def signalr_negotiate():
    """Handle SignalR negotiation."""
    # Bazarr's signalrcore library does not send the API key during negotiate,
    # only check that the integration is enabled rather than validating the key here.
    if not is_symlinked_mode() or not get_setting('Bazarr Integration', 'enabled', False):
        return jsonify({'error': 'Not found'}), 404

    connection_id = f"cli_debrid-{int(time.time() * 1000)}"

    # Bazarr's bundled signalrcore only implements WebSocket transport (it
    # ignores availableTransports and always opens ws://). Advertise WebSockets
    # and require the WS-capable Werkzeug handler in run_server().
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
            }
        ]
    })


@bazarr_bp.route('/signalr/messages', methods=['GET'])
@bazarr_bp.route('/signalr', methods=['GET'])
def signalr_messages():
    """Handle SignalR message stream (WebSocket preferred; SSE fallback)."""
    if not is_symlinked_mode() or not get_setting('Bazarr Integration', 'enabled', False):
        return jsonify({'error': 'Not found'}), 404

    # Prefer the Werkzeug request-handler path for WebSockets (avoids Flask
    # middleware blocking the 101). Keep this branch for non-standard servers.
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        run_signalr_websocket_session(request.environ)
        return ''

    return handle_signalr_sse()


def run_signalr_websocket_session(environ) -> None:
    """Run a SignalR JSON-protocol WebSocket session on an upgraded socket.

    ``environ`` must include ``werkzeug.socket`` (injected by
    ``WebSocketAwareHandler``) so simple_websocket can complete the handshake.
    """
    try:
        from simple_websocket import Server, ConnectionClosed
    except ImportError:
        logging.error("[SignalR] simple-websocket not installed")
        return

    from urllib.parse import parse_qs
    qs = parse_qs(environ.get('QUERY_STRING') or '')
    connection_id = (
        (qs.get('id') or qs.get('connectionToken') or [None])[0]
        or f"ws-{int(time.time() * 1000)}"
    )

    if not is_symlinked_mode() or not get_setting('Bazarr Integration', 'enabled', False):
        logging.warning("[SignalR] WebSocket rejected: Bazarr integration disabled")
        return

    try:
        ws = Server.accept(environ)
    except Exception as e:
        logging.error(f"[SignalR] WebSocket accept failed: {e}")
        return

    from routes.bazarr_signalr import register_connection, unregister_connection, get_pending_events
    register_connection(connection_id)

    try:
        # Wait for client handshake: {"protocol":"json","version":1}\x1e
        try:
            first = ws.receive(timeout=10)
            if first:
                logging.debug(f"[SignalR] client handshake: {str(first)[:80]!r}")
        except Exception:
            pass

        # Handshake response required by SignalR JSON protocol
        ws.send(json.dumps({"error": None}) + '\x1e')

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
            try:
                data = ws.receive(timeout=0.1)
            except Exception:
                data = None

            if data:
                try:
                    msg = json.loads(str(data).rstrip('\x1e'))
                    if msg.get('type') == 6:  # Ping
                        ws.send(json.dumps({'type': 6}) + '\x1e')
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass

            events = get_pending_events(connection_id, timeout=0.1)
            for event in events:
                ws.send(json.dumps(event) + '\x1e')

            if time.time() - last_ping > 10:
                ws.send(json.dumps({'type': 6}) + '\x1e')
                last_ping = time.time()

    except ConnectionClosed:
        logging.info(f"[SignalR] WebSocket connection closed: {connection_id}")
    except Exception as e:
        logging.error(f"[SignalR] WebSocket error: {e}")
    finally:
        unregister_connection(connection_id)


def handle_signalr_sse():
    """Handle SignalR Server-Sent Events connection."""
    connection_id = (
        request.args.get('id')
        or request.args.get('connectionToken')
        or f"sse-{int(time.time() * 1000)}"
    )

    from routes.bazarr_signalr import register_connection, unregister_connection, get_pending_events

    def generate():
        register_connection(connection_id)

        try:
            yield f'data: {json.dumps({"error": None})}\x1e\n\n'

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
                events = get_pending_events(connection_id, timeout=1.0)
                for event in events:
                    yield f'data: {json.dumps(event)}\x1e\n\n'

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
            'X-Accel-Buffering': 'no',
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
        status_message = 'Bazarr integration requires Symlinked/Local or Plex file management mode'
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
