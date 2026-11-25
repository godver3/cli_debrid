"""Plex image proxy, poster cache status and poster fetches."""

from flask import jsonify, Response
from ..models import user_required
from utilities.settings import get_setting
import logging
import requests
from ._common import library_bp

# Keep-alive to the Plex server across the many poster requests of a page
_plex_image_session = requests.Session()

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
        response = _plex_image_session.get(full_url, timeout=10)

        if response.status_code == 200:
            # Plex thumb paths end in the artwork's update timestamp
            # (/library/metadata/<id>/thumb/<ts>), so a changed poster gets a new
            # URL and the browser can keep this one. Unversioned paths keep the
            # enable_caching behaviour.
            if plex_path.rstrip('/').rsplit('/', 1)[-1].isdigit():
                cache_header = 'public, max-age=604800'
            elif get_setting('UI Settings', 'enable_caching', False):
                cache_header = 'public, max-age=86400'
            else:
                cache_header = 'no-cache'

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
