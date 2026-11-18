"""Filter metadata (genres, keywords, certifications, companies, providers, networks) and the filter endpoint."""

import logging
import requests
from flask import jsonify, request
from utilities.tmdb_cache import cache_response
from utilities.settings import get_setting
from routes.models import user_required
from ._common import _enrich_networks, _get_network_cache, discover_bp
from .enrich import add_db_status_and_episode_info, enrich_with_digital_dates

@discover_bp.route('/api/genres')
@user_required
@cache_response('details')
def get_genres():
    """
    Get available genres from TMDB
    Cached for longer periods as genres don't change frequently
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400
        
        # Get movie genres
        movie_genres_url = f"https://api.themoviedb.org/3/genre/movie/list?api_key={tmdb_api_key}&language=en-US"
        movie_response = requests.get(movie_genres_url, timeout=10)
        movie_response.raise_for_status()
        movie_genres = movie_response.json()
        
        # Get TV genres
        tv_genres_url = f"https://api.themoviedb.org/3/genre/tv/list?api_key={tmdb_api_key}&language=en-US"
        tv_response = requests.get(tv_genres_url, timeout=10)
        tv_response.raise_for_status()
        tv_genres = tv_response.json()
        
        return jsonify({
            'movie_genres': movie_genres.get('genres', []),
            'tv_genres': tv_genres.get('genres', [])
        })
        
    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB genres API error: {e}")
        return jsonify({'error': 'Failed to fetch genres'}), 500
    except Exception as e:
        logging.error(f"Discover genres error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/keywords')
@user_required
def search_keywords():
    """
    Search for keywords using TMDB keyword search API
    Returns list of matching keywords with their IDs
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        query = request.args.get('query', '').strip()
        if not query or len(query) < 2:
            return jsonify({'keywords': []})

        # Search TMDB keywords
        url = f"https://api.themoviedb.org/3/search/keyword?api_key={tmdb_api_key}&query={query}&page=1"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Return keyword results (id and name)
        keywords = [{'id': kw['id'], 'name': kw['name']} for kw in data.get('results', [])[:20]]
        return jsonify({'keywords': keywords})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB keywords API error: {e}")
        return jsonify({'error': 'Failed to search keywords'}), 500
    except Exception as e:
        logging.error(f"Discover keywords error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/keyword/<int:keyword_id>')
@user_required
def get_keyword(keyword_id):
    """
    Get a single keyword by ID from TMDB
    Used to resolve keyword names when loading saved adaptive lists
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Get keyword details from TMDB
        url = f"https://api.themoviedb.org/3/keyword/{keyword_id}?api_key={tmdb_api_key}"
        response = requests.get(url, timeout=10)

        # A stale/deleted keyword ID (saved in an old adaptive list) is a normal,
        # expected 404 from TMDB — not a server error. Let the caller fall back to
        # displaying the raw ID instead of surfacing a 500 for a "not found" case.
        if response.status_code == 404:
            return jsonify({'id': keyword_id, 'name': '', 'error': 'Keyword not found'}), 404

        response.raise_for_status()
        data = response.json()

        return jsonify({'id': data.get('id'), 'name': data.get('name', '')})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB keyword API error: {e}")
        return jsonify({'error': 'Failed to fetch keyword'}), 502
    except Exception as e:
        logging.error(f"Get keyword error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/keywords/<string:media_type>/<int:tmdb_id>')
@user_required
def get_item_keywords(media_type, tmdb_id):
    """
    Fetch TMDB keyword IDs for a specific movie or TV show.
    Used by the discover page to support keyword filtering in list mode.
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'keywords': []}), 200

        endpoint = 'tv' if media_type == 'tv' else 'movie'
        url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/keywords?api_key={tmdb_api_key}"
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        data = response.json()
        keywords = data.get('keywords') or data.get('results') or []
        return jsonify({'keywords': [{'id': k['id'], 'name': k.get('name', '')} for k in keywords if isinstance(k, dict)]})

    except Exception as e:
        logging.debug(f"Item keywords fetch error for {media_type}/{tmdb_id}: {e}")
        return jsonify({'keywords': []}), 200

@discover_bp.route('/api/certifications')
@user_required
def get_certifications():
    """
    Get certifications for a specific region and media type from TMDB
    Used to populate certification filter dropdown based on selected watch region
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        region = request.args.get('region', 'US').upper()
        media_type = request.args.get('type', 'movie')  # 'movie' or 'tv'

        # Fetch certifications from TMDB
        if media_type == 'tv':
            url = f"https://api.themoviedb.org/3/certification/tv/list?api_key={tmdb_api_key}"
        else:
            url = f"https://api.themoviedb.org/3/certification/movie/list?api_key={tmdb_api_key}"

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Extract certifications for the specified region
        certifications_data = data.get('certifications', {})
        region_certs = certifications_data.get(region, [])

        # Format certifications for dropdown
        certifications = [
            {
                'certification': cert.get('certification', ''),
                'meaning': cert.get('meaning', ''),
                'order': cert.get('order', 999)
            }
            for cert in region_certs
        ]

        # Sort by order
        certifications.sort(key=lambda x: x['order'])

        return jsonify({'certifications': certifications, 'region': region})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB certifications API error: {e}")
        return jsonify({'error': 'Failed to fetch certifications'}), 500
    except Exception as e:
        logging.error(f"Get certifications error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/companies')
@user_required
def search_companies():
    """
    Search for production companies using TMDB company search API
    Returns list of matching companies with their IDs
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        query = request.args.get('query', '').strip()
        if not query or len(query) < 2:
            return jsonify({'companies': []})

        # Search TMDB companies
        url = f"https://api.themoviedb.org/3/search/company?api_key={tmdb_api_key}&query={query}&page=1"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Return company results (id and name)
        companies = [{'id': co['id'], 'name': co['name']} for co in data.get('results', [])[:20]]
        return jsonify({'companies': companies})

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB companies API error: {e}")
        return jsonify({'error': 'Failed to search companies'}), 500
    except Exception as e:
        logging.error(f"Discover companies error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/providers')
@user_required
def get_providers():
    """
    Get available streaming providers from TMDB for a given region.
    Combines movie + TV providers, deduplicates by provider_id, sorts by display_priority.
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        region = request.args.get('region', 'US').upper()

        movie_url = f"https://api.themoviedb.org/3/watch/providers/movie?api_key={tmdb_api_key}&watch_region={region}&language=en-US"
        tv_url = f"https://api.themoviedb.org/3/watch/providers/tv?api_key={tmdb_api_key}&watch_region={region}&language=en-US"

        movie_resp = requests.get(movie_url, timeout=10)
        tv_resp = requests.get(tv_url, timeout=10)

        movie_providers = movie_resp.json().get('results', []) if movie_resp.ok else []
        tv_providers = tv_resp.json().get('results', []) if tv_resp.ok else []

        # Combine, deduplicate by provider_id, keep lowest display_priority
        seen = {}
        for p in movie_providers + tv_providers:
            pid = p['provider_id']
            priority = p.get('display_priority', 999)
            if pid not in seen or priority < seen[pid]['priority']:
                seen[pid] = {
                    'id': pid,
                    'name': p['provider_name'],
                    'logo_path': p.get('logo_path', ''),
                    'priority': priority
                }

        providers = sorted(seen.values(), key=lambda x: x['priority'])
        return jsonify({'providers': [{'id': p['id'], 'name': p['name'], 'logo_path': p['logo_path']} for p in providers]})

    except Exception as e:
        logging.error(f"Discover providers error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/networks')
@user_required
def search_networks():
    """
    Search for TV networks using cached TMDB daily export.
    Returns proper TMDB network IDs compatible with with_networks discover filter.
    """
    try:
        query = request.args.get('query', '').strip()
        if not query or len(query) < 2:
            return jsonify({'networks': []})

        all_networks = _get_network_cache()
        if not all_networks:
            return jsonify({'error': 'Network list unavailable'}), 503

        tmdb_api_key = get_setting('TMDB', 'api_key', '')

        q = query.lower()
        # Prioritise startswith matches, then contains
        starts = [n for n in all_networks if n['name'].lower().startswith(q)]
        contains = [n for n in all_networks if not n['name'].lower().startswith(q) and q in n['name'].lower()]
        results = (starts + contains)[:20]

        # Enrich with origin_country and logo_path
        results = _enrich_networks(results, tmdb_api_key)
        return jsonify({'networks': results})

    except Exception as e:
        logging.error(f"Discover networks error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/filter')
@user_required
def filter_content():
    """
    Advanced filtering using TMDB discover API
    Supports comprehensive filtering options including:
    - Genre selection (multi-select)
    - Rating ranges (TMDB, IMDb)
    - Vote count filtering
    - Year range with date filtering
    - Runtime range
    - Production company filtering
    - Release date filtering
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Get filter parameters
        media_type = request.args.get('type', 'movie')  # movie or tv
        page = int(request.args.get('page', 1))

        # If 'all' is passed, default to 'movie' (TMDB discover requires specific type)
        if media_type == 'all':
            media_type = 'movie'

        # Sort options
        sort_by = request.args.get('sort_by', 'popularity.desc')

        # Basic filters
        year_from = request.args.get('year_from', '')
        year_to = request.args.get('year_to', '')
        released_within = request.args.get('released_within', '')
        upcoming_days = request.args.get('upcoming_days', '')

        # Rating filters
        tmdb_rating_min = request.args.get('tmdb_rating_min', '')
        tmdb_rating_max = request.args.get('tmdb_rating_max', '')
        tmdb_votes_min = request.args.get('tmdb_votes_min', '')

        # Genre filters (include and exclude)
        selected_genres = request.args.get('genres', '')  # Comma-separated genre IDs to include
        excluded_genres = request.args.get('genres_exclude', '')  # Comma-separated genre IDs to exclude

        # Keyword filters (include and exclude)
        keywords = request.args.get('keywords', '')  # Comma-separated keyword IDs to include
        keywords_exclude = request.args.get('keywords_exclude', '')  # Comma-separated keyword IDs to exclude
        include_video = request.args.get('include_video', '')  # Include video results

        # Language, Country, Watch Provider filters (include and exclude)
        language = request.args.get('language', '')  # ISO 639-1 language codes
        language_exclude = request.args.get('language_exclude', '')
        country = request.args.get('country', '')  # ISO 3166-1 country codes
        country_exclude = request.args.get('country_exclude', '')
        watch_provider = request.args.get('watch_provider', '')  # TMDB watch provider IDs
        watch_provider_exclude = request.args.get('watch_provider_exclude', '')
        watch_region = request.args.get('watch_region', 'US')  # Region for watch providers (default US)

        # Runtime filters
        runtime_min = request.args.get('runtime_min', '')
        runtime_max = request.args.get('runtime_max', '')

        # Revenue filters (box office earnings, in millions)
        # Certification filters (range selector with gte/lte)
        certification_min = request.args.get('certification.gte', '')
        certification_max = request.args.get('certification.lte', '')
        certification_country = request.args.get('certification_country', '')

        # Legacy certification filters (for backward compatibility)
        certification = request.args.get('certification', '')
        certification_exclude = request.args.get('certification_exclude', '')

        # Production company (include and exclude)
        production_company = request.args.get('production_company', '')
        production_company_exclude = request.args.get('production_company_exclude', '')

        # TV Network filter (TV shows only)
        network = request.args.get('network', '')  # TMDB network IDs
        network_exclude = request.args.get('network_exclude', '')

        # Build TMDB discover URL
        if media_type == 'tv':
            base_url = f"https://api.themoviedb.org/3/discover/tv?api_key={tmdb_api_key}&language=en-US"
            date_field = 'first_air_date'
        else:
            base_url = f"https://api.themoviedb.org/3/discover/movie?api_key={tmdb_api_key}&language=en-US"
            date_field = 'primary_release_date'

        # Fix sort_by for release date - TMDB uses different field names per media type
        # Frontend sends "primary_release_date" but TV shows need "first_air_date"
        actual_sort_by = sort_by
        if 'primary_release_date' in sort_by or 'release_date' in sort_by:
            # Extract order (asc/desc)
            order = 'desc'
            if '.asc' in sort_by:
                order = 'asc'
            elif '.desc' in sort_by:
                order = 'desc'
            actual_sort_by = f"{date_field}.{order}"
            logging.info(f"[Discover Filter] Converted sort_by from '{sort_by}' to '{actual_sort_by}'")

        # Add parameters
        params = [f"page={page}", f"sort_by={actual_sort_by}"]

        # Genre filtering (include and exclude)
        # TMDB: comma = AND, pipe = OR. For include we want OR logic (match any genre)
        if selected_genres:
            # Convert comma to pipe for OR logic: "28,35" -> "28|35"
            genres_or = selected_genres.replace(',', '|')
            params.append(f"with_genres={genres_or}")
        if excluded_genres:
            # Exclude uses comma for AND logic (exclude ALL of these)
            params.append(f"without_genres={excluded_genres}")

        # Keyword filtering (include and exclude)
        # TMDB: with_keywords uses pipe for OR logic, comma for AND
        # NOTE: TMDB's discover API has limitations - it doesn't return all movies with a keyword
        # even though they appear on the website's keyword browse page (e.g., /keyword/271585-ufc/movie)
        # This is a known TMDB API limitation. For keyword-only searches, the /keyword/{id}/movies
        # endpoint would be more complete, but it doesn't support combining with other discover filters.
        if keywords:
            # Convert comma to pipe for OR logic: "123,456" -> "123|456"
            keywords_or = keywords.replace(',', '|')
            params.append(f"with_keywords={keywords_or}")
        if keywords_exclude:
            # Exclude uses comma for AND logic (exclude ALL of these)
            params.append(f"without_keywords={keywords_exclude}")
        
        # Include video results
        if include_video and include_video.lower() == 'true':
            params.append("include_video=true")

        # Language filtering (original language of the content)
        # TMDB: with_original_language uses pipe for OR logic
        if language:
            # Convert comma to pipe for OR logic: "en,es" -> "en|es"
            language_or = language.replace(',', '|')
            params.append(f"with_original_language={language_or}")
        # Note: TMDB doesn't have a direct "without_original_language" param

        # Country/Region filtering (origin country)
        # TMDB: with_origin_country uses pipe for OR logic
        if country:
            # Convert comma to pipe for OR logic: "US,GB" -> "US|GB"
            country_or = country.replace(',', '|')
            params.append(f"with_origin_country={country_or}")

        # Watch Provider filtering (streaming services like Netflix, Disney+, etc.)
        # TMDB: with_watch_providers uses pipe for OR logic
        if watch_provider:
            # Convert comma to pipe for OR logic: "8,337" -> "8|337"
            provider_or = watch_provider.replace(',', '|')
            params.append(f"with_watch_providers={provider_or}")
            params.append(f"watch_region={watch_region}")
            # Without this, with_watch_providers matches flatrate (subscription),
            # buy, OR rent — so picking e.g. Netflix also returns titles that are
            # merely purchasable/rentable there, not actually on the subscription.
            params.append("with_watch_monetization_types=flatrate")
        if watch_provider_exclude:
            params.append(f"without_watch_providers={watch_provider_exclude}")

        # TV Network filtering — IDs come from TMDB export, use with_networks (TV only)
        if network and media_type == 'tv':
            network_or = network.replace(',', '|')
            params.append(f"with_networks={network_or}")
        if network_exclude and media_type == 'tv':
            params.append(f"without_networks={network_exclude}")

        # Date filtering - Released Within and Upcoming can work together or separately
        # They take priority over year range
        # 0 means "today" for both filters:
        #   - Released Within 0 = start from today
        #   - Upcoming 0 = end at today
        from datetime import datetime, timedelta

        date_filter_applied = False
        today = datetime.now()
        date_start = None
        date_end = None

        # Parse both values first
        released_within_int = None
        upcoming_days_int = None

        if released_within is not None and released_within != '':
            try:
                released_within_int = int(released_within)
            except ValueError:
                pass

        if upcoming_days is not None and upcoming_days != '':
            try:
                upcoming_days_int = int(upcoming_days)
            except ValueError:
                pass

        # Released Within: items released in the past X days (sets start date)
        # 0 = start from today, positive = start from X days ago
        if released_within_int is not None:
            if released_within_int == 0:
                date_start = today
                date_filter_applied = True
                logging.info(f"[Discover Filter] Released Within 0 days (today): {date_start.strftime('%Y-%m-%d')}")
            elif released_within_int > 0:
                date_start = today - timedelta(days=released_within_int)
                date_filter_applied = True
                logging.info(f"[Discover Filter] Released Within {released_within_int} days from: {date_start.strftime('%Y-%m-%d')}")

        # Upcoming Releases: items releasing in the next X days (sets end date)
        # 0 = end at today, positive = end X days in the future
        if upcoming_days_int is not None:
            if upcoming_days_int == 0:
                date_end = today
                date_filter_applied = True
                logging.info(f"[Discover Filter] Upcoming 0 days (today): {date_end.strftime('%Y-%m-%d')}")
            elif upcoming_days_int > 0:
                date_end = today + timedelta(days=upcoming_days_int)
                date_filter_applied = True
                logging.info(f"[Discover Filter] Upcoming {upcoming_days_int} days until: {date_end.strftime('%Y-%m-%d')}")

        # Apply the combined date range
        if date_start:
            params.append(f"{date_field}.gte={date_start.strftime('%Y-%m-%d')}")
        if date_end:
            params.append(f"{date_field}.lte={date_end.strftime('%Y-%m-%d')}")

        if date_filter_applied:
            logging.info(f"[Discover Filter] Date range: {date_start.strftime('%Y-%m-%d') if date_start else 'any'} to {date_end.strftime('%Y-%m-%d') if date_end else 'any'}")

        # Year range filtering - only apply if no specific date filter is set and user entered values
        year_filter_applied = False
        if not date_filter_applied:
            if year_from:
                try:
                    year_int = int(year_from)
                    params.append(f"{date_field}.gte={year_int}-01-01")
                    year_filter_applied = True
                except ValueError:
                    pass
            if year_to:
                try:
                    year_int = int(year_to)
                    params.append(f"{date_field}.lte={year_int}-12-31")
                    year_filter_applied = True
                except ValueError:
                    pass

        # TMDB Rating filtering
        if tmdb_rating_min:
            try:
                rating = float(tmdb_rating_min)
                if rating > 0:
                    params.append(f"vote_average.gte={rating}")
            except ValueError:
                pass

        if tmdb_rating_max:
            try:
                rating = float(tmdb_rating_max)
                if rating < 10:
                    params.append(f"vote_average.lte={rating}")
            except ValueError:
                pass

        # TMDB Vote count filtering
        if tmdb_votes_min:
            try:
                votes = int(tmdb_votes_min)
                if votes > 0:
                    params.append(f"vote_count.gte={votes}")
            except ValueError:
                pass

        # Runtime filtering (only for movies)
        if media_type == 'movie':
            if runtime_min:
                try:
                    runtime = int(runtime_min)
                    if runtime > 0:
                        params.append(f"with_runtime.gte={runtime}")
                except ValueError:
                    pass

            if runtime_max:
                try:
                    runtime = int(runtime_max)
                    if runtime < 300:
                        params.append(f"with_runtime.lte={runtime}")
                except ValueError:
                    pass

        # Certification filtering (content rating)
        # TMDB: certification parameter has confusing behavior - "certification=G" returns G and LESS restrictive
        # For exact matches, use both .gte and .lte with same value
        # NOTE: Certifications are region-specific and tied to watch_region parameter

        # New range selector format (certification.gte and certification.lte from frontend)
        if certification_min or certification_max:
            country = certification_country or watch_region or 'US'
            if certification_min:
                params.append(f"certification.gte={certification_min}")
                logging.info(f"[Discover Filter] Certification min: {certification_min}")
            if certification_max:
                params.append(f"certification.lte={certification_max}")
                logging.info(f"[Discover Filter] Certification max: {certification_max}")
            params.append(f"certification_country={country}")
        # Legacy format (comma-separated certifications)
        elif certification:
            # Split multiple certifications (comma-separated from frontend)
            certs = [c.strip() for c in certification.split(',') if c.strip()]

            if len(certs) == 1:
                # Single certification - use exact match with .gte and .lte
                params.append(f"certification.gte={certs[0]}")
                params.append(f"certification.lte={certs[0]}")
                params.append(f"certification_country={watch_region}")
                logging.info(f"[Discover Filter] Single certification filter: {certs[0]}")
            else:
                # Multiple certifications - use range approach
                # TMDB API limitation: Can't do exact OR matching, so we get a range
                # Example: Selecting "G" and "PG-13" will return G, PG, and PG-13
                # Define certification order (US ratings)
                cert_order_us = ['G', 'PG', 'PG-13', 'R', 'NC-17']
                cert_order_tv = ['TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA']

                # Determine which order to use based on first certification
                if any(c.startswith('TV-') for c in certs):
                    cert_order = cert_order_tv
                else:
                    cert_order = cert_order_us

                # Get indices of selected certifications
                cert_indices = []
                for cert in certs:
                    if cert in cert_order:
                        cert_indices.append(cert_order.index(cert))

                if cert_indices:
                    # Get range from lowest to highest selected certification
                    min_cert = cert_order[min(cert_indices)]
                    max_cert = cert_order[max(cert_indices)]

                    params.append(f"certification.gte={min_cert}")
                    params.append(f"certification.lte={max_cert}")
                    params.append(f"certification_country={watch_region}")
                    logging.info(f"[Discover Filter] Multiple certifications: {','.join(certs)} -> range {min_cert} to {max_cert}")
                else:
                    # Fallback if certifications not recognized
                    logging.warning(f"[Discover Filter] Unrecognized certifications: {','.join(certs)}")
        # Note: TMDB doesn't have a direct "without_certification" param for exclusion

        # Production company filtering (now accepts direct TMDB company IDs)
        # TMDB: with_companies uses pipe for OR logic (match any company)
        if production_company:
            # production_company can now be comma-separated TMDB company IDs
            # Filter out any non-numeric values for safety
            company_ids = [c.strip() for c in production_company.split(',') if c.strip().isdigit()]
            if company_ids:
                # Use pipe for OR logic: "420,2" -> "420|2" (Marvel OR Disney)
                params.append(f"with_companies={'|'.join(company_ids)}")
        if production_company_exclude:
            # Exclude certain production companies (comma = AND = exclude all of these)
            exclude_ids = [c.strip() for c in production_company_exclude.split(',') if c.strip().isdigit()]
            if exclude_ids:
                params.append(f"without_companies={','.join(exclude_ids)}")

        # When filtering by rating, add a minimum vote count floor to exclude
        # items with very few votes (which can have misleading ratings)
        # Only apply this if user hasn't explicitly set their own vote count filter
        # Use a low threshold (10) to be less restrictive
        # IMPORTANT: Don't apply this for upcoming content - future shows have no votes yet!
        if tmdb_rating_min and not tmdb_votes_min and upcoming_days_int is None:
            try:
                rating = float(tmdb_rating_min)
                if rating > 0:
                    params.append("vote_count.gte=10")
            except ValueError:
                pass

        url = base_url + "&" + "&".join(params)

        # Log filters (hide API key for security)
        url_without_key = url.split('api_key=')[0] + 'api_key=***' + (url.split('api_key=')[1].split('&', 1)[1] if '&' in url.split('api_key=')[1] else '')
        cert_info = f"{certification_min}-{certification_max}" if (certification_min or certification_max) else (certification or 'none')
        logging.debug(f"[Discover] Type={media_type}, Page={page}, Genres={selected_genres}, Cert={cert_info}, URL={url_without_key}")

        response = requests.get(url, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        # Add database status information
        results = data.get('results', [])
        total_results = data.get('total_results', 0)

        logging.debug(f"[Discover] Page {page}: {len(results)} results (total: {total_results})")
        
        # Enrich with digital release dates
        if results:
            results = enrich_with_digital_dates(results, media_type, tmdb_api_key)
            data['results'] = results
        
        if results:
            # Normalize media_type for frontend
            for item in results:
                if 'media_type' not in item:
                    item['media_type'] = media_type

            # Add database status and episode info
            add_db_status_and_episode_info(results, use_battery=False)
        
        return jsonify(data)

    except requests.exceptions.RequestException as e:
        logging.error(f"Discover filter TMDB API error: {e}")
        return jsonify({'error': f'TMDB API error: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Discover filter error: {e}")
        return jsonify({'error': f'Filter error: {str(e)}'}), 500
