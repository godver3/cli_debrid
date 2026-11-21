"""Movie detail page/data, cast, not-wanted magnet."""

from flask import render_template, jsonify, request
from flask_login import current_user
from ..models import user_required, onboarding_required, admin_required
from database.core import get_db_connection
from utilities.settings import get_setting
import logging
import requests
from ..discover_routes import get_digital_release_date, get_certification
from utilities.tmdb_cache import get_tmdb_json
from ._common import library_bp

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
    from ..utils import is_user_system_enabled
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
                    details_data = get_tmdb_json(details_url, timeout=15)

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
        else:
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
        credits_data = get_tmdb_json(credits_url)

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
