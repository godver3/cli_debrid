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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session as SqlAlchemySession, selectinload
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from thefuzz import fuzz

from .logger_config import logger
from .database import (
    init_db, Session as DbSession, managed_session,
    Item, Metadata, Season, Episode, Poster,
    TMDBToIMDBMapping, TVDBToIMDBMapping, DatabaseManager,
    get_timezone_aware_now, normalize_imdb_id,
)
from . import trakt_client
from . import tvdb_client
from . import trakt_auth
from .staleness import is_stale, is_older_than, should_recheck_null_airdate, is_tmdb_mapping_stale
from .xem_utils import fetch_xem_mapping

# Special-case: Lego Masters US season renumbering
LEGO_MASTERS_US_IMDB_ID = "tt9615014"

_refresh_worker_started = False
_plex_guid_backfilled: set = set()  # tracks shows already backfilled this session
_plex_guid_backfilled_lock = __import__('threading').Lock()


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


def _get_metadata_client():
    """Return tvdb_client if TVDB API key is set, else trakt_client."""
    if tvdb_client.is_available():
        return tvdb_client
    return trakt_client


def _get_metadata_source_name() -> str:
    """Return 'tvdb' or 'trakt' depending on which client is active."""
    return 'tvdb' if tvdb_client.is_available() else 'trakt'


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
            'plex_guid': season.plex_guid,
            'tmdb_id': season.tmdb_id,
            'tvdb_id': season.tvdb_id,
            'episodes': {
                ep.episode_number: {
                    'title': ep.title,
                    'overview': ep.overview,
                    'runtime': ep.runtime,
                    'first_aired': ep.first_aired.isoformat() if ep.first_aired else None,
                    'imdb_id': ep.imdb_id,
                    'tmdb_id': ep.tmdb_id,
                    'tvdb_id': ep.tvdb_id,
                    'absolute': ep.absolute_episode,
                    'plex_guid': ep.plex_guid,
                } for ep in season.episodes
            },
        }
    return result


def _ensure_plex_guids_in_data(imdb_id: str, data: dict, media_type: str) -> None:
    """
    Ensure Plex GUIDs are present at all levels (show/season/episode) in data.
    If TVDB is primary (or Trakt data had no plex guid), do a supplementary
    Trakt call to fetch all levels in one request. Mutates data in-place.
    """
    existing_plex = (data.get('ids') or {}).get('plex')
    if existing_plex and media_type == 'movie':
        return  # movie already has guid

    # For shows, always check — even if show-level guid exists, seasons/episodes may be missing
    if existing_plex and media_type == 'show':
        seasons = data.get('seasons') or {}
        # Only skip the Trakt call if every episode has plex_guid, imdb_id, tmdb_id, tvdb_id
        has_all_guids = all(
            isinstance(s, dict) and s.get('plex_guid') and
            all(isinstance(ep, dict) and ep.get('plex_guid') and ep.get('imdb_id')
                and ep.get('tmdb_id') and ep.get('tvdb_id')
                for ep in (s.get('episodes') or {}).values()
                if isinstance(ep, dict))
            for s in seasons.values() if isinstance(s, dict)
        )
        if has_all_guids:
            return

    try:
        result = trakt_client.get_plex_guid(imdb_id, media_type)
        if not result:
            return
        show_guid        = result.get('show_guid')
        season_guids     = result.get('season_guids') or {}
        season_tmdb_ids  = result.get('season_tmdb_ids') or {}
        season_tvdb_ids  = result.get('season_tvdb_ids') or {}
        episode_guids    = result.get('episode_guids') or {}
        episode_imdb_ids = result.get('episode_imdb_ids') or {}
        episode_tmdb_ids = result.get('episode_tmdb_ids') or {}
        episode_tvdb_ids = result.get('episode_tvdb_ids') or {}

        if show_guid:
            data.setdefault('ids', {})['plex'] = {'guid': show_guid}
            logger.debug(f"[PlexGUID] Fetched show GUID for {imdb_id}: {show_guid}")

        if 'seasons' in data and isinstance(data['seasons'], dict):
            for snum, s_guid in season_guids.items():
                s = data['seasons'].get(snum) or data['seasons'].get(str(snum))
                if isinstance(s, dict):
                    s['plex_guid'] = s_guid
            for snum, s_tmdb in season_tmdb_ids.items():
                s = data['seasons'].get(snum) or data['seasons'].get(str(snum))
                if isinstance(s, dict) and not s.get('tmdb_id'):
                    s['tmdb_id'] = s_tmdb
            for snum, s_tvdb in season_tvdb_ids.items():
                s = data['seasons'].get(snum) or data['seasons'].get(str(snum))
                if isinstance(s, dict) and not s.get('tvdb_id'):
                    s['tvdb_id'] = s_tvdb

            # Propagate episode GUIDs into season episode dicts.
            # Two passes:
            #   Pass 1 — direct match: Trakt season+episode key matches TVDB season+episode key.
            #            Works for shows where Trakt and TVDB use the same numbering.
            #   Pass 2 — absolute fallback: for episodes still missing a GUID, match using the
            #            episode's absolute_episode number against Trakt Season 1 episode numbers.
            #            Handles anime where Trakt uses absolute (S01E15) but TVDB splits seasons
            #            (S02E03 with absolute_episode=15).

            # Build absolute→id lookups from Trakt Season 1 episodes
            trakt_s1_ep_guids = episode_guids.get(1) or episode_guids.get('1') or {}
            trakt_s1_ep_imdb  = episode_imdb_ids.get(1) or episode_imdb_ids.get('1') or {}
            trakt_s1_ep_tmdb  = episode_tmdb_ids.get(1) or episode_tmdb_ids.get('1') or {}
            trakt_s1_ep_tvdb  = episode_tvdb_ids.get(1) or episode_tvdb_ids.get('1') or {}

            for snum, ep_map in episode_guids.items():
                s = data['seasons'].get(snum) or data['seasons'].get(str(snum))
                if not isinstance(s, dict):
                    continue
                eps = s.get('episodes') or {}
                if isinstance(eps, dict):
                    for ep_num, ep_guid in ep_map.items():
                        ep = eps.get(ep_num) or eps.get(str(ep_num))
                        if isinstance(ep, dict):
                            ep['plex_guid'] = ep_guid

            # Pass 1b — propagate episode IMDb/TMDb/TVDb IDs via direct season/episode match.
            # For anime where Trakt uses absolute S1 numbering, Pass 2 below handles the offset.
            for _id_map, _field in [
                (episode_imdb_ids, 'imdb_id'),
                (episode_tmdb_ids, 'tmdb_id'),
                (episode_tvdb_ids, 'tvdb_id'),
            ]:
                for snum, id_map in _id_map.items():
                    s = data['seasons'].get(snum) or data['seasons'].get(str(snum))
                    if not isinstance(s, dict):
                        continue
                    eps = s.get('episodes') or {}
                    if isinstance(eps, dict):
                        for ep_num, ep_id_val in id_map.items():
                            ep = eps.get(ep_num) or eps.get(str(ep_num))
                            if isinstance(ep, dict) and not ep.get(_field):
                                ep[_field] = ep_id_val

            # Pass 2 — cumulative offset mapping for episodes still missing plex_guid.
            #
            # Only runs when Trakt has all episodes in S1 (absolute/anime style) but
            # TVDB splits them into multiple seasons. Detection: Trakt S1 episode count
            # equals total non-special TVDB episodes across all seasons.
            #
            # Formula: TVDB S{n}E{e} → Trakt S1E{offset(n) + e}
            # where offset(n) = sum of episode counts of all TVDB seasons before season n.
            #
            # Safety: if Trakt S1 episode count ≠ total TVDB episodes, the structure
            # doesn't match the assumption — skip this pass to avoid wrong mappings.
            if trakt_s1_ep_guids or trakt_s1_ep_imdb or trakt_s1_ep_tmdb or trakt_s1_ep_tvdb:
                _trakt_s1_count = max(len(trakt_s1_ep_guids), len(trakt_s1_ep_imdb),
                                      len(trakt_s1_ep_tmdb), len(trakt_s1_ep_tvdb))

                # Count total non-special TVDB episodes
                _tvdb_total = sum(
                    len(s.get('episodes', {}))
                    for k, s in data['seasons'].items()
                    if str(k).isdigit() and int(k) > 0 and isinstance(s, dict)
                )

                # Run cumulative mapping if TVDB total <= Trakt S1 count.
                # When TVDB has more episodes (e.g. trailing OVA season not in Trakt yet),
                # we still map what fits — episodes where _trakt_abs > _trakt_s1_count
                # simply won't find a guid and are silently skipped.
                _mapping_viable = _tvdb_total > 0 and _trakt_s1_count > 0

                if _mapping_viable:
                    # Structure matches — safe to use cumulative offset mapping
                    logger.debug(
                        f"[PlexGUID] Cumulative offset mapping: TVDB total={_tvdb_total} "
                        f"= Trakt S1={_trakt_s1_count} for {imdb_id}"
                    )
                    _season_offsets: dict = {}
                    _cumulative = 0
                    for _snum in sorted(
                        int(k) for k in data['seasons'].keys()
                        if str(k).isdigit() and int(k) > 0
                    ):
                        _season_offsets[_snum] = _cumulative
                        _s_info = data['seasons'].get(_snum) or data['seasons'].get(str(_snum))
                        _s_ep_count = len(_s_info.get('episodes', {})) if isinstance(_s_info, dict) else 0
                        _cumulative += _s_ep_count

                    for _snum_key, s_data in data['seasons'].items():
                        if not isinstance(s_data, dict):
                            continue
                        try:
                            _snum_int = int(_snum_key)
                        except (ValueError, TypeError):
                            continue
                        if _snum_int == 0:
                            continue
                        _offset = _season_offsets.get(_snum_int, 0)
                        eps = s_data.get('episodes') or {}
                        if not isinstance(eps, dict):
                            continue
                        for ep_num_key, ep_data in eps.items():
                            if not isinstance(ep_data, dict):
                                continue
                            if (ep_data.get('plex_guid') and ep_data.get('imdb_id') and
                                    ep_data.get('tmdb_id') and ep_data.get('tvdb_id')):
                                continue
                            try:
                                _ep_num = int(ep_num_key)
                            except (ValueError, TypeError):
                                continue
                            _trakt_abs = _offset + _ep_num
                            for _src, _field in [
                                (trakt_s1_ep_guids, 'plex_guid'),
                                (trakt_s1_ep_imdb,  'imdb_id'),
                                (trakt_s1_ep_tmdb,  'tmdb_id'),
                                (trakt_s1_ep_tvdb,  'tvdb_id'),
                            ]:
                                if not ep_data.get(_field):
                                    _val = _src.get(_trakt_abs) or _src.get(str(_trakt_abs))
                                    if _val:
                                        ep_data[_field] = _val
                                        logger.debug(
                                            f"[PlexGUID] S{_snum_int}E{_ep_num} "
                                            f"→ Trakt abs {_trakt_abs} → {_field}={_val}"
                                        )
                else:
                    logger.debug(f"[PlexGUID] No episodes to map for {imdb_id}")

        if season_guids or episode_guids:
            logger.debug(f"[PlexGUID] Fetched {len(season_guids)} season + "
                         f"{sum(len(v) for v in episode_guids.values())} episode GUIDs for {imdb_id}")
    except Exception as e:
        logger.debug(f"[PlexGUID] Supplementary Trakt lookup failed for {imdb_id}: {e}")


def _refresh_show(imdb_id: str, session: SqlAlchemySession) -> Optional[dict]:
    """Fetch full show data from metadata provider, persist to DB, return show dict."""
    client = _get_metadata_client()
    show_data = client.get_show_data(imdb_id)
    if not show_data and client is not trakt_client:
        logger.warning(f"{_get_metadata_source_name().upper()} returned no show data for {imdb_id}, trying Trakt fallback")
        show_data = trakt_client.get_show_data(imdb_id)
    if not show_data:
        logger.warning(f"All sources returned no show data for {imdb_id}")
        return None

    # Enrich aliases with TMDB alternative titles + primary TMDB title if different.
    try:
        from .trakt_client import _fetch_tmdb_alternative_titles, _merge_tmdb_aliases
        tmdb_id = (show_data.get('ids') or {}).get('tmdb')
        trakt_title = show_data.get('title', '')
        tmdb_alts = _fetch_tmdb_alternative_titles(tmdb_id, 'tv', trakt_title)
        if tmdb_alts:
            existing = show_data.get('aliases') or {}
            show_data['aliases'] = _merge_tmdb_aliases(existing, tmdb_alts)
    except Exception as _e:
        logger.debug(f"TMDB alias enrichment failed for {imdb_id}: {_e}")

    show_data.setdefault('type', 'show')
    _ensure_plex_guids_in_data(imdb_id, show_data, 'show')
    _persist_item(imdb_id, dict(show_data), session)
    return show_data


def _refresh_movie(imdb_id: str, session: SqlAlchemySession) -> Optional[dict]:
    """Fetch full movie data from metadata provider, persist to DB, return movie dict."""
    client = _get_metadata_client()
    movie_data = client.get_movie_data(imdb_id)
    if not movie_data and client is not trakt_client:
        logger.warning(f"{_get_metadata_source_name().upper()} returned no movie data for {imdb_id}, trying Trakt fallback")
        movie_data = trakt_client.get_movie_data(imdb_id)
    if not movie_data:
        logger.warning(f"All sources returned no movie data for {imdb_id}")
        return None

    # Enrich aliases with TMDB alternative titles + primary TMDB title if different.
    # Applied here so it works regardless of whether Trakt or TVDB is the primary client.
    try:
        from .trakt_client import _fetch_tmdb_alternative_titles, _merge_tmdb_aliases
        tmdb_id = (movie_data.get('ids') or {}).get('tmdb')
        trakt_title = movie_data.get('title', '')
        tmdb_alts = _fetch_tmdb_alternative_titles(tmdb_id, 'movie', trakt_title)
        if tmdb_alts:
            existing = movie_data.get('aliases') or {}
            movie_data['aliases'] = _merge_tmdb_aliases(existing, tmdb_alts)
    except Exception as _e:
        logger.debug(f"TMDB alias enrichment failed for {imdb_id}: {_e}")

    movie_data.setdefault('type', 'movie')

    # If year is still missing, try to recover from release_dates already in movie_data
    if not movie_data.get('year') and movie_data.get('release_dates'):
        try:
            earliest = None
            for country_dates in movie_data['release_dates'].values():
                for rd in (country_dates if isinstance(country_dates, list) else []):
                    d = (rd.get('date') or rd.get('release_date') or '')[:10]
                    if d and len(d) >= 4:
                        y = int(d[:4])
                        if earliest is None or y < earliest:
                            earliest = y
            if earliest:
                movie_data['year'] = earliest
        except Exception:
            pass

    _ensure_plex_guids_in_data(imdb_id, movie_data, 'movie')
    _persist_item(imdb_id, dict(movie_data), session)
    return movie_data


def _persist_item(imdb_id: str, data: dict, session: SqlAlchemySession):
    """Upsert Item row + all Metadata rows + seasons/episodes."""
    from .database import normalize_imdb_id

    # Validate and normalize IMDb ID
    normalized_imdb_id = normalize_imdb_id(imdb_id)
    if normalized_imdb_id != imdb_id:
        if normalized_imdb_id is None:
            logger.warning(f"Rejecting item with invalid IMDb ID: {imdb_id!r}")
            return
        logger.info(f"Normalized IMDb ID: {imdb_id!r} -> {normalized_imdb_id!r}")
        imdb_id = normalized_imdb_id

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
        # Skip None values — storing str(None)="None" causes false "None" metadata
        if value is None:
            continue
        processed = value
        if isinstance(value, (dict, list)):
            try:
                processed = json.dumps(value)
            except TypeError:
                processed = str(value)
        elif not isinstance(value, str):
            processed = str(value)
        session.add(Metadata(item_id=item.id, key=key, value=processed,
                             provider=_get_metadata_source_name(), last_updated=now))

    # Seasons + episodes (shows only)
    if seasons_data and isinstance(seasons_data, dict):
        _upsert_seasons_and_episodes(item.id, seasons_data, session)


def _upsert_seasons_and_episodes(item_id: int, seasons_data: dict, session: SqlAlchemySession):
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
            'item_id': item_id,
            'season_number': season_number,
            'episode_count': episode_count,
            'plex_guid': season_info.get('plex_guid'),
            'tmdb_id': season_info.get('tmdb_id'),
            'tvdb_id': season_info.get('tvdb_id'),
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
            set_=dict(
                episode_count=stmt.excluded.episode_count,
                plex_guid=func.coalesce(stmt.excluded.plex_guid, Season.plex_guid),
                tmdb_id=func.coalesce(stmt.excluded.tmdb_id, Season.tmdb_id),
                tvdb_id=func.coalesce(stmt.excluded.tvdb_id, Season.tvdb_id),
            ),
        )
        session.execute(stmt)
        session.flush()

    season_map = {
        s.season_number: s.id
        for s in session.query(Season.id, Season.season_number).filter_by(item_id=item_id).all()
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

        _ep_ids = ep_data.get('ids', {}) or {}
        ep_rows.append({
            'season_id': season_id,
            'episode_number': ep_num,
            'title': ep_data.get('title', ''),
            'overview': ep_data.get('overview', ''),
            'runtime': ep_data.get('runtime', 0),
            'first_aired': first_aired_dt,
            'imdb_id': ep_data.get('imdb_id') or _ep_ids.get('imdb'),
            'tmdb_id': ep_data.get('tmdb_id') or (str(_ep_ids['tmdb']) if _ep_ids.get('tmdb') else None),
            'tvdb_id': ep_data.get('tvdb_id') or (str(_ep_ids['tvdb']) if _ep_ids.get('tvdb') else None),
            'absolute_episode': ep_data.get('absolute'),
            'plex_guid': ep_data.get('plex_guid'),
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
                    # Preserve existing values when incoming is NULL — prevents TVDB refresh
                    # from wiping fields populated by Trakt supplementary call
                    imdb_id=func.coalesce(stmt.excluded.imdb_id, Episode.imdb_id),
                    tmdb_id=func.coalesce(stmt.excluded.tmdb_id, Episode.tmdb_id),
                    tvdb_id=func.coalesce(stmt.excluded.tvdb_id, Episode.tvdb_id),
                    absolute_episode=stmt.excluded.absolute_episode,
                    plex_guid=func.coalesce(stmt.excluded.plex_guid, Episode.plex_guid),
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
    # Preserve staleness fields for callers
    last_fetch = item.last_trakt_fetch
    if last_fetch and last_fetch.tzinfo is None:
        last_fetch = last_fetch.replace(tzinfo=timezone.utc)
    md['last_trakt_fetch'] = last_fetch
    md['media_status'] = item.media_status
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
        # Guard: Prevent None or invalid imdb_id from reaching database
        if not imdb_id or imdb_id == 'None' or not isinstance(imdb_id, str) or not imdb_id.strip():
            logger.warning(f"DirectAPI.get_movie_metadata called with invalid imdb_id: {repr(imdb_id)}")
            return None, None

        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter_by(imdb_id=imdb_id).first()

                if item and not is_stale(item.type or 'movie', item.media_status, item.last_trakt_fetch):
                    logging.debug(f"get_movie_metadata {imdb_id}: cache HIT")
                    return _build_metadata_dict(item), 'battery'

                # Stale or missing — refresh, unless Trakt is in cooldown
                logging.debug(f"get_movie_metadata {imdb_id}: cache MISS (stale={item is not None})")
                if item:
                    try:
                        from utilities.trakt_coordinator import GlobalTraktCoordinator
                        from datetime import datetime as _dt
                        _coord = GlobalTraktCoordinator.get_instance()
                        if _coord._global_cooldown_until and _dt.now() < _coord._global_cooldown_until:
                            return _build_metadata_dict(item), 'battery'
                    except Exception:
                        pass
                data = _refresh_movie(imdb_id, session)
                if data:
                    return data, _get_metadata_source_name()

                # Trakt failed but we have stale data
                if item:
                    return _build_metadata_dict(item), 'battery'
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_movie_metadata {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_movie_release_dates(
        imdb_id: str,
        max_cache_age: Optional[timedelta] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """Return movie release dates, optionally using a shorter cache lifetime.

        ``max_cache_age`` only affects this release-date record. Other movie
        metadata keeps its normal staleness policy.
        """
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter_by(imdb_id=imdb_id).first()

                item_id = None
                md = None
                stale_value = None
                if item:
                    item_id = item.id
                    md = next((m for m in item.item_metadata if m.key == 'release_dates'), None)
                    if md:
                        stale_value = md.value
                    cache_is_fresh = md and (
                        not is_older_than(md.last_updated, max_cache_age)
                        if max_cache_age is not None
                        else not is_stale('movie', item.media_status, item.last_trakt_fetch)
                    )
                    if cache_is_fresh:
                        try:
                            return json.loads(md.value), 'battery'
                        except (json.JSONDecodeError, TypeError):
                            pass

                    if md and max_cache_age is not None and not cache_is_fresh:
                        logging.info(
                            "Release-date cache for %s is older than %s; refreshing provider metadata",
                            imdb_id,
                            max_cache_age,
                        )

                # Fetch from metadata provider (may open nested sessions, detaching item)
                releases = _get_metadata_client().get_movie_release_dates(imdb_id)
                source = _get_metadata_source_name()
                if releases:
                    # Store if we have an item
                    if item_id is not None:
                        existing = session.query(Metadata).filter_by(item_id=item_id, key='release_dates').first()
                        now = datetime.now(_get_local_tz())
                        if existing:
                            existing.value = json.dumps(releases)
                            existing.last_updated = now
                        else:
                            session.add(Metadata(
                                item_id=item_id, key='release_dates',
                                value=json.dumps(releases), provider=source, last_updated=now,
                            ))
                    return releases, source

                # Return stale data if available
                if stale_value is not None:
                    try:
                        return json.loads(stale_value), 'battery'
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

                    # If stale but Trakt is in cooldown, return stale data rather than blocking
                    if item and md:
                        try:
                            from utilities.trakt_coordinator import GlobalTraktCoordinator
                            from datetime import datetime as _dt
                            _coord = GlobalTraktCoordinator.get_instance()
                            if _coord._global_cooldown_until and _dt.now() < _coord._global_cooldown_until:
                                return json.loads(md.value), 'battery'
                        except Exception:
                            pass

                # Refresh to get aliases
                data = _refresh_movie(imdb_id, session)
                if data and 'aliases' in data:
                    return data['aliases'], _get_metadata_source_name()

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
            # Filter out None/empty/invalid IMDb IDs to prevent disk I/O errors
            valid_ids = []
            for iid in imdb_ids:
                if iid and isinstance(iid, str) and iid.strip() and iid != 'None':
                    normalized = normalize_imdb_id(iid)
                    if normalized:
                        valid_ids.append(normalized)

            if len(valid_ids) < len(imdb_ids):
                logger.warning(f"get_bulk_movie_metadata: filtered {len(imdb_ids) - len(valid_ids)} invalid IDs")

            result: dict = {}

            # Return empty results for invalid IDs
            for iid in imdb_ids:
                if iid not in valid_ids:
                    result[iid] = None

            # Phase 1: bulk DB lookup for cached items
            with managed_session() as session:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter(Item.imdb_id.in_(valid_ids), Item.type == 'movie').all()

                for item in items:
                    if not is_stale(item.type or 'movie', item.media_status, item.last_trakt_fetch):
                        result[item.imdb_id] = _build_metadata_dict(item)

            # Phase 2: fetch missing items from Trakt
            missing = [iid for iid in valid_ids if iid not in result]
            if missing:
                logger.info(f"get_bulk_movie_metadata: {len(result)} cached, fetching {len(missing)} from {_get_metadata_source_name().upper()}")
                for iid in missing:
                    try:
                        with managed_session() as session:
                            data = _refresh_movie(iid, session)
                            result[iid] = data
                    except Exception as e:
                        logging.warning(f"get_bulk_movie_metadata: failed to fetch {iid}: {e}")
                        result[iid] = None

            found = sum(1 for v in result.values() if v is not None)
            logger.info(f"get_bulk_movie_metadata returning {found}/{len(imdb_ids)}")
            return result
        except Exception as e:
            logging.error(f"DirectAPI.get_bulk_movie_metadata: {e}", exc_info=True)
            return {iid: None for iid in imdb_ids}

    # ── Shows ─────────────────────────────────────────────────────────────

    @staticmethod
    def get_show_metadata(imdb_id: str, force_refresh: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        # Guard: Prevent None or invalid imdb_id from reaching database
        if not imdb_id or imdb_id == 'None' or not isinstance(imdb_id, str) or not imdb_id.strip():
            logger.warning(f"DirectAPI.get_show_metadata called with invalid imdb_id: {repr(imdb_id)}")
            return None, None

        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter_by(imdb_id=imdb_id).first()

                if item and not is_stale(item.type or 'show', item.media_status, item.last_trakt_fetch, force=force_refresh):
                    logging.debug(f"get_show_metadata {imdb_id}: cache HIT")
                    md = _build_show_metadata_dict(item)
                    _fetch_and_store_xem(item, session, md)
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID and 'seasons' in md:
                        md['seasons'] = _apply_lego_masters_us_season_fix(md['seasons'])
                    return md, 'battery'

                # Stale or missing — refresh, unless Trakt is in cooldown
                logging.debug(f"get_show_metadata {imdb_id}: cache MISS (stale={item is not None})")
                # Capture fallback before _refresh_show may expire item via session commit
                fallback_md = _build_show_metadata_dict(item) if item else None
                if item:
                    try:
                        from utilities.trakt_coordinator import GlobalTraktCoordinator
                        from datetime import datetime as _dt
                        _coord = GlobalTraktCoordinator.get_instance()
                        if _coord._global_cooldown_until and _dt.now() < _coord._global_cooldown_until:
                            return fallback_md, 'battery'
                    except Exception:
                        pass
                data = _refresh_show(imdb_id, session)
                if data:
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID and 'seasons' in data:
                        data['seasons'] = _apply_lego_masters_us_season_fix(data['seasons'])
                    return data, _get_metadata_source_name()

                if fallback_md:
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID and 'seasons' in fallback_md:
                        fallback_md['seasons'] = _apply_lego_masters_us_season_fix(fallback_md['seasons'])
                    return fallback_md, 'battery'
                return None, None
        except Exception as e:
            logging.error(f"DirectAPI.get_show_metadata {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def get_show_seasons(imdb_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        # Guard: Prevent None or invalid imdb_id from reaching database
        if not imdb_id or imdb_id == 'None' or not isinstance(imdb_id, str) or not imdb_id.strip():
            logger.warning(f"DirectAPI.get_show_seasons called with invalid imdb_id: {repr(imdb_id)}")
            return None, None

        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter_by(imdb_id=imdb_id, type='show').first()

                # Eagerly capture item_id and stale seasons before external calls
                # that may open nested managed_sessions and detach the item
                item_id = item.id if item else None
                stale_seasons = None
                if item and item.seasons:
                    if not is_stale('show', item.media_status, item.last_trakt_fetch):
                        seasons = _format_seasons_from_orm(item.seasons)
                        if imdb_id == LEGO_MASTERS_US_IMDB_ID:
                            seasons = _apply_lego_masters_us_season_fix(seasons)
                        return seasons, 'battery'
                    stale_seasons = _format_seasons_from_orm(item.seasons)

                # Fetch from metadata provider (may open nested sessions)
                seasons_data, source = _get_metadata_client().get_show_seasons_and_episodes(imdb_id, include_specials=True)
                if seasons_data and item_id is not None:
                    _upsert_seasons_and_episodes(item_id, seasons_data, session)
                    session.query(Item).filter_by(id=item_id).update(
                        {'updated_at': datetime.now(_get_local_tz())}
                    )

                if seasons_data:
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID:
                        seasons_data = _apply_lego_masters_us_season_fix(seasons_data)
                    return seasons_data, source

                if stale_seasons is not None:
                    if imdb_id == LEGO_MASTERS_US_IMDB_ID:
                        stale_seasons = _apply_lego_masters_us_season_fix(stale_seasons)
                    return stale_seasons, 'battery'
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

                # Capture md.value as plain string before _refresh_show may expire/detach the ORM object
                md_value = md.value if (item and md) else None

                # If stale but Trakt is in cooldown, return stale data rather than blocking
                if md_value:
                    try:
                        from utilities.trakt_coordinator import GlobalTraktCoordinator
                        from datetime import datetime as _dt
                        _coord = GlobalTraktCoordinator.get_instance()
                        if _coord._global_cooldown_until and _dt.now() < _coord._global_cooldown_until:
                            return json.loads(md_value), 'battery'
                    except Exception:
                        pass

                data = _refresh_show(imdb_id, session)
                if data and 'aliases' in data:
                    return data['aliases'], _get_metadata_source_name()

                if md_value:
                    try:
                        return json.loads(md_value), 'battery'
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
            # Filter out None/empty/invalid IMDb IDs to prevent disk I/O errors
            valid_ids = []
            for iid in imdb_ids:
                if iid and isinstance(iid, str) and iid.strip() and iid != 'None':
                    normalized = normalize_imdb_id(iid)
                    if normalized:
                        valid_ids.append(normalized)

            if len(valid_ids) < len(imdb_ids):
                logger.warning(f"get_bulk_show_airs: filtered {len(imdb_ids) - len(valid_ids)} invalid IDs")

            with managed_session() as session:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata),
                ).filter(Item.imdb_id.in_(valid_ids), Item.type == 'show').all()

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
            # Filter out None/empty/invalid IMDb IDs to prevent disk I/O errors
            valid_ids = []
            for iid in imdb_ids:
                if iid and isinstance(iid, str) and iid.strip() and iid != 'None':
                    normalized = normalize_imdb_id(iid)
                    if normalized:
                        valid_ids.append(normalized)

            if len(valid_ids) < len(imdb_ids):
                logger.warning(f"get_bulk_show_metadata: filtered {len(imdb_ids) - len(valid_ids)} invalid IDs")

            result: dict = {}
            items_missing_xem: dict = {}
            tvdb_ids_to_fetch: dict = {}

            # Return empty results for invalid IDs
            for iid in imdb_ids:
                if iid not in valid_ids:
                    result[iid] = None

            # Phase 1: bulk DB lookup for cached items
            with managed_session() as session:
                items = session.query(Item).options(
                    selectinload(Item.item_metadata),
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter(Item.imdb_id.in_(valid_ids), Item.type == 'show').all()

                for item in items:
                    if not is_stale(item.type or 'show', item.media_status, item.last_trakt_fetch):
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

            # Phase 2: fetch missing items from Trakt
            missing = [iid for iid in valid_ids if iid not in result]
            if missing:
                logger.info(f"get_bulk_show_metadata: {len(result)} cached, fetching {len(missing)} from {_get_metadata_source_name().upper()}")
                for iid in missing:
                    try:
                        with managed_session() as session:
                            data = _refresh_show(iid, session)
                            result[iid] = data
                    except Exception as e:
                        logging.warning(f"get_bulk_show_metadata: failed to fetch {iid}: {e}")
                        result[iid] = None

            # Apply Lego Masters fix
            if LEGO_MASTERS_US_IMDB_ID in result and result[LEGO_MASTERS_US_IMDB_ID]:
                md = result[LEGO_MASTERS_US_IMDB_ID]
                if 'seasons' in md:
                    md['seasons'] = _apply_lego_masters_us_season_fix(md['seasons'])

            found = sum(1 for v in result.values() if v is not None)
            logger.info(f"get_bulk_show_metadata returning {found}/{len(imdb_ids)}")
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
    def _mdblist_tmdb_to_imdb(tmdb_id: str, media_type: str = None) -> Optional[str]:
        """
        Use MDBList API to convert TMDB ID to IMDb ID.

        Note: Caller must check is_mdblist_configured() before calling.

        Args:
            tmdb_id: TMDB ID to convert
            media_type: 'movie' or 'show'/'tv'

        Returns:
            IMDb ID if found, None otherwise
        """
        try:
            import requests
            from utilities.mdblist_api import get_mdblist_api_key, MDBLIST_API_BASE

            api_key = get_mdblist_api_key()

            # MDBList API endpoint for TMDB lookup
            # Include media type to avoid collisions when the same TMDB ID exists for both a movie and a show
            type_param = ''
            if media_type in ('movie',):
                type_param = '&type=movie'
            elif media_type in ('show', 'tv'):
                type_param = '&type=show'
            url = f"{MDBLIST_API_BASE}/?apikey={api_key}&tm={tmdb_id}{type_param}"

            logger.debug(f"MDBList API call: {url.replace(api_key, 'REDACTED')}")
            resp = requests.get(url, timeout=10)
            logger.debug(f"MDBList API response status: {resp.status_code}")

            if resp.status_code == 200:
                data = resp.json()

                # MDBList API returns single item or list
                if isinstance(data, dict):
                    imdb_id = data.get('imdb_id') or data.get('imdbid')
                    if imdb_id and imdb_id.startswith('tt'):
                        return imdb_id
                elif isinstance(data, list) and len(data) > 0:
                    # Use first result if it matches media_type
                    for item in data:
                        item_type = item.get('mediatype', '').lower()
                        # Match media_type if specified
                        if media_type:
                            if (media_type in ('tv', 'show') and item_type in ('show', 'tv')) or \
                               (media_type == 'movie' and item_type == 'movie'):
                                imdb_id = item.get('imdb_id') or item.get('imdbid')
                                if imdb_id and imdb_id.startswith('tt'):
                                    return imdb_id
                        else:
                            # No media_type filter, use first result
                            imdb_id = item.get('imdb_id') or item.get('imdbid')
                            if imdb_id and imdb_id.startswith('tt'):
                                return imdb_id
            elif resp.status_code == 401:
                logger.warning(f"MDBList API: Invalid API key")
            elif resp.status_code == 429:
                logger.warning(f"MDBList API: Rate limit exceeded")
            else:
                logger.warning(f"MDBList API: HTTP {resp.status_code}")

            return None

        except Exception as e:
            logger.error(f"MDBList TMDB lookup error: {e}")
            return None

    @staticmethod
    def tmdb_to_imdb(tmdb_id: str, media_type: str = None) -> Tuple[Optional[str], Optional[str]]:
        logger.info(f"DirectAPI.tmdb_to_imdb for TMDB {tmdb_id} type={media_type}")
        # Normalise media_type for cache key: movies='movie', everything else='show'
        _cache_type = 'movie' if media_type == 'movie' else ('show' if media_type else None)
        try:
            # Check DB cache first — filter by media_type to prevent cross-type collisions
            with managed_session() as session:
                q = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id)
                if _cache_type:
                    q = q.filter_by(media_type=_cache_type)
                mapping = q.first()
                if mapping and not is_tmdb_mapping_stale(mapping.updated_at):
                    return mapping.imdb_id, 'battery'

                # Primary: metadata provider
                # Check if using Trakt and if it's currently rate limited
                metadata_client_name = _get_metadata_source_name()
                skip_trakt = False

                if metadata_client_name == 'trakt':
                    try:
                        from utilities.trakt_coordinator import GlobalTraktCoordinator
                        cooldown_status = GlobalTraktCoordinator.get_instance().get_cooldown_status()
                        if cooldown_status['active']:
                            remaining = cooldown_status['remaining_seconds']
                            logger.info(f"⏭️ Skipping Trakt (rate limited, {remaining:.0f}s remaining), trying fallback methods")
                            skip_trakt = True
                            imdb_id = None
                    except Exception as e:
                        logger.debug(f"Could not check Trakt cooldown status: {e}")
                        skip_trakt = False

                if not skip_trakt:
                    imdb_id, source = _get_metadata_client().convert_tmdb_to_imdb(tmdb_id, media_type=media_type)
                else:
                    imdb_id = None
                    source = None

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

                    # Cache (include media_type to prevent cross-type collisions)
                    if mapping:
                        mapping.imdb_id = imdb_id
                        mapping.media_type = _cache_type
                        mapping.updated_at = datetime.now(_get_local_tz())
                    else:
                        session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id, media_type=_cache_type))
                    return imdb_id, source

            # Fallback: TMDB External IDs
            logger.info(f"Attempting TMDB External IDs fallback for TMDB ID {tmdb_id} (type: {media_type})")
            try:
                from utilities.settings import get_setting
                import requests as req

                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    endpoint = 'movie' if media_type == 'movie' else 'tv'
                    url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
                    logger.debug(f"TMDB External IDs API call: {url.replace(tmdb_api_key, 'REDACTED')}")
                    resp = req.get(url, timeout=10)
                    logger.debug(f"TMDB External IDs response status: {resp.status_code}")
                    if resp.status_code == 200:
                        tmdb_data = resp.json()
                        tmdb_imdb = tmdb_data.get('imdb_id')
                        if tmdb_imdb:
                            logger.info(f"✓ TMDB External IDs fallback SUCCESS: Found IMDb ID {tmdb_imdb} for TMDB {tmdb_id}")
                            with managed_session() as session:
                                existing = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).first()
                                if existing:
                                    existing.imdb_id = tmdb_imdb
                                    existing.media_type = _cache_type
                                    existing.updated_at = datetime.now(_get_local_tz())
                                else:
                                    session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=tmdb_imdb, media_type=_cache_type))
                            return tmdb_imdb, 'tmdb_external_ids'
                        else:
                            logger.warning(f"✗ TMDB External IDs fallback: No IMDb ID in response for TMDB {tmdb_id}")
                    else:
                        logger.warning(f"✗ TMDB External IDs fallback: HTTP {resp.status_code} for TMDB {tmdb_id}")
                else:
                    logger.warning(f"✗ TMDB External IDs fallback: No TMDB API key configured")
            except Exception as e:
                logger.warning(f"✗ TMDB External IDs fallback failed for TMDB {tmdb_id}: {e}")

            # Fallback: MDBList API (if configured)
            logger.info(f"Attempting MDBList API fallback for TMDB ID {tmdb_id} (type: {media_type})")
            try:
                from utilities.mdblist_api import is_mdblist_configured

                if is_mdblist_configured():
                    # Try MDBList lookup by TMDB ID
                    mdblist_imdb = DirectAPI._mdblist_tmdb_to_imdb(tmdb_id, media_type)
                    if mdblist_imdb:
                        logger.info(f"✓ MDBList API fallback SUCCESS: Found IMDb ID {mdblist_imdb} for TMDB {tmdb_id}")
                        with managed_session() as session:
                            q = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id)
                            if _cache_type:
                                q = q.filter_by(media_type=_cache_type)
                            mapping = q.first()
                            if mapping:
                                mapping.imdb_id = mdblist_imdb
                                mapping.media_type = _cache_type
                                mapping.updated_at = datetime.now(_get_local_tz())
                            else:
                                session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=mdblist_imdb, media_type=_cache_type))
                        return mdblist_imdb, 'mdblist'
                    else:
                        logger.warning(f"✗ MDBList API fallback: No IMDb ID found for TMDB {tmdb_id}")
                else:
                    logger.debug(f"✗ MDBList API fallback: No API key configured")
            except Exception as e:
                logger.warning(f"✗ MDBList API fallback failed for TMDB {tmdb_id}: {e}")

            # Fallback: Trakt title search
            logger.info(f"Attempting Trakt title search fallback for TMDB ID {tmdb_id} (type: {media_type})")
            try:
                from utilities.settings import get_setting
                import requests as req

                tmdb_api_key = get_setting('TMDB', 'api_key')
                if tmdb_api_key:
                    endpoint = 'movie' if media_type == 'movie' else 'tv'
                    url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    logger.debug(f"TMDB details API call: {url.replace(tmdb_api_key, 'REDACTED')}")
                    resp = req.get(url, timeout=10)
                    logger.debug(f"TMDB details response status: {resp.status_code}")
                    if resp.status_code == 200:
                        details = resp.json()
                        title = details.get('title') or details.get('name')
                        date_str = details.get('release_date') or details.get('first_air_date')
                        year = int(date_str[:4]) if date_str else None
                        if title:
                            logger.debug(f"Searching Trakt for title='{title}', year={year}, type={media_type}")
                            search_type = 'show' if media_type in ('tv', 'show') else 'movie'
                            results = _get_metadata_client().search_media(title, year=year, media_type=search_type)
                            if results:
                                logger.debug(f"Trakt search returned {len(results)} results for '{title}'")
                                for r in results:
                                    if r.get('imdb_id') and r.get('tmdb_id') == int(tmdb_id):
                                        logger.info(f"✓ Trakt title search fallback SUCCESS: Exact match found IMDb ID {r['imdb_id']} for TMDB {tmdb_id}")
                                        return r['imdb_id'], 'trakt_title_search'
                                for r in results:
                                    if r.get('imdb_id'):
                                        logger.warning(f"⚠ Trakt title search fallback: Using first result IMDb ID {r['imdb_id']} (not exact TMDB match) for TMDB {tmdb_id}")
                                        return r['imdb_id'], 'trakt_title_search_fallback'
                                logger.warning(f"✗ Trakt title search fallback: Found {len(results)} results but none have IMDb IDs for TMDB {tmdb_id}")
                            else:
                                logger.warning(f"✗ Trakt title search fallback: No results found for '{title}' (year: {year})")
                        else:
                            logger.warning(f"✗ Trakt title search fallback: No title found in TMDB response for TMDB {tmdb_id}")
                    else:
                        logger.warning(f"✗ Trakt title search fallback: HTTP {resp.status_code} from TMDB for TMDB {tmdb_id}")
                else:
                    logger.warning(f"✗ Trakt title search fallback: No TMDB API key configured")
            except Exception as e:
                logger.warning(f"✗ Trakt title search fallback failed for TMDB {tmdb_id}: {e}")

            logger.error(f"❌ All TMDB→IMDb conversion methods failed for TMDB ID {tmdb_id} (type: {media_type})")

            # Cache the failure to prevent repeated API calls for known-missing mappings
            # Use NULL to indicate "no mapping exists"
            try:
                with managed_session() as session:
                    q = session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id)
                    if _cache_type:
                        q = q.filter_by(media_type=_cache_type)
                    mapping = q.first()
                    if mapping:
                        mapping.imdb_id = None
                        mapping.updated_at = datetime.now(_get_local_tz())
                    else:
                        session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=None, media_type=_cache_type))
                    logger.info(f"Cached failed TMDB→IMDb lookup for {tmdb_id} (will retry after staleness period)")
            except Exception as e:
                logger.debug(f"Could not cache failed lookup: {e}")

            return None, None
        except Exception as e:
            logger.error(f"DirectAPI.tmdb_to_imdb {tmdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def force_refresh_metadata(imdb_id: str, item_type: str = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        logger.info(f"DirectAPI.force_refresh_metadata for {imdb_id} (type={item_type})")
        try:
            with managed_session() as session:
                if not item_type:
                    item = session.query(Item).filter_by(imdb_id=imdb_id).first()
                    item_type = item.type if item else None
                # Normalize: episode items fetch show-level metadata
                if item_type == 'episode':
                    item_type = 'show'

                source = _get_metadata_source_name()
                if item_type == 'movie':
                    data = _refresh_movie(imdb_id, session)
                    return data, source if data else None
                elif item_type == 'show':
                    data = _refresh_show(imdb_id, session)
                    return data, source if data else None
                else:
                    # Type unknown — try show first, then movie
                    data = _refresh_show(imdb_id, session)
                    if data:
                        return data, source
                    data = _refresh_movie(imdb_id, session)
                    return data, source if data else None
        except Exception as e:
            logging.error(f"DirectAPI.force_refresh_metadata {imdb_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def force_refresh_tmdb_mapping(tmdb_id: str, media_type: str = None) -> Tuple[Optional[str], Optional[str]]:
        _cache_type = 'movie' if media_type == 'movie' else ('show' if media_type else None)
        try:
            with managed_session() as session:
                # Delete all mappings for this tmdb_id (clear both movie and show entries if any)
                session.query(TMDBToIMDBMapping).filter_by(tmdb_id=tmdb_id).delete()
                session.flush()

                imdb_id, source = _get_metadata_client().convert_tmdb_to_imdb(tmdb_id, media_type=media_type)
                if imdb_id:
                    session.add(TMDBToIMDBMapping(tmdb_id=tmdb_id, imdb_id=imdb_id, media_type=_cache_type))
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
            client = _get_metadata_client()
            results = client.search_media(query=query, year=year, media_type=media_type)
            if year is not None and (not results or len(results) == 0):
                logger.info(f"Year-filtered search empty, retrying without year")
                results = client.search_media(query=query, year=None, media_type=media_type)
            source = _get_metadata_source_name() if results is not None else None
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

    @staticmethod
    def get_plex_guid(imdb_id: str, media_type: str,
                      season: Optional[int] = None,
                      episode: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Return stored Plex GUIDs from the battery DB.

        For movies: returns {'show_guid': str|None}
        For shows:  returns {
            'show_guid': str|None,
            'season_guids': {season_num: guid}|None,
            'episode_guids': {season_num: {ep_num: guid}}|None,
        }

        If season+episode are provided, also returns 'episode_guid' for the
        specific episode for convenient access.

        Reads from DB first; falls back to live Trakt lookup if not found.
        """
        if not imdb_id:
            return None
        try:
            with managed_session() as session:
                item = session.query(Item).options(
                    selectinload(Item.item_metadata),
                    selectinload(Item.seasons).selectinload(Season.episodes),
                ).filter_by(imdb_id=imdb_id).first()

                if item:
                    # Extract show-level plex guid from metadata KV store
                    md = _build_metadata_dict(item)
                    plex_ids = (md.get('ids') or {})
                    if isinstance(plex_ids, str):
                        try:
                            import json as _json
                            plex_ids = _json.loads(plex_ids)
                        except Exception:
                            plex_ids = {}
                    plex_obj = plex_ids.get('plex') or {}
                    if isinstance(plex_obj, str):
                        try:
                            import json as _json
                            plex_obj = _json.loads(plex_obj)
                        except Exception:
                            plex_obj = {}
                    show_guid = plex_obj.get('guid') if isinstance(plex_obj, dict) else None

                    if media_type == 'movie':
                        return {'show_guid': show_guid}

                    # Season and episode guids from DB
                    season_guids: Dict[int, str] = {}
                    episode_guids: Dict[int, Dict[int, str]] = {}
                    for s in item.seasons:
                        if s.plex_guid:
                            season_guids[s.season_number] = s.plex_guid
                        for ep in s.episodes:
                            if ep.plex_guid:
                                episode_guids.setdefault(s.season_number, {})[ep.episode_number] = ep.plex_guid

                    result = {
                        'show_guid': show_guid,
                        'season_guids': season_guids or None,
                        'episode_guids': episode_guids or None,
                    }
                    if season is not None and episode is not None:
                        result['episode_guid'] = (episode_guids.get(season) or {}).get(episode)
                        result['season_guid'] = season_guids.get(season)

                    # If episode GUID is missing, backfill from Trakt once per session
                    _need_ep_guid = (season is not None and episode is not None
                                     and not result.get('episode_guid'))
                    with _plex_guid_backfilled_lock:
                        _should_backfill = _need_ep_guid and imdb_id not in _plex_guid_backfilled
                        if _should_backfill:
                            _plex_guid_backfilled.add(imdb_id)
                    if _should_backfill:
                        logger.debug(f"[PlexGUID] Missing episode GUID for {imdb_id} S{season}E{episode} — backfilling from Trakt")
                        try:
                            md_live = _build_show_metadata_dict(item)
                            _ensure_plex_guids_in_data(imdb_id, md_live, 'show')
                            _seasons_live = md_live.get('seasons') or {}
                            if _seasons_live:
                                _updated_ep_guids: Dict[int, Dict[int, str]] = {}
                                with managed_session() as _ws:
                                    _upsert_seasons_and_episodes(item.id, _seasons_live, _ws)
                                    # Re-read updated GUIDs inside the same session to avoid detached instance
                                    from sqlalchemy.orm import selectinload as _sil
                                    from cli_battery.app.database import Item as _Item, Season as _Season
                                    _fresh = _ws.query(_Item).options(
                                        _sil(_Item.seasons).selectinload(_Season.episodes)
                                    ).filter_by(id=item.id).first()
                                    if _fresh:
                                        for _s in _fresh.seasons:
                                            for _ep in _s.episodes:
                                                if _ep.plex_guid:
                                                    _updated_ep_guids.setdefault(_s.season_number, {})[_ep.episode_number] = _ep.plex_guid
                                result['episode_guid'] = (_updated_ep_guids.get(season) or {}).get(episode)
                        except Exception as _be:
                            logger.debug(f"[PlexGUID] Backfill failed for {imdb_id}: {_be}")

                    return result

            # Not in DB — do live lookup
            return trakt_client.get_plex_guid(imdb_id, media_type)
        except Exception as e:
            logger.debug(f"[PlexGUID] Battery lookup failed for {imdb_id}: {e} — falling back to live Trakt")
            try:
                # Fall back to live Trakt lookup if battery DB has I/O errors
                return trakt_client.get_plex_guid(imdb_id, media_type)
            except Exception as e2:
                logger.debug(f"[PlexGUID] Live Trakt fallback also failed for {imdb_id}: {e2}")
                return None
