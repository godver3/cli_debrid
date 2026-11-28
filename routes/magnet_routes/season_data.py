"""Episode-order choices and season data for the assigner."""

from flask import request, jsonify
from ..models import admin_required
import logging
from cli_battery.app.direct_api import DirectAPI
from ._common import _fetch_trakt_season_data_directly, _library_episode_rows, _requested_episode_order, _saved_episode_order, _season_cache, _seasons_for_order, magnet_bp

@magnet_bp.route('/episode_orders')
@admin_required
def episode_orders():
    """The episode orders a show can use, for the assign page's picker."""
    tmdb_id = request.args.get('tmdb_id')
    if not tmdb_id:
        return jsonify({'success': False, 'error': 'tmdb_id is required'}), 400
    try:
        from metadata.metadata import get_imdb_id_if_missing
        from cli_battery.app.episode_order import list_episode_orders
        from utilities.episode_order_change import _plex_ordering_for
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': 'show'})
        if not imdb_id:
            return jsonify({'success': False, 'error': 'No IMDb ID for this show'}), 404
        orders = list_episode_orders(imdb_id, tmdb_id)
        # Either NFO mode, read by Plex: picking any non-default order writes
        # NFO files Plex follows
        from utilities.nfo_writer import enabled as _nfo_on, plex_reads_nfos
        nfo = _nfo_on() and plex_reads_nfos()
        for order in orders:
            # With NFO files Plex follows any order. Without, Plex has TVDB's
            # aired/DVD/absolute orders; it lists only some of a show's TMDB
            # episode groups, so those aren't promised here.
            order['plex_supported'] = nfo or (not str(order['id']).startswith('tmdb_group:')
                                              and _plex_ordering_for(order['id']) is not None)
        return jsonify({
            'success': True,
            'imdb_id': imdb_id,
            'current': _saved_episode_order(imdb_id),
            'library_episodes': _library_episode_rows(imdb_id),
            'orders': orders,
        })
    except Exception as e:
        logging.error(f"Listing episode orders for TMDB {tmdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Could not list episode orders'}), 500

@magnet_bp.route('/get_season_data')
def get_season_data():
    """
    PHASE 1.1 & 3.1: Enhanced with caching support (in-memory + optional Redis)
    Fetches season data for a TV show with 60-minute cache TTL
    """
    tmdb_id = request.args.get('tmdb_id')
    allow_specials_str = request.args.get('allow_specials', 'true').lower()
    allow_specials = allow_specials_str == 'true'
    order = _requested_episode_order(request.args.get('order'))

    if not tmdb_id:
        return jsonify({'error': 'tmdb_id is required'}), 400

    # PHASE 1.1: Check cache first
    cache_key = f"season_data:{tmdb_id}:{allow_specials}:{order or ''}"
    cached_data = _season_cache.get_by_key(cache_key)
    if cached_data is not None:
        logging.info(f"Season data cache HIT for TMDB ID {tmdb_id} (allow_specials={allow_specials})")
        return jsonify(cached_data)

    logging.info(f"Season data cache MISS for TMDB ID {tmdb_id} - fetching fresh data")

    try:
        from metadata.metadata import get_imdb_id_if_missing
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': 'show'})

        if not imdb_id:
            logging.warning(f"Could not find IMDb ID for TMDB ID: {tmdb_id}. Trying with dummy IMDb ID...")
            # Generate a dummy IMDb ID for testing purposes
            dummy_imdb_id = f"tt{tmdb_id.zfill(7)}"
            logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id}")
            imdb_id = dummy_imdb_id

        # An order picked on the page that isn't the show's saved one: list
        # the seasons as that order numbers them.
        if order and order != _saved_episode_order(imdb_id):
            ordered = _seasons_for_order(imdb_id, order)
            if not ordered:
                return jsonify({"error": f"Could not fetch the episode list for order {order}"}), 404
            formatted_seasons = {str(sn): len((sd or {}).get('episodes') or {}) for sn, sd in sorted(ordered.items())
                                 if allow_specials or sn != 0}
            _season_cache.set_by_key(cache_key, formatted_seasons)
            return jsonify(formatted_seasons)

        # Now we should have an IMDb ID. Let's try to fetch season data.
        try:
            # First, try to get seasons from the battery, which is faster.
            # Not just 'battery': a show the battery hasn't stored yet comes back
            # from its metadata client ('tvdb'/'trakt'), which is just as good and
            # the Trakt-only fallback below fails without Trakt auth.
            seasons_data, source = DirectAPI.get_show_seasons(imdb_id)
            if seasons_data:
                logging.info(f"Successfully fetched season data from {source} for IMDb ID: {imdb_id}")
                # Convert battery format to expected object format for frontend
                formatted_seasons = {}
                for season_num, season_data in seasons_data.items():
                    # Filter based on allow_specials
                    if allow_specials or season_num != 0:
                        formatted_seasons[str(season_num)] = season_data.get('episode_count', 0)
                logging.info(f"Returning {len(formatted_seasons)} seasons from battery for IMDb ID {imdb_id} (Allow Specials: {allow_specials}). Original count: {len(seasons_data)}")

                # PHASE 1.1: Cache the result
                _season_cache.set_by_key(cache_key, formatted_seasons)

                return jsonify(formatted_seasons)
            else:
                logging.info(f"Could not fetch season data from battery for {imdb_id}, trying Trakt directly.")
                season_data = _fetch_trakt_season_data_directly(imdb_id)
                if season_data:
                    # Convert to expected object format for frontend
                    formatted_seasons = {}
                    for season in season_data:
                        season_num = season.get('number')
                        # Filter based on allow_specials
                        if allow_specials or season_num != 0:
                            formatted_seasons[str(season_num)] = season.get('episode_count', 0)
                    logging.info(f"Returning {len(formatted_seasons)} seasons from Trakt for IMDb ID {imdb_id} (Allow Specials: {allow_specials}). Original count: {len(season_data)}")

                    # PHASE 1.1: Cache the result
                    _season_cache.set_by_key(cache_key, formatted_seasons)

                    return jsonify(formatted_seasons)
                else:
                    logging.error(f"Could not fetch season data directly from Trakt for IMDb ID: {imdb_id}")
                    return jsonify({"error": f"Could not fetch season data from Trakt for IMDb ID: {imdb_id}"}), 404
        except Exception as e:
            logging.error(f"An unexpected error occurred while fetching season data for IMDb ID {imdb_id}: {e}", exc_info=True)
            return jsonify({"error": "An unexpected error occurred"}), 500

    except Exception as e:
        logging.error(f"Error in get_season_data endpoint: {str(e)}", exc_info=True)
        return jsonify({'error': 'An internal error occurred'}), 500
