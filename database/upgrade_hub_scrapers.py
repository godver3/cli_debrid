"""
Upgrade Hub — search with the user's own scrapers, and cache status for candidates.

The Zilean and Newznab-feed phases only see releases those two sources happen to
hold. This phase runs the normal scrape (Torrentio, Jackett, Prowlarr, Newznab…)
for a rotating slice of the library each scan, so a better release that appears
months after an item was collected is still found.

Items are checked oldest-checked first and the last check time is kept in
``upgrade_hub_scraper_checks``, so a daily scan with a cap of N works through the
whole library over time instead of re-searching the same items.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional

from database.core import get_db_connection
from utilities.settings import get_setting

logger = logging.getLogger(__name__)

DEFAULT_CHECKS_PER_SCAN = 50
MAX_CHECKS_PER_SCAN = 2000
# Scrapers rate-limit; a couple of searches at a time is enough.
SCRAPE_WORKERS = 2

_tables_ready = False
_tables_lock = threading.Lock()


def _ensure_table() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        conn = get_db_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS upgrade_hub_scraper_checks ("
                " item_id INTEGER PRIMARY KEY,"
                " checked_at TEXT NOT NULL)"
            )
            conn.commit()
            _tables_ready = True
        finally:
            conn.close()


def scraper_settings() -> Dict:
    """(enabled, checks_per_scan) from the Upgrade Hub settings."""
    raw = get_setting('Upgrade Hub', 'use_scrapers', True)
    enabled = raw if isinstance(raw, bool) else str(raw).strip().lower() in ('true', '1', 'yes', 'on')
    try:
        limit = int(get_setting('Upgrade Hub', 'scraper_checks_per_scan', DEFAULT_CHECKS_PER_SCAN))
    except (TypeError, ValueError):
        limit = DEFAULT_CHECKS_PER_SCAN
    return {'enabled': enabled, 'limit': max(0, min(MAX_CHECKS_PER_SCAN, limit))}


def last_checked_map(item_ids: List[int]) -> Dict[int, str]:
    _ensure_table()
    if not item_ids:
        return {}
    conn = get_db_connection()
    try:
        out: Dict[int, str] = {}
        ids = list(item_ids)
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            ph = ','.join('?' * len(chunk))
            for row in conn.execute(
                f"SELECT item_id, checked_at FROM upgrade_hub_scraper_checks WHERE item_id IN ({ph})",
                chunk,
            ):
                out[row[0]] = row[1]
        return out
    finally:
        conn.close()


def record_checked(item_ids: List[int]) -> None:
    if not item_ids:
        return
    _ensure_table()
    now = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO upgrade_hub_scraper_checks (item_id, checked_at) VALUES (?, ?)",
            [(iid, now) for iid in item_ids],
        )
        conn.commit()
    finally:
        conn.close()


def pick_items_to_check(items: List[Dict], limit: int) -> List[Dict]:
    """Never-checked items first, then the longest since their last check.

    Ties go to movies, then to the most recently added item.
    """
    if limit <= 0 or not items:
        return []
    checked = last_checked_map([i['id'] for i in items])

    def key(item):
        return (
            checked.get(item['id'], ''),          # '' (never) sorts first
            0 if item.get('type') == 'movie' else 1,
            -int(item['id']),
        )

    return sorted(items, key=key)[:limit]


def _release_age_days(item: Dict, today: date) -> Optional[int]:
    raw = item.get('release_date')
    if not raw:
        return None
    try:
        released = raw if isinstance(raw, date) else datetime.strptime(str(raw)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None
    return (today - released).days


def released_within(items: List[Dict], days: int, today: Optional[date] = None) -> List[Dict]:
    """Items released in the last ``days`` days (today included); unknown or
    future release dates are left out."""
    today = today or datetime.now().date()
    days = max(0, int(days))
    out = []
    for item in items:
        age = _release_age_days(item, today)
        if age is not None and 0 <= age <= days:
            out.append(item)
    return out


def count_recent_items(days: int) -> Dict[str, int]:
    """How many Collected items "Search recent releases" would search for ``days``.

    A preview: it skips the genre / NAS exclusions and the duplicate-movie-file
    dedupe the scan applies, so the scan may search a few fewer.
    """
    from database.database_reading import get_all_media_items
    rows = [dict(r) for r in get_all_media_items(
        state='Collected', columns=['id', 'type', 'imdb_id', 'release_date']) if r['imdb_id']]
    recent = released_within(rows, days)
    return {
        'items': len(recent),
        'movies': sum(1 for r in recent if r['type'] == 'movie'),
        'episodes': sum(1 for r in recent if r['type'] == 'episode'),
        'cap': MAX_CHECKS_PER_SCAN,
    }


class _Ident:
    """The bit of QueueManager that scrape_with_fallback needs."""

    @staticmethod
    def generate_identifier(item: Dict) -> str:
        from queues.upgrading_queue import UpgradingQueue
        return UpgradingQueue.generate_identifier(item)


def _result_link(r: Dict) -> str:
    return r.get('nzb_url') or r.get('magnet') or ''


def _result_hash(r: Dict) -> str:
    link = r.get('magnet') or ''
    if not link.startswith('magnet:'):
        return ''
    try:
        from debrid.common import extract_hash_from_magnet
        return (extract_hash_from_magnet(link) or '').lower()
    except Exception:
        return ''


def _scan_item(item: Dict, threshold: float, default_version: str,
               not_wanted_hashes: Optional[frozenset]) -> Optional[Dict]:
    """Search one Collected item with the configured scrapers.

    Returns a candidate in the same shape the Zilean phase produces, or None.
    """
    from database import get_media_item_by_id
    from database import zilean_upgrade as zu
    from queues.scraping_queue import ScrapingQueue

    full = get_media_item_by_id(item['id'])
    if not full or full.get('state') != 'Collected':
        return None
    full = dict(full)
    current_filename = zu._item_filename(full) or zu._item_filename(item)
    if not current_filename:
        return None  # nothing to compare against

    version = (full.get('version') or default_version or '').rstrip('*').strip()
    full['version'] = version
    vs = zu._get_version_settings(version)

    results, _filtered = ScrapingQueue().scrape_with_fallback(full, False, _Ident, skip_filter=False)
    if not results:
        return None

    from queues.upgrading_queue import is_same_release
    pool = []
    for r in results:
        if is_same_release(full, r):
            continue
        r = dict(r)
        if r.get('nzb_url') and not r.get('magnet'):
            r['magnet'] = r['nzb_url']
        r['info_hash'] = _result_hash(r)
        # Live search results are as fresh as it gets; mark them recent so the
        # hub's recency pick and "recent only" view treat them that way.
        r['ingested_at'] = datetime.now(timezone.utc).isoformat()
        pool.append(r)
    if not pool:
        return None

    is_episode = full.get('type') == 'episode'
    season = full.get('season_number') if is_episode else None
    episode = full.get('episode_number') if is_episode else None
    current_score, scored = zu._score_results_with_current(
        current_filename, pool,
        full['title'], full.get('year'), season, episode,
        'episode' if is_episode else 'movie', False, vs,
        size_bytes=zu._item_size_gb(full),
        genres_raw=full.get('genres') or '',
    )
    if is_episode:
        # Single-episode upgrades only; season packs are a separate hub flow.
        scored = [(s, r) for s, r in scored
                  if episode in ((r.get('parsed_info') or {}).get('episodes') or [])]
    scored = [(s, r) for s, r in scored if not (r.get('title') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        scored = [(s, r) for s, r in scored
                  if _gh(_result_link(r)) not in not_wanted_hashes
                  and _nt(r.get('title', '')) not in not_wanted_hashes]
    current_size_gb = round(zu._item_size_gb(full), 2)
    if current_size_gb > 0:
        scored = [(s, r) for s, r in scored if float(r.get('size') or 0) >= current_size_gb]
    if not scored:
        return None
    best_score, best = scored[0]
    if not zu._is_upgrade(best_score, current_score, threshold):
        return None
    qualifying = [(s, r) for s, r in scored if zu._is_upgrade(s, current_score, threshold)][:3]

    candidate = {
        'type': 'episode' if is_episode else 'movie',
        'item_id': full['id'], 'imdb_id': full.get('imdb_id'),
        'title': full['title'], 'year': full.get('year'), 'version': version,
        'current_score': round(current_score, 2), 'new_score': round(best_score, 2),
        'improvement_pct': round((best_score - current_score) / max(abs(current_score), 1) * 100, 1),
        'current_file': current_filename,
        'current_protocol': 'nzb' if str(full.get('filled_by_torrent_id') or '').startswith('nzb:') else 'torrent',
        'current_size_gb': current_size_gb,
        'new_title': best.get('title', ''), 'new_size_gb': best.get('size', 0),
        'new_magnet': _result_link(best),
        'new_info_hash': best.get('info_hash', ''),
        'protocol': 'nzb' if best.get('nzb_url') else 'torrent',
        'found_by': 'scrapers',
        '_alts': qualifying,
        '_phalanx_only': False,
    }
    if is_episode:
        candidate['season'] = season
        candidate['episode'] = episode
    return candidate


def scan_with_scrapers(items: List[Dict], threshold: float, default_version: str,
                       not_wanted_hashes: Optional[frozenset] = None,
                       limit: int = DEFAULT_CHECKS_PER_SCAN,
                       progress_callback: Optional[Callable[[int, int], None]] = None,
                       errors: Optional[List[str]] = None,
                       searched: Optional[List[int]] = None) -> List[Dict]:
    """Search up to ``limit`` items (oldest-checked first) and return candidates.

    ``searched``, if given, receives the ids of the items that were searched.
    """
    chosen = pick_items_to_check(items, limit)
    if not chosen:
        return []
    logger.info(f"[UPGRADE_HUB] Searching {len(chosen)} item(s) with the configured scrapers "
                f"(limit {limit}, {len(items)} eligible)")
    candidates: List[Dict] = []
    checked: List[int] = []
    done = 0
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
        futures = {pool.submit(_scan_item, it, threshold, default_version, not_wanted_hashes): it
                   for it in chosen}
        for f in as_completed(futures):
            it = futures[f]
            try:
                c = f.result()
                checked.append(it['id'])
                if c:
                    candidates.append(c)
            except Exception as e:
                logger.warning(f"[UPGRADE_HUB] Scraper search failed for '{it.get('title')}': {e}")
                if errors is not None:
                    errors.append(f"Scrapers '{it.get('title')}': {e}")
            done += 1
            if progress_callback:
                progress_callback(done, len(chosen))
    record_checked(checked)
    if searched is not None:
        searched.extend(checked)
    logger.info(f"[UPGRADE_HUB] Scraper search: {len(candidates)} upgrade(s) from {len(checked)} item(s)")
    return candidates


# ---------------------------------------------------------------------------
# Cache status
# ---------------------------------------------------------------------------

def _provider_key() -> str:
    raw = get_setting('Debrid Provider', 'provider', '') or ''
    return raw.strip().lower().replace('-', '').replace(' ', '')


def annotate_cache_status(candidates: List[Dict]) -> Optional[str]:
    """Set ``cached`` (True/False/None) and ``cache_service`` on torrent candidates.

    Uses whatever can answer without adding the torrent to the account:
      * TorBox — its checkcached endpoint, in batches.
      * Real-Debrid — Phalanx community cache data, when Phalanx is enabled.
    Other providers (AllDebrid, Debrid-Link, Premiumize) and usenet candidates
    can only be checked by adding them, so they stay None and are checked when
    queued. Returns the name of the service that answered, or None.
    """
    for c in candidates:
        c['cached'] = None
        c.pop('cache_service', None)

    def _hash(c):
        h = (c.get('new_info_hash') or '').lower()
        if h:
            return h
        return _result_hash({'magnet': c.get('new_magnet') or ''})

    torrent = [(c, _hash(c)) for c in candidates if c.get('protocol') != 'nzb']
    torrent = [(c, h) for c, h in torrent if h]
    if not torrent:
        return None
    hashes = sorted({h for _, h in torrent})

    status: Dict[str, Optional[bool]] = {}
    service: Optional[str] = None
    provider_key = _provider_key()
    try:
        if provider_key == 'torbox':
            from debrid import get_debrid_provider
            provider = get_debrid_provider()
            if provider is not None:
                for i in range(0, len(hashes), 50):
                    chunk = hashes[i:i + 50]
                    res = provider.is_cached_sync([f'magnet:?xt=urn:btih:{h}' for h in chunk],
                                                  skip_phalanx_db=True)
                    if isinstance(res, dict):
                        for h in chunk:
                            v = res.get(h)
                            status[h] = v if isinstance(v, bool) else None
                service = 'TorBox'
        elif provider_key == 'realdebrid' and get_setting('UI Settings', 'enable_phalanx_db', default=False):
            from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
            cache_map = PhalanxDBClassManager().get_multi_cache_status(hashes)
            for h in hashes:
                st = cache_map.get(h)
                status[h] = bool(st['is_cached']) if st is not None else None
            service = 'Real-Debrid'
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Cache check skipped: {e}")
        return None

    if service:
        for c, h in torrent:
            c['cached'] = status.get(h)
            c['cache_service'] = service
        known = [v for v in status.values() if v is not None]
        logger.info(f"[UPGRADE_HUB] {service} cache: {sum(known)} cached, "
                    f"{len(known) - sum(known)} not cached, {len(hashes) - len(known)} unknown")
    return service
