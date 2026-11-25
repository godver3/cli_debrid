"""Add to library, details popup/page data and season episodes."""

import logging
import requests
from flask import render_template, jsonify, request
from flask_login import current_user
from utilities.tmdb_cache import cache_response, get_cached_db_statuses
from utilities.settings import get_setting
from utilities.reverse_parser import get_default_version
from routes.models import user_required
from ._common import discover_bp
from .enrich import _season_list, _show_order_seasons, add_db_status_and_episode_info, get_certification, get_digital_release_date

@discover_bp.route('/api/add', methods=['POST'])
@user_required
def add_to_library():
    """
    Add discovered content to the library/wanted list
    Integrates with existing content source system
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        tmdb_id = data.get('tmdb_id')
        media_type = data.get('media_type')
        title = data.get('title', '')

        if not tmdb_id or not media_type:
            return jsonify({'error': 'tmdb_id and media_type are required'}), 400

        # Get TMDB API key for fetching additional details
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Fetch IMDB ID from TMDB
        if media_type == 'tv':
            external_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
        else:
            external_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={tmdb_api_key}"

        external_response = requests.get(external_url, timeout=10)
        external_response.raise_for_status()
        external_data = external_response.json()

        imdb_id = external_data.get('imdb_id')

        if not imdb_id:
            return jsonify({'error': 'Could not find IMDB ID for this content'}), 404

        # Return IMDB ID for the frontend to handle via scraper
        # The scraper page already has robust add-to-library functionality
        return jsonify({
            'success': True,
            'message': f'Found IMDB ID for {title}',
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'media_type': media_type,
            'title': title
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB external IDs API error: {e}")
        return jsonify({'error': 'Failed to fetch content details'}), 500
    except Exception as e:
        logging.error(f"Discover add error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/details/<int:tmdb_id>')
@user_required
@cache_response('details')
def get_details(tmdb_id):
    """
    Get detailed information about a specific item
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        media_type = request.args.get('type', 'movie')

        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,videos,external_ids"
        else:
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,videos,external_ids"

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Fetch digital release date separately — do not overwrite theatrical release_date
        digital_release_date_1895 = None
        if media_type == 'movie':
            digital_date = get_digital_release_date(tmdb_id, 'movie', tmdb_api_key)
            if digital_date:
                digital_release_date_1895 = digital_date

        # Get certification based on user's preferred region
        certification_region = get_setting('TMDB', 'certification_region', 'US')
        certification = get_certification(tmdb_id, media_type, tmdb_api_key, certification_region)

        # Get database status
        tmdb_id_str = str(tmdb_id)
        db_statuses_result = get_cached_db_statuses([tmdb_id_str])  # Returns Dict[str, str]
        db_status = 'missing'
        if db_statuses_result:
            db_status = db_statuses_result.get(tmdb_id_str, 'missing')  # type: ignore[arg-type]

        # Build response
        result = {
            'id': data['id'],
            'title': data.get('title') or data.get('name', ''),
            'overview': data.get('overview', ''),
            'poster_path': data.get('poster_path'),
            'backdrop_path': data.get('backdrop_path'),
            'release_date': data.get('release_date') or data.get('first_air_date', ''),
            'vote_average': data.get('vote_average', 0),
            'vote_count': data.get('vote_count', 0),
            'runtime': data.get('runtime') or data.get('episode_run_time', [None])[0] if data.get('episode_run_time') else None,
            'genres': [g['name'] for g in data.get('genres', [])],
            'status': data.get('status', ''),
            'tagline': data.get('tagline', ''),
            'certification': certification,
            'imdb_id': data.get('external_ids', {}).get('imdb_id') or data.get('imdb_id'),
            'media_type': media_type,
            'db_status': db_status,
            'digital_release_date': digital_release_date_1895,
        }

        # Add TV-specific fields
        if media_type == 'tv':
            result['number_of_seasons'] = data.get('number_of_seasons', 0)
            result['number_of_episodes'] = data.get('number_of_episodes', 0)
            result['in_production'] = data.get('in_production', False)

        # Add credits (limited)
        credits = data.get('credits', {})
        result['cast'] = [
            {'id': c.get('id'), 'name': c['name'], 'character': c.get('character', ''), 'profile_path': c.get('profile_path')}
            for c in credits.get('cast', [])[:10]
        ]

        # Add trailer
        videos = data.get('videos', {}).get('results', [])
        trailers = [v for v in videos if v.get('type') == 'Trailer' and v.get('site') == 'YouTube']
        if trailers:
            result['trailer_key'] = trailers[0].get('key')

        return jsonify(result)

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB details API error: {e}")
        return jsonify({'error': 'Failed to fetch content details'}), 500
    except Exception as e:
        logging.error(f"Discover details error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/details/<int:tmdb_id>/<media_type>')
@user_required
def details_page(tmdb_id, media_type):
    """
    Detail page for missing/not-in-library content
    Shows TMDB data with option to request/search
    """
    from utilities.web_scraper import get_available_versions
    from ..utils import is_user_system_enabled

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    versions = get_available_versions()

    # Check permissions - User and Admin can search, Requester cannot
    if not is_user_system_enabled():
        has_user_permissions = True
    else:
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('discover_details.html',
                         tmdb_id=tmdb_id,
                         media_type=media_type,
                         tmdb_configured=bool(tmdb_api_key),
                         versions=versions,
                         has_user_permissions=has_user_permissions)

@discover_bp.route('/details/<int:tmdb_id>/<media_type>/data')
@user_required
def details_data(tmdb_id, media_type):
    """
    API endpoint to fetch TMDB details for missing content
    Returns full metadata for display on detail page

    For discover details, always use TMDB API to ensure complete metadata
    (posters, backdrops, ratings, etc.) regardless of library status.
    Battery is better suited for library detail pages.
    """
    try:
        # Get TMDB API key early (needed for all requests)
        tmdb_api_key = get_setting('TMDB', 'api_key', '')

        # Check library status for db_status field
        in_library = False
        try:
            from database.core import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            if media_type == 'tv':
                cursor.execute("SELECT COUNT(*) FROM media_items WHERE tmdb_id = ? AND type = 'episode' LIMIT 1", (str(tmdb_id),))
            else:
                cursor.execute("SELECT COUNT(*) FROM media_items WHERE tmdb_id = ? AND type = 'movie' LIMIT 1", (str(tmdb_id),))
            in_library = cursor.fetchone()[0] > 0
            cursor.close()
            conn.close()
        except Exception as e:
            logging.debug(f"Library check failed for TMDB {tmdb_id}: {e}")

        # Always use TMDB API for discover details to ensure complete metadata
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Determine endpoint based on media type
        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,external_ids,content_ratings"
        else:
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US&append_to_response=credits,external_ids,release_dates"

        logging.info(f"Fetching TMDB metadata for {tmdb_id}")
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        # For TV shows: Use TVDB for seasons/episodes if TVDB API key is configured
        # This ensures proper season splits (not TMDB absolute numbering for anime)
        tvdb_slug = None
        if media_type == 'tv':
            tvdb_api_key = get_setting('TVDB', 'api_key', '')
            if tvdb_api_key:
                try:
                    from cli_battery.app import tvdb_client

                    # Get IMDb ID from TMDB response
                    imdb_id_from_tmdb = data.get('external_ids', {}).get('imdb_id')
                    if imdb_id_from_tmdb:
                        logging.info(f"Fetching TVDB seasons for {tmdb_id} (IMDb: {imdb_id_from_tmdb}) for proper season structure")

                        # Fetch show data from TVDB (returns full show metadata including seasons)
                        tvdb_show_data = tvdb_client.get_show_data(imdb_id_from_tmdb)

                        if tvdb_show_data:
                            tvdb_slug = tvdb_show_data.get('ids', {}).get('slug')

                        if tvdb_show_data and 'seasons' in tvdb_show_data:
                            # TVDB seasons format: {season_number: {'episode_count': N, 'episodes': {...}}}
                            # TMDB seasons format: [{'season_number': N, 'episode_count': M}, ...]
                            # Convert TVDB dict format to TMDB list format
                            tvdb_seasons_dict = tvdb_show_data['seasons']
                            tvdb_seasons_list = []
                            for season_num, season_data in tvdb_seasons_dict.items():
                                if isinstance(season_data, dict):
                                    tvdb_seasons_list.append({
                                        'season_number': int(season_num) if str(season_num).isdigit() else season_num,
                                        'episode_count': season_data.get('episode_count', 0),
                                        'name': f"Season {season_num}",
                                    })

                            # Sort by season number
                            tvdb_seasons_list.sort(key=lambda s: s['season_number'] if isinstance(s['season_number'], int) else 999)

                            # Replace TMDB seasons with TVDB seasons (proper structure, English titles)
                            data['seasons'] = tvdb_seasons_list
                            data['number_of_seasons'] = len([s for s in tvdb_seasons_list if s.get('season_number', 0) > 0])
                            # Recalculate total episode count from TVDB seasons (exclude season 0)
                            data['number_of_episodes'] = sum([s.get('episode_count', 0) for s in tvdb_seasons_list if s.get('season_number', 0) > 0])
                            logging.info(f"✅ Using TVDB seasons for proper structure ({data['number_of_seasons']} seasons, {data['number_of_episodes']} total episodes)")
                        else:
                            logging.debug(f"TVDB returned no season data, using TMDB seasons as fallback")
                except Exception as e:
                    logging.warning(f"Failed to fetch TVDB seasons for {tmdb_id}: {e}")
                    import traceback
                    logging.debug(f"TVDB error traceback: {traceback.format_exc()}")
                    # Continue with TMDB seasons as fallback

            # A show set to another episode order (Dragon Ball Kai in DVD order) is
            # shown in that order, as the library has it
            ordered = _show_order_seasons((data.get('external_ids') or {}).get('imdb_id'))
            if ordered:
                ordered_list = _season_list(ordered)
                data['seasons'] = ordered_list
                data['number_of_seasons'] = len([s for s in ordered_list if s['season_number'] > 0])
                data['number_of_episodes'] = sum(s['episode_count'] for s in ordered_list if s['season_number'] > 0)

        # Enrich with digital release date if it's a movie — store separately, don't overwrite theatrical date
        digital_release_date = None
        if media_type == 'movie':
            digital_date = get_digital_release_date(tmdb_id, 'movie', tmdb_api_key)
            if digital_date and digital_date.get('date'):
                digital_release_date = digital_date['date']

        # Build response object
        if media_type == 'tv':
            # Get content rating (US)
            content_rating = ''
            if 'content_ratings' in data and 'results' in data['content_ratings']:
                for rating in data['content_ratings']['results']:
                    if rating.get('iso_3166_1') == 'US':
                        content_rating = rating.get('rating', '')
                        break

            # Safe extraction of list fields (handle both TMDB and Battery formats)
            genres = data.get('genres', [])
            genres_list = [g['name'] for g in genres] if isinstance(genres, list) and all(isinstance(g, dict) for g in genres) else (genres if isinstance(genres, list) else [])

            networks = data.get('networks', [])
            networks_list = [n['name'] for n in networks] if isinstance(networks, list) and all(isinstance(n, dict) for n in networks) else (networks if isinstance(networks, list) else [])

            created_by = data.get('created_by', [])
            created_by_list = [c['name'] for c in created_by] if isinstance(created_by, list) and all(isinstance(c, dict) for c in created_by) else (created_by if isinstance(created_by, list) else [])

            result = {
                'id': data.get('id'),
                'title': data.get('name') or data.get('title'),
                'original_title': data.get('original_name') or data.get('original_title'),
                'year': str(data.get('first_air_date', ''))[:4] if data.get('first_air_date') else '',
                'overview': data.get('overview'),
                'poster_path': data.get('poster_path'),
                'backdrop_path': data.get('backdrop_path'),
                'vote_average': data.get('vote_average'),
                'vote_count': data.get('vote_count'),
                'genres': genres_list,
                'first_air_date': data.get('first_air_date'),
                'last_air_date': data.get('last_air_date'),
                'status': data.get('status'),
                'number_of_seasons': data.get('number_of_seasons'),
                'number_of_episodes': data.get('number_of_episodes'),
                'episode_run_time': data.get('episode_run_time', []) if isinstance(data.get('episode_run_time'), list) else [],
                'networks': networks_list,
                'created_by': created_by_list,
                'content_rating': content_rating,
                'media_type': 'tv',
                'tmdb_id': data.get('id'),
                'imdb_id': data.get('external_ids', {}).get('imdb_id') if isinstance(data.get('external_ids'), dict) else None,
                'tvdb_id': data.get('external_ids', {}).get('tvdb_id') if isinstance(data.get('external_ids'), dict) else None,
                'tvdb_slug': tvdb_slug,
                'seasons': data.get('seasons', []) if isinstance(data.get('seasons'), list) else []
            }
        else:
            # Get certification (US)
            certification = ''
            if 'release_dates' in data and 'results' in data['release_dates']:
                for release in data['release_dates']['results']:
                    if release.get('iso_3166_1') == 'US':
                        for rd in release.get('release_dates', []):
                            if rd.get('certification'):
                                certification = rd.get('certification')
                                break
                        break

            # Safe extraction of list fields (handle both TMDB and Battery formats)
            genres = data.get('genres', [])
            genres_list = [g['name'] for g in genres] if isinstance(genres, list) and all(isinstance(g, dict) for g in genres) else (genres if isinstance(genres, list) else [])

            result = {
                'id': data.get('id'),
                'title': data.get('title'),
                'original_title': data.get('original_title'),
                'year': str(data.get('release_date', ''))[:4] if data.get('release_date') else '',
                'overview': data.get('overview'),
                'poster_path': data.get('poster_path'),
                'backdrop_path': data.get('backdrop_path'),
                'vote_average': data.get('vote_average'),
                'vote_count': data.get('vote_count'),
                'genres': genres_list,
                'release_date': data.get('release_date'),
                'runtime': data.get('runtime'),
                'status': data.get('status'),
                'budget': data.get('budget'),
                'revenue': data.get('revenue'),
                'tagline': data.get('tagline'),
                'certification': certification,
                'media_type': 'movie',
                'tmdb_id': data.get('id'),
                'imdb_id': data.get('imdb_id') or (data.get('external_ids', {}).get('imdb_id') if isinstance(data.get('external_ids'), dict) else None),
                'digital_release_date': digital_release_date,
            }

        # Get cast (top 10) - safe extraction
        if 'credits' in data and isinstance(data.get('credits'), dict) and 'cast' in data['credits']:
            cast = data['credits']['cast']
            if isinstance(cast, list):
                result['cast'] = [
                    {'id': c.get('id'), 'name': c.get('name', ''), 'character': c.get('character', ''), 'profile_path': c.get('profile_path')}
                    for c in cast[:10] if isinstance(c, dict)
                ]

        # Get director/creator - safe extraction
        if 'credits' in data and isinstance(data.get('credits'), dict) and 'crew' in data['credits']:
            crew = data['credits']['crew']
            if isinstance(crew, list):
                seen_directors = set()
                directors = []
                for c in crew:
                    if not isinstance(c, dict) or c.get('job') != 'Director':
                        continue
                    name = c.get('name')
                    if not name or name in seen_directors:
                        continue
                    seen_directors.add(name)
                    directors.append({'id': c.get('id'), 'name': name})
                result['directors'] = directors

        # Add default version from settings for scraping
        result['default_version'] = get_default_version()

        # Add database status (use Battery for accurate counts on detail page)
        add_db_status_and_episode_info([result], use_battery=True)

        return jsonify(result)

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB API error for {media_type}/{tmdb_id}: {e}")
        return jsonify({'error': 'Failed to fetch data from TMDB'}), 500
    except Exception as e:
        logging.error(f"Error fetching details for {media_type}/{tmdb_id}: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/details/<int:tmdb_id>/tv/season/<int:season_number>')
@user_required
def season_episodes(tmdb_id, season_number):
    """
    API endpoint to fetch episode details for a specific season
    Uses TVDB if API key configured (for proper season structure), otherwise TMDB
    Returns episode list with air dates, names, and runtime, merged with database info
    """
    try:
        from database.core import get_db_connection

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Try TVDB first if API key is configured (for proper season structure)
        tvdb_api_key = get_setting('TVDB', 'api_key', '')
        data = None

        logging.info(f"Season episodes request for TMDB {tmdb_id}, season {season_number}")
        logging.info(f"TVDB API key configured: {bool(tvdb_api_key)}")

        # Fetch episode list from TMDB live API.
        # If TVDB is configured and TMDB doesn't have this season number (e.g. anime where TVDB
        # splits seasons differently), fall back to TVDB episode data so that episode numbers
        # shown on Discover always match the TVDB structure used by the scraper/battery.
        logging.info(f"Fetching TMDB episodes for season {season_number} of TMDB ID {tmdb_id}")
        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={tmdb_api_key}&language=en-US"
        response = requests.get(url, timeout=15)

        tmdb_404 = response.status_code == 404
        if not tmdb_404:
            response.raise_for_status()
            data = response.json()
        else:
            data = None
            logging.info(f"TMDB returned 404 for season {season_number} of TMDB ID {tmdb_id} — will try TVDB fallback")

        # A show set to another episode order (Show options) is listed in that
        # order. TMDB numbers its episodes differently, so its stills and
        # overviews would land on the wrong episodes: leave them out.
        tvdb_ep_map = {}
        ordered_season = None
        try:
            from cli_battery.app.direct_api import DirectAPI as _DirectAPI
            order_imdb_id, _ = _DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='tv')
            ordered = _show_order_seasons(order_imdb_id)
            if ordered:
                ordered_season = ordered.get(season_number) or ordered.get(str(season_number)) or {}
        except Exception as e:
            logging.debug(f"Episode order lookup failed for TMDB {tmdb_id}: {e}")
        if ordered_season is not None:
            tvdb_ep_map = {int(k) if str(k).isdigit() else k: v
                           for k, v in (ordered_season.get('episodes') or {}).items() if isinstance(v, dict)}
            if not tvdb_ep_map:
                return jsonify({'error': f'Season {season_number} is not in this show\'s episode order'}), 404
            data = None
        # Pre-fetch TVDB data once if TVDB is configured (used for supplement and/or fallback)
        elif tvdb_api_key:
            try:
                from cli_battery.app import tvdb_client
                from cli_battery.app.direct_api import DirectAPI

                imdb_id, source = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='tv')
                if imdb_id:
                    tvdb_show_data = tvdb_client.get_show_data(imdb_id)
                    if tvdb_show_data and 'seasons' in tvdb_show_data:
                        tvdb_seasons = tvdb_show_data['seasons']
                        season_data = tvdb_seasons.get(season_number) or tvdb_seasons.get(str(season_number))
                        if season_data and 'episodes' in season_data:
                            for ep_num, ep_data in season_data['episodes'].items():
                                if isinstance(ep_data, dict):
                                    key = int(ep_num) if str(ep_num).isdigit() else ep_num
                                    tvdb_ep_map[key] = ep_data
            except Exception as e:
                logging.debug(f"TVDB pre-fetch failed for season {season_number}: {e}")

        if tvdb_ep_map:
            # TVDB is configured and has episode data for this season — TVDB is authoritative for
            # the episode list (correct count and numbering per TVDB structure). TMDB data is used
            # only to supplement stills and overviews where available.
            tmdb_ep_map = {}
            if data:
                for ep in data.get('episodes', []):
                    ep_num = ep.get('episode_number')
                    if ep_num is not None:
                        tmdb_ep_map[ep_num] = ep

            tvdb_episodes_list = []
            for ep_num in sorted(tvdb_ep_map.keys()):
                ep_data = tvdb_ep_map[ep_num]
                air_date = ep_data.get('first_aired', '')
                if air_date and 'T' in air_date:
                    air_date = air_date.split('T')[0]
                tmdb_ep = tmdb_ep_map.get(ep_num, {})
                tvdb_episodes_list.append({
                    'episode_number': ep_num,
                    'name': ep_data.get('title') or ep_data.get('name') or tmdb_ep.get('name'),
                    'air_date': air_date or tmdb_ep.get('air_date') or None,
                    'runtime': ep_data.get('runtime') or tmdb_ep.get('runtime'),
                    'overview': tmdb_ep.get('overview') or ep_data.get('overview'),
                    'still_path': tmdb_ep.get('still_path'),
                })

            data = {
                'season_number': season_number,
                'name': data.get('name') if data else f"Season {season_number}",
                'air_date': data.get('air_date') if data else (tvdb_episodes_list[0]['air_date'] if tvdb_episodes_list else None),
                'episodes': tvdb_episodes_list,
            }
            logging.info(f"Using TVDB episode structure for season {season_number} of TMDB ID {tmdb_id} ({len(tvdb_episodes_list)} episodes)")
        elif tmdb_404:
            # TMDB 404 and no TVDB data — nothing to show
            logging.warning(f"TMDB 404 and no TVDB data for season {season_number} of TMDB ID {tmdb_id}")
            return jsonify({'error': 'Season not found on TMDB or TVDB'}), 404

        # Query database for episodes in this show/season
        conn = get_db_connection()
        db_episodes_query = """
            SELECT
                id,
                season_number,
                episode_number,
                episode_title,
                state,
                version,
                filled_by_file,
                collected_at,
                release_date,
                airtime,
                content_source,
                content_source_detail,
                imdb_id,
                location_basename,
                location_on_disk,
                size
            FROM media_items
            WHERE tmdb_id = ? AND season_number = ? AND type = 'episode' AND (ghostlisted = 0 OR ghostlisted IS NULL)
            ORDER BY episode_number ASC
        """
        try:
            db_episodes = conn.execute(db_episodes_query, (str(tmdb_id), season_number)).fetchall()
        finally:
            conn.close()

        # Create a lookup dict for DB episodes by episode_number
        # Handle multiple files per episode (store as list)
        db_episodes_map = {}
        for db_ep in db_episodes:
            ep_num = db_ep['episode_number']
            if ep_num not in db_episodes_map:
                db_episodes_map[ep_num] = []
            db_episodes_map[ep_num].append(dict(db_ep))

        # Build episode list merged with DB data
        episodes = []
        for ep in data.get('episodes', []):
            ep_num = ep.get('episode_number')
            episode_data = {
                'episode_number': ep_num,
                'name': ep.get('name'),
                'air_date': ep.get('air_date'),
                'runtime': ep.get('runtime'),
                'overview': ep.get('overview'),
                'still_path': ep.get('still_path')
            }

            # Merge DB data if this episode exists in database
            if ep_num in db_episodes_map:
                db_files = db_episodes_map[ep_num]
                # Use the first file's data for primary episode info
                primary_file = db_files[0]

                episode_data['db_data'] = {
                    'state': primary_file['state'],
                    'version': primary_file['version'],
                    'collected_at': primary_file['collected_at'],
                    'content_source': primary_file['content_source'],
                    'content_source_detail': primary_file['content_source_detail'],
                    'imdb_id': primary_file['imdb_id'],
                    'files': [
                        {
                            'id': f['id'],
                            'basename': f['location_basename'],
                            'path': f['location_on_disk'],
                            'size': f['size'],
                            'version': f['version'],
                            'filled_by_file': f['filled_by_file'],
                            'collected_at': f['collected_at']
                        }
                        for f in db_files
                    ]
                }

            episodes.append(episode_data)

        return jsonify({
            'season_number': data.get('season_number'),
            'name': data.get('name'),
            'air_date': data.get('air_date'),
            'episodes': episodes
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB API error for tv/{tmdb_id}/season/{season_number}: {e}")
        return jsonify({'error': 'Failed to fetch season data from TMDB'}), 500
    except Exception as e:
        logging.error(f"Error fetching season {season_number} for tv/{tmdb_id}: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/addmedia')
@user_required
def addmedia():
    """
    Dedicated page for adding TV shows from discover page
    Displays season/episode selection without search functionality
    """
    # Get parameters from query string
    media_id = request.args.get('id', type=int)
    title = request.args.get('title', '')
    year = request.args.get('year', '')
    media_type = request.args.get('type', 'tv')
    rating = request.args.get('rating', 0, type=float)
    vote_average = request.args.get('vote_average', 0, type=float)
    genre_ids = request.args.get('genres', '[]')  # JSON array as string
    backdrop_path = request.args.get('backdrop', '')
    overview = request.args.get('overview', 'No overview available')
    
    # Check TMDB API configuration
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    tmdb_api_key_set = bool(tmdb_api_key)

    # Check user permissions
    from ..utils import is_user_system_enabled
    if not is_user_system_enabled():
        is_requester = False
        has_user_permissions = True
    else:
        is_requester = current_user.is_authenticated and current_user.role == 'requester'
        has_user_permissions = current_user.is_authenticated and current_user.role in ['admin', 'user']

    return render_template('discover_addmedia.html',
                         media_id=media_id,
                         title=title,
                         year=year,
                         media_type=media_type,
                         rating=rating,
                         vote_average=vote_average,
                         genre_ids=genre_ids,
                         backdrop_path=backdrop_path,
                         overview=overview,
                         tmdb_api_key_set=tmdb_api_key_set,
                         is_requester=is_requester,
                         has_user_permissions=has_user_permissions)
