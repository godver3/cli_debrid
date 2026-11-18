"""Movie release-date overrides, movie metadata refresh, trailers, cache clear."""

from flask import jsonify, request
from flask_login import current_user
from ..models import user_required, admin_required
from database.core import get_db_connection
from utilities.settings import get_setting
import logging
from datetime import datetime, timedelta
from ..poster_cache import load_cache, save_cache
from ._common import library_bp

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
