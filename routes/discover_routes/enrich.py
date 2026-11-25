"""Library status, season lists, digital release dates and certifications for Discover results."""

import logging
from utilities.tmdb_cache import get_cached_db_statuses, get_cached_episode_info, get_tmdb_json, TMDB_JSON_TTL
from utilities.settings import get_setting

def add_db_status_and_episode_info(results, use_battery=False):
    """
    Add database status and episode info to results.
    For TV shows with episodes in the database, adds episode_info dict.

    Args:
        results: List of result items with 'id' and 'media_type' fields
        use_battery: If True, try Battery for season counts (slower but more accurate).
                     If False, skip Battery and only use TMDB (faster for discovery browsing).
    """
    if not results:
        return

    # Get database status for all items
    tmdb_ids = [str(item['id']) for item in results]
    db_statuses = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

    # Get episode info for TV shows only
    tv_show_ids = [str(item['id']) for item in results if item.get('media_type') == 'tv']
    episode_info = get_cached_episode_info(tv_show_ids) if tv_show_ids else {}

    # For all TV shows, fetch season/episode counts if not already present
    tv_shows_needing_counts = []
    for item in results:
        if item.get('media_type') == 'tv':
            if not item.get('number_of_seasons') or not item.get('number_of_episodes'):
                tv_shows_needing_counts.append(item)

    # Try Battery first for shows in library (faster, cached data) - only if requested
    if use_battery and tv_shows_needing_counts:
        try:
            from cli_battery.app.direct_api import DirectAPI
            for item in tv_shows_needing_counts[:]:  # Use slice to allow removal during iteration
                try:
                    # Convert TMDB ID to IMDb ID
                    imdb_id, _ = DirectAPI.tmdb_to_imdb(str(item['id']), media_type='show')
                    if imdb_id:
                        # Try to get metadata from Battery
                        battery_metadata, source = DirectAPI.get_show_metadata(imdb_id)
                        if battery_metadata and battery_metadata.get('seasons'):
                            seasons_data = battery_metadata['seasons']
                            if isinstance(seasons_data, dict):
                                # Exclude Season 0 (specials) from both season and episode counts
                                item['number_of_seasons'] = len([s for s in seasons_data.keys() if s != '0' and s != 0])
                                total_eps = sum(
                                    s.get('episode_count', 0)
                                    for k, s in seasons_data.items()
                                    if isinstance(s, dict) and k != '0' and k != 0
                                )
                                item['number_of_episodes'] = total_eps
                                tv_shows_needing_counts.remove(item)
                                logging.debug(f"Battery provided season counts for TMDB {item['id']} from {source}")
                except Exception as e:
                    logging.debug(f"Battery lookup failed for TMDB {item['id']}: {e}")
        except ImportError:
            logging.debug("Battery not available, falling back to TMDB")

    # Fetch counts from TMDB for remaining shows
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    if tv_shows_needing_counts and tmdb_api_key:
        from concurrent.futures import ThreadPoolExecutor

        def _apply_tmdb_counts(item):
            try:
                url = f"https://api.themoviedb.org/3/tv/{item['id']}?api_key={tmdb_api_key}"
                data = get_tmdb_json(url, timeout=5)
                # Exclude Season 0 (specials) from counts
                seasons = data.get('seasons', [])
                non_special_seasons = [s for s in seasons if s.get('season_number', 0) != 0]
                item['number_of_seasons'] = len(non_special_seasons)
                item['number_of_episodes'] = sum(s.get('episode_count', 0) for s in non_special_seasons)
                logging.debug(f"TMDB API provided season counts for {item['id']} (excluding specials)")
            except Exception as e:
                logging.debug(f"Failed to fetch TMDB counts for {item['id']}: {e}")

        # One TMDB call per show; fetch them in parallel (and through the cache).
        with ThreadPoolExecutor(max_workers=min(8, len(tv_shows_needing_counts))) as executor:
            list(executor.map(_apply_tmdb_counts, tv_shows_needing_counts))

    # Apply to results
    for item in results:
        item['db_status'] = db_statuses.get(str(item['id']), 'missing')
        # Add episode info for TV shows
        if item.get('media_type') == 'tv':
            item_episode_info = episode_info.get(str(item['id']))
            item['episode_info'] = item_episode_info

            # Override status based on wanted episodes and TMDB comparison
            # Partial = some episodes collected/blacklisted AND (some wanted OR missing from DB entirely)
            # This ensures correct navigation: Partial → discover details, Collected → library
            if item_episode_info and item['db_status'] != 'missing':
                # Get episode state counts
                collected = item_episode_info.get('collected_episodes', 0)
                blacklisted = item_episode_info.get('blacklisted_episodes', 0)
                wanted = item_episode_info.get('wanted_episodes', 0)
                db_total = item_episode_info.get('total_episodes', 0)  # Episodes that exist in DB

                # Get TMDB total (excluding Season 0)
                tmdb_total = item.get('number_of_episodes', 0)

                # Check if we're missing episodes entirely from the DB
                missing_from_db = (tmdb_total > 0 and db_total < tmdb_total)

                # Determine status
                if (collected > 0 or blacklisted > 0):
                    # We have some collected/blacklisted episodes
                    if wanted > 0 or missing_from_db:
                        # Either has wanted episodes OR missing episodes entirely from DB
                        item['db_status'] = 'partial'
                    else:
                        # All episodes accounted for and collected/blacklisted
                        item['db_status'] = 'collected'

                # If only wanted/unreleased episodes (no collected/blacklisted), keep current status

def _show_order_seasons(imdb_id):
    """The show's seasons in the episode order set on its page (Show options),
    or None while it uses the default order. Same shape as the battery's
    seasons: {season: {'episode_count', 'episodes': {number: {...}}}}."""
    from cli_battery.app.episode_order import show_episode_order, fetch_seasons_for_order
    order = show_episode_order(imdb_id)
    if not order:
        return None
    try:
        return fetch_seasons_for_order(imdb_id, order) or None
    except Exception as e:
        logging.warning(f"Discover: episode order {order} for {imdb_id} unavailable: {e}")
        return None

def _season_list(seasons):
    """Battery-shaped seasons as TMDB's season list: [{'season_number', 'episode_count', 'name'}]."""
    return sorted(
        ({'season_number': int(s),
          'episode_count': (d or {}).get('episode_count') or len((d or {}).get('episodes') or {}),
          'name': f"Season {s}"} for s, d in seasons.items() if str(s).isdigit()),
        key=lambda s: s['season_number'])

def _has_digital_release(release_dates):
    return any(release.get('type') == 4
               for country in release_dates.get('results', [])
               for release in country.get('release_dates', []))

def _movie_release_dates(tmdb_id, tmdb_api_key):
    """/movie/{id}/release_dates, shared by the digital-date and certification
    lookups. Kept 15 minutes instead of an hour while there is no digital
    release yet, so a new one shows up soon after TMDB has it."""
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={tmdb_api_key}"
    return get_tmdb_json(url, ttl_for=lambda data: TMDB_JSON_TTL if _has_digital_release(data) else 900)

def get_digital_release_date(tmdb_id, media_type, tmdb_api_key):
    """
    Fetch digital release date for a movie/TV show from TMDB release_dates endpoint.
    Falls back to theatrical release if digital release is not available.
    Returns a dict with 'date' and 'type' ('digital' or 'theatrical'), or empty dict if not found.
    """
    try:
        if media_type == 'movie':
            # For movies, use the release_dates endpoint
            data = _movie_release_dates(tmdb_id, tmdb_api_key)
            
            # Priority: US digital release (type 4), then any digital release, then US theatrical (type 3)
            us_digital = None
            any_digital = None
            us_theatrical = None
            
            for country in data.get('results', []):
                for release in country.get('release_dates', []):
                    if release.get('type') == 4:  # Digital release
                        release_date = release.get('release_date', '').split('T')[0]  # Get YYYY-MM-DD only
                        if country['iso_3166_1'] == 'US':
                            us_digital = release_date
                        elif not any_digital:
                            any_digital = release_date
                    elif release.get('type') == 3 and country['iso_3166_1'] == 'US':  # US Theatrical
                        us_theatrical = release.get('release_date', '').split('T')[0]
            
            # Return in priority order with type indicator
            if us_digital or any_digital:
                return {'date': us_digital or any_digital, 'type': 'digital'}
            elif us_theatrical:
                return {'date': us_theatrical, 'type': 'theatrical'}
            else:
                return {}
        else:
            # For TV shows, just return first_air_date (no digital release concept for TV)
            return {}
    except Exception as e:
        logging.debug(f"Error fetching digital release date for {media_type} {tmdb_id}: {e}")
        return {}

def get_certification(tmdb_id, media_type, tmdb_api_key, region='US'):
    """
    Fetch content certification/rating for a specific region from TMDB.
    For movies: uses release_dates endpoint (e.g., US: G, PG, PG-13, R, NC-17)
    For TV shows: uses content_ratings endpoint (e.g., US: TV-Y, TV-PG, TV-14, TV-MA)
    Returns certification string or empty string if not found.
    """
    try:
        if media_type == 'movie':
            # For movies, use the release_dates endpoint
            data = _movie_release_dates(tmdb_id, tmdb_api_key)

            # Find certification for the specified region
            for country in data.get('results', []):
                if country.get('iso_3166_1') == region:
                    for release in country.get('release_dates', []):
                        cert = release.get('certification', '').strip()
                        if cert:
                            return cert
        else:
            # For TV shows, use the content_ratings endpoint
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/content_ratings?api_key={tmdb_api_key}"
            data = get_tmdb_json(url)

            # Find content rating for the specified region
            for rating in data.get('results', []):
                if rating.get('iso_3166_1') == region:
                    cert = rating.get('rating', '').strip()
                    if cert:
                        return cert

        # Log when no certification is found to help with debugging
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug(f"No certification found for {media_type} {tmdb_id} in region {region}")
        return ''
    except Exception as e:
        logging.error(f"Error fetching certification for {media_type} {tmdb_id} ({region}): {e}")
        return ''

def enrich_with_digital_dates(results, media_type, tmdb_api_key):
    """
    Batch enrich results with digital release dates.
    Updates the 'release_date' and 'release_type' fields for each result.
    """
    # One TMDB call per movie; fetch them in parallel (and through the cache).
    from concurrent.futures import ThreadPoolExecutor
    movie_ids = [item['id'] for item in results if item.get('media_type', media_type) == 'movie']
    release_infos = {}
    if movie_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(movie_ids))) as executor:
            for movie_id, info in zip(movie_ids, executor.map(
                    lambda movie_id: get_digital_release_date(movie_id, 'movie', tmdb_api_key), movie_ids)):
                release_infos[movie_id] = info

    for item in results:
        if item.get('media_type', media_type) == 'movie':
            release_info = release_infos.get(item['id'])
            if release_info and release_info.get('date'):
                item['release_date'] = release_info['date']
                item['release_type'] = release_info.get('type', 'theatrical')
            else:
                # Clean existing release_date to ensure no timestamps
                if 'release_date' in item and item['release_date']:
                    item['release_date'] = str(item['release_date']).split('T')[0].split(' ')[0]
                # Always set release_type for movies, default to theatrical
                item['release_type'] = 'theatrical'
        else:
            # For TV shows, clean the date and don't set release_type
            if 'release_date' in item and item['release_date']:
                item['release_date'] = str(item['release_date']).split('T')[0].split(' ')[0]
    return results
