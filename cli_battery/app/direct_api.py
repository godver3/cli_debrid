"""Public API for cli_battery — all static methods, single entry point.

The main app imports ``from cli_battery.app.direct_api import DirectAPI``.
Every public method returns ``(data_dict, source_string)`` on success,
``(None, None)`` on failure.  Data structures are identical to the previous
implementation.
"""

import json
import logging
import concurrent.futures
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session as SqlAlchemySession, selectinload
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from thefuzz import fuzz

from .logger_config import logger
from .database import (
    init_db, Session as DbSession, managed_session,
    Item, Metadata, Season, Episode, Poster,
    TMDBToIMDBMapping, TVDBToIMDBMapping, DatabaseManager,
    get_timezone_aware_now,
)
from . import trakt_client
from . import trakt_auth
from .staleness import is_stale, should_recheck_null_airdate, is_tmdb_mapping_stale
from .xem_utils import fetch_xem_mapping

# Special-case: Lego Masters US season renumbering
LEGO_MASTERS_US_IMDB_ID = "tt9615014"

_refresh_worker_started = False


def _apply_lego_masters_us_season_fix(seasons_data: Dict[str, Any]) -> Dict[str, Any]:
    if not seasons_data or not isinstance(seasons_data, dict):
        return seasons_data
    if '5' in seasons_data:
        del seasons_data['5']
    renumbered = {}
    for k, v in seasons_data.items():
        try:
            n = int(k)
            renumbered[str(n - 1) if n >= 6 else k] = v
        except ValueError:
            renumbered[k] = v
    return renumbered


def _ensure_worker():
    """Lazy-start the background refresh worker (once per process)."""
    global _refresh_worker_started
    if _refresh_worker_started:
        return
    try:
        from .refresh_worker import start
        start()
        _refresh_worker_started = True
    except Exception as e:
        logger.warning(f"Could not start refresh worker: {e}")


def _get_local_tz():
    try:
        from metadata.metadata import _get_local_timezone
        return _get_local_timezone()
    except ImportError:
        return timezone.utc


# ─── Internal helpers ────────────────────────────────────────────────────────

def _format_seasons_from_orm(seasons) -> dict:
    """Convert SQLAlchemy Season/Episode ORM objects to plain dict."""
    result: dict = {}
    for season in seasons:
        result[season.season_number] = {
            'episode_count': season.episode_count,
            'episodes': {
                ep.episode_number: {
                    'title': ep.title,
                    'overview': ep.overview,
                    'runtime': ep.runtime,
                    'first_aired': ep.first_aired.isoformat() if ep.first_aired else None,
                    'imdb_id': ep.imdb_id,
                } for ep in season.episodes
            },
        }
    return result


def _refresh_show(imdb_id: str, session: SqlAlchemySession) -> Optional[dict]:
    """Fetch full show data from Trakt, persist to DB, return show dict."""
    show_data = trakt_client.get_show_data(imdb_id)
    if not show_data:
        logger.warning(f"Trakt returned no show data for {imdb_id}")
        return None

    show_data.setdefault('type', 'show')
    _persist_item(imdb_id, show_data, session)
    return show_data


def _refresh_movie(imdb_id: str, session: SqlAlchemySession) -> Optional[dict]:
    """Fetch full movie data from Trakt, persist to DB, return movie dict."""
    movie_data = trakt_client.get_movie_data(imdb_id)
    if not movie_data:
        logger.warning(f"Trakt returned no movie data for {imdb_id}")
        return None

    movie_data.setdefault('type', 'movie')
    _persist_item(imdb_id, movie_data, session)
    return movie_data


def _persist_item(imdb_id: str, data: dict, session: SqlAlchemySession):
    """Upsert Item row + all Metadata rows + seasons/episodes."""
    now = datetime.now(_get_local_tz())
    item = session.query(Item).filter_by(imdb_id=imdb_id).first()

    if not item:
        item = Item(
            imdb_id=imdb_id,
            title=data.get('title', 'Unknown'),
            year=data.get('year'),
            type=data.get('type'),
        )
        session.add(item)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                raise

    # Update denormalized columns
    item.updated_at = now
    item.last_trakt_fetch = datetime.now(timezone.utc)
    item.media_status = data.get('status')
    if data.get('title'):
        item.title = data['title']
    if data.get('year'):
        item.year = data['year']
    if data.get('type'):
        item.type = data['type']

    # Replace all metadata rows
    seasons_data = data.pop('seasons', None)
    session.query(Metadata).filter(Metadata.item_id == item.id).delete(synchronize_session='fetch')
    session.flush()

    for key, value in data.items():
        processed = value
        if isinstance(value, (dict, list)):
            try:
                processed = json.dumps(value)
            except TypeError:
                processed = str(value)
        elif not isinstance(value, str):
            processed = str(value)
        session.add(Metadata(item_id=item.id, key=key, value=processed,
                             provider='trakt', last_updated=now))

    # Seasons + episodes (shows only)
    if seasons_data and isinstance(seasons_data, dict):
        _upsert_seasons_and_episodes(item, seasons_data, session)


def _upsert_seasons_and_episodes(item: Item, seasons_data: dict, session: SqlAlchemySession):
    """Bulk upsert seasons and episodes using SQLite ON CONFLICT."""
    import iso8601

    season_rows = []
    all_episodes = []

    for season_key, season_info in seasons_data.items():
        if not isinstance(season_info, dict):
            continue
        season_number = season_info.get('number')
        if season_number is None:
            try:
                season_number = int(season_key)
            except (ValueError, TypeError):
                continue

        episode_count = season_info.get('episode_count', 0)
        if not isinstance(episode_count, int):
            try:
                episode_count = int(episode_count)
            except (ValueError, TypeError):
                episode_count = 0

        season_rows.append({
            'item_id': item.id,
            'season_number': season_number,
            'episode_count': episode_count,
        })

        episodes = season_info.get('episodes', {})
        if isinstance(episodes, dict):
            for ep_key, ep_data in episodes.items():
                if isinstance(ep_data, dict):
                    ep_num = ep_data.get('number')
                    if ep_num is None:
                        try:
                            ep_num = int(ep_key)
                        except (ValueError, TypeError):
                            continue
                    ep_data['number'] = ep_num
                    ep_data['_season_number'] = season_number
                    all_episodes.append(ep_data)
        elif isinstance(episodes, list):
            for ep_data in episodes:
                if isinstance(ep_data, dict):
                    ep_data['_season_number'] = season_number
                    all_episodes.append(ep_data)

    if season_rows:
        stmt = insert(Season).values(season_rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=['item_id', 'season_number'],
            set_=dict(episode_count=stmt.excluded.episode_count),
        )
        session.execute(stmt)
        session.flush()

    season_map = {
        s.season_number: s.id
        for s in session.query(Season.id, Season.season_number).filter_by(item_id=item.id).all()
    }

    ep_rows = []
    for ep_data in all_episodes:
        sn = ep_data.get('_season_number')
        ep_num = ep_data.get('number')
        if sn is None or ep_num is None:
            continue
        season_id = season_map.get(sn)
        if not season_id:
            continue

        first_aired_str = ep_data.get('first_aired')
        first_aired_dt = None
        if first_aired_str:
            try:
                first_aired_dt = iso8601.parse_date(first_aired_str)
                if first_aired_dt.tzinfo is None:
                    first_aired_dt = first_aired_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        ep_rows.append({
            'season_id': season_id,
            'episode_number': ep_num,
            'title': ep_data.get('title', ''),
            'overview': ep_data.get('overview', ''),
            'runtime': ep_data.get('runtime', 0),
            'first_aired': first_aired_dt,
            'imdb_id': ep_data.get('imdb_id') or (ep_data.get('ids', {}) or {}).get('imdb'),
            'absolute_episode': ep_data.get('absolute'),
        })

    if ep_rows:
        chunk_size = 100
        for i in range(0, len(ep_rows), chunk_size):
            chunk = ep_rows[i:i + chunk_size]
            stmt = insert(Episode).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=['season_id', 'episode_number'],
                set_=dict(
                    title=stmt.excluded.title,
                    overview=stmt.excluded.overview,
                    runtime=stmt.excluded.runtime,
                    first_aired=stmt.excluded.first_aired,
                    imdb_id=stmt.excluded.imdb_id,
                    absolute_episode=stmt.excluded.absolute_episode,
                ),
            )
            session.execute(stmt)


def _build_metadata_dict(item: Item) -> dict:
    """Build a metadata dict from an Item's ORM relationships."""
    result: dict = {}
    for m in item.item_metadata:
        try:
            result[m.key] = json.loads(m.value)
        except (json.JSONDecodeError, TypeError):
            result[m.key] = m.value
    return result


def _build_show_metadata_dict(item: Item) -> dict:
    """Build show metadata including formatted seasons."""
    md = _build_metadata_dict(item)
    if hasattr(item, 'seasons') and item.seasons:
        md['seasons'] = _format_seasons_from_orm(item.seasons)
    else:
        md['seasons'] = {}
    # Preserve item timestamp for staleness callers
    updated_at = item.updated_at
    if updated_at and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=_get_local_tz())
    md['item_updated_at'] = updated_at
    return md


def _fetch_and_store_xem(item: Item, session: SqlAlchemySession, metadata_dict: dict):
    """Fetch XEM mapping if missing and store it."""
    if 'xem_mapping' in metadata_dict:
        return
    ids = metadata_dict.get('ids', {})
    tvdb_id = ids.get('tvdb') if isinstance(ids, dict) else None
    if not tvdb_id:
        return
    try:
        xem_data = fetch_xem_mapping(tvdb_id)
        xem_value = xem_data if xem_data else {}
        metadata_dict['xem_mapping'] = xem_value
        now = datetime.now(_get_local_tz())
        existing = session.query(Metadata).filter_by(item_id=item.id, key='xem_mapping').first()
        if existing:
            existing.value = json.dumps(xem_value)
            existing.last_updated = now
        else:
            session.add(Metadata(
                item_id=item.id, key='xem_mapping',
                value=json.dumps(xem_value), provider='xem', last_updated=now,
            ))
    except Exception as e:
        logger.error(f"XEM fetch error for {item.imdb_id}: {e}")


# ─── DirectAPI ───────────────────────────────────────────────────────────────

class DirectAPI:
    def __init__(self):
        engine = init_db()
        if engine is None:
            raise RuntimeError("Database engine failed to initialize.")
        logger.info("DirectAPI initialized, database engine ready.")
        _ensure_worker()

    # ── Movies ────────────────────────────────────────────────────────────

    @staticmethod
    def get_movie_metadata(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter_by(imdb_id=imdb_id).first()

                if item and not is_stale(item.type or 'movie', item.media_status, item.last_trakt_fetch):
                    return _build_metadata_dict(item), 'battery'

                # Stale or missing — refresh
                data = _refresh_movie(imdb_id, session)
                if data:
                    return data, 'trakt'

                # Trakt failed but we have stale data
                if item:
                    return _build_metadata_dict(item), 'battery'
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_movie_metadata {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_movie_release_dates(imdb_id: str) -> Tuple[Optional[dict], Optional[str]]:
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter_by(imdb_id=imdb_id).first()

                if item:
                    md = next((m for m in item.item_metadata if m.key == 'release_dates'), None)
                    if md and not is_stale('movie', item.media_status, item.last_trakt_fetch):
                        try:
                            return json.loads(md.value), 'battery'
                        except (json.JSONDecodeError, TypeError):
                            pass

                # Fetch from Trakt
                releases = trakt_client.get_movie_release_dates(imdb_id)
                if releases:
                    # Store if we have an item
                    if item:
                        existing = session.query(Metadata).filter_by(item_id=item.id, key='release_dates').first()
                        now = datetime.now(_get_local_tz())
                        if existing:
                            existing.value = json.dumps(releases)
                            existing.last_updated = now
                        else:
                            session.add(Metadata(
                                item_id=item.id, key='release_dates',
                                value=json.dumps(releases), provider='trakt', last_updated=now,
                            ))
                    return releases, 'trakt'

                # Return stale data if available
                if item and md:
                    try:
                        return json.loads(md.value), 'battery'
                    except (json.JSONDecodeError, TypeError):
                        pass
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_movie_release_dates {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_movie_aliases(imdb_id: str) -> Tuple[Optional[dict], Optional[str]]:
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter_by(imdb_id=imdb_id).first()

                if item:
                    md = next((m for m in item.item_metadata if m.key == 'aliases'), None)
                    if md and not is_stale('movie', item.media_status, item.last_trakt_fetch):
                        try:
                            return json.loads(md.value), 'battery'
                        except (json.JSONDecodeError, TypeError):
                            pass

                # Refresh to get aliases
                data = _refresh_movie(imdb_id, session)
                if data and 'aliases' in data:
                    return data['aliases'], 'trakt'

                if item and md:
                    try:
                        return json.loads(md.value), 'battery'
                    except (json.JSONDecodeError, TypeError):
                        pass
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_movie_aliases {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_movie_title_translation(imdb_id: str, language_code: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            metadata, source = DirectAPI.get_movie_metadata(imdb_id)
            if metadata and 'aliases' in metadata:
                aliases = metadata['aliases']
                if isinstance(aliases, str):
                    try:
                        aliases = json.loads(aliases)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if isinstance(aliases, dict) and language_code in aliases:
                    lang_aliases = aliases[language_code]
                    if lang_aliases:
                        return lang_aliases[0], source
            return None, source if metadata else None
        except Exception as e:
            logging.error(f"DirectAPI.get_movie_title_translation {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_bulk_movie_metadata(imdb_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        logger.info(f"DirectAPI.get_bulk_movie_metadata called for {len(imdb_ids)} movie IDs.")
        try:
            with managed_session() as session:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter(Item.imdb_id.in_(imdb_ids), Item.type == 'movie').all()

                result: dict = {}
                for item in items:
                    result[item.imdb_id] = _build_metadata_dict(item)

                found = sum(1 for v in result.values() if v is not None)
                logger.info(f"get_bulk_movie_metadata returning {found}/{len(imdb_ids)}")
                return result
        except Exception as e:
            logging.error(f"DirectAPI.get_bulk_movie_metadata: {e}", exc_info=True)
            return {iid: None for iid in imdb_ids}

    # ── Shows ─────────────────────────────────────────────────────────────

    @staticmethod
    def get_show_metadata(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        logging.info(f"DirectAPI.get_show_metadata called for {imdb_id}")
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter_by(imdb_id=imdb_id).first()

                if item and not is_stale(item.type or 'show', item.media_status, item.last_trakt_fetch):
                    md = _build_show_metadata_dict(item)
                    _fetch_and_store_xem(item, session, md)
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID and 'seasons' in md:
                        md['seasons'] = _apply_lego_masters_us_season_fix(md['seasons'])
                    return md, 'battery'

                # Stale or missing
                data = _refresh_show(imdb_id, session)
                if data:
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID and 'seasons' in data:
                        data['seasons'] = _apply_lego_masters_us_season_fix(data['seasons'])
                    return data, 'trakt'

                if item:
                    md = _build_show_metadata_dict(item)
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID and 'seasons' in md:
                        md['seasons'] = _apply_lego_masters_us_season_fix(md['seasons'])
                    return md, 'battery'
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_show_metadata {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_seasons(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter_by(imdb_id=imdb_id, type='show').first()

                if item and item.seasons and not is_stale('show', item.media_status, item.last_trakt_fetch):
                    seasons = _format_seasons_from_orm(item.seasons)
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID:
                        seasons = _apply_lego_masters_us_season_fix(seasons)
                    return seasons, 'battery'

                # Fetch from Trakt
                seasons_data, source = trakt_client.get_show_seasons_and_episodes(imdb_id, include_specials=True)
                if seasons_data and item:
                    _upsert_seasons_and_episodes(item, seasons_data, session)
                    item.updated_at = datetime.now(_get_local_tz())

                if seasons_data:
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID:
                        seasons_data = _apply_lego_masters_us_season_fix(seasons_data)
                    return seasons_data, source

                if item and item.seasons:
                    seasons = _format_seasons_from_orm(item.seasons)
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID:
                        seasons = _apply_lego_masters_us_season_fix(seasons)
                    return seasons, 'battery'
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_show_seasons {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_aliases(imdb_id: str) -> Tuple[Optional[dict], Optional[str]]:
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter_by(imdb_id=imdb_id).first()

                if item:
                    md = next((m for m in item.item_metadata if m.key == 'aliases'), None)
                    if md and not is_stale('show', item.media_status, item.last_trakt_fetch):
                        try:
                            return json.loads(md.value), 'battery'
                        except (json.JSONDecodeError, TypeError):
                            pass

                data = _refresh_show(imdb_id, session)
                if data and 'aliases' in data:
                    return data['aliases'], 'trakt'

                if item and md:
                    try:
                        return json.loads(md.value), 'battery'
                    except (json.JSONDecodeError, TypeError):
                        pass
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_show_aliases {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_title_translation(imdb_id: str, language_code: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            metadata, source = DirectAPI.get_show_metadata(imdb_id)
            if metadata and 'aliases' in metadata:
                aliases = metadata['aliases']
                if isinstance(aliases, str):
                    try:
                        aliases = json.loads(aliases)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if isinstance(aliases, dict) and language_code in aliases:
                    lang_aliases = aliases[language_code]
                    if lang_aliases:
                        return lang_aliases[0], source
            return None, source if metadata else None
        except Exception as e:
            logging.error(f"DirectAPI.get_show_title_translation {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_bulk_show_airs(imdb_ids: list) -> dict:
        logger.info(f"DirectAPI.get_bulk_show_airs called for {len(imdb_ids)} IDs.")
        try:
            with managed_session() as session:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter(Item.imdb_id.in_(imdb_ids), Item.type == 'show').all()

                result: dict = {}
                for item in items:
                    airs = None
                    for m in item.item_metadata:
                        if m.key == 'airs':
                            try:
                                airs = json.loads(m.value)
                            except (json.JSONDecodeError, TypeError):
                                airs = m.value
                            break
                    result[item.imdb_id] = airs

                for iid in imdb_ids:
                    if iid not in result:
                        result[iid] = None
                return result
        except Exception as e:
            logging.error(f"DirectAPI.get_bulk_show_airs: {e}", exc_info=True)
            return {iid: None for iid in imdb_ids}

    @staticmethod
    def get_bulk_show_metadata(imdb_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        logger.info(f"DirectAPI.get_bulk_show_metadata called for {len(imdb_ids)} show IDs.")
        try:
            with managed_session() as session:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata),
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter(Item.imdb_id.in_(imdb_ids), Item.type == 'show').all()

                result: dict = {}
                items_missing_xem: dict = {}
                tvdb_ids_to_fetch: dict = {}

                for item in items:
                    md = _build_show_metadata_dict(item)
                    result[item.imdb_id] = md

                    # Check XEM
                    if 'xem_mapping' not in md:
                        items_missing_xem[item.id] = item.imdb_id
                        ids = md.get('ids', {})
                        tvdb_id = ids.get('tvdb') if isinstance(ids, dict) else None
                        if tvdb_id:
                            tvdb_ids_to_fetch[tvdb_id] = item.id

                # Fetch missing XEM mappings
                for tvdb_id, item_id in tvdb_ids_to_fetch.items():
                    imdb_id_for_xem = items_missing_xem.get(item_id)
                    try:
                        xem_data = fetch_xem_mapping(tvdb_id) or {}
                        if imdb_id_for_xem and imdb_id_for_xem in result:
                            result[imdb_id_for_xem]['xem_mapping'] = xem_data
                        now = datetime.now(_get_local_tz())
                        existing = session.query(Metadata).filter_by(item_id=item_id, key='xem_mapping').first()
                        if existing:
                            existing.value = json.dumps(xem_data)
                            existing.last_updated = now
                        else:
                            session.add(Metadata(
                                item_id=item_id, key='xem_mapping',
                                value=json.dumps(xem_data), provider='xem', last_updated=now,
                            ))
                    except Exception as e:
                        logger.error(f"XEM fetch error for TVDB {tvdb_id}: {e}")

                # Apply Lego Masters fix
                if LEGO_MASTERS_US_IMDB_ID in result and result[LEGO_MASTERS_US_IMDB_ID]:
                    md = result[LEGO_MASTERS_US_IMDB_ID]
                    if 'seasons' in md:
                        md['seasons'] = _apply_lego_masters_us_season_fix(md['seasons'])

                not_found = set(imdb_ids) - {item.imdb_id for item in items}
                if not_found:
                    logger.info(f"Not found in battery: {list(not_found)}")

                return result
        except Exception as e:
            logging.error(f"DirectAPI.get_bulk_show_metadata: {e}", exc_info=True)
            return {iid: None for iid in imdb_ids}

    # ── TMDB conversion ──────────────────────────────────────────────────

    @staticmethod
    def _validate_imdb_id_parallel(tmdb_id: str, media_type: str, primary_imdb: str) -> Tuple[Optional[str], bool]:
        try:
            from utilities.settings import get_setting
            import requests as req

            tmdb_api_key = get_setting('TMDB', 'api_key')
            if not tmdb_api_key:
                return primary_imdb, False

            endpoint = 'movie' if media_type == 'movie' else 'tv'
            url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
            resp = req.get(url, timeout=5)
            if resp.status_code == 200:
                tmdb_imdb = resp.json().get('imdb_id')
                if tmdb_imdb:
                    if tmdb_imdb == primary_imdb:
                        return primary_imdb, True
                    logger.warning(f"TMDB conflict {tmdb_id}: Trakt={primary_imdb}, TMDB={tmdb_imdb}")
                    return tmdb_imdb, False
            return primary_imdb, False
        except Exception:
            return primary_imdb, False

    @staticmethod
    def tmdb_to_imdb(tmdb_id: str, media_type: str = None) -> Tuple[Optional[str], Optional[str]]:
        logger.info(f"DirectAPI.tmdb_to_imdb for TMDB {tmdb_id} type={media_type}")
        try:
            # Check DB cache first
            with managed_session() as session:
                mapping = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                if mapping and not is_tmdb_mapping_stale(mapping.updated_at):
                    return mapping.imdb_id, 'battery'

                # Primary: Trakt
                imdb_id, source = trakt_client.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)
                if imdb_id:
                    # Validate via TMDB API
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(DirectAPI._validate_imdb_id_parallel, tmdb_id, media_type, imdb_id)
                        try:
                            validated, is_valid = future.result(timeout=10)
                            if validated != imdb_id:
                                logger.warning(f"Validation corrected {tmdb_id}: {imdb_id} -> {validated}")
                                imdb_id = validated
                                source = 'validated'
                            elif is_valid:
                                source = 'validated'
                        except Exception:
                            pass

                    # Cache
                    if mapping:
                        mapping.imdb_id = imdb_id
                        mapping.updated_at = datetime.now(_get_local_tz())
                    else:
                        session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id))
                    return imdb_id, source

            # Fallback: TMDB External IDs
            try:
                from utilities.settings import get_setting
                import requests as req

                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    endpoint = 'movie' if media_type == 'movie' else 'tv'
                    url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
                    resp = req.get(url, timeout=10)
                    if resp.status_code == 200:
                        tmdb_data = resp.json()
                        tmdb_imdb = tmdb_data.get('imdb_id')
                        if tmdb_imdb:
                            with managed_session() as session:
                                session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=tmdb_imdb))
                            return tmdb_imdb, 'tmdb_external_ids'
            except Exception as e:
                logger.warning(f"TMDB fallback failed: {e}")

            # Fallback: Trakt title search
            try:
                from utilities.settings import get_setting
                import requests as req

                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    endpoint = 'movie' if media_type == 'movie' else 'tv'
                    url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    resp = req.get(url, timeout=10)
                    if resp.status_code == 200:
                        details = resp.json()
                        title = details.get('title') or details.get('name')
                        date_str = details.get('release_date') or details.get('first_air_date')
                        year = int(date_str[:4]) if date_str else None
                        if title:
                            search_type = 'show' if media_type in ('tv', 'show') else 'movie'
                            results = trakt_client.search_media(title, year=year, media_type=search_type)
                            if results:
                                for r in results:
                                    if r.get('imdb_id') and r.get('tmdb_id') == int(tmdb_id):
                                        return r['imdb_id'], 'trakt_title_search'
                                for r in results:
                                    if r.get('imdb_id'):
                                        return r['imdb_id'], 'trakt_title_search_fallback'
            except Exception as e:
                logger.warning(f"Trakt title search fallback failed: {e}")

            return None, None
        except Exception as e:
            logger.error(f"DirectAPI.tmdb_to_imdb {tmdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def force_refresh_metadata(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        logger.info(f"DirectAPI.force_refresh_metadata for {imdb_id}")
        try:
            with managed_session() as session:
                item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                item_type = item.type if item else None

                if item_type == 'movie':
                    data = _refresh_movie(imdb_id, session)
                    return data, 'trakt' if data else None
                elif item_type == 'show':
                    data = _refresh_show(imdb_id, session)
                    return data, 'trakt' if data else None
                else:
                    # Try show first, then movie
                    data = _refresh_show(imdb_id, session)
                    if data:
                        return data, 'trakt'
                    data = _refresh_movie(imdb_id, session)
                    return data, 'trakt' if data else None
        except Exception as e:
            logging.error(f"DirectAPI.force_refresh_metadata {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def force_refresh_tmdb_mapping(tmdb_id: str, media_type: str = None) -> Tuple[Optional[str], Optional[str]]:
        try:
            with managed_session() as session:
                existing = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                if existing:
                    session.delete(existing)
                    session.flush()

                imdb_id, source = trakt_client.convert_tmdb_to_imdb(tmdb_id, media_type=media_type)
                if imdb_id:
                    session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id))
                return imdb_id, source
        except Exception as e:
            logging.error(f"DirectAPI.force_refresh_tmdb_mapping {tmdb_id}: {e}", exc_info=True)
            return None, None

    # ── Search ────────────────────────────────────────────────────────────

    @staticmethod
    def search_media(query: str, year: Optional[int] = None,
                     media_type: Optional[str] = None) -> Tuple[Optional[List[Dict]], Optional[str]]:
        logger.info(f"DirectAPI.search_media: query='{query}', year={year}, type={media_type}")
        try:
            results = trakt_client.search_media(query=query, year=year, media_type=media_type)
            if year is not None and (not results or len(results) == 0):
                logger.info(f"Year-filtered search empty, retrying without year")
                results = trakt_client.search_media(query=query, year=None, media_type=media_type)
            source = 'trakt' if results is not None else None
            return results, source
        except Exception as e:
            logger.error(f"DirectAPI.search_media '{query}': {e}", exc_info=True)
            return None, None

    @staticmethod
    def find_best_match_from_results(
        original_query_title: str,
        query_year: Optional[int],
        search_results: List[Dict[str, Any]],
        year_match_boost: int = 30,
        min_score_threshold: int = 70,
    ) -> Optional[Dict[str, Any]]:
        if not search_results:
            return None

        cleaned = original_query_title.replace('.', ' ').lower().strip() if original_query_title else ''
        best = None
        highest = -1

        for result in search_results:
            title = result.get('title', '')
            if not title:
                continue
            score = fuzz.WRatio(cleaned, title.lower())
            if query_year is not None and result.get('year') == query_year:
                score += year_match_boost
            if score > highest:
                highest = score
                best = result

        if best and highest >= min_score_threshold:
            return best
        return None

    # ── Trakt auth (replaces HTTP endpoints) ─────────────────────────────

    @staticmethod
    def check_trakt_auth() -> dict:
        return trakt_auth.check_auth()

    @staticmethod
    def receive_trakt_auth(auth_data: dict) -> dict:
        return trakt_auth.receive_auth(auth_data)
