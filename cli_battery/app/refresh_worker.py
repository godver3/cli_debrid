"""Background daemon thread for refreshing airing shows and NULL air-date episodes.

- Single daemon thread, no APScheduler.
- 10-minute cycle: refresh_airing_shows + recheck_null_airdates.
- Rate limited: 1 request per 3 seconds between Trakt calls.
"""

import time
import threading
from datetime import datetime, timezone

from .logger_config import logger
from .database import init_db, Session as DbSession, Item, Season, Episode, Metadata, managed_session
from .staleness import is_stale, should_recheck_null_airdate
from . import trakt_client

_stop_event = threading.Event()
_thread: threading.Thread | None = None

CYCLE_INTERVAL = 600  # 10 minutes
REQUEST_INTERVAL = 3  # seconds between Trakt API calls


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
            _refresh_airing_shows()
        except Exception as e:
            logger.error(f"refresh_airing_shows error: {e}", exc_info=True)

        if _stop_event.is_set():
            break

        try:
            _recheck_null_airdates()
        except Exception as e:
            logger.error(f"recheck_null_airdates error: {e}", exc_info=True)

        _stop_event.wait(CYCLE_INTERVAL)


def _refresh_airing_shows():
    """Re-fetch metadata for shows where media_status='returning series' and stale."""
    logger.info("Refresh worker: checking airing shows...")
    with managed_session() as session:
        shows = session.query(Item).filter(
            Item.type == 'show',
            Item.media_status.in_(['returning series', 'in production']),
        ).all()

        refreshed = 0
        for show in shows:
            if _stop_event.is_set():
                break
            if not is_stale('show', show.media_status, show.last_trakt_fetch):
                continue

            logger.info(f"Refresh worker: refreshing airing show {show.imdb_id} ({show.title})")
            try:
                from .direct_api import _refresh_show
                _refresh_show(show.imdb_id, session)
                refreshed += 1
            except Exception as e:
                logger.error(f"Error refreshing {show.imdb_id}: {e}")

            time.sleep(REQUEST_INTERVAL)

        logger.info(f"Refresh worker: refreshed {refreshed}/{len(shows)} airing shows.")


def _recheck_null_airdates():
    """Re-fetch episodes where first_aired IS NULL and recheck interval has elapsed."""
    logger.info("Refresh worker: checking NULL air dates...")
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
                    shows_to_refresh[season.item_id] = item

        refreshed = 0
        for item_id, item in shows_to_refresh.items():
            if _stop_event.is_set():
                break

            logger.info(f"Refresh worker: re-checking NULL airdates for {item.imdb_id} ({item.title})")
            try:
                seasons_data, _ = trakt_client.get_show_seasons_and_episodes(item.imdb_id, include_specials=True)
                if seasons_data:
                    from .direct_api import _upsert_seasons_and_episodes
                    _upsert_seasons_and_episodes(item, seasons_data, session)

                # Mark all NULL-airdate episodes for this show as checked
                now = datetime.now(timezone.utc)
                show_seasons = session.query(Season).filter_by(item_id=item.id).all()
                for s in show_seasons:
                    null_eps = session.query(Episode).filter(
                        Episode.season_id == s.id,
                        Episode.first_aired.is_(None),
                    ).all()
                    for ep in null_eps:
                        ep.null_airdate_checked_at = now

                refreshed += 1
            except Exception as e:
                logger.error(f"Error re-checking airdates for {item.imdb_id}: {e}")

            time.sleep(REQUEST_INTERVAL)

        logger.info(f"Refresh worker: re-checked NULL airdates for {refreshed}/{len(shows_to_refresh)} shows.")
