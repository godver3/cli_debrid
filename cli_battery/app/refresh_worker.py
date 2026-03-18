"""Background daemon thread for refreshing metadata.

Hybrid refresh strategy:
1. Updates-driven: Check Trakt /updates endpoint each cycle, refresh only
   items Trakt reports as changed.
2. Staleness fallback: Any item past its staleness threshold gets refreshed
   regardless, as a safety net in case the updates endpoint misses something.
3. NULL air-date re-check: Episodes with no air date are re-checked every 24h.

Single daemon thread, no APScheduler.  10-minute cycle.
"""

import time
import threading
from datetime import datetime, timedelta, timezone

from .logger_config import logger
from .database import init_db, Session as DbSession, Item, Season, Episode, Metadata, managed_session
from .staleness import is_stale, should_recheck_null_airdate
from . import trakt_client
from . import tvdb_client

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _get_metadata_client():
    """Return tvdb_client if TVDB API key is set, else trakt_client."""
    if tvdb_client.is_available():
        return tvdb_client
    return trakt_client


def _get_metadata_source_name() -> str:
    return 'tvdb' if tvdb_client.is_available() else 'trakt'

CYCLE_INTERVAL = 600  # 10 minutes
REQUEST_INTERVAL = 3  # seconds between Trakt API calls

# Track when we last checked the /updates endpoints (module-level, resets on restart)
_last_updates_check: datetime | None = None


def start():
    """Start the background refresh worker (idempotent)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run_loop, daemon=True, name='battery-refresh-worker')
    _thread.start()
    logger.info("Refresh worker started.")


def stop():
    """Signal the worker to stop."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=15)
    logger.info("Refresh worker stopped.")


def _run_loop():
    # Wait a bit on startup to let the app fully initialize
    _stop_event.wait(30)
    while not _stop_event.is_set():
        try:
            _refresh_from_updates()
        except Exception as e:
            logger.error(f"refresh_from_updates error: {e}", exc_info=True)

        if _stop_event.is_set():
            break

        try:
            _refresh_stale_fallback()
        except Exception as e:
            logger.error(f"refresh_stale_fallback error: {e}", exc_info=True)

        if _stop_event.is_set():
            break

        try:
            _recheck_null_airdates()
        except Exception as e:
            logger.error(f"recheck_null_airdates error: {e}", exc_info=True)

        _stop_event.wait(CYCLE_INTERVAL)


def _refresh_from_updates():
    """Check Trakt /updates endpoints and refresh items that Trakt reports as changed."""
    global _last_updates_check

    # On first run after startup, look back 6 hours as a reasonable window
    if _last_updates_check is None:
        since = datetime.now(timezone.utc) - timedelta(hours=6)
    else:
        since = _last_updates_check

    since_iso = since.isoformat()
    source = _get_metadata_source_name().upper()
    client = _get_metadata_client()
    logger.info(f"Refresh worker: checking {source} /updates since {since_iso}")

    # Collect IMDb IDs that the metadata provider says have been updated
    updated_show_ids: set = set()
    updated_movie_ids: set = set()

    try:
        updated_shows = client.get_updated_shows(since_iso)
        updated_show_ids = {s['imdb_id'] for s in updated_shows}
    except Exception as e:
        logger.error(f"Error fetching show updates: {e}")

    if _stop_event.is_set():
        return

    try:
        updated_movies = client.get_updated_movies(since_iso)
        updated_movie_ids = {m['imdb_id'] for m in updated_movies}
    except Exception as e:
        logger.error(f"Error fetching movie updates: {e}")

    if not updated_show_ids and not updated_movie_ids:
        logger.info(f"Refresh worker: no updates reported by {source}.")
        _last_updates_check = datetime.now(timezone.utc)
        return

    # Cross-reference with items we actually have in our DB
    refreshed = 0
    with managed_session() as session:
        known_shows = session.query(Item.imdb_id).filter(
            Item.type == 'show',
            Item.imdb_id.in_(updated_show_ids),
        ).all()
        known_movies = session.query(Item.imdb_id).filter(
            Item.type == 'movie',
            Item.imdb_id.in_(updated_movie_ids),
        ).all()
    shows_to_refresh = [r[0] for r in known_shows]
    movies_to_refresh = [r[0] for r in known_movies]

    logger.info(f"Refresh worker: {source} reports {len(updated_show_ids)} show updates, "
                f"{len(updated_movie_ids)} movie updates. "
                f"We have {len(shows_to_refresh)} shows and {len(movies_to_refresh)} movies to refresh.")

    from .direct_api import _refresh_show, _refresh_movie

    for imdb_id in shows_to_refresh:
        if _stop_event.is_set():
            break
        try:
            with managed_session() as session:
                _refresh_show(imdb_id, session)
            refreshed += 1
        except Exception as e:
            logger.error(f"Error refreshing show {imdb_id} from updates: {e}")
        time.sleep(REQUEST_INTERVAL)

    for imdb_id in movies_to_refresh:
        if _stop_event.is_set():
            break
        try:
            with managed_session() as session:
                _refresh_movie(imdb_id, session)
            refreshed += 1
        except Exception as e:
            logger.error(f"Error refreshing movie {imdb_id} from updates: {e}")
        time.sleep(REQUEST_INTERVAL)

    logger.info(f"Refresh worker: refreshed {refreshed} items from {source} /updates.")
    _last_updates_check = datetime.now(timezone.utc)


def _refresh_stale_fallback():
    """Safety net: refresh any items that have exceeded their staleness threshold.

    This catches anything the /updates endpoint might have missed.
    Only checks items that already have a last_trakt_fetch (items with NULL
    are handled on-demand when accessed).
    """
    logger.info("Refresh worker: checking staleness fallback...")
    from .database import is_valid_imdb_id

    with managed_session() as session:
        # Only check items that have been fetched before but are now stale
        # Skip NULL last_trakt_fetch — those get populated on-demand
        items = session.query(Item).filter(
            Item.last_trakt_fetch.isnot(None),
        ).all()
        candidates = [
            (i.imdb_id, i.title, i.type, i.media_status, i.last_trakt_fetch)
            for i in items
            if i.imdb_id and is_valid_imdb_id(i.imdb_id)  # Skip items without valid IMDb IDs
        ]
        del items  # Release ORM objects immediately; candidates only holds primitive tuples

    stale_shows = []
    stale_movies = []
    for imdb_id, title, item_type, media_status, last_fetch in candidates:
        if is_stale(item_type or 'movie', media_status, last_fetch):
            if item_type == 'show':
                stale_shows.append((imdb_id, title))
            else:
                stale_movies.append((imdb_id, title))

    if not stale_shows and not stale_movies:
        logger.info("Refresh worker: no stale items found in fallback check.")
        return

    logger.info(f"Refresh worker: staleness fallback found {len(stale_shows)} shows, "
                f"{len(stale_movies)} movies to refresh.")

    from .direct_api import _refresh_show, _refresh_movie
    refreshed = 0

    for imdb_id, title in stale_shows:
        if _stop_event.is_set():
            break
        logger.info(f"Refresh worker (fallback): refreshing stale show {imdb_id} ({title})")
        try:
            with managed_session() as session:
                _refresh_show(imdb_id, session)
            refreshed += 1
        except Exception as e:
            logger.error(f"Error refreshing stale show {imdb_id}: {e}")
        time.sleep(REQUEST_INTERVAL)

    for imdb_id, title in stale_movies:
        if _stop_event.is_set():
            break
        logger.info(f"Refresh worker (fallback): refreshing stale movie {imdb_id} ({title})")
        try:
            with managed_session() as session:
                _refresh_movie(imdb_id, session)
            refreshed += 1
        except Exception as e:
            logger.error(f"Error refreshing stale movie {imdb_id}: {e}")
        time.sleep(REQUEST_INTERVAL)

    logger.info(f"Refresh worker: staleness fallback refreshed {refreshed} items.")


def _recheck_null_airdates():
    """Re-fetch episodes where first_aired IS NULL and recheck interval has elapsed."""
    logger.info("Refresh worker: checking NULL air dates...")
    # Collect candidates with a short-lived read session
    with managed_session() as session:
        episodes = session.query(Episode).filter(
            Episode.first_aired.is_(None),
        ).all()

        # Group by show (via season -> item)
        shows_to_refresh: dict = {}
        for ep in episodes:
            if not should_recheck_null_airdate(ep.null_airdate_checked_at):
                continue
            season = session.query(Season).filter_by(id=ep.season_id).first()
            if season and season.item_id not in shows_to_refresh:
                item = session.query(Item).filter_by(id=season.item_id).first()
                if item:
                    shows_to_refresh[season.item_id] = (item.id, item.imdb_id, item.title)
        del episodes  # Release Episode ORM objects; shows_to_refresh holds only primitives

    refreshed = 0
    for item_id, (db_id, imdb_id, title) in shows_to_refresh.items():
        if _stop_event.is_set():
            break

        # Skip items without valid IMDb IDs
        if not imdb_id:
            logger.debug(f"Refresh worker: skipping {title} (no IMDb ID)")
            continue

        from .database import is_valid_imdb_id
        if not is_valid_imdb_id(imdb_id):
            logger.warning(f"Refresh worker: skipping {title} - invalid IMDb ID format: {imdb_id}")
            continue

        logger.info(f"Refresh worker: re-checking NULL airdates for {imdb_id} ({title})")
        try:
            seasons_data, _ = _get_metadata_client().get_show_seasons_and_episodes(imdb_id, include_specials=True)
            if seasons_data:
                with managed_session() as session:
                    from .direct_api import _upsert_seasons_and_episodes
                    item = session.query(Item).filter_by(id=db_id).first()
                    if item:
                        _upsert_seasons_and_episodes(item.id, seasons_data, session)

                    # Mark all NULL-airdate episodes for this show as checked
                    now = datetime.now(timezone.utc)
                    show_seasons = session.query(Season).filter_by(item_id=db_id).all()
                    for s in show_seasons:
                        null_eps = session.query(Episode).filter(
                            Episode.season_id == s.id,
                            Episode.first_aired.is_(None),
                        ).all()
                        for ep in null_eps:
                            ep.null_airdate_checked_at = now

            refreshed += 1
        except Exception as e:
            logger.error(f"Error re-checking airdates for {imdb_id}: {e}")

        time.sleep(REQUEST_INTERVAL)

    logger.info(f"Refresh worker: re-checked NULL airdates for {refreshed}/{len(shows_to_refresh)} shows.")
