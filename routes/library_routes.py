"""
Library Routes - Media library browser with infinite scroll
Fast visual browser for collected media
"""
from flask import Blueprint, render_template, jsonify, request, Response
from flask_login import current_user
from .models import user_required, onboarding_required, admin_required
from database.core import get_db_connection
from utilities.settings import get_setting, get_nas_paths
from debrid import get_debrid_provider
import logging
import requests
from datetime import datetime, timedelta
from .discover_routes import get_digital_release_date, get_certification
from .poster_cache import load_cache, save_cache

library_bp = Blueprint('library', __name__, url_prefix='/library')

# Constants for pagination
ITEMS_PER_BATCH = 50  # Items to load per infinite scroll batch
MAX_BATCH_SIZE = 200  # Maximum items allowed per request

@library_bp.route('/')
@user_required
@onboarding_required
def index():
    """Main library page - displays collected media in grid layout"""
    # Check if either Plex or TMDB is configured
    plex_url = get_setting('Plex', 'url', default='')
    plex_token = get_setting('Plex', 'token', default='')
    tmdb_api_key = get_setting('TMDB', 'api_key', default='')

    plex_configured = bool(plex_url and plex_token)
    tmdb_configured = bool(tmdb_api_key)

    # Check user role for permission restrictions - if auth is disabled, grant all permissions
    from .utils import is_user_system_enabled
    if not is_user_system_enabled():
        has_admin_permissions = True
    else:
        has_admin_permissions = current_user.is_authenticated and current_user.role == 'admin'

    # Pass configuration status and permissions to template
    return render_template('library.html',
                         plex_configured=plex_configured,
                         tmdb_configured=tmdb_configured,
                         has_admin_permissions=has_admin_permissions,
                         nas_paths=get_nas_paths())

@library_bp.route('/plex_image/<path:plex_path>')
@user_required
def plex_image_proxy(plex_path):
    """
    Proxy endpoint for Plex poster/backdrop images
    Takes a Plex image path and proxies it through the Plex server with authentication
    """
    try:
        plex_url = get_setting('Plex', 'url', default='').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')

        if not plex_url or not plex_token:
            logging.error("Plex URL or token not configured")
            return Response(status=404)

        # Build full Plex image URL - ensure path starts with /
        if not plex_path.startswith('/'):
            plex_path = '/' + plex_path

        # Request original poster without overlays by adding includeAllConcerts parameter
        # This tells Plex to return the raw poster image without custom overlays
        full_url = f"{plex_url}{plex_path}?X-Plex-Token={plex_token}&includeAllConcerts=1"

        # Fetch image from Plex
        response = requests.get(full_url, timeout=10, stream=True)

        if response.status_code == 200:
            # Return the image with appropriate content type
            # Only cache if web caching is enabled in settings
            from utilities.settings import load_config
            config = load_config()
            enable_caching = config.get('UI Settings', {}).get('enable_caching', False)
            cache_header = 'public, max-age=86400' if enable_caching else 'no-cache'

            return Response(
                response.content,
                content_type=response.headers.get('Content-Type', 'image/jpeg'),
                headers={'Cache-Control': cache_header}
            )
        else:
            logging.warning(f"Failed to fetch Plex image: {plex_path}, status: {response.status_code}")
            return Response(status=404)

    except Exception as e:
        logging.error(f"Error proxying Plex image {plex_path}: {e}")
        return Response(status=500)

@library_bp.route('/debug_posters/<int:plex_rating_key>')
@user_required
def debug_posters(plex_rating_key):
    """
    Debug endpoint to explore available poster options from PlexAPI
    Shows all available posters for a media item
    """
    try:
        from plexapi.server import PlexServer

        plex_url = get_setting('Plex', 'url', default='').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')

        if not plex_url or not plex_token:
            return jsonify({'error': 'Plex not configured'}), 500

        plex = PlexServer(plex_url, plex_token)
        item = plex.fetchItem(plex_rating_key)

        result = {
            'title': item.title,
            'type': item.type,
            'current_thumb': item.thumb,
            'current_art': item.art if hasattr(item, 'art') else None,
            'posters': [],
            'arts': []
        }

        # Get all available posters
        if hasattr(item, 'posters'):
            for poster in item.posters():
                result['posters'].append({
                    'key': poster.key,
                    'selected': poster.selected,
                    'provider': poster.provider,
                    'ratingKey': poster.ratingKey,
                    'thumb': poster.thumb if hasattr(poster, 'thumb') else None
                })

        # Get all available arts/backdrops
        if hasattr(item, 'arts'):
            for art in item.arts():
                result['arts'].append({
                    'key': art.key,
                    'selected': art.selected,
                    'provider': art.provider,
                    'ratingKey': art.ratingKey,
                    'thumb': art.thumb if hasattr(art, 'thumb') else None
                })

        return jsonify(result)

    except Exception as e:
        logging.error(f"Error debugging posters for {plex_rating_key}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@library_bp.route('/cache_status')
@user_required
def cache_status():
    """
    Diagnostic endpoint to check poster cache status
    Shows statistics about TMDB vs Plex posters in cache
    """
    try:
        import os
        import pickle
        from datetime import datetime

        DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'poster_cache.pkl')

        result = {
            'cache_file': CACHE_FILE,
            'exists': False,
            'statistics': {}
        }

        if not os.path.exists(CACHE_FILE):
            result['message'] = 'Poster cache file does not exist yet. Run a Plex collection to populate it.'
            return jsonify(result)

        result['exists'] = True
        file_size = os.path.getsize(CACHE_FILE)
        file_mtime = os.path.getmtime(CACHE_FILE)

        result['file_size_bytes'] = file_size
        result['file_size_mb'] = round(file_size / 1024 / 1024, 2)
        result['last_modified'] = datetime.fromtimestamp(file_mtime).isoformat()

        # Load and analyze cache
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)

        tmdb_count = 0
        plex_count = 0
        backdrop_count = 0
        other_count = 0

        recent_tmdb = []
        recent_plex = []

        for key, (url, timestamp) in cache.items():
            key_str = str(key)
            if '_backdrop' in key_str:
                backdrop_count += 1
            elif 'image.tmdb.org' in url:
                tmdb_count += 1
                recent_tmdb.append({
                    'key': key,
                    'url': url,
                    'timestamp': datetime.fromtimestamp(timestamp).isoformat()
                })
            elif url.startswith('/library/'):
                plex_count += 1
                recent_plex.append({
                    'key': key,
                    'url': url,
                    'timestamp': datetime.fromtimestamp(timestamp).isoformat()
                })
            else:
                other_count += 1

        # Sort by timestamp and get recent items
        recent_tmdb.sort(key=lambda x: x['timestamp'], reverse=True)
        recent_plex.sort(key=lambda x: x['timestamp'], reverse=True)

        total_posters = tmdb_count + plex_count
        tmdb_percentage = (tmdb_count / total_posters * 100) if total_posters > 0 else 0

        result['statistics'] = {
            'total_entries': len(cache),
            'tmdb_posters': tmdb_count,
            'plex_posters': plex_count,
            'backdrops': backdrop_count,
            'other': other_count,
            'tmdb_percentage': round(tmdb_percentage, 1),
            'recent_tmdb_samples': recent_tmdb[:5],
            'recent_plex_samples': recent_plex[:5]
        }

        if tmdb_percentage > 80:
            result['status'] = 'excellent'
            result['message'] = 'Excellent! Most posters are from TMDB (clean, no overlays)'
        elif tmdb_percentage > 50:
            result['status'] = 'good'
            result['message'] = 'Good! Majority of posters are from TMDB'
        elif tmdb_count > 0:
            result['status'] = 'partial'
            result['message'] = 'TMDB posters are being cached, but many Plex posters remain'
        else:
            result['status'] = 'none'
            result['message'] = 'No TMDB posters found. TMDB caching may not be working.'

        return jsonify(result)

    except Exception as e:
        logging.error(f"Error checking cache status: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@library_bp.route('/fetch_poster/<int:tmdb_id>/<media_type>')
@user_required
def fetch_poster(tmdb_id, media_type):
    """
    On-demand poster fetching endpoint
    Fetches poster from TMDB, caches it, and returns the poster path
    """
    try:
        from utilities.web_scraper import get_media_meta
        from routes.poster_cache import cache_poster_url

        # Convert show to tv for TMDB API
        tmdb_media_type = 'tv' if media_type == 'show' else 'movie'

        # Fetch metadata from TMDB using web_scraper function
        # Returns: (poster_url, overview, genres, vote_average, backdrop_path)
        media_meta = get_media_meta(str(tmdb_id), tmdb_media_type)

        if media_meta and media_meta[0]:
            # media_meta[0] is the full TMDB poster URL
            full_tmdb_url = media_meta[0]

            # Extract poster path from URL (e.g., "/abc123.jpg" from "https://image.tmdb.org/t/p/w300/abc123.jpg")
            if 'image.tmdb.org' in full_tmdb_url:
                parts = full_tmdb_url.split('/t/p/')
                if len(parts) > 1:
                    # Get everything after /t/p/ (e.g., "w300/abc123.jpg")
                    full_path = parts[1]
                    # Extract just the filename (e.g., "/abc123.jpg")
                    if '/' in full_path:
                        tmdb_poster = '/' + full_path.split('/', 1)[1]
                    else:
                        tmdb_poster = '/' + full_path

                    # Cache the TMDB poster URL
                    cache_poster_url(str(tmdb_id), media_type, full_tmdb_url)

                    return jsonify({
                        'success': True,
                        'poster_path': tmdb_poster,
                        'source': 'tmdb'
                    })

        return jsonify({
            'success': False,
            'error': 'No poster found in TMDB'
        })

    except Exception as e:
        logging.error(f"Error fetching poster for {tmdb_id} ({media_type}): {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/fetch_poster_imdb/<imdb_id>/<media_type>')
@user_required
def fetch_poster_imdb(imdb_id, media_type):
    """
    On-demand poster fetching endpoint using IMDB ID
    Looks up TMDB ID from IMDB ID, then fetches poster from TMDB
    """
    try:
        from utilities.web_scraper import get_tmdb_id_from_imdb, get_media_meta
        from routes.poster_cache import cache_poster_url

        # Convert show to tv for TMDB API
        tmdb_media_type = 'tv' if media_type == 'show' else 'movie'

        # First, look up TMDB ID from IMDB ID
        tmdb_id = get_tmdb_id_from_imdb(imdb_id, tmdb_media_type)
        if not tmdb_id:
            return jsonify({
                'success': False,
                'error': f'Could not find TMDB ID for IMDB ID {imdb_id}'
            })

        # Fetch metadata from TMDB
        media_meta = get_media_meta(tmdb_id, tmdb_media_type)

        if media_meta and media_meta[0]:
            full_tmdb_url = media_meta[0]

            if 'image.tmdb.org' in full_tmdb_url:
                parts = full_tmdb_url.split('/t/p/')
                if len(parts) > 1:
                    full_path = parts[1]
                    if '/' in full_path:
                        tmdb_poster = '/' + full_path.split('/', 1)[1]
                    else:
                        tmdb_poster = '/' + full_path

                    # Cache using both TMDB ID and IMDB ID for future lookups
                    cache_poster_url(tmdb_id, media_type, full_tmdb_url)
                    cache_poster_url(imdb_id, media_type, full_tmdb_url)

                    return jsonify({
                        'success': True,
                        'poster_path': tmdb_poster,
                        'tmdb_id': tmdb_id,
                        'source': 'tmdb_via_imdb'
                    })

        return jsonify({
            'success': False,
            'error': 'No poster found in TMDB'
        })

    except Exception as e:
        logging.error(f"Error fetching poster for IMDB {imdb_id} ({media_type}): {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/data')
@user_required
def get_library_data():
    """
    API endpoint for infinite scroll data - OPTIMIZED for speed
    Returns JSON with media items for the library grid
    Uses direct SQL queries with LIMIT/OFFSET for fast pagination

    Query Parameters:
        limit (int): Number of items to return (default: 50, max: 200)
        offset (int): Starting position for pagination (default: 0)
        search (str): Optional search term to filter results
        status (str): Filter by status (all/collected/partial/missing/upcoming/upgraded)
        media_type (str): Filter by type (all/movie/show)
        sort (str): Sort field (title_asc, title_desc, year_asc, year_desc, added_desc)
    """
    import time
    start_time = time.time()
    conn = None
    try:
        # Get query parameters
        limit = request.args.get('limit', default=ITEMS_PER_BATCH, type=int)
        offset = request.args.get('offset', default=0, type=int)
        search_term = request.args.get('search', default='', type=str).strip()
        status_filter = request.args.get('status', default='all', type=str)
        duplicates_state = request.args.get('duplicates_state', default='all', type=str)
        media_type_filter = request.args.get('media_type', default='all', type=str)
        resolution_filter = request.args.get('resolution', default='all', type=str)
        sort_by = request.args.get('sort', default='title_asc', type=str)

        # Enforce limits
        limit = max(1, min(limit, MAX_BATCH_SIZE))
        offset = max(0, offset)

        logging.info(f"Library data request: limit={limit}, offset={offset}, search='{search_term}', status={status_filter}, duplicates_state={duplicates_state}, type={media_type_filter}, resolution={resolution_filter}, sort={sort_by}")

        # Build SQL query - do everything at database level for speed
        query_start = time.time()
        conn = get_db_connection()
        db_connect_time = time.time() - query_start

        # Base query with deduplication using GROUP BY on tmdb_id/imdb_id
        # This is FAST because it happens in SQL, not Python
        # Exclude ghostlisted items (ghostlisted=1)
        # FIX: Use MAX(collected_at) and MAX(release_date) for correct sorting after GROUP BY
        query = """
            SELECT
                id, title, year, type, tmdb_id, imdb_id, state,
                version, resolution,
                MAX(collected_at) as collected_at,
                size,
                MAX(release_date) as release_date,
                MIN(id) as first_id
            FROM media_items
            WHERE 1=1
            AND (ghostlisted = 0 OR ghostlisted IS NULL)
        """
        params = []
        # Use NULLIF to convert empty strings to NULL for proper COALESCE behavior
        count_query = "SELECT COUNT(DISTINCT COALESCE(NULLIF(tmdb_id, ''), NULLIF(imdb_id, ''), title || year)) FROM media_items WHERE 1=1 AND (ghostlisted = 0 OR ghostlisted IS NULL)"
        count_params = []

        # Status filter
        if status_filter == 'all':
            # Exclude Unreleased items (they belong in Upcoming filter)
            # Also exclude queue states (Final Scrape, Scraping, Wanted)
            # Note: Upgrading is included in 'all' and 'collected' (it means "have it, getting better version")
            query += " AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Wanted')"
            count_query += " AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Wanted')"
        elif status_filter == 'collected':
            # Include Upgrading state (treat as collected - we have it, just upgrading)
            query += " AND state IN ('Collected', 'Upgrading') AND state != 'Unreleased'"
            count_query += " AND state IN ('Collected', 'Upgrading') AND state != 'Unreleased'"
        elif status_filter == 'missing':
            # For movies: No collected/upgrading files AND not ANY duplicates (all duplicates shown in Duplicates filter)
            # For TV shows: Partially collected (at least 1 collected/upgrading, but not all RELEASED episodes)
            # Also exclude Unreleased items (they belong in Upcoming filter) and queue states
            # Note: Upgrading is treated as Collected (we have it, just getting better version)
            # Fixed: Only count released episodes in total (exclude Unreleased from denominator)
            query += """ AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Wanted') AND (
                (type = 'movie' AND state NOT IN ('Collected', 'Upgrading') AND imdb_id NOT IN (
                    SELECT imdb_id
                    FROM media_items
                    WHERE type = 'movie' AND imdb_id IS NOT NULL
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    GROUP BY imdb_id
                    HAVING COUNT(*) > 1
                ))
                OR (type IN ('episode', 'show') AND tmdb_id IN (
                    SELECT DISTINCT tmdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    AND (season_number IS NULL OR season_number != 0)
                    GROUP BY tmdb_id
                    HAVING COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading') THEN season_number || '-' || episode_number END) > 0
                    AND COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading') THEN season_number || '-' || episode_number END) < COUNT(DISTINCT CASE WHEN state != 'Unreleased' THEN season_number || '-' || episode_number END)
                ))
            )"""
            count_query += """ AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Wanted') AND (
                (type = 'movie' AND state NOT IN ('Collected', 'Upgrading') AND imdb_id NOT IN (
                    SELECT imdb_id
                    FROM media_items
                    WHERE type = 'movie' AND imdb_id IS NOT NULL
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    GROUP BY imdb_id
                    HAVING COUNT(*) > 1
                ))
                OR (type IN ('episode', 'show') AND tmdb_id IN (
                    SELECT DISTINCT tmdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    AND (season_number IS NULL OR season_number != 0)
                    GROUP BY tmdb_id
                    HAVING COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading') THEN season_number || '-' || episode_number END) > 0
                    AND COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading') THEN season_number || '-' || episode_number END) < COUNT(DISTINCT CASE WHEN state != 'Unreleased' THEN season_number || '-' || episode_number END)
                ))
            )"""
        elif status_filter == 'blacklist':
            # For movies: state = 'Blacklisted' AND not ANY duplicates (all duplicates shown in Duplicates filter)
            # For TV shows: ALL episodes must be blacklisted (duplicates are shown separately in Duplicates filter)
            # Also exclude Unreleased items (they belong in Upcoming filter) and queue states
            query += """ AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Upgrading', 'Wanted') AND (
                (type = 'movie' AND state = 'Blacklisted' AND imdb_id NOT IN (
                    SELECT imdb_id
                    FROM media_items
                    WHERE type = 'movie' AND imdb_id IS NOT NULL
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    GROUP BY imdb_id
                    HAVING COUNT(*) > 1
                ))
                OR (type IN ('episode', 'show') AND tmdb_id IN (
                    SELECT DISTINCT tmdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    GROUP BY tmdb_id
                    HAVING COUNT(DISTINCT CASE WHEN state = 'Blacklisted' THEN season_number || '-' || episode_number END) = COUNT(DISTINCT season_number || '-' || episode_number)
                    AND COUNT(DISTINCT season_number || '-' || episode_number) > 0
                ))
            )"""
            count_query += """ AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Upgrading', 'Wanted') AND (
                (type = 'movie' AND state = 'Blacklisted' AND imdb_id NOT IN (
                    SELECT imdb_id
                    FROM media_items
                    WHERE type = 'movie' AND imdb_id IS NOT NULL
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    GROUP BY imdb_id
                    HAVING COUNT(*) > 1
                ))
                OR (type IN ('episode', 'show') AND tmdb_id IN (
                    SELECT DISTINCT tmdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    GROUP BY tmdb_id
                    HAVING COUNT(DISTINCT CASE WHEN state = 'Blacklisted' THEN season_number || '-' || episode_number END) = COUNT(DISTINCT season_number || '-' || episode_number)
                    AND COUNT(DISTINCT season_number || '-' || episode_number) > 0
                ))
            )"""
        elif status_filter == 'upcoming':
            # For movies: show if state is 'Unreleased'
            # For TV shows: show if at least one season has episode 1 with state='Unreleased' OR future release_date
            # Note: Use release_date (not airtime) - airtime is just the time portion (e.g., "22:00")
            query += """ AND (
                (type = 'movie' AND state = 'Unreleased')
                OR (type IN ('episode', 'show') AND imdb_id IN (
                    SELECT DISTINCT imdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND episode_number = 1
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    AND (
                        state = 'Unreleased'
                        OR release_date > date('now')
                    )
                ))
            )"""
            count_query += """ AND (
                (type = 'movie' AND state = 'Unreleased')
                OR (type IN ('episode', 'show') AND imdb_id IN (
                    SELECT DISTINCT imdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND episode_number = 1
                    AND (ghostlisted = 0 OR ghostlisted IS NULL)
                    AND (
                        state = 'Unreleased'
                        OR release_date > date('now')
                    )
                ))
            )"""
        elif status_filter == 'duplicates':
            # Show items with duplicate database entries
            # duplicates_state: 'all' = mixed states, 'collected' = only Collected/Upgrading, 'blacklisted' = only Blacklisted
            # Note: Upgrading is treated as Collected (we have it, just getting better version)
            state_filter_sql = ""
            subquery_state_sql = ""
            if duplicates_state == 'collected':
                state_filter_sql = " AND state IN ('Collected', 'Upgrading')"
                subquery_state_sql = " AND state IN ('Collected', 'Upgrading')"
            elif duplicates_state == 'blacklisted':
                state_filter_sql = " AND state = 'Blacklisted'"
                subquery_state_sql = " AND state = 'Blacklisted'"

            query += f""" AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Wanted'){state_filter_sql} AND (
                (type = 'movie' AND imdb_id IN (
                    SELECT imdb_id
                    FROM media_items
                    WHERE type = 'movie' AND imdb_id IS NOT NULL
                    AND (ghostlisted = 0 OR ghostlisted IS NULL){subquery_state_sql}
                    GROUP BY imdb_id
                    HAVING COUNT(*) > 1
                ))
                OR (type IN ('episode', 'show') AND tmdb_id IN (
                    SELECT tmdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND (ghostlisted = 0 OR ghostlisted IS NULL){subquery_state_sql}
                    GROUP BY tmdb_id, season_number, episode_number
                    HAVING COUNT(*) > 1
                ))
            )"""
            count_query += f""" AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Wanted'){state_filter_sql} AND (
                (type = 'movie' AND imdb_id IN (
                    SELECT imdb_id
                    FROM media_items
                    WHERE type = 'movie' AND imdb_id IS NOT NULL
                    AND (ghostlisted = 0 OR ghostlisted IS NULL){subquery_state_sql}
                    GROUP BY imdb_id
                    HAVING COUNT(*) > 1
                ))
                OR (type IN ('episode', 'show') AND tmdb_id IN (
                    SELECT tmdb_id
                    FROM media_items
                    WHERE type = 'episode'
                    AND (ghostlisted = 0 OR ghostlisted IS NULL){subquery_state_sql}
                    GROUP BY tmdb_id, season_number, episode_number
                    HAVING COUNT(*) > 1
                ))
            )"""
        elif status_filter == 'upgraded':
            query += " AND upgraded = ? AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Upgrading', 'Wanted')"
            count_query += " AND upgraded = ? AND state NOT IN ('Unreleased', 'Final Scrape', 'Scraping', 'Upgrading', 'Wanted')"
            params.append(1)
            count_params.append(1)
        elif status_filter == 'broken':
            # Collected/Upgrading items with no Plex match (ms_item_id is NULL)
            query += " AND state IN ('Collected', 'Upgrading') AND (ms_item_id IS NULL OR ms_item_id = '')"
            count_query += " AND state IN ('Collected', 'Upgrading') AND (ms_item_id IS NULL OR ms_item_id = '')"
        elif status_filter == 'nas':
            # Collected items stored on NAS/network drives (location_on_disk matches configured NAS prefixes)
            nas_paths = get_nas_paths()
            if nas_paths:
                nas_conditions = " OR ".join(["location_on_disk LIKE ?" for _ in nas_paths])
                query += f" AND state IN ('Collected', 'Upgrading') AND ({nas_conditions})"
                count_query += f" AND state IN ('Collected', 'Upgrading') AND ({nas_conditions})"
                for p in nas_paths:
                    params.append(p.rstrip('/') + '/%')
                    count_params.append(p.rstrip('/') + '/%')
            else:
                # No NAS paths configured — return nothing
                query += " AND 1=0"
                count_query += " AND 1=0"
        else:
            # Default to Collected and Partial for unknown filter values
            query += " AND state IN (?, ?)"
            count_query += " AND state IN (?, ?)"
            params.extend(['Collected', 'Partial'])
            count_params.extend(['Collected', 'Partial'])

        # Media type filter
        if media_type_filter == 'movie':
            query += " AND type = ?"
            count_query += " AND type = ?"
            params.append('movie')
            count_params.append('movie')
        elif media_type_filter == 'show':
            query += " AND type IN (?, ?)"
            count_query += " AND type IN (?, ?)"
            params.extend(['episode', 'show'])
            count_params.extend(['episode', 'show'])

        # Resolution filter
        if resolution_filter != 'all':
            if resolution_filter == 'unknown':
                query += " AND (resolution IS NULL OR resolution = '')"
                count_query += " AND (resolution IS NULL OR resolution = '')"
            else:
                query += " AND resolution = ?"
                count_query += " AND resolution = ?"
                params.append(resolution_filter)
                count_params.append(resolution_filter)

        # Search filter
        if search_term:
            query += " AND title LIKE ?"
            count_query += " AND title LIKE ?"
            search_param = f"%{search_term}%"
            params.append(search_param)
            count_params.append(search_param)

        # Group by tmdb_id (or imdb_id if no tmdb_id) to deduplicate
        # Use NULLIF to convert empty strings to NULL for proper COALESCE behavior
        query += " GROUP BY COALESCE(NULLIF(tmdb_id, ''), NULLIF(imdb_id, ''), title || year)"

        # Sorting
        # FIX: Use MAX() aggregate functions for fields used after GROUP BY
        sort_field, sort_order = parse_sort(sort_by)
        if sort_field == 'title':
            query += f" ORDER BY title COLLATE NOCASE {sort_order.upper()}"
        elif sort_field == 'year':
            query += f" ORDER BY year {sort_order.upper()}"
        elif sort_field == 'collected_at':
            # Use MAX(collected_at) since we're grouping
            query += f" ORDER BY MAX(collected_at) {sort_order.upper()}"
        elif sort_field == 'release_date':
            # For release date sorting, put NULL and "Unknown" values last regardless of sort order
            # Use MAX(release_date) since we're grouping
            if sort_order == 'asc':
                query += " ORDER BY CASE WHEN MAX(release_date) IS NULL OR MAX(release_date) = 'Unknown' THEN 1 ELSE 0 END, MAX(release_date) ASC"
            else:
                query += " ORDER BY CASE WHEN MAX(release_date) IS NULL OR MAX(release_date) = 'Unknown' THEN 1 ELSE 0 END, MAX(release_date) DESC"
        else:
            query += " ORDER BY title COLLATE NOCASE ASC"

        # Pagination — fetch one extra row to detect has_more without a separate COUNT query
        query += " LIMIT ? OFFSET ?"
        params.extend([limit + 1, offset])

        # Execute query
        query_exec_start = time.time()
        cursor = conn.execute(query, params)
        items = [dict(row) for row in cursor.fetchall()]
        query_exec_time = time.time() - query_exec_start

        # Determine has_more and total using the n+1 trick (no separate COUNT query)
        has_more = len(items) > limit
        if has_more:
            items = items[:limit]  # Drop the sentinel row

        # On the first page (offset=0), run the count query once so the UI can show "X/Y" from the start.
        # On subsequent pages keep total_count=None to avoid repeated COUNT queries.
        if offset == 0:
            total_count = conn.execute(count_query, count_params).fetchone()[0]
        elif not has_more:
            total_count = offset + len(items)  # Exact total on the last page
        else:
            total_count = None  # Still paginating — total already known from first page

        # Batch-fetch episode counts for all shows in one query (PERFORMANCE OPTIMIZATION)
        episode_start = time.time()
        episode_counts = {}
        show_tmdb_ids = [
            item.get('tmdb_id') for item in items
            if item.get('tmdb_id') and item.get('type') in ['episode', 'show']
        ]

        if show_tmdb_ids:
            # Remove duplicates
            unique_show_ids = list(set(show_tmdb_ids))
            placeholders = ','.join(['?'] * len(unique_show_ids))

            # Single query to get episode counts for ALL shows at once
            # Count unique episodes (not individual files) by grouping on season_number and episode_number
            # Note: Upgrading is treated as Collected (we have it, just getting better version)
            episode_count_query = f"""
                SELECT
                    tmdb_id,
                    COUNT(DISTINCT season_number || '-' || episode_number) as total_episodes,
                    COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading') THEN season_number || '-' || episode_number END) as collected_episodes,
                    COUNT(DISTINCT CASE WHEN state = 'Blacklisted' THEN season_number || '-' || episode_number END) as blacklisted_episodes,
                    COUNT(DISTINCT CASE WHEN state = 'Unreleased' THEN season_number || '-' || episode_number END) as unreleased_episodes,
                    COUNT(DISTINCT CASE WHEN state IN ('Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping') THEN season_number || '-' || episode_number END) as wanted_episodes,
                    SUM(CASE WHEN state IN ('Collected', 'Upgrading') THEN size ELSE 0 END) as total_size
                FROM media_items
                WHERE tmdb_id IN ({placeholders}) AND type = 'episode'
                GROUP BY tmdb_id
            """

            episode_cursor = conn.execute(episode_count_query, unique_show_ids)
            for row in episode_cursor:
                episode_counts[row['tmdb_id']] = {
                    'collected': row['collected_episodes'],
                    'total': row['total_episodes'],
                    'blacklisted': row['blacklisted_episodes'],
                    'unreleased': row['unreleased_episodes'],
                    'wanted': row['wanted_episodes'],
                    'total_size': row['total_size']
                }
            episode_cursor.close()
        episode_time = time.time() - episode_start

        # Batch-fetch upcoming episode release dates for TV shows (PERFORMANCE OPTIMIZATION)
        upcoming_release_start = time.time()
        upcoming_release_dates = {}
        if show_tmdb_ids:
            # Single query to get earliest upcoming episode release date for ALL shows at once
            upcoming_release_query = f"""
                SELECT
                    tmdb_id,
                    MIN(release_date) as release_date
                FROM media_items
                WHERE tmdb_id IN ({placeholders})
                AND type = 'episode'
                AND episode_number = 1
                AND (state = 'Unreleased' OR release_date > date('now'))
                AND (ghostlisted = 0 OR ghostlisted IS NULL)
                GROUP BY tmdb_id
            """

            release_cursor = conn.execute(upcoming_release_query, unique_show_ids)
            for row in release_cursor:
                if row['release_date']:
                    upcoming_release_dates[row['tmdb_id']] = row['release_date']
            release_cursor.close()
        upcoming_release_time = time.time() - upcoming_release_start

        # Format response
        format_start = time.time()

        # Load poster cache ONCE for all items (PERFORMANCE OPTIMIZATION)
        poster_cache_load_start = time.time()
        from routes.poster_cache import load_cache
        poster_cache = load_cache()
        poster_cache_load_time = time.time() - poster_cache_load_start

        # Identify items missing posters for background fetching
        missing_poster_items = []
        formatted_items = []
        for item in items:
            formatted_item = format_library_item(item, episode_counts, poster_cache, upcoming_release_dates)
            formatted_items.append(formatted_item)

            # Track items without posters for potential background fetch
            if formatted_item.get('poster_path') is None and formatted_item.get('tmdb_id'):
                missing_poster_items.append({
                    'tmdb_id': formatted_item['tmdb_id'],
                    'media_type': formatted_item['type']
                })

        # Log missing posters for monitoring
        if missing_poster_items:
            logging.debug(f"Found {len(missing_poster_items)} items without cached posters")

        format_time = time.time() - format_start

        total_time = time.time() - start_time
        logging.info(f"Returning {len(formatted_items)} items (has_more={has_more}, total={total_count})")
        logging.info(f"Performance: total={total_time:.3f}s, db_connect={db_connect_time:.3f}s, query={query_exec_time:.3f}s, episodes={episode_time:.3f}s, upcoming_release={upcoming_release_time:.3f}s, poster_cache_load={poster_cache_load_time:.3f}s, format={format_time:.3f}s")

        return jsonify({
            'success': True,
            'items': formatted_items,
            'has_more': has_more,
            'total': total_count,
            'offset': offset,
            'limit': limit
        })

    except Exception as e:
        logging.error(f"Error in library data endpoint: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'items': [],
            'has_more': False,
            'total': 0
        }), 500
    finally:
        if conn:
            conn.close()

def parse_sort(sort_by):
    """Parse sort parameter into field and order"""
    sort_map = {
        'title_asc': ('title', 'asc'),
        'title_desc': ('title', 'desc'),
        'year_asc': ('year', 'asc'),
        'year_desc': ('year', 'desc'),
        'added_desc': ('collected_at', 'desc'),
        'added_asc': ('collected_at', 'asc'),
        'release_asc': ('release_date', 'asc'),
        'release_desc': ('release_date', 'desc'),
    }
    return sort_map.get(sort_by, ('title', 'asc'))

def sort_items(items, field, order):
    """Sort items by field and order"""
    reverse = (order == 'desc')

    if field == 'title':
        return sorted(items, key=lambda x: (x.get('title') or '').lower(), reverse=reverse)
    elif field == 'year':
        return sorted(items, key=lambda x: x.get('year') or 0, reverse=reverse)
    elif field == 'collected_at':
        return sorted(items, key=lambda x: x.get('collected_at') or '', reverse=reverse)

    return items

def format_library_item(item, episode_counts=None, poster_cache=None, upcoming_release_dates=None):
    """
    Format a media item for library display
    Returns dict with necessary fields for frontend

    Args:
        item: Database row as dict
        episode_counts: Pre-fetched episode counts dict {tmdb_id: {'collected': int, 'total': int}}
        poster_cache: Pre-loaded poster cache dict (for performance)
        upcoming_release_dates: Pre-fetched upcoming episode release dates dict {tmdb_id: 'YYYY-MM-DD'}
    """
    # Get item type
    item_type = item.get('type', 'movie')
    if item_type == 'episode':
        item_type = 'show'

    # Build poster path using poster cache system
    # PRIORITY: TMDB first (clean posters), Plex second (may have overlays)
    poster_path = None
    tmdb_id = item.get('tmdb_id')
    imdb_id = item.get('imdb_id')

    # Helper function to extract poster path from cached URL
    def extract_poster_from_cache(cached_url):
        if not cached_url:
            return None
        # PRIORITY 1: Check if it's a TMDB URL (clean, no overlays)
        if 'image.tmdb.org' in cached_url:
            # TMDB URL: Extract just the path part after /t/p/
            parts = cached_url.split('/t/p/')
            if len(parts) > 1:
                # Get everything after /t/p/ (e.g., "w300/abc123.jpg")
                full_path = parts[1]
                # Extract just the filename (e.g., "/abc123.jpg")
                if '/' in full_path:
                    return '/' + full_path.split('/', 1)[1]
        # PRIORITY 2: Fall back to Plex URL (may have overlays)
        elif cached_url.startswith('/library/'):
            # Plex URL: Mark it with a special prefix so frontend knows to use Plex proxy
            return f"plex:{cached_url}"
        else:
            # Unknown format - use as-is
            return cached_url
        return None

    # Get poster from poster cache - try TMDB ID first, then IMDB ID
    if poster_cache is not None:
        try:
            from routes.poster_cache import normalize_media_type
            from datetime import datetime, timedelta

            # Determine media type for poster cache
            media_type = 'tv' if item_type == 'show' else 'movie'
            normalized_type = normalize_media_type(media_type)

            cached_url = None

            # Try TMDB ID first
            if tmdb_id:
                cache_key = f"{tmdb_id}_{normalized_type}"
                cache_item = poster_cache.get(cache_key)
                if cache_item:
                    url, timestamp = cache_item
                    # Check if not expired (7 days)
                    if datetime.now() - timestamp < timedelta(days=7):
                        cached_url = url

            # If no poster from TMDB ID, try IMDB ID
            if not cached_url and imdb_id:
                cache_key = f"{imdb_id}_{normalized_type}"
                cache_item = poster_cache.get(cache_key)
                if cache_item:
                    url, timestamp = cache_item
                    if datetime.now() - timestamp < timedelta(days=7):
                        cached_url = url

            if cached_url:
                poster_path = extract_poster_from_cache(cached_url)
            # Note: If no cached poster, poster_path remains None
            # Frontend will display placeholder image or fetch from TMDB on-demand
        except Exception as e:
            logging.debug(f"Could not get poster for {tmdb_id or imdb_id}: {e}")
            poster_path = None

    # Determine status
    state = item.get('state', '')
    upgraded = item.get('upgraded', False)

    if state == 'Collected':
        status = 'collected'
        status_label = 'Collected'
    elif state == 'Partial':
        status = 'partial'
        status_label = 'Partial'
    elif state == 'Unreleased':
        status = 'upcoming'
        status_label = 'Upcoming'
    elif upgraded:
        status = 'upgraded'
        status_label = 'Upgraded'
    else:
        status = 'missing'
        status_label = 'Missing'

    # Get episode counts for shows (using pre-fetched data for performance)
    episode_info = None
    total_size = None
    if item_type == 'show' and tmdb_id:
        if episode_counts and tmdb_id in episode_counts:
            # Use pre-fetched episode counts (fast - no DB query)
            episode_info = episode_counts[tmdb_id]
            total_size = episode_info.get('total_size')
        else:
            # Fallback: if not in batch results, set to zero
            # This shouldn't happen if batch query worked correctly
            episode_info = {
                'collected': 0,
                'total': 0
            }
            total_size = 0
    else:
        # For movies, use the size from the item
        total_size = item.get('size')

    # Determine release date to use
    # For TV shows, prefer the upcoming episode's release date if available
    release_date = item.get('release_date')
    if item_type == 'show' and upcoming_release_dates and tmdb_id in upcoming_release_dates:
        release_date = upcoming_release_dates[tmdb_id]
    
    # Clean release_date to extract just the date part (YYYY-MM-DD)
    if release_date:
        release_date = str(release_date).split('T')[0].split(' ')[0]

    return {
        'id': item.get('id'),
        'title': item.get('title', 'Unknown Title'),
        'year': item.get('year'),
        'type': item_type,
        'tmdb_id': tmdb_id,
        'imdb_id': item.get('imdb_id'),
        'poster_path': poster_path,
        'state': state,  # Include raw state from database
        'status': status,
        'status_label': status_label,
        'episode_info': episode_info,
        'version': item.get('version'),
        'resolution': item.get('resolution'),
        'collected_at': item.get('collected_at'),
        'size': total_size,  # Total size in GB (sum of all episodes for shows, single file for movies)
        'release_date': release_date,  # Release date (upcoming episode date for shows, movie release date for movies)
    }

@library_bp.route('/show/<media_id>')
@user_required
@onboarding_required
def show_detail(media_id):
    """
    Show detail page - displays all seasons and episodes for a TV show
    media_id can be either tmdb_id or imdb_id
    """
    from utilities.web_scraper import get_available_versions

    # Check if either Plex or TMDB is configured
    plex_url = get_setting('Plex', 'url', default='')
    plex_token = get_setting('Plex', 'token', default='')
    tmdb_api_key = get_setting('TMDB', 'api_key', default='')

    plex_configured = bool(plex_url and plex_token)
    tmdb_configured = bool(tmdb_api_key)
    versions = get_available_versions()

    # Check user permissions - if auth is disabled, grant all permissions
    from .utils import is_user_system_enabled
    if not is_user_system_enabled():
        has_admin_permissions = True
        has_user_permissions = True
    else:
        has_admin_permissions = current_user.is_authenticated and current_user.role == 'admin'
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('library_show.html',
                         media_id=media_id,
                         plex_configured=plex_configured,
                         tmdb_configured=tmdb_configured,
                         versions=versions,
                         has_admin_permissions=has_admin_permissions,
                         has_user_permissions=has_user_permissions)

@library_bp.route('/show/<media_id>/data')
@user_required
def show_detail_data(media_id):
    """
    API endpoint to fetch show details and all episodes
    Returns show metadata and episodes grouped by season
    media_id can be either tmdb_id or imdb_id
    """
    try:
        conn = get_db_connection()

        # Try to find show by tmdb_id, imdb_id, or id
        # First try tmdb_id and imdb_id (string fields)
        # Note: TV show episodes have type='episode', not 'show'
        show_query = """
            SELECT
                id,
                title,
                year,
                tmdb_id,
                imdb_id,
                version,
                location_on_disk,
                collected_at
            FROM media_items
            WHERE (tmdb_id = ? OR imdb_id = ?) AND type = 'episode'
            LIMIT 1
        """
        show_data = conn.execute(show_query, (str(media_id), str(media_id))).fetchone()

        # If not found and media_id is numeric, try by database id
        if not show_data and str(media_id).isdigit():
            show_query = """
                SELECT
                    id,
                    title,
                    year,
                    tmdb_id,
                    imdb_id,
                    version,
                    location_on_disk,
                    collected_at
                FROM media_items
                WHERE id = ? AND type = 'episode'
                LIMIT 1
            """
            show_data = conn.execute(show_query, (int(media_id),)).fetchone()

        if not show_data:
            # Debug: Check if show exists with different criteria
            debug_query = "SELECT COUNT(*) as count, type FROM media_items WHERE tmdb_id = ? OR imdb_id = ? GROUP BY type"
            debug_result = conn.execute(debug_query, (str(media_id), str(media_id))).fetchall()

            logging.warning(f"Show {media_id} not found. Debug info: {debug_result}")

            return jsonify({
                'success': False,
                'error': f'Show not found in library. ID: {media_id}'
            }), 404

        # Get all episodes for this show - use whichever ID is available
        # Prefer tmdb_id for grouping episodes, fall back to imdb_id, then database id
        # This ensures all episodes of the same show are grouped together
        if show_data['tmdb_id']:
            id_field = 'tmdb_id'
            id_value = show_data['tmdb_id']
        elif show_data['imdb_id']:
            id_field = 'imdb_id'
            id_value = show_data['imdb_id']
        elif show_data['id']:
            id_field = 'id'
            id_value = show_data['id']
        else:
            logging.error(f"Show {media_id} found but has no valid ID fields for grouping episodes")
            return jsonify({
                'success': False,
                'error': 'Show found but has no valid ID for grouping episodes'
            }), 500

        episodes_query = f"""
            SELECT
                id,
                season_number,
                episode_number,
                episode_title,
                state,
                version,
                filled_by_file,
                filled_by_magnet,
                collected_at,
                release_date,
                airtime,
                content_source,
                content_source_detail,
                imdb_id,
                tmdb_id,
                location_basename,
                location_on_disk,
                ghostlisted,
                size,
                manual_replace,
                ms_item_id,
                ms_audio_track,
                ms_subtitle_track
            FROM media_items
            WHERE {id_field} = ? AND type = 'episode' AND (ghostlisted = 0 OR ghostlisted IS NULL)
            ORDER BY season_number ASC, episode_number ASC
        """
        episodes_raw = conn.execute(episodes_query, (id_value,)).fetchall()
        conn.close()

        # Collect unique content sources from episodes
        content_sources = set()
        for ep in episodes_raw:
            if ep['content_source']:
                content_sources.add(ep['content_source'])

        # Get content source display names
        from queues.config_manager import get_content_source_display_names
        content_source_display_map = get_content_source_display_names()
        content_source_display_list = [
            content_source_display_map.get(src, src) for src in sorted(content_sources)
        ]

        # Get rclone mount path from settings with intelligent content folder handling
        from utilities.path_utils import get_mount_path_for_content
        rclone_shows_path = get_mount_path_for_content(media_type='show')

        # Group episodes by season
        seasons = {}
        for ep in episodes_raw:
            season_num = ep['season_number'] or 0

            if season_num not in seasons:
                seasons[season_num] = {
                    'season_number': season_num,
                    'episodes': []
                }

            # An episode can have multiple entries (different files/versions)
            # Group by episode_number
            episode_data = {
                'id': ep['id'],
                'episode_number': ep['episode_number'],
                'episode_title': ep['episode_title'],
                'state': ep['state'],
                'version': ep['version'],
                'filled_by_file': ep['filled_by_file'],
                'filled_by_magnet': ep['filled_by_magnet'],
                'collected_at': ep['collected_at'],
                'release_date': ep['release_date'],
                'airtime': ep['airtime'],
                'imdb_id': ep['imdb_id'],
                'tmdb_id': ep['tmdb_id'],
                'location_basename': ep['location_basename'],
                'location_on_disk': ep['location_on_disk'],
                'ghostlisted': ep['ghostlisted'],
                'size': ep['size'],
                'manual_replace': bool(ep['manual_replace']),
                'ms_item_id': ep['ms_item_id'],
                'ms_audio_track': ep['ms_audio_track'],
                'ms_subtitle_track': ep['ms_subtitle_track'],
            }

            seasons[season_num]['episodes'].append(episode_data)

        # Convert seasons dict to list and sort
        # Add has_pending_replace flag to each season
        for season in seasons.values():
            season['has_pending_replace'] = any(ep.get('manual_replace') for ep in season['episodes'])
        seasons_list = sorted(seasons.values(), key=lambda x: x['season_number'])

        # Initialize metadata variables
        overview = None
        genres = None
        network = None
        status = None
        rating = None
        vote_count = None
        tmdb_poster_path = None
        tmdb_backdrop_path = None
        tmdb_id = show_data['tmdb_id']
        tvdb_id = None  # TODO: Extract from database if available
        tvdb_slug = None  # TVDB slug for correct URL format
        number_of_seasons = 0  # Total seasons from TMDB for phantom season detection

        # If no TMDB ID but IMDB ID exists, try to look it up
        if not tmdb_id and show_data['imdb_id']:
            from utilities.web_scraper import get_tmdb_id_from_imdb
            logging.info(f"No TMDB ID for show {show_data['title']}, attempting lookup via IMDB ID {show_data['imdb_id']}")
            tmdb_id = get_tmdb_id_from_imdb(show_data['imdb_id'], 'tv')
            if tmdb_id:
                logging.info(f"Found TMDB ID {tmdb_id} for show {show_data['title']} via IMDB lookup")

        # Try Battery first for basic metadata (overview, genres, network, status)
        battery_metadata = None
        if show_data['imdb_id']:
            try:
                from cli_battery.app.direct_api import DirectAPI
                logging.info(f"Fetching metadata from Battery for show {show_data['imdb_id']}")
                battery_metadata, source = DirectAPI.get_show_metadata(show_data['imdb_id'])
                if battery_metadata:
                    logging.info(f"Battery metadata retrieved from {source}")
                    overview = battery_metadata.get('overview', '')
                    genres_list = battery_metadata.get('genres', [])
                    if isinstance(genres_list, list):
                        genres = ', '.join(genres_list) if genres_list else None
                    else:
                        genres = genres_list
                    network = battery_metadata.get('network', '')
                    status = battery_metadata.get('status', '')
                    # Get TVDB ID and slug from Battery metadata
                    battery_ids = battery_metadata.get('ids', {})
                    if isinstance(battery_ids, dict):
                        tvdb_id = battery_ids.get('tvdb')
                        tvdb_slug = battery_ids.get('slug')  # Get TVDB slug for proper URL format
                    logging.info(f"Battery metadata - overview: {len(overview) if overview else 0} chars, genres: {genres}, network: {network}, status: {status}, tvdb_slug: {tvdb_slug}")
            except Exception as e:
                logging.warning(f"Battery metadata fetch failed for {show_data['imdb_id']}: {e}")

        # Always fetch TMDB for ratings and vote counts (not available from Battery)
        # Also used as fallback if Battery didn't provide basic metadata
        if tmdb_id:
            tmdb_api_key = get_setting('TMDB', 'api_key')
            if tmdb_api_key:
                try:
                    details_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    if battery_metadata:
                        logging.info(f"Fetching TMDB ratings for show {tmdb_id} (to supplement Battery data)")
                    else:
                        logging.info(f"Fetching TMDB metadata for show {tmdb_id} (Battery unavailable)")
                    details_response = requests.get(details_url, timeout=15, headers={'Accept-Encoding': 'identity'})
                    details_response.raise_for_status()
                    details_data = details_response.json()

                    # Always get ratings from TMDB
                    rating = details_data.get('vote_average')  # TMDB rating (0-10)
                    vote_count = details_data.get('vote_count')  # Number of votes

                    # Get poster and backdrop paths from TMDB
                    tmdb_poster_path = details_data.get('poster_path')
                    tmdb_backdrop_path = details_data.get('backdrop_path')

                    # Get total number of seasons for phantom season detection
                    number_of_seasons = details_data.get('number_of_seasons', 0)

                    # If Battery didn't provide data, use TMDB for everything
                    if not battery_metadata:
                        overview = details_data.get('overview', '')
                        genres_list = details_data.get('genres', [])
                        genres = ', '.join([g['name'] for g in genres_list]) if genres_list else None

                        # Get network (first network if multiple)
                        networks_list = details_data.get('networks', [])
                        if networks_list:
                            network = networks_list[0].get('name', '')

                        status = details_data.get('status', '')

                    # Get TVDB ID if not already from Battery
                    if not tvdb_id:
                        tvdb_id = details_data.get('external_ids', {}).get('tvdb_id')

                    logging.info(f"TMDB data: rating: {rating}, vote_count: {vote_count}, poster: {tmdb_poster_path}, backdrop: {tmdb_backdrop_path}")

                except Exception as e:
                    logging.error(f"Error fetching TMDB metadata for show {tmdb_id}: {e}")
            else:
                logging.warning(f"TMDB API key not configured, skipping ratings fetch for show {tmdb_id}")
        elif not battery_metadata:
            logging.warning(f"No TMDB ID or IMDb ID available for show {show_data['title']}, skipping metadata fetch")

        # Add phantom seasons for missing seasons if TMDB data is available
        if number_of_seasons > 0 and tmdb_id and tmdb_api_key:
            try:
                existing_season_numbers = set(s['season_number'] for s in seasons_list)
                missing_seasons = []

                # Find missing seasons (1 to number_of_seasons, excluding Season 0/Specials)
                for season_num in range(1, number_of_seasons + 1):
                    if season_num not in existing_season_numbers:
                        missing_seasons.append(season_num)

                if missing_seasons:
                    logging.info(f"Found {len(missing_seasons)} missing season(s): {missing_seasons}")

                    # Fetch episode counts for missing seasons from TMDB
                    for season_num in missing_seasons:
                        try:
                            season_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}?api_key={tmdb_api_key}&language=en-US"
                            logging.info(f"Fetching phantom season {season_num} details from TMDB")
                            season_response = requests.get(season_url, timeout=10)
                            season_response.raise_for_status()
                            season_data = season_response.json()

                            # Get episode count for this season
                            episodes_in_season = season_data.get('episodes', [])
                            episode_count = len(episodes_in_season)

                            if episode_count > 0:
                                # Create phantom season with phantom episodes
                                phantom_episodes = []
                                for ep_num in range(1, episode_count + 1):
                                    phantom_episodes.append({
                                        'id': None,
                                        'episode_number': ep_num,
                                        'episode_title': f'Episode {ep_num}',
                                        'state': 'Missing',
                                        'version': None,
                                        'filled_by_file': None,
                                        'collected_at': None,
                                        'release_date': None,
                                        'airtime': None,
                                        'imdb_id': None,
                                        'tmdb_id': None,
                                        'location_basename': None,
                                        'location_on_disk': None,
                                        'ghostlisted': None,
                                        'size': None,
                                        'is_phantom': True
                                    })

                                phantom_season = {
                                    'season_number': season_num,
                                    'is_phantom_season': True,
                                    'episodes': phantom_episodes
                                }

                                seasons_list.append(phantom_season)
                                logging.info(f"Created phantom season {season_num} with {episode_count} episodes")
                            else:
                                logging.warning(f"Season {season_num} has no episodes according to TMDB, skipping")

                        except requests.exceptions.RequestException as e:
                            logging.error(f"Failed to fetch TMDB data for phantom season {season_num}: {e}")
                            continue
                        except Exception as e:
                            logging.error(f"Error creating phantom season {season_num}: {e}")
                            continue

                    # Re-sort seasons list after adding phantom seasons
                    seasons_list = sorted(seasons_list, key=lambda x: x['season_number'])

            except Exception as e:
                logging.error(f"Error in phantom season creation: {e}")
                # Continue without phantom seasons if there's an error

        # Get poster and backdrop URLs from cache, with TMDB fallback
        from routes.poster_cache import get_cached_poster_url
        cache_id = tmdb_id or show_data['imdb_id'] or media_id
        poster_url = get_cached_poster_url(cache_id, 'tv')
        backdrop_url = get_cached_poster_url(f"{cache_id}_backdrop", 'tv')

        # If not in cache, use TMDB paths directly
        if not poster_url and tmdb_poster_path:
            poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_poster_path}"
            logging.info(f"Using TMDB poster path directly (cache empty): {poster_url}")
        if not backdrop_url and tmdb_backdrop_path:
            backdrop_url = f"https://image.tmdb.org/t/p/original{tmdb_backdrop_path}"
            logging.info(f"Using TMDB backdrop path directly (cache empty): {backdrop_url}")

        # Extract storage path from first episode's filled_by_file or use location_on_disk
        storage_path = show_data['location_on_disk'] if show_data['location_on_disk'] else None
        season_folders = None
        if not storage_path and episodes_raw and episodes_raw[0]['filled_by_file']:
            first_file = episodes_raw[0]['filled_by_file']
            # Extract directory from file path
            import os
            file_dir = os.path.dirname(first_file)

            # Check if using season folders (contains "Season XX" or "S0X")
            import re
            season_folder_pattern = r'[/\\](Season\s*\d+|S\d{2})[/\\]'
            season_folders = bool(re.search(season_folder_pattern, first_file, re.IGNORECASE))

            # Get the show's root directory (go up one level if season folders exist)
            if season_folders:
                storage_path = os.path.dirname(file_dir)
            else:
                storage_path = file_dir

        # Extract path up to content folder (works for any depth, both Plex and Symlink modes)
        from utilities.path_utils import get_content_folder_path
        display_path = get_content_folder_path(storage_path, media_type='show') if storage_path else storage_path

        # Find the added date: use collected_at from first episode of season 1
        added_date = None
        if seasons_list:
            # Find season 1
            season_1 = next((s for s in seasons_list if s['season_number'] == 1), None)
            if season_1 and season_1['episodes']:
                # Sort episodes by episode_number and get the first one with collected_at
                sorted_episodes = sorted(season_1['episodes'], key=lambda ep: ep.get('episode_number', 999))
                first_ep_with_date = next((ep for ep in sorted_episodes if ep.get('collected_at')), None)
                if first_ep_with_date:
                    added_date = first_ep_with_date['collected_at']

        # Calculate total size from all episodes
        total_size = 0
        for season in seasons_list:
            for episode in season['episodes']:
                episode_size = episode.get('size')
                if episode_size is not None:
                    total_size += episode_size

        # Check if there's an upcoming episode within 2 weeks (override status to "Airing")
        from datetime import datetime, timedelta
        from dateutil import parser as date_parser

        has_upcoming_episode = False
        try:
            now = datetime.now()
            two_weeks_from_now = now + timedelta(days=14)

            for season in seasons_list:
                for episode in season['episodes']:
                    # Only use release_date for upcoming episode checks
                    # Skip airtime-only entries as they default to today's date and cause false positives
                    air_date_str = episode.get('release_date')
                    if air_date_str:
                        try:
                            # Parse the air date using dateutil parser for better compatibility
                            air_date = date_parser.parse(str(air_date_str))
                            # Remove timezone info for comparison
                            if air_date.tzinfo:
                                air_date = air_date.replace(tzinfo=None)

                            logging.info(f"[STATUS CHECK] S{season['season_number']}E{episode.get('episode_number')} - Air date: {air_date}, In range: {now <= air_date <= two_weeks_from_now}")

                            # Check if episode airs within the next 2 weeks
                            if now <= air_date <= two_weeks_from_now:
                                has_upcoming_episode = True
                                logging.info(f"[STATUS CHECK] Found upcoming episode! S{season['season_number']}E{episode.get('episode_number')} airs on {air_date}")
                                break
                        except (ValueError, AttributeError, TypeError) as e:
                            logging.debug(f"Could not parse air date '{air_date_str}': {e}")
                            continue
                if has_upcoming_episode:
                    break

            # Override status if there's an upcoming episode
            if has_upcoming_episode:
                logging.info(f"[STATUS CHECK] Overriding status to 'Airing' (found upcoming episode)")
                status = "Airing"
            else:
                logging.info(f"[STATUS CHECK] No upcoming episodes found, keeping TMDB status: {status}")
        except Exception as e:
            logging.error(f"Error checking upcoming episodes: {e}")
            # Continue without overriding status if there's an error

        # Get auto-ghostlist setting
        auto_ghostlist_enabled = get_setting('Library Manager', 'ghostlist_mode', False)

        # Fetch certification based on user's preferred region
        certification = ''
        if show_data['tmdb_id'] and tmdb_api_key:
            certification_region = get_setting('TMDB', 'certification_region', 'US')
            certification = get_certification(show_data['tmdb_id'], 'tv', tmdb_api_key, certification_region)

        return jsonify({
            'success': True,
            'show': {
                'title': show_data['title'],
                'year': show_data['year'],
                'tmdb_id': show_data['tmdb_id'],
                'imdb_id': show_data['imdb_id'],
                'tvdb_id': tvdb_id,
                'tvdb_slug': tvdb_slug,
                'version': show_data['version'],
                'poster_url': poster_url,
                'backdrop_url': backdrop_url,
                'overview': overview,
                'genres': genres,
                'certification': certification,
                'network': network,
                'status': status,
                'rating': rating,
                'vote_count': vote_count,
                'path': display_path,
                'season_folders': season_folders,
                'added_date': added_date,
                'content_sources': content_source_display_list,
                'rclone_path': rclone_shows_path,
                'total_size': total_size,
                'auto_ghostlist_enabled': auto_ghostlist_enabled
            },
            'seasons': seasons_list
        })

    except Exception as e:
        logging.error(f"Error fetching show details for {media_id}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/check_broken_files', methods=['POST'])
@user_required
def check_broken_files():
    """
    Check for broken files in the __unplayable__ folder
    Returns a set of broken filenames
    """
    try:
        import os

        # Get rclone mount path from settings
        rclone_mount_root = get_setting('Plex', 'mounted_file_location', default='')

        if not rclone_mount_root:
            return jsonify({
                'success': True,
                'broken_files': []
            })

        # Build path to __unplayable__ folder
        # Convert mount path like /media/mount/__all__ to /media/mount
        mount_root = rclone_mount_root.replace('/__all__', '')
        unplayable_path = os.path.join(mount_root, '__unplayable__')
        bad_path = os.path.join(mount_root, '__bad__')

        broken_files = []

        # Check if __unplayable__ folder exists
        if os.path.exists(unplayable_path) and os.path.isdir(unplayable_path):
            # List all files in the __unplayable__ folder
            for filename in os.listdir(unplayable_path):
                file_path = os.path.join(unplayable_path, filename)
                if os.path.isfile(file_path):
                    broken_files.append(filename)

            logging.info(f"Found {len(broken_files)} broken files in {unplayable_path}")

        if os.path.exists(bad_path) and os.path.isdir(bad_path):
            # List all files in the __bad__ folder
            for filename in os.listdir(bad_path):
                file_path = os.path.join(bad_path, filename)
                if os.path.isfile(file_path):
                    broken_files.append(filename)

            logging.info(f"Found {len(broken_files)} broken files in {bad_path}")

        return jsonify({
            'success': True,
            'broken_files': broken_files
        })

    except Exception as e:
        logging.error(f"Error checking broken files: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'broken_files': []
        }), 500

@library_bp.route('/move_missing_to_wanted', methods=['POST'])
@user_required
def move_missing_to_wanted():
    """
    Move all missing episodes (Blacklisted, Error, Ghostlisted states) to Wanted state
    for a specific show
    """
    try:
        from database.database_writing import update_media_item_state

        data = request.json
        imdb_id = data.get('imdb_id')
        tmdb_id = data.get('tmdb_id')

        if not imdb_id and not tmdb_id:
            return jsonify({
                'success': False,
                'error': 'Either imdb_id or tmdb_id is required'
            }), 400

        # Get database connection
        db_content = get_db_connection()
        cursor = db_content.cursor()

        # Build query to find episodes in Blacklisted/Error/Ghostlisted states
        id_field = 'imdb_id' if imdb_id else 'tmdb_id'
        id_value = imdb_id if imdb_id else tmdb_id

        query = f"""
            SELECT id, imdb_id, tmdb_id, season_number, episode_number, state
            FROM media_items
            WHERE {id_field} = ?
            AND type = 'episode'
            AND state IN ('Blacklisted', 'Error', 'Ghostlisted')
            AND state != 'Unreleased'
        """

        cursor.execute(query, (id_value,))
        episodes = cursor.fetchall()

        updated_count = 0
        for episode in episodes:
            ep_id = episode[0]
            ep_imdb = episode[1]
            ep_tmdb = episode[2]
            season = episode[3]
            ep_num = episode[4]
            old_state = episode[5]

            # Update state to Wanted
            update_media_item_state(ep_id, 'Wanted')
            updated_count += 1
            logging.info(f"Moved episode S{season:02d}E{ep_num:02d} from {old_state} to Wanted")

        cursor.close()
        db_content.close()

        return jsonify({
            'success': True,
            'updated_count': updated_count,
            'message': f'Moved {updated_count} episode(s) to Wanted state'
        })

    except Exception as e:
        logging.error(f"Error moving episodes to wanted: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/refresh_metadata/show/<media_id>', methods=['POST'])
@user_required
def refresh_show_metadata(media_id):
    """
    Refresh show metadata from TMDB/Trakt and update episode titles in database
    """
    try:
        from cli_battery.app.direct_api import DirectAPI

        # Get the show from database
        db_content = get_db_connection()
        cursor = db_content.cursor()

        # Determine if media_id is IMDB or TMDB format
        if media_id.startswith('tt'):
            id_field = 'imdb_id'
            imdb_id = media_id
        else:
            id_field = 'tmdb_id'
            # Need to get imdb_id from database for TMDB-only shows
            cursor.execute("SELECT imdb_id FROM media_items WHERE tmdb_id = ? AND type = 'episode' LIMIT 1", (media_id,))
            result = cursor.fetchone()
            if not result or not result[0]:
                cursor.close()
                db_content.close()
                return jsonify({
                    'success': False,
                    'error': 'Cannot refresh metadata for shows without IMDb ID'
                }), 400
            imdb_id = result[0]

        query = f"""
            SELECT imdb_id, tmdb_id, title, type
            FROM media_items
            WHERE {id_field} = ? AND type = 'episode'
            LIMIT 1
        """

        cursor.execute(query, (media_id,))
        show = cursor.fetchone()

        if not show:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': 'Show not found'
            }), 404

        imdb_id, tmdb_id, title, media_type = show

        # Force fresh metadata from TVDB/Trakt (not cached - ensures English titles)
        metadata, source = DirectAPI.force_refresh_metadata(imdb_id)

        if not metadata:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': f'Could not fetch metadata from {source or "API"}'
            }), 500

        # Extract show-level metadata from DirectAPI results (Source of Truth)
        show_title = metadata.get('title', title)
        show_year = str(metadata.get('year', '')) if metadata.get('year') else None
        show_status = metadata.get('status', '')

        # Format genres from list to comma-separated string
        genres_list = metadata.get('genres', [])
        if isinstance(genres_list, list):
            show_genres = ', '.join(genres_list) if genres_list else None
        else:
            show_genres = str(genres_list) if genres_list else None

        # Update episode titles and air dates from fresh metadata
        updated_count = 0
        if 'seasons' in metadata:
            for season_number, season_data in metadata['seasons'].items():
                if 'episodes' in season_data:
                    for episode_number, episode_data in season_data['episodes'].items():
                        episode_title = episode_data.get('title', f"Episode {episode_number}")
                        first_aired = episode_data.get('first_aired')

                        # Extract date and time from first_aired
                        release_date = None
                        airtime = None
                        if first_aired:
                            try:
                                # Handle both space and T separator formats
                                first_aired_str = str(first_aired).replace('T', ' ')
                                if ' ' in first_aired_str:
                                    date_part, time_part = first_aired_str.split(' ', 1)
                                    release_date = date_part[:10]  # YYYY-MM-DD
                                    airtime = time_part[:5]  # HH:MM
                                else:
                                    release_date = first_aired_str[:10]
                            except Exception as e:
                                logging.warning(f"Could not parse first_aired '{first_aired}' for S{season_number}E{episode_number}: {e}")

                        if episode_title and season_number is not None and episode_number is not None:
                            # Update episode metadata in database
                            cursor.execute("""
                                UPDATE media_items
                                SET episode_title = ?,
                                    release_date = COALESCE(?, release_date),
                                    airtime = COALESCE(?, airtime),
                                    metadata_updated = ?,
                                    last_updated = ?
                                WHERE imdb_id = ?
                                  AND type = 'episode'
                                  AND season_number = ?
                                  AND episode_number = ?
                            """, (
                                episode_title,
                                release_date,
                                airtime,
                                datetime.now(),
                                datetime.now(),
                                imdb_id,
                                season_number,
                                episode_number
                            ))
                            if cursor.rowcount > 0:
                                updated_count += cursor.rowcount

        # Fetch fresh show-level metadata from TMDB (Optional fallback/supplement)
        tmdb_updated = False
        if tmdb_id:
            try:
                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    import requests
                    # Fetch show details from TMDB
                    tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}"
                    response = requests.get(tmdb_url, timeout=10)
                    response.raise_for_status()
                    tmdb_data = response.json()

                    # Only update fields from TMDB if they are missing from DirectAPI/Battery
                    if not show_year:
                        show_year = tmdb_data.get('first_air_date', '')[:4] if tmdb_data.get('first_air_date') else None
                    if not show_genres:
                        show_genres = ', '.join([g['name'] for g in tmdb_data.get('genres', [])])
                    if not show_status:
                        show_status = tmdb_data.get('status', '')

                    tmdb_updated = True
                    logging.info(f"Updated supplementary TMDB metadata for {show_title}")
            except Exception as e:
                logging.warning(f"Failed to fetch supplementary TMDB metadata: {e}")

        # Update show-level metadata in media_items (title and genres for episodes)
        cursor.execute("""
            UPDATE media_items
            SET title = ?,
                genres = ?
            WHERE imdb_id = ? AND type = 'episode'
        """, (
            show_title,
            show_genres,
            imdb_id
        ))
        try:
            # Calculate total_seasons from fresh metadata
            total_seasons_refresh = None
            if 'seasons' in metadata:
                total_seasons_refresh = 0
                for k in metadata['seasons'].keys():
                    try:
                        if int(k) != 0:
                            total_seasons_refresh += 1
                    except (ValueError, TypeError):
                        pass
            # Update show-level metadata in tv_shows table (title, year, status, total_seasons)
            cursor.execute("""
                UPDATE tv_shows
                SET title = ?,
                    year = ?,
                    status = ?,
                    total_seasons = COALESCE(?, total_seasons),
                    last_updated = ?
                WHERE imdb_id = ?
            """, (
                show_title,
                show_year,
                show_status,
                total_seasons_refresh,
                datetime.now(),
                imdb_id
            ))

            tmdb_updated = True
            logging.info(f"Updated metadata for {show_title} in both media_items and tv_shows tables")
        except Exception as e:
            logging.warning(f"Failed to update show metadata: {e}")

        # Also update timestamps for all episodes (even if title didn't change)
        cursor.execute(f"""
            UPDATE media_items
            SET metadata_updated = ?,
                last_updated = ?
            WHERE {id_field} = ? AND type = 'episode'
        """, (
            datetime.now(),
            datetime.now(),
            media_id
        ))

        # Add any episodes that are in the refreshed metadata but not yet in the DB.
        # This handles the case where new episodes have been announced/released since the
        # show was first requested (e.g. "3 of 8 episodes" becoming "8 of 8").
        new_episodes_added = 0
        try:
            from metadata.metadata import process_metadata
            from database.wanted_items import add_wanted_items

            # Get the versions used by existing episodes of this show
            cursor.execute(
                "SELECT versions FROM media_items WHERE imdb_id = ? AND type = 'episode' AND versions IS NOT NULL LIMIT 1",
                (imdb_id,)
            )
            versions_row = cursor.fetchone()
            import json as _json
            existing_versions = {}
            if versions_row and versions_row[0]:
                try:
                    existing_versions = _json.loads(versions_row[0])
                except Exception:
                    pass

            wanted_item = {
                'imdb_id': imdb_id,
                'tmdb_id': tmdb_id,
                'media_type': 'tv',
                'title': title,
                'versions': existing_versions,
                'content_source': 'content_requester',
                'content_source_detail': 'Metadata-Refresh',
            }
            db_content.commit()  # Commit current updates before process_metadata reads the DB

            processed = process_metadata([wanted_item])
            if processed:
                new_items = processed.get('episodes', [])
                if new_items:
                    for ep in new_items:
                        ep['content_source'] = 'content_requester'
                        ep['content_source_detail'] = 'Metadata-Refresh'
                    added = add_wanted_items(new_items, existing_versions)
                    new_episodes_added = added or 0
                    if new_episodes_added:
                        logging.info(f"Metadata refresh for {title}: added {new_episodes_added} previously-missing episode(s) to the queue.")
        except Exception as e_add:
            logging.warning(f"Could not add missing episodes during metadata refresh for {imdb_id}: {e_add}")

        db_content.commit()
        cursor.close()
        db_content.close()

        # Clear poster/backdrop cache then immediately re-fetch so library page
        # shows the updated poster without requiring a full page reload
        new_poster_url = None
        if tmdb_id:
            try:
                cache = load_cache()
                poster_key = f"{tmdb_id}_tv"
                backdrop_key = f"{tmdb_id}_backdrop_tv"
                cache.pop(poster_key, None)
                cache.pop(backdrop_key, None)
                save_cache(cache)
                logging.info(f"Cleared poster/backdrop cache for show {tmdb_id}")
                # Re-fetch fresh poster and cache it immediately
                from routes.poster_cache import cache_poster_url, get_cached_poster_url
                from utilities.settings import get_setting as _gs
                import requests as _req
                _api_key = _gs('TMDB', 'api_key', default='')
                if _api_key:
                    _resp = _req.get(
                        f"https://api.themoviedb.org/3/tv/{tmdb_id}/images"
                        f"?api_key={_api_key}&include_image_language=en",
                        timeout=10
                    )
                    if _resp.status_code == 200:
                        _posters = _resp.json().get('posters', [])
                        # Fall back to en+null if no English-only results
                        if not _posters:
                            _resp2 = _req.get(
                                f"https://api.themoviedb.org/3/tv/{tmdb_id}/images"
                                f"?api_key={_api_key}&include_image_language=en,null",
                                timeout=10
                            )
                            if _resp2.status_code == 200:
                                # Only keep English (iso_639_1='en') posters from fallback
                                _all = _resp2.json().get('posters', [])
                                _posters = [p for p in _all if p.get('iso_639_1') == 'en'] or _all
                        if _posters:
                            _best = sorted(_posters, key=lambda p: p.get('vote_average', 0), reverse=True)[0]
                            new_poster_url = f"https://image.tmdb.org/t/p/w300{_best['file_path']}"
                            cache_poster_url(tmdb_id, 'tv', new_poster_url)
            except Exception as e:
                logging.warning(f"Failed to clear/refresh poster cache: {e}")

        logging.info(f"Refreshed metadata for {title} (IMDb: {imdb_id}): Updated {updated_count} episode titles from {source}, added {new_episodes_added} missing episode(s){', TMDB data updated' if tmdb_updated else ''}")

        msg = f'Metadata refreshed for {title}'
        if new_episodes_added:
            msg += f' — added {new_episodes_added} previously-missing episode(s) to the queue'

        return jsonify({
            'success': True,
            'message': msg,
            'updated_episodes': updated_count,
            'new_episodes_added': new_episodes_added,
            'tmdb_updated': tmdb_updated,
            'source': source,
            'cache_cleared': tmdb_id is not None,
            'new_poster_url': new_poster_url
        })

    except Exception as e:
        logging.error(f"Error refreshing metadata: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/movie/<media_id>/release-date-override', methods=['PUT'])
@admin_required
def set_movie_release_date_override_route(media_id):
    """Set one durable release-date override for every version of a movie."""
    try:
        payload = request.get_json(silent=True) or {}
        release_date = str(payload.get('release_date') or '').strip()
        try:
            datetime.strptime(release_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'error': 'release_date must use YYYY-MM-DD'}), 400

        from database.movie_release_overrides import set_movie_release_override

        updated_by = 'local-admin'
        if getattr(current_user, 'is_authenticated', False):
            updated_by = str(
                getattr(current_user, 'username', None)
                or getattr(current_user, 'id', None)
                or 'admin'
            )
        result = set_movie_release_override(media_id, release_date, updated_by=updated_by)
        logging.info(
            "Manual release-date override set for %s: %s (%s row(s))",
            result['media_key'],
            result['release_date'],
            result['affected_count'],
        )
        return jsonify({'success': True, **result})
    except LookupError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        logging.error("Failed to set movie release-date override: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


@library_bp.route('/movie/<media_id>/release-date-override', methods=['DELETE'])
@admin_required
def clear_movie_release_date_override_route(media_id):
    """Clear an override and immediately restore freshly resolved provider data."""
    conn = None
    try:
        conn = get_db_connection()
        value = str(media_id).strip()
        if value.lower().startswith('tt'):
            row = conn.execute(
                "SELECT imdb_id FROM media_items WHERE imdb_id = ? AND type = 'movie' LIMIT 1",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT imdb_id FROM media_items WHERE tmdb_id = ? AND type = 'movie' LIMIT 1",
                (value,),
            ).fetchone()
            if not row and value.isdigit():
                row = conn.execute(
                    "SELECT imdb_id FROM media_items WHERE id = ? AND type = 'movie' LIMIT 1",
                    (int(value),),
                ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Movie not found'}), 404
        imdb_id = row['imdb_id']
    finally:
        if conn:
            conn.close()

    try:
        provider_release_date = 'Unknown'
        if imdb_id:
            from metadata.metadata import get_release_date

            provider_release_date = get_release_date(
                {'released': 'Unknown'},
                imdb_id,
                release_date_cache_max_age=timedelta(0),
            )

        from database.movie_release_overrides import clear_movie_release_override

        result = clear_movie_release_override(media_id, provider_release_date)
        logging.info(
            "Manual release-date override cleared for %s; provider date restored to %s",
            result['media_key'],
            result['release_date'],
        )
        return jsonify({'success': True, **result})
    except LookupError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except Exception as exc:
        logging.error("Failed to clear movie release-date override: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


@library_bp.route('/movie/<media_id>/refresh-release-date', methods=['POST'])
@admin_required
def refresh_movie_release_date_route(media_id):
    """Re-fetch just the release date from the provider, bypassing the metadata cache."""
    conn = None
    try:
        conn = get_db_connection()
        value = str(media_id).strip()
        if value.lower().startswith('tt'):
            row = conn.execute(
                "SELECT imdb_id FROM media_items WHERE imdb_id = ? AND type = 'movie' LIMIT 1",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT imdb_id FROM media_items WHERE tmdb_id = ? AND type = 'movie' LIMIT 1",
                (value,),
            ).fetchone()
            if not row and value.isdigit():
                row = conn.execute(
                    "SELECT imdb_id FROM media_items WHERE id = ? AND type = 'movie' LIMIT 1",
                    (int(value),),
                ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Movie not found'}), 404
        imdb_id = row['imdb_id']
    finally:
        if conn:
            conn.close()

    try:
        provider_release_date = 'Unknown'
        if imdb_id:
            from metadata.metadata import get_release_date

            provider_release_date = get_release_date(
                {'released': 'Unknown'},
                imdb_id,
                release_date_cache_max_age=timedelta(0),
            )

        from database.movie_release_overrides import refresh_movie_release_date

        result = refresh_movie_release_date(media_id, provider_release_date)
        logging.info(
            "Release date refreshed from provider for %s: %s (%s row(s))",
            result['media_key'],
            result['release_date'],
            result['affected_count'],
        )
        return jsonify({'success': True, **result})
    except LookupError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 409
    except Exception as exc:
        logging.error("Failed to refresh movie release date from provider: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


@library_bp.route('/refresh_metadata/movie/<media_id>', methods=['POST'])
@user_required
def refresh_movie_metadata(media_id):
    """
    Refresh movie metadata from TMDB/Trakt and update database
    """
    try:
        from cli_battery.app.direct_api import DirectAPI

        # Get the movie from database
        db_content = get_db_connection()
        cursor = db_content.cursor()

        # Determine if media_id is IMDB or TMDB format
        if media_id.startswith('tt'):
            id_field = 'imdb_id'
            imdb_id = media_id
        else:
            id_field = 'tmdb_id'
            # Need to get imdb_id from database for TMDB-only movies
            cursor.execute("SELECT imdb_id FROM media_items WHERE tmdb_id = ? AND type = 'movie' LIMIT 1", (media_id,))
            result = cursor.fetchone()
            if result and result[0]:
                imdb_id = result[0]
            else:
                # TMDB-only movie (like UFC) - we can still update some fields
                imdb_id = None

        query = f"""
            SELECT imdb_id, tmdb_id, title, type
            FROM media_items
            WHERE {id_field} = ? AND type = 'movie'
            LIMIT 1
        """

        cursor.execute(query, (media_id,))
        movie = cursor.fetchone()

        if not movie:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': 'Movie not found'
            }), 404

        imdb_id, tmdb_id, title, media_type = movie

        # Get fresh metadata from DirectAPI
        if imdb_id:
            metadata, source = DirectAPI.get_movie_metadata(imdb_id)
        elif tmdb_id:
            # For TMDB-only movies, try to get metadata from TMDB
            from metadata.metadata import get_tmdb_metadata
            metadata = get_tmdb_metadata(str(tmdb_id), 'movie')
            source = 'TMDB'
        else:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': 'Movie has neither IMDb nor TMDB ID'
            }), 400

        if not metadata:
            cursor.close()
            db_content.close()
            return jsonify({
                'success': False,
                'error': f'Could not fetch metadata from {source or "API"}'
            }), 500

        # Update movie metadata in database
        update_fields = []
        update_values = []

        if 'title' in metadata and metadata['title']:
            update_fields.append('title = ?')
            update_values.append(metadata['title'])

        if 'year' in metadata and metadata['year']:
            update_fields.append('year = ?')
            update_values.append(metadata['year'])

        if 'genres' in metadata and metadata['genres']:
            # Convert list to comma-separated string
            genres_str = ', '.join(metadata['genres']) if isinstance(metadata['genres'], list) else metadata['genres']
            update_fields.append('genres = ?')
            update_values.append(genres_str)

        if 'runtime' in metadata and metadata['runtime']:
            update_fields.append('runtime = ?')
            update_values.append(metadata['runtime'])

        # Also fetch fresh TMDB data if we have tmdb_id to update additional fields
        tmdb_updated = False
        if tmdb_id:
            try:
                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    import requests
                    # Fetch movie details from TMDB
                    tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}"
                    response = requests.get(tmdb_url, timeout=10)
                    response.raise_for_status()
                    tmdb_data = response.json()

                    # Update genres from TMDB (this column exists)
                    if tmdb_data.get('genres'):
                        genres_str = ', '.join([g['name'] for g in tmdb_data['genres']])
                        update_fields.append('genres = ?')
                        update_values.append(genres_str)

                    # Update runtime from TMDB if not already set
                    if tmdb_data.get('runtime') and 'runtime = ?' not in update_fields:
                        update_fields.append('runtime = ?')
                        update_values.append(tmdb_data['runtime'])

                    tmdb_updated = True
                    logging.info(f"Updated TMDB metadata for {title}")
            except Exception as e:
                logging.warning(f"Failed to update TMDB metadata: {e}")

        # Always update timestamps
        update_fields.extend(['metadata_updated = ?', 'last_updated = ?'])
        update_values.extend([datetime.now(), datetime.now()])

        if update_fields:
            update_query = f"""
                UPDATE media_items
                SET {', '.join(update_fields)}
                WHERE {id_field} = ? AND type = 'movie'
            """
            update_values.append(media_id)

            cursor.execute(update_query, update_values)
            updated_count = cursor.rowcount
            db_content.commit()
        else:
            updated_count = 0

        cursor.close()
        db_content.close()

        # Clear poster/backdrop cache then immediately re-fetch so library page
        # shows the updated poster without requiring a full page reload
        new_poster_url = None
        if tmdb_id:
            try:
                cache = load_cache()
                poster_key = f"{tmdb_id}_movie"
                backdrop_key = f"{tmdb_id}_backdrop_movie"
                cache.pop(poster_key, None)
                cache.pop(backdrop_key, None)
                save_cache(cache)
                logging.info(f"Cleared poster/backdrop cache for movie {tmdb_id}")
                # Re-fetch fresh poster and cache it immediately
                from routes.poster_cache import cache_poster_url
                from utilities.settings import get_setting as _gs
                import requests as _req
                _api_key = _gs('TMDB', 'api_key', default='')
                if _api_key:
                    _resp = _req.get(
                        f"https://api.themoviedb.org/3/movie/{tmdb_id}/images"
                        f"?api_key={_api_key}&include_image_language=en",
                        timeout=10
                    )
                    if _resp.status_code == 200:
                        _posters = _resp.json().get('posters', [])
                        # Fall back to en+null if no English-only results
                        if not _posters:
                            _resp2 = _req.get(
                                f"https://api.themoviedb.org/3/movie/{tmdb_id}/images"
                                f"?api_key={_api_key}&include_image_language=en,null",
                                timeout=10
                            )
                            if _resp2.status_code == 200:
                                # Only keep English (iso_639_1='en') posters from fallback
                                _all = _resp2.json().get('posters', [])
                                _posters = [p for p in _all if p.get('iso_639_1') == 'en'] or _all
                        if _posters:
                            _best = sorted(_posters, key=lambda p: p.get('vote_average', 0), reverse=True)[0]
                            new_poster_url = f"https://image.tmdb.org/t/p/w300{_best['file_path']}"
                            cache_poster_url(tmdb_id, 'movie', new_poster_url)
            except Exception as e:
                logging.warning(f"Failed to clear/refresh poster cache: {e}")

        logging.info(f"Refreshed metadata for {title} (IMDb: {imdb_id}, TMDB: {tmdb_id}): Updated {updated_count} records from {source}{', TMDB data updated' if tmdb_updated else ''}")

        return jsonify({
            'success': True,
            'message': f'Metadata refreshed for {title}',
            'updated_count': updated_count,
            'tmdb_updated': tmdb_updated,
            'source': source,
            'cache_cleared': tmdb_id is not None,
            'new_poster_url': new_poster_url
        })

    except Exception as e:
        logging.error(f"Error refreshing metadata: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/api/trailer/<media_type>/<tmdb_id>', methods=['GET'])
def get_trailer(media_type, tmdb_id):
    """
    Fetch trailer from TMDB API for a given movie or TV show.
    Returns YouTube trailer key and metadata.
    """
    try:
        from utilities.settings import get_setting
        import requests

        tmdb_api_key = get_setting('TMDB', 'api_key')
        if not tmdb_api_key:
            return jsonify({
                'success': False,
                'error': 'TMDB API key not configured'
            }), 400

        # Determine endpoint based on media type
        endpoint = 'tv' if media_type == 'show' else 'movie'
        url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/videos"

        response = requests.get(
            url,
            params={'api_key': tmdb_api_key},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        # Filter for trailers from YouTube
        trailers = [
            v for v in data.get('results', [])
            if v.get('site') == 'YouTube' and v.get('type') in ['Trailer', 'Teaser']
        ]

        if not trailers:
            return jsonify({
                'success': False,
                'error': 'No trailer found'
            }), 404

        # Return the first official trailer, or first trailer if no official one
        official_trailer = next((t for t in trailers if t.get('official')), None)
        trailer = official_trailer if official_trailer else trailers[0]

        return jsonify({
            'success': True,
            'trailer': {
                'key': trailer['key'],
                'name': trailer['name'],
                'site': trailer['site'],
                'type': trailer['type']
            }
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching trailer from TMDB: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to fetch trailer from TMDB'
        }), 500
    except Exception as e:
        logging.error(f"Error in get_trailer: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/clear_cache', methods=['POST'])
@admin_required
def clear_cache():
    """
    Clear all poster/backdrop cache
    This will force re-fetching of all artwork on next library load
    """
    try:
        from routes.poster_cache import clear_all_cache

        success = clear_all_cache()

        if success:
            return jsonify({
                'success': True,
                'message': 'Cache cleared successfully. Artwork will be refreshed on next library load.'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to clear cache. Check logs for details.'
            }), 500

    except Exception as e:
        logging.error(f"Error clearing cache: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# =============================================================================
# SHARED DELETION API ENDPOINTS
# Used by: Library multi-select, Maintenance page, Show multi-level deletion
# =============================================================================

@library_bp.route('/check_deletion_impact', methods=['POST'])
@user_required
def check_deletion_impact():
    """
    Check the impact of deleting items before performing deletion
    Used by all deletion features to preview what will be deleted

    Request JSON:
    {
        "item_ids": [1, 2, 3],           # Database IDs to delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache"],
        "blacklist": false,               # Whether to blacklist items
        "blacklist_sources": false        # Whether to blacklist content sources
    }

    Returns impact analysis with counts and details for each layer
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_items_by_ids

        data = request.json
        item_ids = data.get('item_ids', [])
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)
        blacklist_sources = data.get('blacklist_sources', False)

        if not item_ids:
            return jsonify({
                'success': False,
                'error': 'No items specified for deletion'
            }), 400

        # Get items from database
        items = get_items_by_ids(item_ids)

        if not items:
            return jsonify({
                'success': False,
                'error': 'No items found with the specified IDs'
            }), 404

        # Initialize deletion manager
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)

        # Build impact analysis
        impact = {
            'success': True,
            'items_count': len(items),
            'items': [],
            'layers': {}
        }

        # Analyze each item
        for item in items:
            item_impact = {
                'id': item['id'],
                'title': item.get('title', 'Unknown'),
                'type': item.get('type', 'unknown'),
                'state': item.get('state', 'Unknown'),
                'layers': {}
            }

            # Check each layer
            if 'database' in layers:
                item_impact['layers']['database'] = {
                    'will_delete': True,
                    'description': 'Database entry'
                }

            if 'media_server' in layers:
                # Check if item exists in media server
                media_server_check = deletion_manager.remove_from_media_server(item)
                item_impact['layers']['media_server'] = {
                    'will_delete': media_server_check.get('found', False),
                    'description': f"Media server ({deletion_manager.media_server})",
                    'details': media_server_check
                }

            if 'filesystem' in layers:
                # Check filesystem impact
                file_path = item.get('filled_by_file') or item.get('location_on_disk')
                item_impact['layers']['filesystem'] = {
                    'will_delete': bool(file_path),
                    'description': 'File on disk',
                    'file_path': file_path,
                    'file_size': None  # Could add size calculation here
                }

            if 'debrid' in layers:
                torrent_id = item.get('filled_by_torrent_id', '') or ''
                is_nzb = str(torrent_id).startswith('nzb:')
                debrid_hash = item.get('filled_by_magnet_hash') if not is_nzb else torrent_id
                item_impact['layers']['debrid'] = {
                    'will_delete': bool(is_nzb or debrid_hash),
                    'description': 'Usenet NZB (cli_mount)' if is_nzb else 'Debrid torrent',
                    'hash': debrid_hash,
                    'is_nzb': is_nzb,
                }

            if 'symlinks' in layers and deletion_manager.using_symlinks:
                # Check symlink impact
                symlink_check = deletion_manager._check_symlinks(item)
                item_impact['layers']['symlinks'] = {
                    'will_delete': symlink_check.get('found', False),
                    'description': 'Symbolic links',
                    'details': symlink_check
                }

            if 'cache' in layers:
                # Check content source cache
                source_check = deletion_manager.check_content_sources(item)
                item_impact['layers']['cache'] = {
                    'will_delete': len(source_check.get('cache_files', [])) > 0,
                    'description': 'Content source cache',
                    'sources': source_check.get('sources', []),
                    'cache_files': source_check.get('cache_files', [])
                }

            impact['items'].append(item_impact)

        # Aggregate layer statistics
        for layer in layers:
            layer_stats = {
                'affected_count': 0,
                'items': []
            }

            for item_impact in impact['items']:
                if layer in item_impact['layers'] and item_impact['layers'][layer].get('will_delete', False):
                    layer_stats['affected_count'] += 1
                    layer_stats['items'].append({
                        'id': item_impact['id'],
                        'title': item_impact['title']
                    })

            # Add layer-specific counts
            if layer == 'filesystem':
                layer_stats['files_count'] = layer_stats['affected_count']
            elif layer == 'debrid':
                layer_stats['torrents_count'] = layer_stats['affected_count']
                layer_stats['nzb_count'] = sum(1 for ii in impact['items'] if ii['layers'].get('debrid', {}).get('is_nzb'))
                layer_stats['torrent_only_count'] = layer_stats['torrents_count'] - layer_stats['nzb_count']
            elif layer == 'symlinks':
                layer_stats['symlinks_count'] = layer_stats['affected_count']

            impact['layers'][layer] = layer_stats

        # Add blacklist recommendations
        if blacklist or blacklist_sources:
            impact['blacklist_info'] = {
                'will_blacklist_items': blacklist,
                'will_blacklist_sources': blacklist_sources,
                'affected_sources': []
            }

            if blacklist_sources:
                # Collect unique sources
                sources_set = set()
                for item_impact in impact['items']:
                    if 'cache' in item_impact['layers']:
                        for source in item_impact['layers']['cache'].get('sources', []):
                            sources_set.add(source)
                impact['blacklist_info']['affected_sources'] = list(sources_set)

        logging.info(f"Deletion impact check completed for {len(items)} items")
        return jsonify(impact)

    except Exception as e:
        logging.error(f"Error checking deletion impact: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/delete_items', methods=['POST'])
@admin_required
def delete_items():
    """
    Delete multiple items with specified layers and options
    Used by all deletion features (library multi-select, maintenance, show multi-level)

    Request JSON:
    {
        "item_ids": [1, 2, 3],           # Database IDs to delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache"],
        "blacklist": false,               # Whether to blacklist items
        "blacklist_sources": false,       # Whether to blacklist content sources
        "remove_from_content_source": false  # Whether to remove from Trakt/Overseerr/etc.
    }

    Returns deletion results with success/failure for each item and layer
    """
    from routes.program_operation_routes import get_program_runner

    # Track if we paused the queue
    paused_queue = False
    program_runner = None

    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_items_by_ids
        from utilities.settings import get_setting

        data = request.json
        item_ids = data.get('item_ids', [])
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)
        blacklist_sources = data.get('blacklist_sources', False)
        # Use global setting for content source removal (ignores frontend request value)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)

        if not item_ids:
            return jsonify({
                'success': False,
                'error': 'No items specified for deletion'
            }), 400

        # Get items from database
        items = get_items_by_ids(item_ids)

        if not items:
            return jsonify({
                'success': False,
                'error': 'No items found with the specified IDs'
            }), 404

        # Check auto-ghostlist setting (applies to all content types)
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        logging.info(f"[DELETE_ITEMS] Auto-ghostlist setting: {auto_ghostlist}")
        logging.info(f"[DELETE_ITEMS] Processing {len(item_ids)} items")

        # Determine if we need to pause queue
        # Pause for large batches or operations with physical files
        needs_pause = (
            len(item_ids) > 5 or  # Large batch
            'filesystem' in layers or  # Physical file operations
            'debrid' in layers or
            'media_server' in layers
        )

        # Pause queue if needed (prevents race conditions during long operations)
        program_runner = get_program_runner()
        if needs_pause and program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
            if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                logging.info(f"[DELETE_ITEMS] Pausing queue for batch deletion of {len(item_ids)} items (layers: {layers})")
                program_runner.pause_info = {
                    "reason_string": "Library batch deletion in progress",
                    "error_type": "SYSTEM_MAINTENANCE",
                    "service_name": "Library Delete",
                    "status_code": None,
                    "retry_count": 0
                }
                program_runner.pause_queue()
                paused_queue = True
                logging.info("[DELETE_ITEMS] Queue paused successfully")
            else:
                logging.info("[DELETE_ITEMS] Queue pause requested but pause_queue method not available")
        else:
            if not needs_pause:
                logging.info(f"[DELETE_ITEMS] Queue pause not needed for {len(item_ids)} items with layers {layers}")
            elif not program_runner:
                logging.info("[DELETE_ITEMS] Program runner not available for queue pause")
            elif not hasattr(program_runner, 'is_running') or not program_runner.is_running():
                logging.info("[DELETE_ITEMS] Program runner not running, queue pause skipped")

        # Initialize deletion manager
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # Skip database layer - we'll handle it ourselves based on auto_ghostlist
        result = deletion_manager.delete_multiple_items(
            item_ids=item_ids,
            blacklist=blacklist,
            blacklist_sources=blacklist_sources,
            delete_from_media_server='media_server' in layers,
            delete_files='filesystem' in layers,
            delete_from_debrid='debrid' in layers,
            delete_symlinks='symlinks' in layers,
            clear_cache='cache' in layers,
            remove_from_content_source=remove_from_content_source,
            skip_database=True  # We'll handle database ourselves based on auto_ghostlist
        )

        logging.info(f"[DELETE_ITEMS] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_ITEMS] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_ITEMS] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_ITEMS] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            return jsonify({
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_ITEMS] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        if 'database' in layers:
            if auto_ghostlist:
                # Ghostlist: Update items to ghostlisted=1 and state=Blacklisted
                logging.info(f"[DELETE_ITEMS] Taking GHOSTLIST path")
                try:
                    from datetime import datetime
                    from database.core import get_db_connection

                    conn = get_db_connection()
                    cursor = conn.cursor()

                    cursor.execute('BEGIN TRANSACTION')

                    # Ghostlist all specified items
                    placeholders = ','.join('?' * len(item_ids))
                    cursor.execute(
                        f'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        ['Blacklisted', datetime.now()] + item_ids
                    )

                    updated_count = cursor.rowcount
                    conn.commit()
                    conn.close()

                    auto_ghostlisted = True
                    logging.info(f"[DELETE_ITEMS] Ghostlisted {updated_count} item(s)")

                    # Also add each unique imdb_id to manual blacklist
                    try:
                        from database.manual_blacklist import add_to_manual_blacklist
                        seen_imdb = {}
                        for item in items:
                            iid = item.get('imdb_id')
                            if iid and iid not in seen_imdb:
                                seen_imdb[iid] = item
                        for iid, item in seen_imdb.items():
                            media_type = 'episode' if item.get('type') == 'episode' else 'movie'
                            add_to_manual_blacklist(
                                imdb_id=iid,
                                media_type=media_type,
                                title=item.get('title', ''),
                                year=str(item.get('year', ''))
                            )
                            logging.info(f"[DELETE_ITEMS] Added {iid} to manual blacklist")
                    except Exception as mb_err:
                        logging.warning(f"[DELETE_ITEMS] Could not add to manual blacklist: {mb_err}")

                except Exception as e:
                    logging.error(f"[DELETE_ITEMS] Failed to ghostlist: {e}")
                    if conn:
                        conn.rollback()
                        conn.close()

            else:
                # Delete: Remove items from database
                logging.info(f"[DELETE_ITEMS] Taking DELETE path")
                try:
                    from database.database_writing import remove_from_media_items

                    deleted_count = 0
                    for item_id in item_ids:
                        if remove_from_media_items(item_id):
                            deleted_count += 1

                    logging.info(f"[DELETE_ITEMS] Deleted {deleted_count} item(s) from database")
                except Exception as e:
                    logging.error(f"[DELETE_ITEMS] Failed to delete from database: {e}")

        # Check for database lock errors
        if result.get('database_locked'):
            logging.error("[DELETE_ITEMS] Database lock detected during batch deletion")
            return jsonify({
                'success': False,
                'error': 'database is locked',
                'database_locked': True
            }), 503

        # Add auto_ghostlisted flag to result
        result['auto_ghostlisted'] = auto_ghostlisted

        if result['success']:
            logging.info(f"[DELETE_ITEMS] Successfully processed {result['deleted_count']} items")
        else:
            logging.error(f"[DELETE_ITEMS] Deletion completed with errors: {result.get('errors', [])}")

        return jsonify(result)

    except Exception as e:
        logging.error(f"[DELETE_ITEMS] Error deleting items: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

    finally:
        # ALWAYS resume queue if we paused it
        if paused_queue and program_runner:
            if hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
                logging.info("[DELETE_ITEMS] Resuming queue in finally block")
                program_runner.resume_queue()
                logging.info("[DELETE_ITEMS] Queue resumed successfully")
            else:
                logging.warning("[DELETE_ITEMS] Queue was paused but resume_queue method not available")

@library_bp.route('/delete_show/<imdb_id>', methods=['POST'])
@admin_required
def delete_show(imdb_id):
    """
    Delete entire show (all seasons and episodes)

    Request body:
    {
        "blacklist": bool,  # Whether to blacklist instead of permanent delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache", "content_source"]
    }
    """
    from routes.program_operation_routes import get_program_runner
    import sqlite3

    # Track if we paused the queue
    paused_queue = False
    program_runner = None

    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_all_episodes_for_show

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)

        # Get all episodes for this show
        episodes = get_all_episodes_for_show(imdb_id)

        if not episodes:
            return jsonify({
                'success': False,
                'error': 'No episodes found for this show'
            }), 404

        episode_ids = [ep['id'] for ep in episodes]

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        # Use global setting for content source removal (ignores frontend layers)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)
        blacklist_sources = data.get('blacklist_sources', False)

        logging.info(f"[DELETE_SHOW] Starting deletion for show {imdb_id}")
        logging.info(f"[DELETE_SHOW] Found {len(episode_ids)} episodes to delete")
        logging.info(f"[DELETE_SHOW] Layers requested: {layers}")

        # ONLY check auto-ghostlist setting - this determines database behavior
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        logging.info(f"[DELETE_SHOW] Auto-ghostlist setting: {auto_ghostlist} (type: {type(auto_ghostlist)})")

        # Additional debug - check what the setting value actually is
        if auto_ghostlist:
            logging.info(f"[DELETE_SHOW] WILL GHOSTLIST - auto_ghostlist is truthy")
        else:
            logging.info(f"[DELETE_SHOW] WILL DELETE - auto_ghostlist is falsy")

        # Determine if we need to pause queue
        # Pause for show deletions with physical operations
        needs_pause = (
            len(episode_ids) > 5 or  # Large show
            delete_files or  # Physical file operations
            delete_from_debrid or
            delete_from_media_server
        )

        # Pause queue if needed (prevents race conditions during long operations)
        program_runner = get_program_runner()
        if needs_pause and program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
            if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                logging.info(f"[DELETE_SHOW] Pausing queue for show deletion of {len(episode_ids)} episodes")
                program_runner.pause_info = {
                    "reason_string": f"Deleting show {imdb_id}",
                    "error_type": "SYSTEM_MAINTENANCE",
                    "service_name": "Show Delete",
                    "status_code": None,
                    "retry_count": 0
                }
                program_runner.pause_queue()
                paused_queue = True
                logging.info("[DELETE_SHOW] Queue paused successfully")
            else:
                logging.info("[DELETE_SHOW] Queue pause requested but pause_queue method not available")
        else:
            if not needs_pause:
                logging.info(f"[DELETE_SHOW] Queue pause not needed for {len(episode_ids)} episodes")
            elif not program_runner:
                logging.info("[DELETE_SHOW] Program runner not available for queue pause")
            elif not hasattr(program_runner, 'is_running') or not program_runner.is_running():
                logging.info("[DELETE_SHOW] Program runner not running, queue pause skipped")

        # Handle content source removal
        content_source_result = None
        if remove_from_content_source and episodes:
            logging.info(f"[DELETE_SHOW] Removing show from content sources")
            debrid_provider = get_debrid_provider()
            deletion_manager = DeletionManager(debrid_provider=debrid_provider)
            show_item = episodes[0].copy()
            show_item['type'] = 'show'
            content_source_result = deletion_manager.remove_from_content_source(show_item)
            logging.info(f"[DELETE_SHOW] Content source removal result: {content_source_result.get('message')}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # Get show title from first episode for Plex content-level deletion
        show_title = episodes[0].get('title') if episodes else None
        # tmdb_id falls back to imdb_id if not available
        show_tmdb_id = (episodes[0].get('tmdb_id') or imdb_id) if episodes else imdb_id

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=episode_ids,
            blacklist=blacklist,
            blacklist_sources=blacklist_sources,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Already handled above
            force_delete_parent_folder=True,  # Whole show delete - remove show folder
            skip_database=True,  # We'll handle database ourselves based on auto_ghostlist
            # Plex content-level deletion params - deletes whole show in 1 API call
            plex_deletion_type='show',
            plex_content_title=show_title,
            plex_imdb_id=imdb_id,
            plex_tmdb_id=show_tmdb_id
        )

        logging.info(f"[DELETE_SHOW] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_SHOW] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_SHOW] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_SHOW] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            response_data = {
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }
            if result.get('plex_not_found'):
                response_data['plex_not_found'] = True
            return jsonify(response_data), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_SHOW] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        if auto_ghostlist:
            # Ghostlist: Update all episodes to ghostlisted=1 and state=Blacklisted
            logging.info(f"[DELETE_SHOW] Taking GHOSTLIST path (batch UPDATE)")
            try:
                from database.core import get_db_connection
                from datetime import datetime

                conn = get_db_connection()
                cursor = conn.cursor()

                # BEGIN TRANSACTION
                cursor.execute('BEGIN TRANSACTION')

                # BATCH UPDATE - Ghostlist all episodes in one statement
                cursor.execute(
                    'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE imdb_id = ? AND type = ?',
                    ['Blacklisted', datetime.now(), imdb_id, 'episode']
                )

                updated_count = cursor.rowcount

                # COMMIT TRANSACTION
                conn.commit()
                conn.close()

                auto_ghostlisted = True
                logging.info(f"[DELETE_SHOW] Ghostlisted {updated_count} episodes for show {imdb_id} - ghostlisted=1, state=Blacklisted")

                # Also add to manual blacklist to block future episodes
                try:
                    from database.manual_blacklist import add_to_manual_blacklist
                    first_ep = episodes[0]
                    add_to_manual_blacklist(
                        imdb_id=imdb_id,
                        media_type='episode',
                        title=first_ep.get('title', ''),
                        year=str(first_ep.get('year', ''))
                    )
                    logging.info(f"[DELETE_SHOW] Added {imdb_id} to manual blacklist")
                except Exception as mb_err:
                    logging.warning(f"[DELETE_SHOW] Could not add to manual blacklist: {mb_err}")

            except sqlite3.OperationalError as e:
                # Rollback and check for database lock
                if conn:
                    conn.rollback()
                    conn.close()

                if "database is locked" in str(e):
                    logging.error(f"[DELETE_SHOW] Database locked during ghostlist of show {imdb_id}")
                    return jsonify({
                        'success': False,
                        'error': 'database is locked',
                        'database_locked': True
                    }), 503
                else:
                    logging.error(f"[DELETE_SHOW] OperationalError during ghostlist: {e}")
                    return jsonify({
                        'success': False,
                        'error': f'Database error during ghostlist: {str(e)}'
                    }), 500

            except Exception as e:
                logging.error(f"[DELETE_SHOW] Failed to ghostlist: {e}")
                if conn:
                    conn.rollback()
                    conn.close()
                return jsonify({
                    'success': False,
                    'error': f'Failed to ghostlist show: {str(e)}'
                }), 500

        else:
            # Delete: Remove all episodes from database using BATCH DELETE
            logging.info(f"[DELETE_SHOW] Taking DELETE path (batch DELETE)")
            try:
                from database.database_writing import delete_items_batch

                # BATCH DELETE - All episodes in one transaction
                db_result = delete_items_batch(
                    item_ids=episode_ids,
                    blacklist=False
                )

                if db_result['success']:
                    deleted_count = db_result['deleted_count']
                    logging.info(f"[DELETE_SHOW] Deleted {deleted_count} episodes from database")
                else:
                    # Database deletion failed
                    if db_result.get('database_locked'):
                        logging.error(f"[DELETE_SHOW] Database locked during deletion of show {imdb_id}")
                        return jsonify({
                            'success': False,
                            'error': 'database is locked',
                            'database_locked': True
                        }), 503
                    else:
                        logging.error(f"[DELETE_SHOW] Database deletion failed: {db_result['error']}")
                        return jsonify({
                            'success': False,
                            'error': f"Failed to delete from database: {db_result['error']}"
                        }), 500

            except Exception as e:
                logging.error(f"[DELETE_SHOW] Failed to delete from database: {e}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to delete show: {str(e)}'
                }), 500

        # Build detailed layer execution summary (only report actual successes)
        layers_executed = []
        layers_skipped = []  # Track skipped/failed layers

        # Database layer - ghostlist or delete
        if auto_ghostlisted:
            layers_executed.append('Database (Ghostlisted)')
        else:
            layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                # Check if Plex is configured
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Check file collection management mode for appropriate deletion messages
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')

        # Filesystem - only if files actually deleted
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                # In Plex mode, filesystem layer is not applicable since Plex manages files
                if file_management == 'Plex':
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'Not applicable in Plex mode'})
                else:
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                # Check if debrid provider is available
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up (only show in Symlink mode)
        symlink_count = sum(len(r.get('deleted_symlinks', [])) for r in result.get('results', []))
        if delete_symlinks and file_management != 'Plex':
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0 or result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache - always succeeds if requested
        if clear_cache:
            layers_executed.append('Cache')

        # Content Source - report based on actual result
        if remove_from_content_source and content_source_result:
            if content_source_result.get('success'):
                layers_executed.append(f"Content Source ({', '.join(content_source_result.get('sources_succeeded', []))})")
            else:
                layers_executed.append('Content Source (Failed)')

        # Build response with both 'error' (singular) and 'errors' (plural) for frontend compatibility
        errors_list = result.get('errors', [])
        response_data = {
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': errors_list,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,  # New field for skipped layers
            'content_source_removal': content_source_result,
            'auto_ghostlisted': auto_ghostlisted,  # Flag if show was auto-ghostlisted
            'message': f"{'Ghostlisted' if auto_ghostlisted else 'Deleted'} {result.get('deleted_count', 0)} episodes from show"
        }

        # Add 'error' (singular) field for frontend compatibility - uses first error if available
        if errors_list:
            response_data['error'] = errors_list[0]

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"[DELETE_SHOW] Error deleting show {imdb_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

    finally:
        # ALWAYS resume queue if we paused it
        if paused_queue and program_runner:
            if hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
                logging.info("[DELETE_SHOW] Resuming queue in finally block")
                program_runner.resume_queue()
                logging.info("[DELETE_SHOW] Queue resumed successfully")
            else:
                logging.warning("[DELETE_SHOW] Queue was paused but resume_queue method not available")

@library_bp.route('/delete_movie/<imdb_id>', methods=['POST'])
@admin_required
def delete_movie(imdb_id):
    """
    Delete entire movie (all video files)

    Request body:
    {
        "blacklist": bool,  # Whether to blacklist instead of permanent delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache", "content_source"]
    }
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.core import get_db_connection
        from utilities.settings import get_setting

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)

        # Get ALL movie files from database (movies can have multiple files/versions)
        conn = get_db_connection()
        movie_query = """
            SELECT id, title, year, tmdb_id, imdb_id, type
            FROM media_items
            WHERE (tmdb_id = ? OR imdb_id = ?) AND type = 'movie'
        """
        movie_rows = conn.execute(movie_query, (str(imdb_id), str(imdb_id))).fetchall()
        conn.close()

        if not movie_rows:
            return jsonify({
                'success': False,
                'error': 'No movie found with this ID'
            }), 404

        # Collect all movie file IDs
        movie_ids = [row['id'] for row in movie_rows]
        movie = dict(movie_rows[0])  # Use first row for metadata
        movie_title = movie['title']

        logging.info(f"[DELETE_MOVIE] Found {len(movie_ids)} file(s) for movie {imdb_id} ({movie_title}): {movie_ids}")

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        # Use global setting for content source removal (ignores frontend layers)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)
        blacklist_sources = data.get('blacklist_sources', False)

        logging.info(f"[DELETE_MOVIE] Starting deletion for movie {imdb_id} ({movie_title})")
        logging.info(f"[DELETE_MOVIE] Layers requested: {layers}")

        # Check auto-ghostlist setting
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        logging.info(f"[DELETE_MOVIE] Auto-ghostlist setting: {auto_ghostlist}")

        # Handle content source removal
        content_source_result = None
        if remove_from_content_source:
            logging.info(f"[DELETE_MOVIE] Removing movie from content sources")
            debrid_provider = get_debrid_provider()
            deletion_manager = DeletionManager(debrid_provider=debrid_provider)
            movie['type'] = 'movie'
            content_source_result = deletion_manager.remove_from_content_source(movie)
            logging.info(f"[DELETE_MOVIE] Content source removal result: {content_source_result.get('message')}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # tmdb_id falls back to imdb_id if not available
        movie_tmdb_id = movie.get('tmdb_id') or imdb_id

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=movie_ids,  # Delete ALL files for this movie
            blacklist=blacklist,
            blacklist_sources=blacklist_sources,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Already handled above
            force_delete_parent_folder=True,  # Whole movie delete - remove movie folder
            skip_database=True,  # We'll handle database ourselves based on auto_ghostlist
            # Plex content-level deletion params - deletes whole movie in 1 API call
            plex_deletion_type='movie',
            plex_content_title=movie_title,
            plex_imdb_id=imdb_id,
            plex_tmdb_id=movie_tmdb_id
        )

        logging.info(f"[DELETE_MOVIE] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_MOVIE] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_MOVIE] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_MOVIE] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            response_data = {
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }
            if result.get('plex_not_found'):
                response_data['plex_not_found'] = True
            return jsonify(response_data), 500

        # Handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_MOVIE] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        if auto_ghostlist:
            # Ghostlist: Update movie to ghostlisted=1 and state=Blacklisted
            logging.info(f"[DELETE_MOVIE] Taking GHOSTLIST path")
            try:
                from datetime import datetime

                conn = get_db_connection()
                cursor = conn.cursor()

                # Begin transaction
                cursor.execute('BEGIN TRANSACTION')

                # Ghostlist all movie files
                placeholders = ','.join('?' * len(movie_ids))
                cursor.execute(
                    f'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                    ['Blacklisted', datetime.now()] + movie_ids
                )

                updated_count = cursor.rowcount
                conn.commit()
                conn.close()

                auto_ghostlisted = True
                logging.info(f"[DELETE_MOVIE] Ghostlisted movie {imdb_id} - ghostlisted=1, state=Blacklisted")

                # Also add to manual blacklist to prevent re-addition
                try:
                    from database.manual_blacklist import add_to_manual_blacklist
                    add_to_manual_blacklist(
                        imdb_id=imdb_id,
                        media_type='movie',
                        title=movie.get('title', ''),
                        year=str(movie.get('year', ''))
                    )
                    logging.info(f"[DELETE_MOVIE] Added {imdb_id} to manual blacklist")
                except Exception as mb_err:
                    logging.warning(f"[DELETE_MOVIE] Could not add to manual blacklist: {mb_err}")

            except Exception as e:
                logging.error(f"[DELETE_MOVIE] Failed to ghostlist: {e}")
                if conn:
                    conn.rollback()
                    conn.close()

        else:
            # Delete: Remove all movie files from database
            logging.info(f"[DELETE_MOVIE] Taking DELETE path")
            try:
                from database.database_writing import remove_from_media_items

                # Delete all movie files
                deleted_count = 0
                for mid in movie_ids:
                    if remove_from_media_items(mid):
                        deleted_count += 1

                logging.info(f"[DELETE_MOVIE] Deleted {deleted_count}/{len(movie_ids)} movie file(s) from database")
                if deleted_count < len(movie_ids):
                    logging.warning(f"[DELETE_MOVIE] Failed to delete {len(movie_ids) - deleted_count} movie file(s) from database")

            except Exception as e:
                logging.error(f"[DELETE_MOVIE] Failed to delete from database: {e}")

        # Build detailed layer execution summary
        layers_executed = []
        layers_skipped = []

        # Database layer - ghostlist or delete
        if auto_ghostlisted:
            layers_executed.append('Database (Ghostlisted)')
        else:
            layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Check file collection management mode for appropriate deletion messages
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')

        # Filesystem - only if files actually deleted
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                # In Plex mode, filesystem layer is not applicable since Plex manages files
                if file_management == 'Plex':
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'Not applicable in Plex mode'})
                else:
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up (only show in Symlink mode)
        symlink_count = sum(len(r.get('deleted_symlinks', [])) for r in result.get('results', []))
        if delete_symlinks and file_management != 'Plex':
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0 or result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache - always succeeds if requested
        if clear_cache:
            layers_executed.append('Cache')

        # Content Source - report based on actual result
        if remove_from_content_source and content_source_result:
            if content_source_result.get('success'):
                layers_executed.append(f"Content Source ({', '.join(content_source_result.get('sources_succeeded', []))})")
            else:
                layers_executed.append('Content Source (Failed)')

        # Build response with both 'error' (singular) and 'errors' (plural) for frontend compatibility
        errors_list = result.get('errors', [])
        response_data = {
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': errors_list,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'content_source_removal': content_source_result,
            'auto_ghostlisted': auto_ghostlisted,
            'message': f"{'Ghostlisted' if auto_ghostlisted else 'Deleted'} movie: {movie_title}"
        }

        # Add 'error' (singular) field for frontend compatibility - uses first error if available
        if errors_list:
            response_data['error'] = errors_list[0]

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error deleting movie {imdb_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/delete_movie_files', methods=['POST'])
@admin_required
def delete_movie_files():
    """
    Delete specific movie files (for movies with multiple versions/files)

    Request body:
    {
        "file_ids": [int],  # List of media_items IDs to delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks"]
    }

    Note: Does NOT include content_source removal - movie still exists, just fewer files
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.core import get_db_connection

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks'])
        item_ids = data.get('file_ids', [])

        if not item_ids:
            return jsonify({
                'success': False,
                'error': 'No file IDs provided'
            }), 400

        # Verify all files exist and are movies
        conn = get_db_connection()
        for item_id in item_ids:
            file_row = conn.execute('SELECT id, title, type FROM media_items WHERE id = ?', (item_id,)).fetchone()
            if not file_row:
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'File ID {item_id} not found'
                }), 404
            if file_row['type'] != 'movie':
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'File ID {item_id} is not a movie'
                }), 400
        conn.close()

        # Parse layers
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        remove_from_content_source = 'content_source' in layers

        logging.info(f"[DELETE_MOVIE_FILES] Starting deletion for {len(item_ids)} file(s)")
        logging.info(f"[DELETE_MOVIE_FILES] Item IDs to delete: {item_ids}")
        logging.info(f"[DELETE_MOVIE_FILES] Layers requested: {layers}")

        # Note: Content source removal is NOT supported for movie file deletion
        # If user wants to remove from content source, they should delete the entire movie
        if remove_from_content_source:
            logging.warning(f"[DELETE_MOVIE_FILES] Content source removal requested but not supported for file deletion - use delete_movie instead")

        # Check auto-ghostlist setting
        from utilities.settings import get_setting
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        #logging.info(f"[DELETE_MOVIE_FILES] Auto-ghostlist setting: {auto_ghostlist}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=item_ids,
            blacklist=False,
            blacklist_sources=False,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Never remove content source for file deletion
            skip_database=True  # Handle database ourselves based on auto_ghostlist
        )

        logging.info(f"[DELETE_MOVIE_FILES] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_MOVIE_FILES] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_MOVIE_FILES] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_MOVIE_FILES] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            return jsonify({
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }), 500

        # Handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_MOVIE_FILES] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        # if auto_ghostlist:
        #     # Ghostlist: Update files to ghostlisted=1 and state=Blacklisted
        #     logging.info(f"[DELETE_MOVIE_FILES] Taking GHOSTLIST path")
        #     try:
        #         from datetime import datetime

        #         conn = get_db_connection()
        #         cursor = conn.cursor()

        #         cursor.execute('BEGIN TRANSACTION')

        #         # Ghostlist specified files
        #         placeholders = ','.join('?' * len(item_ids))
        #         cursor.execute(
        #             f'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({placeholders})',
        #             ['Blacklisted', datetime.now()] + item_ids
        #         )

        #         updated_count = cursor.rowcount
        #         conn.commit()
        #         conn.close()

        #         auto_ghostlisted = True
        #         logging.info(f"[DELETE_MOVIE_FILES] Ghostlisted {updated_count} file(s)")

        #     except Exception as e:
        #         logging.error(f"[DELETE_MOVIE_FILES] Failed to ghostlist: {e}")
        #         if conn:
        #             conn.rollback()
        #             conn.close()

        # else:
        # Delete: Remove files from database
        logging.info(f"[DELETE_MOVIE_FILES] Taking DELETE path")
        try:
            from database.database_writing import remove_from_media_items

            deleted_count = 0
            for item_id in item_ids:
                if remove_from_media_items(item_id):
                    deleted_count += 1

            logging.info(f"[DELETE_MOVIE_FILES] Deleted {deleted_count}/{len(item_ids)} file(s) from database")
            if deleted_count < len(item_ids):
                logging.warning(f"[DELETE_MOVIE_FILES] Failed to delete {len(item_ids) - deleted_count} file(s) from database")

        except Exception as e:
            logging.error(f"[DELETE_MOVIE_FILES] Failed to delete from database: {e}")

        # Build detailed layer execution summary
        layers_executed = []
        layers_skipped = []

        # Database layer
        # if auto_ghostlisted:
        #     layers_executed.append('Database (Ghostlisted)')
        # else:
        layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Filesystem
        if delete_files:
            if file_count > 0:
                layers_executed.append(f'Filesystem ({file_count} file{"s" if file_count != 1 else ""})')
            else:
                layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed') for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                # Show count if available from batch removal
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0)
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up
        symlink_count = sum(len(r.get('deleted_symlinks', [])) for r in result.get('results', []))
        if delete_symlinks:
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0:
                layers_executed.append(f'Symlinks ({symlink_count} symlink{"s" if symlink_count != 1 else ""})')
            elif result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache
        cache_cleared = any(r.get('cache_cleared') for r in result.get('results', []))
        if clear_cache:
            if cache_cleared:
                layers_executed.append('Cache')
            else:
                layers_skipped.append({'layer': 'Cache', 'reason': 'No cache files found'})

        return jsonify({
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': result.get('errors', []),
            'auto_ghostlisted': auto_ghostlisted,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'content_source_removal': None  # File deletion doesn't remove from content sources
        })

    except Exception as e:
        logging.error(f"Error deleting movie files: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/delete_season/<imdb_id>/<int:season_number>', methods=['POST'])
@admin_required
def delete_season(imdb_id, season_number):
    """
    Delete entire season (all episodes in season)

    Request body:
    {
        "blacklist": bool,  # Whether to blacklist instead of permanent delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache", "content_source"]
    }
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_all_episodes_for_season
        from utilities.settings import get_setting

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)

        # Get all episodes for this season
        episodes = get_all_episodes_for_season(imdb_id, season_number)

        if not episodes:
            return jsonify({
                'success': False,
                'error': f'No episodes found for season {season_number}'
            }), 404

        episode_ids = [ep['id'] for ep in episodes]

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        # Use global setting for content source removal (ignores frontend layers)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)
        blacklist_sources = data.get('blacklist_sources', False)

        logging.info(f"[DELETE_SEASON] Starting deletion for show {imdb_id} season {season_number}")
        logging.info(f"[DELETE_SEASON] Found {len(episode_ids)} episodes to delete")
        logging.info(f"[DELETE_SEASON] Layers requested: {layers}")

        # ONLY check auto-ghostlist setting - this determines database behavior
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        #logging.info(f"[DELETE_SEASON] Auto-ghostlist setting: {auto_ghostlist} (type: {type(auto_ghostlist)})")

        # Additional debug - check what the setting value actually is
        # if auto_ghostlist:
        #     logging.info(f"[DELETE_SEASON] WILL GHOSTLIST - auto_ghostlist is truthy")
        # else:
        logging.info(f"[DELETE_SEASON] WILL DELETE - auto_ghostlist is falsy")

        # Note: Content source removal is NOT supported for season deletion
        # If user wants to remove from content source, they should delete the entire show
        if remove_from_content_source:
            logging.warning(f"[DELETE_SEASON] Content source removal requested but not supported for season deletion - use delete_show instead")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # Get show title from first episode for Plex content-level deletion
        show_title = episodes[0].get('title') if episodes else None
        # tmdb_id falls back to imdb_id if not available
        show_tmdb_id = (episodes[0].get('tmdb_id') or imdb_id) if episodes else imdb_id

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=episode_ids,
            blacklist=False,
            blacklist_sources=False,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Content source removal only in delete_show
            skip_database=True,  # We'll handle database ourselves based on auto_ghostlist
            force_delete_parent_folder=True,  # Force delete season folder even if not empty (symlink mode only, with Plex verification)
            # Plex content-level deletion params - deletes whole season in 1 API call
            plex_deletion_type='season',
            plex_content_title=show_title,
            plex_imdb_id=imdb_id,
            plex_tmdb_id=show_tmdb_id,
            plex_season_number=season_number
        )

        logging.info(f"[DELETE_SEASON] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_SEASON] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_SEASON] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_SEASON] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            response_data = {
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }
            if result.get('plex_not_found'):
                response_data['plex_not_found'] = True
            return jsonify(response_data), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        #logging.info(f"[DELETE_SEASON] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        # if auto_ghostlist:
        #     # Ghostlist: Update all episodes to ghostlisted=1 and state=Blacklisted
        #     logging.info(f"[DELETE_SEASON] Taking GHOSTLIST path")
        #     try:
        #         from database.core import get_db_connection
        #         from datetime import datetime

        #         conn = get_db_connection()
        #         cursor = conn.cursor()

        #         # Begin transaction
        #         cursor.execute('BEGIN TRANSACTION')

        #         # Ghostlist all episodes in this season (set ghostlisted=1 and state=Blacklisted)
        #         cursor.execute(
        #             'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({})'.format(','.join('?' * len(episode_ids))),
        #             ['Blacklisted', datetime.now()] + episode_ids
        #         )

        #         updated_count = cursor.rowcount
        #         conn.commit()
        #         conn.close()

        #         auto_ghostlisted = True
        #         logging.info(f"[DELETE_SEASON] Ghostlisted {updated_count} episodes for season {season_number} - ghostlisted=1, state=Blacklisted")

        #     except Exception as e:
        #         logging.error(f"[DELETE_SEASON] Failed to ghostlist: {e}")
        #         if conn:
        #             conn.rollback()
        #             conn.close()

        # else:
        # Delete: Remove all episodes from database
        logging.info(f"[DELETE_SEASON] Taking DELETE path")
        try:
            from database.database_writing import remove_from_media_items

            # Delete all episodes
            deleted_count = 0
            for episode_id in episode_ids:
                if remove_from_media_items(episode_id):
                    deleted_count += 1

            logging.info(f"[DELETE_SEASON] Deleted {deleted_count} episodes from database")
        except Exception as e:
            logging.error(f"[DELETE_SEASON] Failed to delete from database: {e}")

        # Build detailed layer execution summary (only report actual successes)
        layers_executed = []
        layers_skipped = []  # Track skipped/failed layers

        # Database layer - ghostlist or delete
        # if auto_ghostlisted:
        #     layers_executed.append('Database (Ghostlisted)')
        # else:
        layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                # Check if Plex is configured
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Filesystem - only if files actually deleted
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                # Check if debrid provider is available
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up
        symlink_count = sum(r.get('symlinks_deleted', 0) for r in result.get('results', []))
        if delete_symlinks:
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0 or result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache - only if actually cleared
        if clear_cache:
            layers_executed.append('Cache')

        # Build response with both 'error' (singular) and 'errors' (plural) for frontend compatibility
        errors_list = result.get('errors', [])
        response_data = {
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': errors_list,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'message': f"Season {season_number} deleted - {len(episode_ids)} episodes"
        }

        # Add 'error' (singular) field for frontend compatibility - uses first error if available
        if errors_list:
            response_data['error'] = errors_list[0]

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error deleting season {season_number} of show {imdb_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/mark_season_replace/<imdb_id>/<int:season_number>', methods=['POST'])
@admin_required
def mark_season_replace(imdb_id, season_number):
    """
    Mark all collected episodes in a season for replacement.
    Sets manual_replace=1 on each Collected episode so the collection hook
    can clean them up after the new season pack is collected.
    """
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            """UPDATE media_items SET manual_replace = 1, last_updated = ?
               WHERE imdb_id = ? AND season_number = ? AND type = 'episode'
               AND state NOT IN ('Blacklisted') AND (ghostlisted = 0 OR ghostlisted IS NULL)""",
            (datetime.now(), imdb_id, season_number)
        )
        marked_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_SEASON] Marked {marked_count} episodes for replacement: {imdb_id} S{season_number:02d}")
        return jsonify({'success': True, 'marked_count': marked_count})
    except Exception as e:
        logging.error(f"Error marking season {season_number} of {imdb_id} for replace: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/cancel_season_replace/<imdb_id>/<int:season_number>', methods=['POST'])
@admin_required
def cancel_season_replace(imdb_id, season_number):
    """
    Cancel a pending season replacement by clearing the manual_replace flag.
    """
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            """UPDATE media_items SET manual_replace = 0, last_updated = ?
               WHERE imdb_id = ? AND season_number = ? AND type = 'episode'
               AND manual_replace = 1""",
            (datetime.now(), imdb_id, season_number)
        )
        cleared_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_SEASON] Cancelled replacement for {cleared_count} episodes: {imdb_id} S{season_number:02d}")
        return jsonify({'success': True, 'cleared_count': cleared_count})
    except Exception as e:
        logging.error(f"Error cancelling season replace for {imdb_id} S{season_number}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/mark_movie_replace', methods=['POST'])
@admin_required
def mark_movie_replace():
    """
    Mark specific movie file entries for replacement.
    Sets manual_replace=1 on each provided file ID so the collection hook
    can clean them up after a new version is collected.
    """
    try:
        data = request.get_json()
        file_ids = data.get('file_ids', [])
        if not file_ids:
            return jsonify({'success': False, 'error': 'No file IDs provided'}), 400
        conn = get_db_connection()
        placeholders = ','.join(['?'] * len(file_ids))
        cursor = conn.execute(
            f"UPDATE media_items SET manual_replace = 1, last_updated = ? WHERE id IN ({placeholders}) AND type = 'movie'",
            [datetime.now()] + list(file_ids)
        )
        marked_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_MOVIE] Marked {marked_count} movie file(s) for replacement. IDs: {file_ids}")
        return jsonify({'success': True, 'marked_count': marked_count})
    except Exception as e:
        logging.error(f"Error marking movie files for replace: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/cancel_movie_replace', methods=['POST'])
@admin_required
def cancel_movie_replace():
    """
    Cancel a pending movie replacement by clearing the manual_replace flag.
    """
    try:
        data = request.get_json()
        imdb_id = data.get('imdb_id')
        if not imdb_id:
            return jsonify({'success': False, 'error': 'No imdb_id provided'}), 400
        conn = get_db_connection()
        cursor = conn.execute(
            "UPDATE media_items SET manual_replace = 0, last_updated = ? WHERE imdb_id = ? AND type = 'movie' AND manual_replace = 1",
            (datetime.now(), imdb_id)
        )
        cleared_count = cursor.rowcount
        conn.commit()
        conn.close()
        logging.info(f"[REPLACE_MOVIE] Cancelled replacement for {cleared_count} movie file(s): {imdb_id}")
        return jsonify({'success': True, 'cleared_count': cleared_count})
    except Exception as e:
        logging.error(f"Error cancelling movie replace for {imdb_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/delete_episode/<imdb_id>/<int:season_number>/<int:episode_number>', methods=['POST'])
@admin_required
def delete_episode(imdb_id, season_number, episode_number):
    """
    Delete specific episode (one or more files for the same episode number)

    Request body:
    {
        "item_ids": [123, 456],  # Specific database IDs to delete (for multi-file episodes)
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache"]
    }
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.core import get_db_connection

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        item_ids = data.get('item_ids', [])

        # If no specific item_ids provided, get all items for this episode
        if not item_ids:
            conn = get_db_connection()
            cursor = conn.execute('''
                SELECT id FROM media_items
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ? AND type = 'episode'
            ''', (imdb_id, season_number, episode_number))
            item_ids = [row['id'] for row in cursor.fetchall()]
            conn.close()

        if not item_ids:
            return jsonify({
                'success': False,
                'error': f'No files found for episode S{season_number:02d}E{episode_number:02d}'
            }), 404

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        remove_from_content_source = 'content_source' in layers

        logging.info(f"[DELETE_EPISODE] Starting deletion for {imdb_id} S{season_number:02d}E{episode_number:02d}")
        logging.info(f"[DELETE_EPISODE] Item IDs to delete: {item_ids}")
        logging.info(f"[DELETE_EPISODE] Layers requested: {layers}")

        # Note: Content source removal is NOT supported for episode deletion
        # If user wants to remove from content source, they should delete the entire show
        if remove_from_content_source:
            logging.warning(f"[DELETE_EPISODE] Content source removal requested but not supported for episode deletion - use delete_show instead")

        # ONLY check auto-ghostlist setting - this determines database behavior
        from utilities.settings import get_setting
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        # Check file collection management mode for appropriate deletion messages
        file_management_mode = get_setting('File Management', 'file_collection_management', default='Plex')

        #logging.info(f"[DELETE_EPISODE] Auto-ghostlist setting: {auto_ghostlist}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=item_ids,
            blacklist=False,
            blacklist_sources=False,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,
            skip_database=True  # We'll handle database ourselves based on auto_ghostlist
        )

        logging.info(f"[DELETE_EPISODE] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_EPISODE] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_EPISODE] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_EPISODE] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            return jsonify({
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        #logging.info(f"[DELETE_EPISODE] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        # if auto_ghostlist:
        #     # Ghostlist: Update episodes to ghostlisted=1 and state=Blacklisted
        #     logging.info(f"[DELETE_EPISODE] Taking GHOSTLIST path")
        #     try:
        #         from datetime import datetime

        #         conn = get_db_connection()
        #         cursor = conn.cursor()

        #         cursor.execute('BEGIN TRANSACTION')

        #         # Ghostlist specified episodes
        #         cursor.execute(
        #             'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({})'.format(','.join('?' * len(item_ids))),
        #             ['Blacklisted', datetime.now()] + item_ids
        #         )

        #         updated_count = cursor.rowcount
        #         conn.commit()
        #         conn.close()

        #         auto_ghostlisted = True
        #         logging.info(f"[DELETE_EPISODE] Ghostlisted {updated_count} file(s) for episode S{season_number:02d}E{episode_number:02d}")

        #     except Exception as e:
        #         logging.error(f"[DELETE_EPISODE] Failed to ghostlist: {e}")
        #         if conn:
        #             conn.rollback()
        #             conn.close()

        # else:
        # Delete: Remove episodes from database
        logging.info(f"[DELETE_EPISODE] Taking DELETE path")
        try:
            from database.database_writing import remove_from_media_items

            deleted_count = 0
            for item_id in item_ids:
                if remove_from_media_items(item_id):
                    deleted_count += 1

            logging.info(f"[DELETE_EPISODE] Deleted {deleted_count} file(s) from database")
        except Exception as e:
            logging.error(f"[DELETE_EPISODE] Failed to delete from database: {e}")

        # Build detailed layer execution summary
        layers_executed = []
        layers_skipped = []

        # Database layer
        # if auto_ghostlisted:
        #     layers_executed.append('Database (Ghostlisted)')
        # else:
        layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Filesystem
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                # In Plex mode, filesystem layer is not applicable since Plex manages files
                if file_management_mode == 'Plex':
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'Not applicable in Plex mode'})
                else:
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up (only relevant in Symlinked/Local mode)
        if file_management_mode != 'Plex':
            symlink_count = sum(r.get('symlinks_deleted', 0) for r in result.get('results', []))
            if delete_symlinks:
                # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
                if symlink_count > 0 or result['success']:
                    layers_executed.append('Symlinks')
                else:
                    layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache
        if clear_cache:
            layers_executed.append('Cache')

        return jsonify({
            'success': result['success'],
            'deleted_count': len(item_ids),
            'failed_count': result.get('failed_count', 0),
            'errors': result.get('errors', []),
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,
            'message': f"S{season_number:02d}E{episode_number:02d} deleted - {len(item_ids)} file(s)"
        })

    except Exception as e:
        logging.error(f"Error deleting episode {imdb_id} S{season_number:02d}E{episode_number:02d}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ========================================
# Movie Detail Routes
# ========================================

@library_bp.route('/movie/<media_id>')
@user_required
@onboarding_required
def movie_detail(media_id):
    """
    Movie detail page - displays movie metadata and files
    media_id can be either tmdb_id or imdb_id
    """
    from utilities.web_scraper import get_available_versions

    # Check if either Plex or TMDB is configured
    plex_url = get_setting('Plex', 'url', default='')
    plex_token = get_setting('Plex', 'token', default='')
    tmdb_api_key = get_setting('TMDB', 'api_key', default='')

    plex_configured = bool(plex_url and plex_token)
    tmdb_configured = bool(tmdb_api_key)
    versions = get_available_versions()

    # Check user permissions - if auth is disabled, grant all permissions
    from .utils import is_user_system_enabled
    if not is_user_system_enabled():
        has_admin_permissions = True
        has_user_permissions = True
    else:
        has_admin_permissions = current_user.is_authenticated and current_user.role == 'admin'
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('library_movie.html',
                         media_id=media_id,
                         plex_configured=plex_configured,
                         tmdb_configured=tmdb_configured,
                         versions=versions,
                         has_admin_permissions=has_admin_permissions,
                         has_user_permissions=has_user_permissions)

@library_bp.route('/movie/<media_id>/data')
@user_required
def movie_detail_data(media_id):
    """
    API endpoint to fetch movie details and files
    Returns movie metadata and file information
    media_id can be either tmdb_id or imdb_id
    """
    try:
        conn = get_db_connection()

        # Try to find movie by tmdb_id, imdb_id, or id
        movie_query = """
            SELECT
                id,
                title,
                year,
                tmdb_id,
                imdb_id,
                version,
                location_on_disk,
                collected_at,
                state,
                filled_by_file,
                content_source,
                content_source_detail,
                location_basename,
                release_date
            FROM media_items
            WHERE (tmdb_id = ? OR imdb_id = ?) AND type = 'movie'
            LIMIT 1
        """
        movie_data = conn.execute(movie_query, (str(media_id), str(media_id))).fetchone()

        # If not found and media_id is numeric, try by database id
        if not movie_data and str(media_id).isdigit():
            movie_query = """
                SELECT
                    id,
                    title,
                    year,
                    tmdb_id,
                    imdb_id,
                    version,
                    location_on_disk,
                    collected_at,
                    state,
                    filled_by_file,
                    content_source,
                    content_source_detail,
                    location_basename,
                    release_date
                FROM media_items
                WHERE id = ? AND type = 'movie'
                LIMIT 1
            """
            movie_data = conn.execute(movie_query, (int(media_id),)).fetchone()

        if not movie_data:
            conn.close()
            logging.warning(f"Movie {media_id} not found")
            return jsonify({
                'success': False,
                'error': f'Movie not found in library. ID: {media_id}'
            }), 404

        # Get ALL files for this movie (there can be multiple files for the same movie)
        # Use the same ID field that worked for finding the movie
        if movie_data['tmdb_id']:
            id_field = 'tmdb_id'
            id_value = movie_data['tmdb_id']
        elif movie_data['imdb_id']:
            id_field = 'imdb_id'
            id_value = movie_data['imdb_id']
        else:
            id_field = 'id'
            id_value = movie_data['id']

        all_files_query = f"""
            SELECT
                id,
                filled_by_file,
                filled_by_magnet,
                location_basename,
                state,
                collected_at,
                release_date,
                version,
                ghostlisted,
                size,
                manual_replace,
                ms_item_id,
                ms_audio_track,
                ms_subtitle_track
            FROM media_items
            WHERE {id_field} = ? AND type = 'movie'
            ORDER BY
                CASE
                    WHEN state = 'Collected' THEN 0
                    WHEN state = 'Blacklisted' THEN 1
                    ELSE 2
                END,
                collected_at DESC
        """
        all_movie_files = conn.execute(all_files_query, (id_value,)).fetchall()

        conn.close()

        # Fetch TMDB metadata if tmdb_id is available
        # If no tmdb_id but imdb_id exists, try to look up tmdb_id from imdb_id
        overview = None
        genres = None
        runtime = None
        rating = None
        vote_count = None
        tagline = None
        tmdb_poster_path = None
        tmdb_backdrop_path = None
        tmdb_id = movie_data['tmdb_id']

        # If no TMDB ID but IMDB ID exists, try to look it up
        if not tmdb_id and movie_data['imdb_id']:
            from utilities.web_scraper import get_tmdb_id_from_imdb
            logging.info(f"No TMDB ID for movie {movie_data['title']}, attempting lookup via IMDB ID {movie_data['imdb_id']}")
            tmdb_id = get_tmdb_id_from_imdb(movie_data['imdb_id'], 'movie')
            if tmdb_id:
                logging.info(f"Found TMDB ID {tmdb_id} for movie {movie_data['title']} via IMDB lookup")

        # Always use TMDB API for complete metadata (posters, backdrops, ratings)
        # This ensures all display fields are populated correctly
        if tmdb_id:
            tmdb_api_key = get_setting('TMDB', 'api_key')
            if tmdb_api_key:
                try:
                    details_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    logging.info(f"Fetching TMDB metadata for movie {tmdb_id} (Battery fallback)")
                    details_response = requests.get(details_url, timeout=15, headers={'Accept-Encoding': 'identity'})
                    details_response.raise_for_status()
                    details_data = details_response.json()

                    overview = details_data.get('overview', '')
                    genres_list = details_data.get('genres', [])
                    genres = ', '.join([g['name'] for g in genres_list]) if genres_list else None
                    runtime = details_data.get('runtime')  # Runtime in minutes
                    rating = details_data.get('vote_average')  # TMDB rating (0-10)
                    vote_count = details_data.get('vote_count')  # Number of votes
                    tagline = details_data.get('tagline', '')  # Movie tagline
                    tmdb_poster_path = details_data.get('poster_path')  # Poster path from TMDB
                    tmdb_backdrop_path = details_data.get('backdrop_path')  # Backdrop path from TMDB

                    logging.info(f"TMDB metadata fetched - overview: {len(overview) if overview else 0} chars, genres: {genres}, runtime: {runtime}, rating: {rating}, vote_count: {vote_count}, tagline: {tagline}, poster: {tmdb_poster_path}, backdrop: {tmdb_backdrop_path}")

                except Exception as e:
                    logging.error(f"Error fetching TMDB metadata for movie {tmdb_id}: {e}")
            else:
                logging.warning(f"TMDB API key not configured, skipping metadata fetch for movie {tmdb_id}")
        elif not battery_metadata:
            logging.warning(f"No TMDB ID or IMDb ID available for movie {movie_data['title']}, skipping metadata fetch")

        # Get content source display name
        content_sources = []
        if movie_data['content_source']:
            from queues.config_manager import get_content_source_display_names
            content_source_display_map = get_content_source_display_names()
            content_sources.append(content_source_display_map.get(movie_data['content_source'], movie_data['content_source']))

        # Get rclone mount path from settings with intelligent content folder handling
        from utilities.path_utils import get_mount_path_for_content, get_content_folder_path
        rclone_movies_path = get_mount_path_for_content(media_type='movie')

        # Extract storage path up to content folder (works for any depth, both Plex and Symlink modes)
        storage_path = get_content_folder_path(movie_data['location_on_disk'], media_type='movie') if movie_data['location_on_disk'] else None

        # Build file information from all movie files
        # Include ALL entries (both with and without files) to show duplicates with different states
        files = []
        largest_size = 0
        for file_row in all_movie_files:
            # Track largest file size
            file_size = file_row['size']
            if file_size is not None and file_size > largest_size:
                largest_size = file_size

            # Clean release_date to extract just the date part
            file_release_date = None
            if file_row['release_date']:
                file_release_date = str(file_row['release_date']).split('T')[0].split(' ')[0]

            # Include entry even if no filename (e.g., Blacklisted entries without files)
            files.append({
                'id': file_row['id'],
                'filename': file_row['filled_by_file'] or 'No file',
                'basename': file_row['location_basename'] or file_row['filled_by_file'] or 'No file',
                'state': file_row['state'],
                'collected_at': file_row['collected_at'],
                'release_date': file_release_date,
                'version': file_row['version'],
                'ghostlisted': file_row['ghostlisted'],
                'size': file_row['size'],
                'manual_replace': bool(file_row['manual_replace']),
                'ms_item_id': file_row['ms_item_id'],
                'ms_audio_track': file_row['ms_audio_track'],
                'ms_subtitle_track': file_row['ms_subtitle_track'],
                'filled_by_magnet': file_row['filled_by_magnet']
            })

        # Fetch poster and backdrop URLs from cache, fallback to TMDB if not cached
        from routes.poster_cache import get_cached_poster_url
        cache_id = movie_data['tmdb_id'] or media_id
        poster_url = get_cached_poster_url(cache_id, 'movie')
        backdrop_url = get_cached_poster_url(f"{cache_id}_backdrop", 'movie')
        
        # If not in cache, use TMDB paths directly
        if not poster_url and tmdb_poster_path:
            poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_poster_path}"
        if not backdrop_url and tmdb_backdrop_path:
            backdrop_url = f"https://image.tmdb.org/t/p/original{tmdb_backdrop_path}"

        # Get auto-ghostlist setting
        auto_ghostlist_enabled = get_setting('Library Manager', 'ghostlist_mode', False)

        from database.movie_release_overrides import get_movie_release_override
        release_override = get_movie_release_override(
            imdb_id=movie_data['imdb_id'],
            tmdb_id=movie_data['tmdb_id'],
        )

        # Fetch digital release date from TMDB (prioritizes digital, falls back to theatrical)
        tmdb_api_key = get_setting('TMDB', 'api_key', default='')
        clean_release_date = None
        release_type = 'theatrical'  # Default to theatrical
        if release_override:
            clean_release_date = release_override['release_date']
            release_type = 'manual'
        elif movie_data['tmdb_id'] and tmdb_api_key:
            # Try to get digital release date from TMDB
            release_info = get_digital_release_date(movie_data['tmdb_id'], 'movie', tmdb_api_key)
            if release_info and release_info.get('date'):
                clean_release_date = release_info['date']
                release_type = release_info.get('type', 'theatrical')
            elif movie_data['release_date']:
                # Fallback to database date if TMDB fetch fails
                clean_release_date = str(movie_data['release_date']).split('T')[0].split(' ')[0]
        elif movie_data['release_date']:
            # No TMDB, use database date
            clean_release_date = str(movie_data['release_date']).split('T')[0].split(' ')[0]

        # Fetch certification based on user's preferred region
        certification = ''
        if movie_data['tmdb_id'] and tmdb_api_key:
            certification_region = get_setting('TMDB', 'certification_region', 'US')
            certification = get_certification(movie_data['tmdb_id'], 'movie', tmdb_api_key, certification_region)

        response_data = {
            'success': True,
            'movie': {
                'id': movie_data['id'],
                'title': movie_data['title'],
                'year': movie_data['year'],
                'tmdb_id': movie_data['tmdb_id'],
                'imdb_id': movie_data['imdb_id'],
                'version': movie_data['version'] or 'Default',
                'location_on_disk': movie_data['location_on_disk'],
                'path': storage_path,
                'collected_at': movie_data['collected_at'],
                'state': movie_data['state'],
                'content_sources': content_sources,
                'rclone_path': rclone_movies_path,
                'release_date': clean_release_date,
                'release_type': release_type,
                'release_date_override': release_override['release_date'] if release_override else None,
                'poster_url': poster_url,
                'backdrop_url': backdrop_url,
                'overview': overview,
                'genres': genres,
                'certification': certification,
                'runtime': runtime,
                'rating': rating,
                'vote_count': vote_count,
                'tagline': tagline,
                'size': largest_size,
                'auto_ghostlist_enabled': auto_ghostlist_enabled
            },
            'files': files,
            'has_pending_replace': any(f['manual_replace'] for f in files)
        }

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error fetching movie data for {media_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@library_bp.route('/cast/<media_type>/<int:tmdb_id>')
@user_required
def get_cast(media_type, tmdb_id):
    """
    Fetch cast data from TMDB for a movie or TV show.
    media_type: 'movie' or 'tv'
    tmdb_id: TMDB ID of the media
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key')
        if not tmdb_api_key:
            return jsonify({'success': False, 'error': 'TMDB API key not configured'}), 400

        # Validate media type
        if media_type not in ['movie', 'tv']:
            return jsonify({'success': False, 'error': 'Invalid media type'}), 400

        # Fetch credits from TMDB
        credits_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/credits?api_key={tmdb_api_key}"
        response = requests.get(credits_url, timeout=10)
        response.raise_for_status()
        credits_data = response.json()

        # Extract top 10 cast members
        cast = credits_data.get('cast', [])[:10]
        simplified_cast = [
            {
                'name': person.get('name', ''),
                'character': person.get('character', ''),
                'profile_path': person.get('profile_path')
            }
            for person in cast
        ]

        # Extract director(s) from crew (for movies)
        directors = []
        if media_type == 'movie':
            crew = credits_data.get('crew', [])
            directors = [
                person.get('name', '')
                for person in crew
                if person.get('job', '').lower() == 'director'
            ]

        return jsonify({
            'success': True,
            'cast': simplified_cast,
            'directors': directors
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching cast for {media_type}/{tmdb_id}: {e}")
        return jsonify({'success': False, 'error': 'Failed to fetch cast data'}), 500
    except Exception as e:
        logging.error(f"Error processing cast for {media_type}/{tmdb_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/add_not_wanted_magnet', methods=['POST'])
@admin_required
def add_not_wanted_magnet():
    """Add magnet(s) from given item IDs to the not-wanted magnets list."""
    req = request.get_json(silent=True) or {}
    item_ids = req.get('item_ids', [])
    if not item_ids or not isinstance(item_ids, list):
        return jsonify({'success': False, 'error': 'item_ids list required'}), 400

    try:
        from database.not_wanted_magnets import add_to_not_wanted
        conn = get_db_connection()
        added = []
        skipped = []
        for item_id in item_ids:
            row = conn.execute(
                'SELECT filled_by_magnet, title, type FROM media_items WHERE id = ?',
                (item_id,)
            ).fetchone()
            if not row or not row['filled_by_magnet']:
                skipped.append(item_id)
                continue
            add_to_not_wanted(row['filled_by_magnet'])
            added.append({'id': item_id, 'magnet': row['filled_by_magnet'][:60]})
            logging.info(f"[NotWanted] Added magnet for item {item_id} ({row['title']}) to not-wanted list")
        conn.close()
        return jsonify({'success': True, 'added': len(added), 'skipped': len(skipped)})
    except Exception as e:
        logging.error(f"[NotWanted] Error adding to not-wanted: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/download_subtitles/item/<int:item_id>', methods=['POST'])
def download_subtitles_item(item_id):
    """Download subtitles for a single movie or episode."""
    try:
        from database.core import get_db_connection
        from utilities.settings import get_setting
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id, title, type, location_on_disk, filled_by_file FROM media_items WHERE id = ? AND state IN ("Collected", "Upgrading")',
            (item_id,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'error': 'Item not found or not collected'}), 404

        location = row['location_on_disk'] or ''
        filled_by_file = row['filled_by_file'] or ''
        if not location or not filled_by_file:
            return jsonify({'success': False, 'error': 'No file path available'}), 400

        is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
        if is_plex:
            from utilities.downsub import download_subtitles_for_video
            import os
            # Remap /debrid/ to actual mount path
            from utilities.settings import load_config
            import json as _json
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            mount_base = ''
            if data_path:
                try:
                    with open(os.path.join(data_path, 'config.json')) as f:
                        dc_cfg = _json.load(f)
                    mount_base = (dc_cfg.get('mount', {}).get('mount_path') or '').rstrip('/')
                except Exception:
                    pass
            if mount_base:
                parts = location.split('/', 3)
                if len(parts) >= 3 and parts[1] == 'debrid':
                    location = mount_base + '/' + '/'.join(parts[2:])
            full_path = os.path.join(location, filled_by_file) if not location.endswith(filled_by_file) else location
        else:
            full_path = location

        from utilities.downsub import download_subtitles_for_video
        found = download_subtitles_for_video(full_path)
        if found:
            return jsonify({'success': True, 'message': f'Subtitles downloaded for {row["title"]}'})
        else:
            return jsonify({'success': False, 'message': f'No subtitles found for {row["title"]}'})
    except Exception as e:
        logging.error(f"[DownSub] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/download_subtitles/season', methods=['POST'])
def download_subtitles_season():
    """Download subtitles for all episodes in a season."""
    try:
        from database.core import get_db_connection
        from utilities.settings import get_setting, load_config
        import os, json as _json, threading
        data = request.get_json() or {}
        imdb_id = data.get('imdb_id')
        season_number = data.get('season_number')
        if not imdb_id or season_number is None:
            return jsonify({'success': False, 'error': 'imdb_id and season_number required'}), 400

        conn = get_db_connection()
        rows = conn.execute(
            'SELECT location_on_disk, filled_by_file FROM media_items WHERE imdb_id = ? AND season_number = ? AND state IN ("Collected", "Upgrading") AND type = "episode"',
            (imdb_id, season_number)
        ).fetchall()
        conn.close()

        if not rows:
            return jsonify({'success': False, 'error': 'No collected episodes found'}), 404

        is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
        mount_base = ''
        if is_plex:
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            if data_path:
                try:
                    with open(os.path.join(data_path, 'config.json')) as f:
                        dc_cfg = _json.load(f)
                    mount_base = (dc_cfg.get('mount', {}).get('mount_path') or '').rstrip('/')
                except Exception:
                    pass

        paths = []
        for row in rows:
            loc = row['location_on_disk'] or ''
            fbf = row['filled_by_file'] or ''
            if not loc or not fbf:
                continue
            if is_plex:
                if mount_base:
                    parts = loc.split('/', 3)
                    if len(parts) >= 3 and parts[1] == 'debrid':
                        loc = mount_base + '/' + '/'.join(parts[2:])
                paths.append(os.path.join(loc, fbf) if not loc.endswith(fbf) else loc)
            else:
                # Symlink mode: location_on_disk is already the full path to the symlink file
                paths.append(loc)

        def _run():
            from utilities.downsub import download_subtitles_for_video
            found = sum(1 for p in paths if download_subtitles_for_video(p))
            logging.info(f"[DownSub] Season done: {found}/{len(paths)} subtitles downloaded")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'success': True, 'message': f'Searching subtitles for {len(paths)} episodes (check logs for results)'})
    except Exception as e:
        logging.error(f"[DownSub] Season error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/download_subtitles/show', methods=['POST'])
def download_subtitles_show():
    """Download subtitles for all collected episodes in a show."""
    try:
        from database.core import get_db_connection
        from utilities.settings import get_setting, load_config
        import os, json as _json, threading
        data = request.get_json() or {}
        imdb_id = data.get('imdb_id')
        if not imdb_id:
            return jsonify({'success': False, 'error': 'imdb_id required'}), 400

        conn = get_db_connection()
        rows = conn.execute(
            'SELECT location_on_disk, filled_by_file FROM media_items WHERE imdb_id = ? AND state IN ("Collected", "Upgrading") AND type = "episode"',
            (imdb_id,)
        ).fetchall()
        conn.close()

        if not rows:
            return jsonify({'success': False, 'error': 'No collected episodes found'}), 404

        is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
        mount_base = ''
        if is_plex:
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            if data_path:
                try:
                    with open(os.path.join(data_path, 'config.json')) as f:
                        dc_cfg = _json.load(f)
                    mount_base = (dc_cfg.get('mount', {}).get('mount_path') or '').rstrip('/')
                except Exception:
                    pass

        paths = []
        for row in rows:
            loc = row['location_on_disk'] or ''
            fbf = row['filled_by_file'] or ''
            if not loc or not fbf:
                continue
            if is_plex:
                if mount_base:
                    parts = loc.split('/', 3)
                    if len(parts) >= 3 and parts[1] == 'debrid':
                        loc = mount_base + '/' + '/'.join(parts[2:])
                paths.append(os.path.join(loc, fbf) if not loc.endswith(fbf) else loc)
            else:
                # Symlink mode: location_on_disk is already the full path to the symlink file
                paths.append(loc)

        def _run():
            from utilities.downsub import download_subtitles_for_video
            found = sum(1 for p in paths if download_subtitles_for_video(p))
            logging.info(f"[DownSub] Show done: {found}/{len(paths)} subtitles downloaded")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'success': True, 'message': f'Searching subtitles for {len(paths)} episodes (check logs for results)'})
    except Exception as e:
        logging.error(f"[DownSub] Show error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
