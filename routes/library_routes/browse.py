"""Library page and its infinite-scroll data feed."""

from flask import render_template, jsonify, request
from flask_login import current_user
from ..models import user_required, onboarding_required
from database.core import get_db_connection
from utilities.settings import get_setting, get_nas_paths
import logging
from ._common import ITEMS_PER_BATCH, MAX_BATCH_SIZE, library_bp

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
    from ..utils import is_user_system_enabled
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
        # Accent-insensitive ("pokemon" finds "Pokémon") and alias-aware
        # ("money heist" finds "La casa de papel" via battery alternative titles).
        if search_term:
            from utilities.title_search import register_fold_function, title_search_clause
            register_fold_function(conn)
            clause, search_params = title_search_clause(search_term)
            query += f" AND {clause}"
            count_query += f" AND {clause}"
            params.extend(search_params)
            count_params.extend(search_params)

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
