"""
NZB Upgrade Scanner — Upgrade Hub backend for Newznab indexers

Fetches the latest NZBs from all enabled Newznab indexers (browse mode,
up to 1000 per category per indexer) once per scan trigger, then matches
candidates to library items using PTT-parsed title + year.

Matching strategy (no IMDB ID in feed):
  - PTT parses the NZB release name to extract title and year
  - Match requires BOTH:
      1. token_sort_ratio(query_title, nzb_title) >= 0.90
      2. Year matches exactly OR nzb has no year (year-agnostic releases)
  - For episodes: additionally filtered by season+episode numbers from PTT
  - For season packs: additionally filtered to single-season packs

This avoids false matches like "The Owl House" → "The Last Kingdom" because
the year (2020 vs 2022) and strict title threshold would reject it.
"""

import logging
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from utilities.settings import load_config

logger = logging.getLogger(__name__)

_CAT_MOVIE = '2000,2030,2040,2045,2050,2060'
_CAT_TV    = '5000,5010,5020,5030,5040,5045,5060,5070'

# Per-scan in-memory pool — reset at start of each scan trigger
_nzb_pool_lock = threading.Lock()
_nzb_pool: Dict[str, List[Dict]] = {}  # 'movie' | 'tv' -> results
_nzb_pool_ready = False


def _reset_pool() -> None:
    global _nzb_pool, _nzb_pool_ready
    with _nzb_pool_lock:
        _nzb_pool = {}
        _nzb_pool_ready = False


def get_newznab_scrapers() -> List[Tuple[str, Dict]]:
    """Return list of (instance_name, settings) for all enabled Newznab scrapers."""
    config = load_config()
    scrapers = []
    for instance, settings in config.get('Scrapers', {}).items():
        if not isinstance(settings, dict):
            continue
        if settings.get('type') == 'Newznab' and settings.get('enabled') and settings.get('url'):
            scrapers.append((instance, settings))
    return scrapers


def _fetch_browse_page(url: str, api_key: str, cats: str, limit: int,
                        offset: int, timeout: int = 30) -> Tuple[List[ET.Element], int]:
    """Fetch one browse page. Returns (items, total)."""
    from routes.api_tracker import api as _api
    try:
        r = _api.get(f'{url}/api', params={
            'apikey': api_key, 't': 'search', 'q': '',
            'cat': cats, 'limit': limit, 'offset': offset,
        }, timeout=timeout)
        if r.status_code != 200:
            return [], 0
        root = ET.fromstring(r.text)
        channel = root.find('channel')
        if channel is None:
            return [], 0
        ns = 'http://www.newznab.com/DTD/2010/feeds/attributes/'
        total = 0
        resp_el = channel.find(f'{{{ns}}}response')
        if resp_el is not None:
            try:
                total = int(resp_el.get('total', 0))
            except (ValueError, TypeError):
                pass
        return channel.findall('item'), total
    except Exception as e:
        logger.warning(f'[NZB_UPGRADE] Browse error {url}: {e}')
        return [], 0


def _parse_items(items: List[ET.Element], instance: str) -> List[Dict]:
    """Parse Newznab XML items into result dicts with PTT-parsed info."""
    from PTT import parse_title as ptt_parse
    ns = {'newznab': 'http://www.newznab.com/DTD/2010/feeds/attributes/'}
    results = []
    for item in items:
        title_el = item.find('title')
        if title_el is None or not title_el.text:
            continue
        title = title_el.text.strip()

        enclosure_el = item.find('enclosure')
        link_el = item.find('link')
        nzb_url = None
        if enclosure_el is not None:
            nzb_url = enclosure_el.get('url', '')
        if not nzb_url and link_el is not None:
            nzb_url = (link_el.text or '').strip()
        if not nzb_url:
            continue

        guid_el = item.find('guid')
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else nzb_url

        size_bytes = 0
        if enclosure_el is not None:
            try:
                size_bytes = int(enclosure_el.get('length', 0))
            except (ValueError, TypeError):
                pass
        if not size_bytes:
            for attr in item.findall('newznab:attr', ns):
                if attr.get('name') == 'size':
                    try:
                        size_bytes = int(attr.get('value', 0))
                    except (ValueError, TypeError):
                        pass
                    break

        pub_date_el = item.find('pubDate')
        pub_date = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ''
        size_gb = round(size_bytes / (1024 ** 3), 2) if size_bytes else 0.0

        try:
            ptt = ptt_parse(title)
        except Exception:
            ptt = {}

        parsed_info = {
            'guid': guid,
            'protocol': 'nzb',
            'publish_date': pub_date,
            'source_instance': instance,
            'title': ptt.get('title', title),
            'year': ptt.get('year'),
            'resolution': ptt.get('resolution'),
            'source': ptt.get('quality'),
            'audio': ptt.get('audio', []),
            'codec': ptt.get('codec'),
            'group': ptt.get('group'),
            'season': ptt.get('seasons', [None])[0] if ptt.get('seasons') else None,
            'seasons': ptt.get('seasons', []),
            'episode': ptt.get('episodes', [None])[0] if ptt.get('episodes') else None,
            'episodes': ptt.get('episodes', []),
            'hdr': ptt.get('hdr', []),
            'is_hdr': bool(ptt.get('hdr')),
            'trash': ptt.get('trash', False),
            'original_title': title,
        }

        # Skip results outside usenet retention window
        from scraper.newznab import _get_retention_days, _is_within_retention_days
        if not _is_within_retention_days(pub_date, _get_retention_days()):
            continue

        results.append({
            'title': title,
            'original_title': title,
            'size': size_gb,
            'source': f'NZB-{instance}',
            'seeders': 0,
            'hash': '',
            'magnet': nzb_url,
            'nzb_url': nzb_url,
            'protocol': 'nzb',
            'ingested_at': pub_date,
            'parsed_info': parsed_info,
        })
    return results


def _fetch_instance_category(instance: str, settings: Dict, cats: str,
                              max_items: int = 1000) -> List[Dict]:
    """Fetch up to max_items NZBs from one indexer for one category, paginating as needed."""
    url = settings.get('url', '').rstrip('/')
    api_key = settings.get('api_key', '').strip()
    if not url or not api_key:
        return []

    page_limit = min(int(settings.get('browse_limit', 100)), 1000)
    all_results: List[Dict] = []
    offset = 0

    while len(all_results) < max_items:
        fetch_size = min(page_limit, max_items - len(all_results))
        items, total = _fetch_browse_page(url, api_key, cats, fetch_size, offset)
        if not items:
            break
        all_results.extend(_parse_items(items, instance))
        offset += len(items)
        if offset >= min(total, max_items) or len(items) < fetch_size:
            break

    logger.info(f'[NZB_UPGRADE] {instance} ({cats[:4]}…): fetched {len(all_results)} NZBs')
    return all_results


def fetch_nzb_pool(scrapers: List[Tuple[str, Dict]], max_items: int = 1000) -> Dict[str, List[Dict]]:
    """Fetch latest NZBs from all indexers in parallel. Returns {'movie': [...], 'tv': [...]}."""
    global _nzb_pool, _nzb_pool_ready
    _reset_pool()

    if not scrapers:
        return {'movie': [], 'tv': []}

    tasks = [(inst, cfg, 'movie', _CAT_MOVIE) for inst, cfg in scrapers] + \
            [(inst, cfg, 'tv', _CAT_TV)    for inst, cfg in scrapers]

    raw: Dict[str, List[Dict]] = {'movie': [], 'tv': []}
    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
        future_map = {
            pool.submit(_fetch_instance_category, inst, cfg, cats, max_items): cat
            for inst, cfg, cat, cats in tasks
        }
        for fut in as_completed(future_map):
            cat = future_map[fut]
            try:
                raw[cat].extend(fut.result())
            except Exception as e:
                logger.warning(f'[NZB_UPGRADE] Pool fetch error: {e}')

    # Deduplicate by guid across indexers
    deduped: Dict[str, List[Dict]] = {}
    for cat, results in raw.items():
        seen: set = set()
        unique = []
        for r in results:
            guid = (r.get('parsed_info') or {}).get('guid') or r.get('nzb_url', '')
            if guid and guid not in seen:
                seen.add(guid)
                unique.append(r)
            elif not guid:
                unique.append(r)
        deduped[cat] = unique
        logger.info(f'[NZB_UPGRADE] Pool ready: {len(unique)} unique {cat} NZBs')

    with _nzb_pool_lock:
        _nzb_pool = deduped
        _nzb_pool_ready = True

    return deduped


# ---------------------------------------------------------------------------
# Title + year matching against pool
# ---------------------------------------------------------------------------

import re as _re
_NORM_RE = _re.compile(r'[^a-z0-9\s]')


def _norm(s: str) -> str:
    return _NORM_RE.sub('', (s or '').lower()).strip()


def _filter_pool_for_item(pool: List[Dict], title: str, year: Optional[int],
                           title_threshold: float = 0.90) -> List[Dict]:
    """
    Filter pool to candidates matching title + year.

    Requires:
      1. token_sort_ratio(query_title, nzb_ptt_title) >= title_threshold
      2. Year matches exactly OR nzb has no parsed year (year-agnostic)

    token_sort_ratio handles word-order differences and is strict enough at
    0.90 to reject unrelated titles that share only common words like "The".
    """
    from rapidfuzz import fuzz as _fuzz
    norm_query = _norm(title)
    candidates = []
    for r in pool:
        pi = r.get('parsed_info') or {}
        nzb_title = pi.get('title') or r.get('title', '')
        nzb_year  = pi.get('year')

        # Year gate: reject if both have a year and they don't match
        if year and nzb_year and abs(int(nzb_year) - int(year)) > 1:
            continue

        sim = _fuzz.token_sort_ratio(_norm(nzb_title), norm_query) / 100.0
        if sim >= title_threshold:
            candidates.append(r)

    return candidates


def _filter_pool_for_episode(pool: List[Dict], season: int, episode: int) -> List[Dict]:
    """Keep only NZBs whose PTT-parsed seasons/episodes include the target."""
    result = []
    for r in pool:
        pi = r.get('parsed_info') or {}
        seasons  = pi.get('seasons', [])
        episodes = pi.get('episodes', [])
        if season in seasons and (not episodes or episode in episodes):
            result.append(r)
    return result


def _filter_pool_for_pack(pool: List[Dict], season: int) -> List[Dict]:
    """Keep only NZBs that are single-season packs for the given season."""
    from database.zilean_upgrade import _is_single_season_pack
    result = []
    for r in pool:
        pi = r.get('parsed_info') or {}
        seasons  = pi.get('seasons', [])
        episodes = pi.get('episodes', [])
        if season in seasons and not episodes:
            if _is_single_season_pack(r.get('title', ''), season):
                result.append(r)
    return result


# ---------------------------------------------------------------------------
# Per-item scan workers
# ---------------------------------------------------------------------------

def scan_nzb_movie(item: Dict, pool_movie: List[Dict],
                   threshold: float, default_version: str,
                   not_wanted_hashes: Optional[frozenset] = None) -> Optional[Dict]:
    from database.zilean_upgrade import (
        _get_version_settings, _score_results_with_current,
        _item_size_gb, _item_filename, _is_upgrade,
    )
    current_filename = _item_filename(item)
    if not current_filename:
        return None

    version = (item.get('version') or default_version).rstrip('*').strip()
    vs = _get_version_settings(version)

    candidates = _filter_pool_for_item(pool_movie, item['title'], item.get('year'))
    if not candidates:
        return None

    current_score, candidate_scored = _score_results_with_current(
        current_filename, candidates,
        item['title'], item.get('year'), None, None, 'movie', False, vs,
        size_bytes=_item_size_gb(item),
        genres_raw=item.get('genres') or '',
    )
    if not candidate_scored:
        return None

    candidate_scored = [(s, r) for s, r in candidate_scored
                        if not (r.get('title', '') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        candidate_scored = [(s, r) for s, r in candidate_scored
                            if _gh(r.get('nzb_url', '') or r.get('magnet', '')) not in not_wanted_hashes
                            and _nt(r.get('title', '')) not in not_wanted_hashes]
    if not candidate_scored:
        return None

    current_size_gb = round(_item_size_gb(item), 2)
    if current_size_gb > 0:
        candidate_scored = [(s, r) for s, r in candidate_scored
                            if float(r.get('size') or 0) >= current_size_gb]
    if not candidate_scored:
        return None

    best_score, best_result = candidate_scored[0]
    if not _is_upgrade(best_score, current_score, threshold):
        return None

    qualifying = [(s, r) for s, r in candidate_scored if _is_upgrade(s, current_score, threshold)][:3]
    return {
        'type': 'movie', 'item_id': item['id'], 'imdb_id': item['imdb_id'],
        'title': item['title'], 'year': item.get('year'), 'version': version,
        'current_score': round(current_score, 2), 'new_score': round(best_score, 2),
        'improvement_pct': round((best_score - current_score) / max(abs(current_score), 1) * 100, 1),
        'current_file': current_filename,
        'current_protocol': 'nzb' if str(item.get('filled_by_torrent_id') or '').startswith('nzb:') else 'torrent',
        'current_size_gb': current_size_gb,
        'new_title': best_result.get('title', ''), 'new_size_gb': best_result.get('size', 0),
        'new_magnet': best_result.get('nzb_url', '') or best_result.get('magnet', ''),
        'protocol': 'nzb',
        '_alts': qualifying,
        '_phalanx_only': False,
    }


def scan_nzb_episode(item: Dict, pool_tv: List[Dict],
                     threshold: float, default_version: str,
                     not_wanted_hashes: Optional[frozenset] = None) -> Optional[Dict]:
    from database.zilean_upgrade import (
        _get_version_settings, _score_results_with_current,
        _item_size_gb, _item_filename, _is_upgrade,
    )
    current_filename = _item_filename(item)
    if not current_filename:
        return None

    version = (item.get('version') or default_version).rstrip('*').strip()
    vs = _get_version_settings(version)
    season, episode = item.get('season_number'), item.get('episode_number')
    if season is None or episode is None:
        return None

    title_candidates = _filter_pool_for_item(pool_tv, item['title'], item.get('year'))
    candidates = _filter_pool_for_episode(title_candidates, season, episode)
    if not candidates:
        return None

    current_score, candidate_scored = _score_results_with_current(
        current_filename, candidates,
        item['title'], item.get('year'), season, episode, 'episode', False, vs,
        size_bytes=_item_size_gb(item),
        genres_raw=item.get('genres') or '',
    )
    if not candidate_scored:
        return None

    candidate_scored = [
        (s, r) for s, r in candidate_scored
        if (r.get('parsed_info') or {}).get('episodes')
        and episode in (r.get('parsed_info') or {}).get('episodes', [])
    ]
    if not candidate_scored:
        return None

    candidate_scored = [(s, r) for s, r in candidate_scored
                        if not (r.get('title', '') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        candidate_scored = [(s, r) for s, r in candidate_scored
                            if _gh(r.get('nzb_url', '') or r.get('magnet', '')) not in not_wanted_hashes
                            and _nt(r.get('title', '')) not in not_wanted_hashes]
    if not candidate_scored:
        return None

    current_size_gb = round(_item_size_gb(item), 2)
    if current_size_gb > 0:
        candidate_scored = [(s, r) for s, r in candidate_scored
                            if float(r.get('size') or 0) >= current_size_gb]
    if not candidate_scored:
        return None

    best_score, best_result = candidate_scored[0]
    if not _is_upgrade(best_score, current_score, threshold):
        return None

    qualifying = [(s, r) for s, r in candidate_scored if _is_upgrade(s, current_score, threshold)][:3]
    return {
        'type': 'episode', 'item_id': item['id'], 'imdb_id': item['imdb_id'],
        'title': item['title'], 'year': item.get('year'),
        'season': season, 'episode': episode, 'version': version,
        'current_score': round(current_score, 2), 'new_score': round(best_score, 2),
        'improvement_pct': round((best_score - current_score) / max(abs(current_score), 1) * 100, 1),
        'current_file': current_filename,
        'current_protocol': 'nzb' if str(item.get('filled_by_torrent_id') or '').startswith('nzb:') else 'torrent',
        'current_size_gb': current_size_gb,
        'new_title': best_result.get('title', ''), 'new_size_gb': best_result.get('size', 0),
        'new_magnet': best_result.get('nzb_url', '') or best_result.get('magnet', ''),
        'protocol': 'nzb',
        '_alts': qualifying,
        '_phalanx_only': False,
    }


def scan_nzb_season_pack(imdb_id: str, season_num: int, season_eps: List[Dict],
                          pool_tv: List[Dict], pack_threshold: float,
                          default_version: str,
                          not_wanted_hashes: Optional[frozenset] = None) -> Optional[Dict]:
    from database.zilean_upgrade import (
        _get_version_settings, _score_results_with_current,
        _item_size_gb, _item_filename, _is_upgrade, _is_single_season_pack,
    )
    from database.core import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE imdb_id=? AND season_number=? AND type='episode'",
            (imdb_id, season_num),
        ).fetchone()
        total_in_db = row[0] if row else 0
    finally:
        conn.close()
    if total_in_db == 0:
        return None
    ratio = len(season_eps) / total_in_db
    if ratio < pack_threshold:
        return None

    sample = season_eps[0]
    current_filename = _item_filename(sample)
    if not current_filename:
        return None

    version = (sample.get('version') or default_version).rstrip('*').strip()
    vs = _get_version_settings(version)

    title_candidates = _filter_pool_for_item(pool_tv, sample['title'], sample.get('year'))
    pack_results = _filter_pool_for_pack(title_candidates, season_num)
    if not pack_results:
        return None

    current_score, scored_candidates = _score_results_with_current(
        current_filename, pack_results,
        sample['title'], sample.get('year'),
        season_num, None, 'episode', True, vs,
        size_bytes=_item_size_gb(sample),
        genres_raw=sample.get('genres') or '',
    )
    if not scored_candidates:
        return None

    scored_candidates = [(s, r) for s, r in scored_candidates
                         if _is_single_season_pack(r.get('title', ''), season_num)]
    scored_candidates = [(s, r) for s, r in scored_candidates
                         if not (r.get('title', '') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        scored_candidates = [(s, r) for s, r in scored_candidates
                             if _gh(r.get('nzb_url', '') or r.get('magnet', '')) not in not_wanted_hashes
                             and _nt(r.get('title', '')) not in not_wanted_hashes]
    if not scored_candidates:
        return None

    best_score, best_result = scored_candidates[0]
    improvement_pct = round((best_score - current_score) / max(abs(current_score), 1) * 100, 1)
    return {
        'type': 'season_pack', 'imdb_id': imdb_id,
        'title': sample['title'], 'year': sample.get('year'),
        'season': season_num, 'collected_count': len(season_eps),
        'total_count': total_in_db, 'ratio_pct': round(ratio * 100, 1),
        'item_ids': [ep['id'] for ep in season_eps], 'version': version,
        'current_score': round(current_score, 2), 'best_score': round(best_score, 2),
        'improvement_pct': improvement_pct,
        'current_size_gb': round(sum(_item_size_gb(ep) for ep in season_eps), 2),
        'current_protocol': 'nzb' if str(sample.get('filled_by_torrent_id') or '').startswith('nzb:') else 'torrent',
        'new_title': best_result.get('title', ''), 'new_size_gb': best_result.get('size', 0),
        'new_magnet': best_result.get('nzb_url', '') or best_result.get('magnet', ''),
        'protocol': 'nzb',
        '_alts': scored_candidates[:3],
        '_phalanx_only': False,
        'current_files': sorted([
            {'ep': ep.get('episode_number') or ep.get('episode') or 0,
             'file': _item_filename(ep)}
            for ep in season_eps
        ], key=lambda x: x['ep']),
    }


# ---------------------------------------------------------------------------
# Main NZB scan entry point
# ---------------------------------------------------------------------------

def scan_nzb_upgrades(
    movies: List[Dict],
    episodes: List[Dict],
    season_groups: Dict,
    threshold: float,
    pack_threshold: float,
    default_version: str,
    not_wanted_hashes: frozenset,
    max_workers: int = 20,
    progress_callback=None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Fetch latest NZBs once (bulk pool), then score each library item against
    matching candidates using title+year filtering.
    """
    scrapers = get_newznab_scrapers()
    if not scrapers:
        logger.info('[NZB_UPGRADE] No enabled Newznab scrapers — skipping NZB scan')
        return [], []

    logger.info(f'[NZB_UPGRADE] Fetching NZB pool from {len(scrapers)} indexer(s)…')
    pool = fetch_nzb_pool(scrapers, max_items=1000)
    pool_movie = pool.get('movie', [])
    pool_tv    = pool.get('tv', [])

    if not pool_movie and not pool_tv:
        logger.info('[NZB_UPGRADE] NZB pool empty — skipping NZB scan')
        return [], []

    upgrade_candidates: List[Dict] = []
    pack_candidates:    List[Dict] = []
    total = len(movies) + len(episodes) + len(season_groups)
    futures: Dict = {}

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, total))) as pool_ex:
        for m in movies:
            f = pool_ex.submit(scan_nzb_movie, m, pool_movie, threshold,
                               default_version, not_wanted_hashes)
            futures[f] = ('movie', m)
        for ep in episodes:
            f = pool_ex.submit(scan_nzb_episode, ep, pool_tv, threshold,
                               default_version, not_wanted_hashes)
            futures[f] = ('episode', ep)
        for (imdb_id, season_num), eps in season_groups.items():
            f = pool_ex.submit(scan_nzb_season_pack, imdb_id, season_num, eps,
                               pool_tv, pack_threshold, default_version, not_wanted_hashes)
            futures[f] = ('pack', (imdb_id, season_num))

        done = 0
        for future in as_completed(futures):
            ftype, item = futures[future]
            try:
                r = future.result()
                if r:
                    if ftype == 'pack':
                        pack_candidates.append(r)
                    else:
                        upgrade_candidates.append(r)
            except Exception as e:
                logger.debug(f'[NZB_UPGRADE] Worker error ({ftype}): {e}')
            done += 1
            if progress_callback:
                progress_callback(done, total)

    logger.info(f'[NZB_UPGRADE] Scan complete: {len(upgrade_candidates)} upgrade(s), '
                f'{len(pack_candidates)} pack(s)')
    return upgrade_candidates, pack_candidates
