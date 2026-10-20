"""Per-show episode order.

A show normally uses its metadata source's default numbering: TVDB aired
order, or Trakt's (TMDB-based) order without a TVDB key. The show page can
switch it to another order (database/show_overrides.py, episode_order):

    'tvdb:<season type>'   a TVDB season type: dvd, absolute, alternate, ...
    'tmdb_group:<id>'      a TMDB episode group

seasons_in_show_order() is applied wherever cli_battery stores a show's
seasons, so every refresh keeps the chosen order.
"""
from typing import Optional

from .logger_config import logger

DEFAULT = 'default'


def is_default_order(order: Optional[str]) -> bool:
    return not order or order in (DEFAULT, 'tvdb:default', 'tvdb:official')


def show_episode_order(imdb_id: Optional[str]) -> Optional[str]:
    """The show's chosen order, or None for the default."""
    if not imdb_id:
        return None
    try:
        from database.show_overrides import get_show_override
        order = get_show_override(imdb_id).get('episode_order')
    except Exception as e:
        logger.debug(f"Could not read episode order for {imdb_id}: {e}")
        return None
    return None if is_default_order(order) else order


def fetch_seasons_for_order(imdb_id: str, order: Optional[str]) -> Optional[dict]:
    """Seasons/episodes (specials included) in the given order; None if unavailable.

    The default order is whatever the active metadata client returns.
    """
    from . import tvdb_client, trakt_client
    if is_default_order(order):
        client = tvdb_client if tvdb_client.is_available() else trakt_client
        seasons, _ = client.get_show_seasons_and_episodes(imdb_id, include_specials=True)
        return seasons
    kind, _, value = order.partition(':')
    if kind == 'tvdb' and value:
        if not tvdb_client.is_available():
            logger.warning(f"Episode order {order} for {imdb_id} needs a TVDB API key")
            return None
        return tvdb_client.get_seasons_for_season_type(imdb_id, value)
    if kind == 'tmdb_group' and value:
        from .tmdb_episode_groups import get_seasons_for_episode_group
        return get_seasons_for_episode_group(value)
    logger.warning(f"Unknown episode order {order!r} for {imdb_id}")
    return None


def seasons_in_show_order(imdb_id: str, seasons: Optional[dict]) -> Optional[dict]:
    """`seasons` (fetched in the default order) replaced by the show's chosen
    order, if it has one.

    None when the chosen order can't be fetched: callers then store nothing
    and keep the seasons they already have. Storing the default seasons would
    write aired numbering over a show kept in DVD/absolute/group numbering.
    """
    order = show_episode_order(imdb_id)
    if not order:
        return seasons
    try:
        ordered = fetch_seasons_for_order(imdb_id, order)
    except Exception as e:
        logger.error(f"Fetching episode order {order} for {imdb_id} failed: {e}", exc_info=True)
        ordered = None
    if not ordered:
        logger.warning(f"Episode order {order} unavailable for {imdb_id}; keeping the stored seasons this time")
        return None
    logger.info(f"Using episode order {order} for {imdb_id} ({len(ordered)} seasons)")
    return ordered


def list_episode_orders(imdb_id: str, tmdb_id=None) -> list:
    """Orders the show can use: [{'id', 'name', 'source'}], the default first."""
    from . import tvdb_client
    tvdb = tvdb_client.is_available()
    orders = [{'id': DEFAULT, 'name': 'Default (TVDB aired order)' if tvdb else 'Default (Trakt / TMDB order)',
               'source': 'tvdb' if tvdb else 'trakt'}]
    if tvdb:
        try:
            for season_type in tvdb_client.get_season_types(imdb_id):
                if season_type['type'] in ('official', 'default'):
                    continue
                orders.append({'id': f"tvdb:{season_type['type']}",
                               'name': f"{season_type['name']} (TVDB)", 'source': 'tvdb'})
        except Exception as e:
            logger.warning(f"Could not list TVDB orders for {imdb_id}: {e}")
    if tmdb_id:
        try:
            from .tmdb_episode_groups import list_episode_groups
            for group in list_episode_groups(tmdb_id):
                if not group.get('episode_count'):
                    continue
                orders.append({'id': f"tmdb_group:{group['id']}",
                               'name': f"{group['name']} — {group['type_name']}, {group['episode_count']} episodes (TMDB)",
                               'source': 'tmdb'})
        except Exception as e:
            logger.warning(f"Could not list TMDB episode groups for {imdb_id}: {e}")
    return orders
