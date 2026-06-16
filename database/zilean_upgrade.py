"""
Zilean Upgrade Scanner — Upgrade Hub backend

Scans Collected media items against Zilean for better-quality releases.

Two query modes:
  1. Direct DB  — psycopg2 connects to the Zilean PostgreSQL instance.
                  Bulk queries by IMDB ID; much faster than per-item REST calls.
                  Enabled when Zilean scraper has db_enabled=True + credentials.
  2. REST API   — Falls back to the existing scrape_zilean_instance() HTTP calls
                  when DB credentials are not configured.

Upgrade types supported:
  - Movie  -> Movie        (same IMDB ID, higher-scoring release)
  - Episode -> Episode     (same show IMDB ID + season/episode, higher score)
  - Season Pack -> Pack    (same show IMDB ID + season, higher-scoring pack)
  - Episodes -> Season Pack (threshold of episodes collected => suggest pack)
"""

import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database.core import get_db_connection
from database.database_reading import get_all_media_items
from scraper.functions.rank_results import rank_result_key
from utilities.reverse_parser import get_default_version
from utilities.settings import get_setting, load_config

logger = logging.getLogger(__name__)

# Common public trackers to append to bare Zilean magnets so RD can resolve
# uncached torrents without tracker info.
_COMMON_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.demonii.com:1320/announce',
    'udp://open.stealth.si:80/announce',
    'udp://tracker.torrent.eu.org:451/announce',
    'udp://explodie.org:6969/announce',
]

def _add_trackers(magnet: str) -> str:
    """Append common trackers to a bare magnet link if none are present."""
    if not magnet or '&tr=' in magnet:
        return magnet
    return magnet + ''.join(f'&tr={t}' for t in _COMMON_TRACKERS)


def _batch_check_rd_cache(hashes: List[str], batch_size: int = 25) -> Dict[str, bool]:
    """
    Check which hashes are instantly available (cached) on Real-Debrid.

    Uses /torrents/instantAvailability/h1/h2/.../hN — a single lightweight GET
    per batch, no torrents added to account, returns immediately.

    Returns dict mapping lowercase hash -> True (cached) / False (not cached).
    Missing hashes default to False.
    """
    import time as _time

    if not hashes:
        return {}

    try:
        from debrid.real_debrid.api import make_request, get_api_key
        api_key = get_api_key()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Could not get RD API key for cache check: {e}")
        return {}

    results: Dict[str, bool] = {}
    lower_hashes = [h.lower() for h in hashes if h]
    max_retries = 4

    for i in range(0, len(lower_hashes), batch_size):
        batch = lower_hashes[i:i + batch_size]
        endpoint = '/torrents/instantAvailability/' + '/'.join(batch)
        success = False
        for attempt in range(max_retries):
            try:
                data = make_request('GET', endpoint, api_key)
                if not isinstance(data, dict):
                    for h in batch:
                        results[h] = False
                else:
                    for h in batch:
                        rd_list = data.get(h, {}).get('rd', [])
                        results[h] = bool(rd_list)
                success = True
                break
            except Exception as e:
                err_str = str(e)
                if '429' in err_str and attempt < max_retries - 1:
                    wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                    logger.warning(f"[UPGRADE_HUB] Cache check 429 rate-limit (batch {i // batch_size + 1}), retrying in {wait}s…")
                    _time.sleep(wait)
                else:
                    logger.warning(f"[UPGRADE_HUB] instantAvailability batch failed: {e}")
                    for h in batch:
                        results[h] = False
                    break
        # Throttle between batches to avoid rate-limiting
        if success and i + batch_size < len(lower_hashes):
            _time.sleep(1.5)

    return results


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_scan_in_progress = False
_last_scan_results: Optional[Dict[str, Any]] = None
_last_scan_time: Optional[datetime] = None
_scan_progress: Dict[str, Any] = {}

# Per-scan version settings cache.  Reset at the start of every scan so
# changes to version config are always picked up.  Pre-populated before
# any workers start so all threads share a single read-only dict — no
# locks required for concurrent reads.
_vs_cache: Dict[str, Dict] = {}

# Keyed by item_id — populated by queue_upgrade_candidates so the UpgradingQueue
# can skip re-scraping and use the already-known candidate magnet directly.
_queued_magnets: Dict[int, Dict] = {}

# Track whether persistent cache tables have been initialised this session
_cache_tables_ready = False
_cache_initialized = False


# ---------------------------------------------------------------------------
# Persistent cache — SQLite helpers
# ---------------------------------------------------------------------------

def _ensure_cache_tables() -> None:
    """Create upgrade hub cache tables if they don't exist."""
    global _cache_tables_ready
    if _cache_tables_ready:
        return
    from database.upgrade_hub_activity import create_upgrade_hub_activity_table
    create_upgrade_hub_activity_table()
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS zilean_rest_cache (
                imdb_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                zilean_url TEXT NOT NULL,
                results_json TEXT NOT NULL,
                cached_at TEXT NOT NULL,
                PRIMARY KEY (imdb_id, season, zilean_url)
            );
            CREATE TABLE IF NOT EXISTS upgrade_hub_scan_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                results_json TEXT NOT NULL,
                version_used TEXT NOT NULL,
                scanned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upgrade_hub_queued_magnets (
                item_id INTEGER PRIMARY KEY,
                magnet_json TEXT NOT NULL,
                stored_at TEXT NOT NULL
            );
        """)
        conn.commit()
        _cache_tables_ready = True
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Cache table creation error: {e}")
    finally:
        conn.close()


def _ensure_cache_initialized() -> None:
    """Load persisted scan results and queued magnets into memory (once per process)."""
    global _cache_initialized
    if _cache_initialized:
        return
    _ensure_cache_tables()
    _load_cached_scan_results()
    _load_queued_magnets_from_db()
    _cache_initialized = True


# ── Scan results ────────────────────────────────────────────────────────────

def _load_cached_scan_results() -> None:
    """Load the last persisted scan results into memory on startup."""
    global _last_scan_results, _last_scan_time
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT results_json, scanned_at FROM upgrade_hub_scan_cache WHERE id=1"
        ).fetchone()
        if row:
            results = json.loads(row['results_json'])
            _last_scan_results = results
            _last_scan_time = datetime.fromisoformat(row['scanned_at'])
            logger.info(f"[UPGRADE_HUB] Loaded cached scan results from DB (scanned_at={row['scanned_at']})")
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not load cached scan results: {e}")
    finally:
        conn.close()


def _save_scan_results_to_db(results: Dict, version_used: str, scanned_at: str) -> None:
    """Persist scan results to SQLite."""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO upgrade_hub_scan_cache (id, results_json, version_used, scanned_at) VALUES (1, ?, ?, ?)",
            (json.dumps(results), version_used, scanned_at)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Could not save scan results to DB: {e}")
    finally:
        conn.close()


# ── Queued magnets ───────────────────────────────────────────────────────────

def _load_queued_magnets_from_db() -> None:
    """Load persisted queued magnets into memory."""
    global _queued_magnets
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT item_id, magnet_json FROM upgrade_hub_queued_magnets"
        ).fetchall()
        loaded = 0
        for row in rows:
            try:
                _queued_magnets[row['item_id']] = json.loads(row['magnet_json'])
                loaded += 1
            except Exception:
                pass
        if loaded:
            logger.info(f"[UPGRADE_HUB] Loaded {loaded} queued magnets from DB")
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not load queued magnets: {e}")
    finally:
        conn.close()


def _save_queued_magnet_to_db(item_id: int, candidate: Dict) -> None:
    """Persist a queued magnet candidate to SQLite."""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO upgrade_hub_queued_magnets (item_id, magnet_json, stored_at) VALUES (?, ?, ?)",
            (item_id, json.dumps(candidate), datetime.now().isoformat())
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not save queued magnet for item {item_id}: {e}")
    finally:
        conn.close()


def delete_queued_magnet_from_db(item_id: int) -> None:
    """Remove a consumed queued magnet from SQLite (called by upgrading_queue)."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM upgrade_hub_queued_magnets WHERE item_id=?", (item_id,))
        conn.commit()
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not delete queued magnet for item {item_id}: {e}")
    finally:
        conn.close()


# ── Zilean REST cache ────────────────────────────────────────────────────────

def _load_rest_cache_from_db(zilean_url: str) -> Dict[Tuple, List[Dict]]:
    """Load all cached REST responses for a Zilean URL from SQLite."""
    conn = get_db_connection()
    cache: Dict[Tuple, List[Dict]] = {}
    try:
        rows = conn.execute(
            "SELECT imdb_id, season, results_json FROM zilean_rest_cache WHERE zilean_url=?",
            (zilean_url,)
        ).fetchall()
        for row in rows:
            try:
                cache[(row['imdb_id'], row['season'])] = json.loads(row['results_json'])
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not load REST cache: {e}")
    finally:
        conn.close()
    return cache


def _save_rest_cache_batch_to_db(entries: List[Tuple[str, int, List]], zilean_url: str) -> None:
    """Persist multiple (imdb_id, season, results) triples to REST cache."""
    now = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO zilean_rest_cache (imdb_id, season, zilean_url, results_json, cached_at) VALUES (?, ?, ?, ?, ?)",
            [(imdb_id, season, zilean_url, json.dumps(results), now)
             for imdb_id, season, results in entries]
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Could not save REST cache batch: {e}")
    finally:
        conn.close()


def _invalidate_rest_cache_keys(keys: List[Tuple[str, int]], zilean_url: str) -> None:
    """Remove specific (imdb_id, season) entries from REST cache."""
    if not keys:
        return
    conn = get_db_connection()
    try:
        conn.executemany(
            "DELETE FROM zilean_rest_cache WHERE imdb_id=? AND season=? AND zilean_url=?",
            [(imdb_id, season, zilean_url) for imdb_id, season in keys]
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] REST cache invalidation error: {e}")
    finally:
        conn.close()


# ── Zilean change detection ──────────────────────────────────────────────────

def _get_zilean_last_import_time(db_cfg: Dict) -> Optional[datetime]:
    """Query Zilean's ImportMetadata for DmmLastImport.OccuredAt."""
    try:
        import psycopg2
        conn = psycopg2.connect(**db_cfg, connect_timeout=5)
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "Value" FROM "ImportMetadata" WHERE "Key" = %s LIMIT 1',
                ('DmmLastImport',)
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return None
        val = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        occurred_at = val.get('OccuredAt') or val.get('occuredAt')
        if occurred_at:
            from datetime import timezone
            return datetime.fromisoformat(str(occurred_at).replace('Z', '+00:00'))
        return None
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not query ImportMetadata: {e}")
        return None


def _get_changed_season_keys_since(db_cfg: Dict, since: datetime) -> Optional[List[Tuple[str, int]]]:
    """
    Query Zilean for (ImdbId, Season) pairs with new torrents ingested after `since`.
    Returns None if the query fails (caller should fall back to fetching all uncached).
    """
    try:
        import psycopg2
        from datetime import timezone
        # Ensure since is timezone-aware
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        conn = psycopg2.connect(**db_cfg, connect_timeout=5)
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                'SELECT DISTINCT "ImdbId", unnest("Seasons") AS season '
                'FROM "Torrents" WHERE "IngestedAt" > %s AND "ImdbId" IS NOT NULL LIMIT 50000',
                (since,)
            )
            rows = cur.fetchall()
        conn.close()
        return [(imdb_id, season) for imdb_id, season in rows
                if imdb_id and season is not None]
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not query changed seasons: {e}")
        return None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_zilean_config() -> Optional[Tuple[str, Dict]]:
    """Return (instance_name, settings_dict) for the first enabled Zilean scraper,
    or None if not configured/enabled."""
    config = load_config()
    for key, val in config.get('Scrapers', {}).items():
        if isinstance(val, dict) and val.get('type', '').lower() == 'zilean' and val.get('enabled') and val.get('url'):
            return key, val
    return None


def _get_db_config(zilean_settings: Dict) -> Optional[Dict]:
    """Return DB connection params if db_enabled and credentials are present."""
    if not zilean_settings.get('db_enabled'):
        return None
    url = zilean_settings.get('url', '')
    # Extract host from URL (strip scheme + port)
    host = url.replace('https://', '').replace('http://', '').split(':')[0].split('/')[0]
    if not host:
        return None
    return {
        'host':     host,
        'port':     int(zilean_settings.get('db_port') or 5432),
        'dbname':   zilean_settings.get('db_name') or '',
        'user':     zilean_settings.get('db_username') or '',
        'password': zilean_settings.get('db_password') or '',
    }


def _item_filename(item: Dict) -> str:
    """Return the best available filename for an item, falling back to location_basename."""
    return (item.get('filled_by_file') or item.get('location_basename') or '')


def _item_size_gb(item: Dict) -> float:
    """Return item size in GB: DB value if set, else stat location_on_disk, else 0."""
    db_size = float(item.get('size') or 0)
    if db_size > 0:
        return db_size
    path = item.get('location_on_disk') or ''
    if path:
        try:
            import os as _os
            return round(_os.path.getsize(path) / (1024 ** 3), 4)
        except OSError:
            pass
    return 0.0


def _get_version_settings(version: str) -> Dict:
    version = (version or '').rstrip('*').strip() or version
    if version in _vs_cache:
        return _vs_cache[version]
    try:
        from queues.config_manager import get_version_settings as _gvs
        vs = _gvs(version)
        if vs:
            _vs_cache[version] = vs
            return vs
        default = get_default_version()
        if default and default != version:
            vs = _gvs(default)
            if vs:
                _vs_cache[version] = vs
                return vs
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Could not load version settings for '{version}': {e}")
    _vs_cache[version] = {}
    return {}


# ---------------------------------------------------------------------------
# Scoring helper
# ---------------------------------------------------------------------------

def _ensure_parsed_info(results: List[Dict]) -> None:
    """Run PTT on any result missing resolution_rank so rank_result_key scores properly."""
    from scraper.functions.file_processing import parse_torrent_info
    for r in results:
        if 'resolution_rank' not in (r.get('parsed_info') or {}):
            r['parsed_info'] = parse_torrent_info(r.get('title', ''), size=r.get('size'))


def _apply_version_hard_filters(candidates: List[Dict], version_settings: Dict) -> List[Dict]:
    """
    Apply hard gates from version settings to upgrade candidates.

    Mirrors the hard-reject logic in scraper/functions/filter_results.py:
      - max_resolution / resolution_wanted: reject candidates that exceed the
        version's resolution ceiling (e.g. drop 4K candidates for a 1080p version).
      - filter_out: reject candidates whose title/filename matches any excluded term.
      - filter_in: reject candidates whose title/filename matches none of the
        required terms.

    preferred_filter_in / preferred_filter_out are *scoring* adjustments, not hard
    gates — they are already handled by rank_result_key() and are not applied here.

    The current file (source == '__current__') is never filtered.
    Requires parsed_info to be populated on candidates before calling.
    """
    from scraper.functions.filter_results import resolution_filter
    from scraper.functions.other_functions import smart_search
    from scraper.functions.similarity_checks import normalize_title

    max_resolution = version_settings.get('max_resolution', '1080p') or '1080p'
    resolution_wanted = version_settings.get('resolution_wanted', '==') or '=='
    def _pat(item):
        return item['pattern'] if isinstance(item, dict) else item
    filter_in_patterns  = [_pat(x) for x in (version_settings.get('filter_in',  []) or [])]
    filter_out_patterns = [_pat(x) for x in (version_settings.get('filter_out', []) or [])]

    # Nothing to filter — skip the loop entirely (2160p/<= is the most permissive combination)
    if not filter_in_patterns and not filter_out_patterns and max_resolution == '2160p' and resolution_wanted == '<=':
        return candidates

    kept = []
    for r in candidates:
        title_str    = r.get('title', '') or ''
        filename_str = (r.get('additional_metadata') or {}).get('filename') or title_str

        # 1. Resolution ceiling
        detected_res = (r.get('parsed_info') or {}).get('resolution', 'Unknown')
        if not resolution_filter(detected_res, max_resolution, resolution_wanted):
            logger.debug(
                f"[UPGRADE_HUB] Hard filter: resolution '{detected_res}' rejected "
                f"(want {resolution_wanted} {max_resolution}) for '{title_str}'"
            )
            continue

        # 2. filter_out — hard reject on raw title/filename (pre-normalization)
        if filter_out_patterns:
            rejected = False
            for pattern in filter_out_patterns:
                if smart_search(pattern, title_str) or smart_search(pattern, filename_str):
                    logger.debug(
                        f"[UPGRADE_HUB] Hard filter: filter_out '{pattern}' matched '{title_str}'"
                    )
                    rejected = True
                    break
            if rejected:
                continue

        # 3. filter_in — hard reject if no required term matches (post-normalization)
        if filter_in_patterns:
            norm_title    = normalize_title(title_str).lower()
            norm_filename = normalize_title(filename_str).lower() if filename_str else norm_title
            matched = any(
                smart_search(p, norm_title) or smart_search(p, norm_filename)
                for p in filter_in_patterns
            )
            if not matched:
                logger.debug(
                    f"[UPGRADE_HUB] Hard filter: no filter_in pattern matched '{title_str}'"
                )
                continue

        kept.append(r)

    return kept


def _score_results(
    results: List[Dict],
    title: str,
    year: Optional[int],
    season: Optional[int],
    episode: Optional[int],
    content_type: str,
    is_multi: bool,
    version_settings: Dict,
) -> List[Tuple[float, Dict]]:
    # Ensure all results have PTT-parsed parsed_info (resolution_rank, is_hdr, etc.)
    # so rank_result_key can differentiate quality properly.
    _ensure_parsed_info(results)
    scored = []
    for r in results:
        try:
            key = rank_result_key(
                result=r,
                all_results=results,
                query=title,
                query_year=year,
                query_season=season,
                query_episode=episode if not is_multi else None,
                multi=is_multi,
                content_type=content_type,
                version_settings=version_settings,
                upgrade_mode=True,
            )
            scored.append((-key[0], r))
        except Exception as e:
            logger.debug(f"[UPGRADE_HUB] Scoring error for '{r.get('title')}': {e}")
    scored.sort(key=lambda x: -x[0])
    return scored


def _make_current_result(filename: str, title: str, size_bytes: float = 0,
                         genres_raw: str = '') -> Dict:
    """
    Build a synthetic 'current file' result suitable for rank_result_key scoring.

    Key design decisions:
      - `title` is set to the CLEAN query title (e.g. "Star Wars: The Rise of Skywalker"),
        NOT the raw filename.  rank_result_key runs a regex check for /episode|season|s\\d{2}/
        on the result title to detect wrong content-type and applies a -500 penalty.
        Filenames like "Star.Wars.Episode.IX..." or "Handmaids.Tale.S03..." would
        incorrectly trigger that penalty if the filename were used as the title.
      - The raw filename is passed via `additional_metadata.filename` so that
        preferred_filter_in/out patterns ("REMUX", "BluRay", "WEBRip", etc.) are
        still matched against the real quality/source info in the filename.
      - PTT is run on the filename to extract resolution, HDR, codec → ensures
        rank_result_key sees the same quality attributes as it does for Zilean results.
      - size_bytes is the DB-stored file size in bytes (same unit as media_items.size);
        converted to GB here to match the unit rank_result_key expects.
      - genres_raw / is_anime: rank_result_key uses genres to detect anime content and
        adjusts season/episode matching behaviour. Without these the current file would
        be scored under different assumptions than the candidates, producing asymmetric
        scores for anime titles.
    """
    from scraper.functions.file_processing import parse_torrent_info
    parsed = parse_torrent_info(filename, size=0)
    size_gb = round(float(size_bytes or 0), 4)  # media_items.size is already in GB
    genres_list = list(_parse_genres(genres_raw or ''))
    is_anime = 'anime' in {g.lower() for g in genres_list}
    return {
        'title': title,
        'additional_metadata': {'filename': filename},
        'source': '__current__',
        'parsed_info': parsed,
        'size': size_gb,
        'genres': genres_list,
        'is_anime': is_anime,
    }


def _score_results_with_current(
    filename: str,
    candidates: List[Dict],
    title: str,
    year: Optional[int],
    season: Optional[int],
    episode: Optional[int],
    content_type: str,
    is_multi: bool,
    version_settings: Dict,
    size_bytes: float = 0,
    genres_raw: str = '',
) -> Tuple[float, List[Tuple[float, Dict]]]:
    """
    Score the current file AND all candidates in a single pool using the same
    version settings.  Scoring everything together ensures consistent size
    normalization and a fair relative comparison.

    Returns (current_score, [(score, result), ...]) where the list is sorted
    best-first and excludes the current-file entry.
    """
    if not candidates:
        return 0.0, []

    # Parse candidates first so resolution is available for hard filters.
    _ensure_parsed_info(list(candidates))

    # Apply hard version filters before scoring: max_resolution, filter_in, filter_out.
    # The current file is not a candidate so it is never filtered out.
    # preferred_filter_in / preferred_filter_out are scoring adjustments handled by
    # rank_result_key() and are intentionally not hard gates.
    candidates = _apply_version_hard_filters(list(candidates), version_settings)

    # Reject candidates that are the same release as the current file.
    # Normalize by lowercasing and stripping non-alphanumeric chars so that
    # minor formatting differences (dots vs spaces, .mkv suffix) don't create
    # false upgrades against the exact same encode.
    if filename:
        import re as _re2
        _strip = _re2.compile(r'[^a-z0-9]')
        _norm_current = _strip.sub('', filename.lower())
        candidates = [
            r for r in candidates
            if _strip.sub('', (r.get('title') or r.get('original_title') or '').lower()) != _norm_current
        ]

    current_result = _make_current_result(filename, title, size_bytes=size_bytes,
                                          genres_raw=genres_raw) if filename else None
    pool = ([current_result] if current_result else []) + list(candidates)

    _ensure_parsed_info(pool)  # no-op for candidates already parsed above

    # Scraper priorities (both global scraper_priority and per-version version_scraper_priority)
    # exist to prefer certain sources when picking NEW downloads.  They must not influence
    # upgrade quality comparisons — every Zilean result would otherwise get a fixed +1000
    # (or whatever the configured priority is) making the current file always look worse
    # by a constant offset regardless of actual quality difference.
    vs_no_priority = dict(version_settings)
    vs_no_priority['enable_scraper_priorities'] = False
    for r in pool:
        r['scraper_priority'] = 0

    scored = _score_results(pool, title, year, season, episode, content_type, is_multi, vs_no_priority)

    current_score = 0.0
    candidate_scored: List[Tuple[float, Dict]] = []
    for s, r in scored:
        if r.get('source') == '__current__':
            current_score = s
        else:
            candidate_scored.append((s, r))

    return current_score, candidate_scored


def _is_upgrade(best_score: float, current_score: float, threshold: float) -> bool:
    if best_score <= 0:
        return False
    if current_score <= 0:
        return True
    return (best_score - current_score) / abs(current_score) >= threshold


def _is_single_season_pack(title: str, season_num: int) -> bool:
    """Return True only if title is a single-season pack for season_num (not multi-season)."""
    import re
    t = title.lower()
    # Reject episode-specific files (e.g. S07E02, S01E14)
    if re.search(r's\d{1,2}e\d{2,}', t):
        return False
    # Reject multi-season ranges: S01-S09, S01-09, S1-S9
    if re.search(r's\d{1,2}\s*[-–]\s*s?\d{1,2}', t):
        return False
    # Reject written ranges: Seasons 1-9, Season 1 & 2
    if re.search(r'seasons?\s*\d+\s*[-–&]\s*\d+', t):
        return False
    # Reject complete/full series labels
    if re.search(r'complete\s*(series|collection|pack|seasons?)?', t):
        return False
    # Reject titles containing multiple distinct season markers (e.g. S01S02S03)
    season_markers = re.findall(r's(\d{1,2})(?:e\d{2})?(?!\d)', t)
    unique_seasons = set(int(m) for m in season_markers)
    if len(unique_seasons) > 1:
        return False
    # Must contain the target season
    if season_num not in unique_seasons:
        return False
    return True


def _pick_best_alt(alts: List[Tuple[float, Dict]], recent_days: int = 90) -> Tuple[Optional[float], Optional[Dict], bool]:
    """
    Pick the best upgrade alternative from a scored list.

    Prefers recently-ingested results (proxy for RD cache likelihood) with the
    highest score.  Falls back to the best-scored result overall if nothing is
    recent.

    Returns (score, result, is_recent).
    """
    from datetime import datetime, timezone, timedelta
    if not alts:
        return None, None, False
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    recent: List[Tuple[float, Dict]] = []
    for s, r in alts:
        iat_str = r.get('ingested_at', '')
        if iat_str:
            try:
                iat = datetime.fromisoformat(iat_str.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                try:
                    from email.utils import parsedate_to_datetime as _p2dt
                    iat = _p2dt(iat_str)
                except Exception:
                    continue
            except Exception:
                continue
            if iat.tzinfo is None:
                iat = iat.replace(tzinfo=timezone.utc)
            if iat >= cutoff:
                recent.append((s, r))
    if recent:
        best = max(recent, key=lambda x: x[0])
        return best[0], best[1], True
    # Fall back to best-scored overall
    return alts[0][0], alts[0][1], False


# ---------------------------------------------------------------------------
# Direct DB query mode  —  batch-first helpers
# ---------------------------------------------------------------------------

def _rows_to_results(rows) -> List[Dict]:
    """Convert raw Zilean DB rows (without Seasons/Episodes) to result dicts."""
    results = []
    for raw_title, info_hash, size, year, ingested_at in rows:
        if not info_hash:
            continue
        size_gb = round(float(size or 0) / (1024 ** 3), 2)
        results.append({
            'title': raw_title or '',
            'size': size_gb,
            'source': 'Zilean-DB',
            'magnet': _add_trackers(f'magnet:?xt=urn:btih:{info_hash}'),
            'info_hash': info_hash,
            'year': year,
            'ingested_at': str(ingested_at) if ingested_at else '',
            'parsed_info': {},
        })
    return results


def _db_batch_fetch_movies(conn, imdb_ids: List[str],
                           after_date=None) -> Dict[str, List[Dict]]:
    """
    Fetch Zilean torrent data for ALL movie IMDB IDs in one query.
    Returns dict mapping imdb_id -> list of result dicts.
    Replaces N individual movie queries with 1.
    """
    if not imdb_ids:
        return {}
    try:
        with conn.cursor() as cur:
            if after_date:
                cur.execute(
                    'SELECT "ImdbId", "RawTitle", "InfoHash", "Size", "Year", "IngestedAt"'
                    ' FROM "Torrents" WHERE "ImdbId" = ANY(%s) AND "Category" = \'movies\' AND "IngestedAt" >= %s LIMIT 100000',
                    (imdb_ids, after_date)
                )
            else:
                cur.execute(
                    'SELECT "ImdbId", "RawTitle", "InfoHash", "Size", "Year", "IngestedAt"'
                    ' FROM "Torrents" WHERE "ImdbId" = ANY(%s) AND "Category" = \'movies\' LIMIT 100000',
                    (imdb_ids,)
                )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Batch movie fetch error: {e}")
        return {}

    by_id: Dict[str, List[Dict]] = {}
    for imdb_id, raw_title, info_hash, size, year, ingested_at in rows:
        if not info_hash:
            continue
        size_gb = round(float(size or 0) / (1024 ** 3), 2)
        by_id.setdefault(imdb_id, []).append({
            'title': raw_title or '',
            'size': size_gb,
            'source': 'Zilean-DB',
            'magnet': _add_trackers(f'magnet:?xt=urn:btih:{info_hash}'),
            'info_hash': info_hash,
            'year': year,
            'ingested_at': str(ingested_at) if ingested_at else '',
            'parsed_info': {},
        })
    return by_id


def _db_fetch_show_torrents(conn, imdb_id: str, after_date=None) -> List[tuple]:
    """
    Fetch ALL torrents for a show (all seasons/episodes) in one query.
    Returns raw rows including Seasons and Episodes arrays so callers can
    filter client-side for specific episodes or season packs.
    Replaces N_episodes individual queries with 1 per show.
    """
    try:
        with conn.cursor() as cur:
            if after_date:
                cur.execute(
                    'SELECT "RawTitle", "InfoHash", "Size", "Year", "IngestedAt", "Seasons", "Episodes"'
                    ' FROM "Torrents" WHERE "ImdbId" = %s AND "IngestedAt" >= %s LIMIT 5000',
                    (imdb_id, after_date)
                )
            else:
                cur.execute(
                    'SELECT "RawTitle", "InfoHash", "Size", "Year", "IngestedAt", "Seasons", "Episodes"'
                    ' FROM "Torrents" WHERE "ImdbId" = %s LIMIT 5000',
                    (imdb_id,)
                )
            return cur.fetchall()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] Show fetch error for {imdb_id}: {e}")
        return []


def _filter_show_for_episode(rows: List[tuple], season: int, episode: int) -> List[Dict]:
    """Filter pre-fetched show rows for a specific season+episode."""
    results = []
    for raw_title, info_hash, size, year, ingested_at, seasons, episodes in rows:
        if not info_hash:
            continue
        s_list = seasons or []
        e_list = episodes or []
        if season in s_list and episode in e_list:
            size_gb = round(float(size or 0) / (1024 ** 3), 2)
            results.append({
                'title': raw_title or '',
                'size': size_gb,
                'source': 'Zilean-DB',
                'magnet': _add_trackers(f'magnet:?xt=urn:btih:{info_hash}'),
                'info_hash': info_hash,
                'year': year,
                'ingested_at': str(ingested_at) if ingested_at else '',
                'parsed_info': {},
            })
    return results


def _filter_show_for_pack(rows: List[tuple], season: int) -> List[Dict]:
    """Filter pre-fetched show rows for season pack candidates (season present, any episodes)."""
    results = []
    for raw_title, info_hash, size, year, ingested_at, seasons, episodes in rows:
        if not info_hash:
            continue
        if season in (seasons or []):
            size_gb = round(float(size or 0) / (1024 ** 3), 2)
            results.append({
                'title': raw_title or '',
                'size': size_gb,
                'source': 'Zilean-DB',
                'magnet': _add_trackers(f'magnet:?xt=urn:btih:{info_hash}'),
                'info_hash': info_hash,
                'year': year,
                'ingested_at': str(ingested_at) if ingested_at else '',
                'parsed_info': {},
            })
    return results


def _filter_rest_cache_for_episode(
    cache: Dict, imdb_id: str, season: Optional[int], episode: Optional[int]
) -> Optional[List[Dict]]:
    """
    Filter pre-fetched REST season cache for a specific episode.

    The cache is keyed by (imdb_id, season_number) and holds PTT-pre-parsed
    results from a season-level Zilean query (no episode filter).

    Inclusion rule:
      - Season packs: PTT found no specific episodes → included for every episode
      - Episode-specific: PTT found an episodes list → include only if `episode` is in it

    Returns None if key is not in cache (caller falls back to individual REST call).
    Returns the full season list if PTT filtering yields nothing (conservative fallback).
    """
    if season is None or episode is None:
        return None
    cached = cache.get((imdb_id, season))
    if cached is None:
        return None  # Not cached – worker makes its own API call
    if not cached:
        return []
    filtered = []
    for r in cached:
        pi = r.get('parsed_info') or {}
        eps = pi.get('episodes', []) or []
        if not eps or episode in eps:
            filtered.append(r)
    return filtered if filtered else cached  # Fallback to full season if over-filtered


def _db_query_imdb(conn, imdb_id: str, season: Optional[int] = None,
                   episode: Optional[int] = None, multi: bool = False,
                   after_date=None) -> List[Dict]:
    """Single-item fallback query (used only when pre-fetch is unavailable)."""
    date_clause = ' AND "IngestedAt" >= %s' if after_date else ''
    try:
        with conn.cursor() as cur:
            if season is None:
                cur.execute(
                    f'SELECT "RawTitle", "InfoHash", "Size", "Year", "IngestedAt" FROM "Torrents" WHERE "ImdbId" = %s{date_clause} LIMIT 200',
                    (imdb_id, after_date) if after_date else (imdb_id,)
                )
            elif multi:
                cur.execute(
                    f'SELECT "RawTitle", "InfoHash", "Size", "Year", "IngestedAt" FROM "Torrents"'
                    f' WHERE "ImdbId" = %s AND %s = ANY("Seasons"){date_clause} LIMIT 200',
                    (imdb_id, season, after_date) if after_date else (imdb_id, season)
                )
            else:
                cur.execute(
                    f'SELECT "RawTitle", "InfoHash", "Size", "Year", "IngestedAt" FROM "Torrents"'
                    f' WHERE "ImdbId" = %s AND %s = ANY("Seasons") AND %s = ANY("Episodes"){date_clause} LIMIT 200',
                    (imdb_id, season, episode, after_date) if after_date else (imdb_id, season, episode)
                )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"[UPGRADE_HUB] DB query error for {imdb_id}: {e}")
        return []
    return _rows_to_results(rows)


# ---------------------------------------------------------------------------
# REST API query mode (fallback)
# ---------------------------------------------------------------------------

def _parse_genres(genres_str: str) -> set:
    """Parse genres from a JSON array or comma-separated string. Returns lowercase set."""
    if not genres_str:
        return set()
    import json as _json
    try:
        parsed = _json.loads(genres_str)
        if isinstance(parsed, list):
            return {g.strip().lower() for g in parsed if isinstance(g, str) and g.strip()}
    except (ValueError, TypeError):
        pass
    return {g.strip().lower() for g in genres_str.split(',') if g.strip()}


def _filter_by_date(results: List[Dict], after_date) -> List[Dict]:
    """Remove results with ingested_at older than after_date. Returns original list if after_date is None."""
    if not after_date or not results:
        return results
    from datetime import timezone
    cutoff = after_date if after_date.tzinfo else after_date.replace(tzinfo=timezone.utc)
    filtered = []
    for r in results:
        iat_str = r.get('ingested_at', '')
        if not iat_str:
            continue  # exclude items with no ingestion date when filter is active
        try:
            iat = datetime.fromisoformat(iat_str.replace('Z', '+00:00'))
            if iat.tzinfo is None:
                iat = iat.replace(tzinfo=timezone.utc)
            if iat >= cutoff:
                filtered.append(r)
        except (ValueError, TypeError):
            continue
    return filtered


def _api_query(zilean_instance: str, zilean_settings: Dict,
               imdb_id: str, title: str, year: Optional[int],
               content_type: str, season: Optional[int] = None,
               episode: Optional[int] = None, multi: bool = False) -> List[Dict]:
    from scraper.zilean import scrape_zilean_instance
    try:
        return scrape_zilean_instance(
            zilean_instance, zilean_settings,
            imdb_id=imdb_id, title=title, year=year,
            content_type=content_type,
            season=season, episode=episode, multi=multi,
        )
    except Exception as e:
        raise RuntimeError(f"Zilean REST error: {e}") from e


# ---------------------------------------------------------------------------
# Per-item workers
# ---------------------------------------------------------------------------

def _scan_movie(item: Dict, zilean_instance: str, zilean_settings: Dict,
                threshold: float, default_version: str,
                db_conn=None, prefetched: Optional[List[Dict]] = None,
                after_date=None, not_wanted_hashes: Optional[frozenset] = None) -> Optional[Dict]:
    version = (item.get('version') or default_version).rstrip('*').strip()
    vs = _get_version_settings(version)
    phalanx_only = False
    if prefetched is not None:
        results = _filter_by_date(prefetched, after_date)
        if not results and after_date and prefetched:
            results = prefetched
            phalanx_only = True
    elif db_conn:
        results = _db_query_imdb(db_conn, item['imdb_id'], after_date=after_date)
        if not results and after_date:
            results = _db_query_imdb(db_conn, item['imdb_id'], after_date=None)
            phalanx_only = bool(results)
    else:
        _all_api = _api_query(zilean_instance, zilean_settings,
                              item['imdb_id'], item['title'], item.get('year'), 'movie')
        # Filter REST results to movies only (Zilean may return tvSeries results for shared IMDB IDs)
        _all_api = [r for r in _all_api
                    if (r.get('parsed_info') or {}).get('category', 'movies').lower() in ('movies', 'movie', '')]
        results = _filter_by_date(_all_api, after_date)
        if not results and after_date and _all_api:
            results = _all_api
            phalanx_only = True
    if not results:
        return None
    current_filename = _item_filename(item)
    if not current_filename:
        return None  # no baseline to score against — skip to avoid false upgrades
    # Score current file and all candidates in the same pool with the same version settings.
    current_score, candidate_scored = _score_results_with_current(
        current_filename, results,
        item['title'], item.get('year'), None, None, 'movie', False, vs,
        size_bytes=_item_size_gb(item),
        genres_raw=item.get('genres') or '',
    )
    if not candidate_scored:
        return None
    # Filter www-spam and not-wanted
    candidate_scored = [(s, r) for s, r in candidate_scored
                        if not (r.get('title', '') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        candidate_scored = [(s, r) for s, r in candidate_scored
                            if _gh(r.get('magnet', '')) not in not_wanted_hashes
                            and _nt(r.get('title', '')) not in not_wanted_hashes]
    if not candidate_scored:
        return None
    # Skip candidates smaller than current file (size regression guard)
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
        'current_file': _item_filename(item),
        'current_protocol': 'nzb' if str(item.get('filled_by_torrent_id') or '').startswith('nzb:') else 'torrent',
        'current_size_gb': current_size_gb,
        'new_title': best_result.get('title', ''), 'new_size_gb': best_result.get('size', 0),
        'new_magnet': best_result.get('magnet', ''),
        '_alts': qualifying,
        '_phalanx_only': phalanx_only,
    }


def _scan_episode(item: Dict, zilean_instance: str, zilean_settings: Dict,
                  threshold: float, default_version: str,
                  db_conn=None, prefetched: Optional[List[Dict]] = None,
                  after_date=None, not_wanted_hashes: Optional[frozenset] = None) -> Optional[Dict]:
    version = (item.get('version') or default_version).rstrip('*').strip()
    vs = _get_version_settings(version)
    season, episode = item.get('season_number'), item.get('episode_number')
    phalanx_only = False
    if prefetched is not None:
        results = _filter_by_date(prefetched, after_date)
        if not results and after_date and prefetched:
            results = prefetched
            phalanx_only = True
    elif db_conn:
        results = _db_query_imdb(db_conn, item['imdb_id'], season, episode, multi=False, after_date=after_date)
        if not results and after_date:
            results = _db_query_imdb(db_conn, item['imdb_id'], season, episode, multi=False, after_date=None)
            phalanx_only = bool(results)
    else:
        _all_api = _api_query(zilean_instance, zilean_settings,
                              item['imdb_id'], item['title'], item.get('year'),
                              'episode', season, episode, multi=False)
        results = _filter_by_date(_all_api, after_date)
        if not results and after_date and _all_api:
            results = _all_api
            phalanx_only = True
    if not results:
        return None
    current_filename = _item_filename(item)
    if not current_filename:
        return None  # no baseline to score against — skip to avoid false upgrades
    # Score current file and all candidates in the same pool with the same version settings.
    current_score, candidate_scored = _score_results_with_current(
        current_filename, results,
        item['title'], item.get('year'), season, episode, 'episode', False, vs,
        size_bytes=_item_size_gb(item),
        genres_raw=item.get('genres') or '',
    )
    if not candidate_scored:
        return None
    # Remove season pack results — _ensure_parsed_info has already run inside
    # _score_results_with_current, so parsed_info is populated. Season packs have
    # no specific episodes list; episode files always have at least one episode number.
    candidate_scored = [
        (s, r) for s, r in candidate_scored
        if (r.get('parsed_info') or {}).get('episodes')
        and episode in (r.get('parsed_info') or {}).get('episodes', [])
    ]
    if not candidate_scored:
        return None
    # Filter www-spam and not-wanted
    candidate_scored = [(s, r) for s, r in candidate_scored
                        if not (r.get('title', '') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        candidate_scored = [(s, r) for s, r in candidate_scored
                            if _gh(r.get('magnet', '')) not in not_wanted_hashes
                            and _nt(r.get('title', '')) not in not_wanted_hashes]
    if not candidate_scored:
        return None
    # Skip candidates smaller than current file (size regression guard)
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
        'current_file': _item_filename(item),
        'current_protocol': 'nzb' if str(item.get('filled_by_torrent_id') or '').startswith('nzb:') else 'torrent',
        'current_size_gb': current_size_gb,
        'new_title': best_result.get('title', ''), 'new_size_gb': best_result.get('size', 0),
        'new_magnet': best_result.get('magnet', ''),
        '_alts': qualifying,
        '_phalanx_only': phalanx_only,
    }


def _scan_season_pack(imdb_id: str, season_num: int, season_eps: List[Dict],
                      zilean_instance: str, zilean_settings: Dict,
                      pack_threshold: float, default_version: str,
                      db_conn=None, prefetched: Optional[List[Dict]] = None,
                      after_date=None, not_wanted_hashes: Optional[frozenset] = None) -> Optional[Dict]:
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
    version = (sample.get('version') or default_version).rstrip('*').strip()
    vs = _get_version_settings(version)
    phalanx_only = False
    if prefetched is not None:
        pack_results = _filter_by_date(prefetched, after_date)
        if not pack_results and after_date and prefetched:
            pack_results = prefetched
            phalanx_only = True
    elif db_conn:
        pack_results = _db_query_imdb(db_conn, imdb_id, season_num, multi=True, after_date=after_date)
        if not pack_results and after_date:
            pack_results = _db_query_imdb(db_conn, imdb_id, season_num, multi=True, after_date=None)
            phalanx_only = bool(pack_results)
    else:
        _all_api = _api_query(zilean_instance, zilean_settings,
                              imdb_id, sample['title'], sample.get('year'),
                              'episode', season_num, multi=True)
        pack_results = _filter_by_date(_all_api, after_date)
        if not pack_results and after_date and _all_api:
            pack_results = _all_api
            phalanx_only = True
    if not pack_results:
        return None
    # Score sample episode's current file alongside pack candidates in the same pool,
    # so current_score is computed consistently (not read from stale DB column).
    current_score, scored_candidates = _score_results_with_current(
        _item_filename(sample), pack_results,
        sample['title'], sample.get('year'),
        season_num, None, 'episode', True, vs,
        size_bytes=_item_size_gb(sample),
        genres_raw=sample.get('genres') or '',
    )
    if not scored_candidates:
        return None
    scored_candidates = [(s, r) for s, r in scored_candidates
                         if _is_single_season_pack(r.get('title', ''), season_num)]
    if not scored_candidates:
        return None
    # Filter www-spam and not-wanted
    scored_candidates = [(s, r) for s, r in scored_candidates
                         if not (r.get('title', '') or '').lower().startswith('www')]
    if not_wanted_hashes:
        from database.not_wanted_magnets import get_base_filename as _gh, normalize_title as _nt
        scored_candidates = [(s, r) for s, r in scored_candidates
                             if _gh(r.get('magnet', '')) not in not_wanted_hashes
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
        'new_magnet': best_result.get('magnet', ''),
        '_alts': scored_candidates[:3],
        '_phalanx_only': phalanx_only,
        'current_files': sorted([
            {'ep': ep.get('episode_number') or ep.get('episode') or 0,
             'file': _item_filename(ep)}
            for ep in season_eps
        ], key=lambda x: x['ep']),
    }


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_for_upgrades(max_workers: int = 20, scan_limit: Optional[int] = None,
                      triggered_by: str = 'manual') -> Dict[str, Any]:
    """
    Scan Collected items against Zilean for better releases.

    Performance design:
      DB mode  — Pre-fetch ALL data upfront in bulk queries (1 query for all movies,
                 1 per show for episodes), then close the DB connection.  Workers do
                 only CPU-bound scoring — no DB access, true parallelism with 20 threads.
      REST mode — Pre-fetch phase: 1 REST call per unique (show, season) group,
                 cached and PTT-pre-parsed. Main phase: movies (REST, 20 workers)
                 + episodes/packs (CPU-only scoring against cache, essentially free).
                 ~82% fewer REST calls vs. per-item queries.
                 REST responses are persisted to SQLite and reused across restarts.
                 Smart invalidation via Zilean's ImportMetadata table.

    Args:
      scan_limit: If set, caps total Collected items processed (applied before type split).
    """
    global _scan_in_progress, _last_scan_results, _last_scan_time, _scan_progress, _vs_cache

    # Reset version settings cache at the start of every scan so any config
    # changes since the last run are always picked up.
    _vs_cache = {}

    _ensure_cache_tables()

    with _state_lock:
        if _scan_in_progress:
            return {'error': 'Scan already in progress'}
        _scan_in_progress = True
        _scan_progress = {'phase': 'starting', 'done': 0, 'total': 0, 'errors': 0}

    # Suppress verbose per-result INFO logs from rank_results during the scan
    # (each call logs once per candidate; with 38K items this generates millions of writes)
    _rr_logger = logging.getLogger('rank_results')
    _rr_old_level = _rr_logger.level
    _rr_logger.setLevel(logging.WARNING)

    used_db = False
    try:
        upgrade_source = get_setting('Upgrade Hub', 'upgrade_source', 'both') or 'both'
        use_zilean = upgrade_source in ('both', 'zilean_only')
        use_nzb    = upgrade_source in ('both', 'nzb_only')
        logger.info(f'[UPGRADE_HUB] Upgrade source: {upgrade_source} (zilean={use_zilean}, nzb={use_nzb})')

        zilean_info = get_zilean_config()
        if use_zilean and not zilean_info:
            if not use_nzb:
                return {'error': 'Zilean scraper not enabled or URL not configured'}
            # Zilean wanted but not configured — fall back to NZB only
            use_zilean = False
            logger.warning('[UPGRADE_HUB] Zilean not configured — falling back to NZB-only scan')

        zilean_instance = zilean_settings = zilean_url = db_cfg = None
        if zilean_info:
            zilean_instance, zilean_settings = zilean_info
            zilean_url = zilean_settings.get('url', '')
            db_cfg = _get_db_config(zilean_settings)

        threshold = float(get_setting('Scraping', 'upgrading_percentage_threshold', 0.1))
        pack_threshold = float(get_setting('Debug', 'zilean_pack_threshold', 0.5))
        default_version = get_default_version()

        # Compute after_date if show_recent_only is enabled
        after_date = None
        if get_setting('Upgrade Hub', 'show_recent_only', False) in (True, 'true', 'True', 1):
            recent_days = int(get_setting('Upgrade Hub', 'recent_threshold_days', 90) or 90)
            from datetime import timezone, timedelta
            after_date = datetime.now(timezone.utc) - timedelta(days=recent_days)
            logger.info(f"[UPGRADE_HUB] show_recent_only active: filtering results after {after_date.date()}")

        columns = ['id', 'type', 'imdb_id', 'title', 'year',
                   'season_number', 'episode_number', 'version',
                   'current_score', 'filled_by_file', 'location_on_disk', 'location_basename',
                   'state', 'genres', 'size', 'filled_by_torrent_id']
        all_collected = [
            dict(row) for row in get_all_media_items(state='Collected', columns=columns)
            if row['imdb_id']
        ]

        # ── Genre exclusion filter ────────────────────────────────────────────
        excluded_genres_raw = get_setting('Upgrade Hub', 'excluded_genres', '')
        excluded_genres = (
            {g.strip().lower() for g in excluded_genres_raw.split(',') if g.strip()}
            if excluded_genres_raw else set()
        )
        if excluded_genres:
            before = len(all_collected)
            all_collected = [
                item for item in all_collected
                if not (_parse_genres(item.get('genres') or '') & excluded_genres)
            ]
            skipped = before - len(all_collected)
            if skipped:
                logger.info(f"[UPGRADE_HUB] Genre filter: skipped {skipped} items "
                            f"(excluded: {', '.join(sorted(excluded_genres))})")

        # ── NAS exclusion filter ──────────────────────────────────────────────
        if get_setting('Upgrade Hub', 'exclude_nas_items', False) in (True, 'true', 'True', 1):
            from utilities.settings import get_nas_paths, is_nas_path
            nas_paths = get_nas_paths()
            if nas_paths:
                before = len(all_collected)
                all_collected = [
                    item for item in all_collected
                    if not is_nas_path(item.get('location_on_disk') or '', nas_paths)
                ]
                skipped = before - len(all_collected)
                if skipped:
                    logger.info(f"[UPGRADE_HUB] NAS exclusion: skipped {skipped} items "
                                f"(matched configured NAS path prefixes)")

        # Apply scan limit before splitting by type
        if scan_limit and scan_limit > 0 and len(all_collected) > scan_limit:
            logger.info(f"[UPGRADE_HUB] Scan limit {scan_limit}: scanning {scan_limit}/{len(all_collected)} items")
            all_collected = all_collected[:scan_limit]

        movies_raw = [i for i in all_collected if i['type'] == 'movie']
        episodes   = [i for i in all_collected if i['type'] == 'episode']

        # Deduplicate movies by (imdb_id, version): keep only the largest file per
        # group. Torrents often contain extras (interviews, samples, featurettes)
        # that end up as separate Collected rows with the same imdb_id. Scanning
        # them individually produces spurious high-% upgrades (a 0.09 GB Sample.mkv
        # looks like a +300% improvement vs. any real release).
        _movie_best: dict = {}
        for _m in movies_raw:
            _key = (_m['imdb_id'], (_m.get('version') or default_version).rstrip('*').strip())
            _existing = _movie_best.get(_key)
            if _existing is None or _item_size_gb(_m) > _item_size_gb(_existing):
                _movie_best[_key] = _m
        movies = list(_movie_best.values())
        _dedup_removed = len(movies_raw) - len(movies)
        if _dedup_removed:
            logger.info(f"[UPGRADE_HUB] Deduped {_dedup_removed} extra/duplicate movie files "
                        f"(kept largest per imdb_id+version, {len(movies)} unique movies remain)")

        # Pre-populate version settings cache for every unique version in this
        # scan batch.  Workers only read the dict — no locking needed.
        # Typically 1–6 unique versions so this is a handful of config reads total.
        unique_versions = {
            (item.get('version') or default_version).rstrip('*').strip()
            for item in all_collected
        }
        for _v in unique_versions:
            _get_version_settings(_v)
        logger.info(f"[UPGRADE_HUB] Version settings cached for: {sorted(unique_versions)}")

        # Build season groups now — needed for pack workers and pre-fetch
        season_groups: Dict = defaultdict(list)
        for ep in episodes:
            if ep.get('season_number'):
                season_groups[(ep['imdb_id'], ep['season_number'])].append(ep)
        unique_seasons = list(season_groups.items())

        # ── DB pre-fetch phase (single connection, bulk queries) ──────────────
        if not use_zilean:
            logger.info('[UPGRADE_HUB] Zilean disabled by upgrade_source setting — skipping Zilean phases')
        # All Zilean data is loaded before workers start. Workers receive
        # pre-built lists and do zero DB access → fully parallel scoring.
        movie_prefetch: Optional[Dict[str, List[Dict]]] = None   # imdb_id → results
        show_prefetch:  Optional[Dict[str, List[tuple]]] = None  # imdb_id → raw rows

        if use_zilean and db_cfg:
            db_conn = None
            try:
                import psycopg2
                db_conn = psycopg2.connect(**db_cfg, connect_timeout=10)
                db_conn.set_session(readonly=True, autocommit=True)
                with db_conn.cursor() as _cur:
                    _cur.execute("SET statement_timeout = '30000'")  # 30s for bulk queries
                logger.info(f"[UPGRADE_HUB] Connected to Zilean DB at {db_cfg['host']}:{db_cfg['port']}")

                _scan_progress.update({'phase': 'prefetch', 'done': 0, 'total': 0})

                # 1 query for ALL movies
                if movies:
                    movie_imdb_ids = list({m['imdb_id'] for m in movies})
                    movie_prefetch = _db_batch_fetch_movies(db_conn, movie_imdb_ids, after_date=after_date)
                    logger.info(f"[UPGRADE_HUB] Pre-fetched {sum(len(v) for v in movie_prefetch.values())} movie torrents for {len(movie_imdb_ids)} IMDB IDs")

                # 1 query per unique show (N_shows, not N_episodes)
                if episodes:
                    unique_show_ids = list({ep['imdb_id'] for ep in episodes})
                    show_prefetch = {}
                    for show_id in unique_show_ids:
                        show_prefetch[show_id] = _db_fetch_show_torrents(db_conn, show_id, after_date=after_date)
                    total_show_rows = sum(len(v) for v in show_prefetch.values())
                    logger.info(f"[UPGRADE_HUB] Pre-fetched {total_show_rows} episode torrents for {len(unique_show_ids)} shows")

                used_db = True
            except Exception as e:
                logger.warning(f"[UPGRADE_HUB] DB pre-fetch failed, falling back to REST API: {e}")
                movie_prefetch = None
                show_prefetch  = None
                used_db = False
            finally:
                if db_conn:
                    try:
                        db_conn.close()
                    except Exception:
                        pass

        # ── REST season pre-fetch (REST mode only) ────────────────────────────
        # One REST call per unique (show, season) instead of one per episode.
        # Reduces episode+pack REST calls: ~31K+2.6K → 2.6K (saves ~82%).
        # Results are PTT-pre-parsed here so episode workers skip that step.
        # Persistent cache: responses saved to SQLite, reused across restarts.
        # Smart invalidation: only re-fetch seasons where Zilean has new content.
        rest_season_cache: Dict[Tuple, List[Dict]] = {}
        if use_zilean and not used_db and episodes:
            # Load persisted REST cache from SQLite
            sqlite_rest_cache = _load_rest_cache_from_db(zilean_url)
            rest_season_cache = dict(sqlite_rest_cache)

            unique_season_key_set = {k for k, _ in unique_seasons}

            # Determine which keys need a fresh REST call
            keys_needing_fetch: set = set()
            if db_cfg and _last_scan_time:
                last_import = _get_zilean_last_import_time(db_cfg)
                if last_import is not None:
                    from datetime import timezone
                    scan_time = _last_scan_time
                    if scan_time.tzinfo is None:
                        scan_time = scan_time.replace(tzinfo=timezone.utc)
                    if last_import <= scan_time:
                        # Zilean unchanged since last scan — serve everything from cache
                        logger.info(
                            f"[UPGRADE_HUB] Zilean unchanged since last scan "
                            f"(import={last_import.isoformat()[:19]}) — using full REST cache "
                            f"({len(sqlite_rest_cache)} entries)"
                        )
                        keys_needing_fetch = unique_season_key_set - set(sqlite_rest_cache)
                    else:
                        # New content ingested — find changed seasons
                        changed = _get_changed_season_keys_since(db_cfg, scan_time)
                        if changed is not None:
                            changed_set = set(changed)
                            # Invalidate changed keys from persistent cache
                            _invalidate_rest_cache_keys(list(changed_set), zilean_url)
                            for k in changed_set:
                                rest_season_cache.pop(k, None)
                            keys_needing_fetch = (
                                (unique_season_key_set & changed_set)        # changed
                                | (unique_season_key_set - set(sqlite_rest_cache))  # new/uncached
                            )
                            logger.info(
                                f"[UPGRADE_HUB] Zilean has new content since last scan "
                                f"({len(changed)} changed seasons) — "
                                f"{len(keys_needing_fetch)} REST calls needed"
                            )
                        else:
                            # Fallback: can't determine changes, fetch uncached only
                            keys_needing_fetch = unique_season_key_set - set(sqlite_rest_cache)
                            logger.info(f"[UPGRADE_HUB] Could not determine Zilean changes — fetching {len(keys_needing_fetch)} uncached seasons")
                else:
                    keys_needing_fetch = unique_season_key_set - set(sqlite_rest_cache)
            else:
                # No DB config or first scan — fetch everything not in cache
                keys_needing_fetch = unique_season_key_set - set(sqlite_rest_cache)

            seasons_to_fetch = [(k, eps) for k, eps in unique_seasons if k in keys_needing_fetch]
            cache_hits = len(unique_season_key_set) - len(seasons_to_fetch)
            logger.info(
                f"[UPGRADE_HUB] REST pre-fetch: {len(seasons_to_fetch)} seasons to fetch, "
                f"{cache_hits} served from cache (total {len(unique_seasons)})"
            )

            if seasons_to_fetch:
                _scan_progress.update({'phase': 'prefetch', 'done': 0, 'total': len(seasons_to_fetch)})

                def _fetch_season_rest(imdb_id: str, season_num: int, sample_ep: Dict):
                    results = _api_query(
                        zilean_instance, zilean_settings,
                        imdb_id, sample_ep['title'], sample_ep.get('year'),
                        'episode', season=season_num, episode=None, multi=False,
                    )
                    _ensure_parsed_info(results)  # Pre-parse once; reused by all episode workers
                    return (imdb_id, season_num), results

                new_entries: List[Tuple[str, int, List]] = []
                with ThreadPoolExecutor(max_workers=max_workers) as prefetch_pool:
                    season_futs = {
                        prefetch_pool.submit(_fetch_season_rest, imdb_id, sn, eps[0]): (imdb_id, sn)
                        for (imdb_id, sn), eps in seasons_to_fetch
                    }
                    for f in as_completed(season_futs):
                        try:
                            key, results = f.result()
                            rest_season_cache[key] = results
                            new_entries.append((key[0], key[1], results))
                        except Exception as exc:
                            logger.debug(f"[UPGRADE_HUB] Season pre-fetch error: {exc}")
                        with _state_lock:
                            _scan_progress['done'] = _scan_progress.get('done', 0) + 1

                # Persist new/updated REST results to SQLite
                if new_entries:
                    _save_rest_cache_batch_to_db(new_entries, zilean_url)

            logger.info(f"[UPGRADE_HUB] Season pre-fetch done: {len(rest_season_cache)} seasons in working cache")

        upgrade_candidates: List[Dict] = []
        pack_candidates: List[Dict] = []
        errors: List[str] = []

        # Load not-wanted hashes once before any workers start — passed as a
        # frozenset so all threads share a single read-only object (no locks needed).
        try:
            from database.not_wanted_magnets import get_not_wanted_magnets, get_base_filename as _get_hash
            _not_wanted_hashes: frozenset = frozenset(
                _get_hash(h) for h in get_not_wanted_magnets() if h
            )
        except Exception:
            _not_wanted_hashes = frozenset()

        # ── Phase A: Score episodes & packs (CPU-only via cache, very fast) ──
        # Separate from movies so their instant completion isn't mixed into
        # the REST-bound movie rate, which would corrupt the ETA.
        ep_pack_total = len(episodes) + len(unique_seasons)
        if use_zilean and ep_pack_total > 0:
            _scan_progress.update({'phase': 'scoring', 'done': 0, 'total': ep_pack_total})
            logger.info(f"[UPGRADE_HUB] Scoring {len(episodes)} episodes, "
                        f"{len(unique_seasons)} packs (CPU-only)…")
            cpu_workers = min(100, ep_pack_total)
            cpu_futures: Dict = {}
            with ThreadPoolExecutor(max_workers=cpu_workers) as cpu_pool:
                for ep in episodes:
                    season = ep.get('season_number')
                    episode_num = ep.get('episode_number')
                    if show_prefetch is not None:
                        raw_rows = show_prefetch.get(ep['imdb_id'], [])
                        pf = _filter_show_for_episode(raw_rows, season, episode_num) if (season and episode_num) else []
                    elif rest_season_cache:
                        pf = _filter_rest_cache_for_episode(rest_season_cache, ep['imdb_id'], season, episode_num)
                    else:
                        pf = None
                    f = cpu_pool.submit(_scan_episode, ep, zilean_instance, zilean_settings,
                                        threshold, default_version, None, pf, after_date,
                                        _not_wanted_hashes)
                    cpu_futures[f] = ('episode', ep)

                for (imdb_id, season_num), eps in unique_seasons:
                    if show_prefetch is not None:
                        raw_rows = show_prefetch.get(imdb_id, [])
                        pf = _filter_show_for_pack(raw_rows, season_num)
                    elif rest_season_cache:
                        pf = rest_season_cache.get((imdb_id, season_num))
                    else:
                        pf = None
                    f = cpu_pool.submit(_scan_season_pack, imdb_id, season_num, eps,
                                        zilean_instance, zilean_settings,
                                        pack_threshold, default_version, None, pf, after_date,
                                        _not_wanted_hashes)
                    cpu_futures[f] = ('pack', (imdb_id, season_num))

                for future in as_completed(cpu_futures):
                    ftype, item = cpu_futures[future]
                    try:
                        r = future.result()
                        if r:
                            if ftype == 'pack':
                                pack_candidates.append(r)
                            else:
                                upgrade_candidates.append(r)
                    except Exception as e:
                        if ftype == 'episode':
                            errors.append(f"Ep '{item.get('title')}' S{item.get('season_number')}E{item.get('episode_number')}: {e}")
                        else:
                            errors.append(f"Pack {item}: {e}")
                    with _state_lock:
                        _scan_progress['done'] = _scan_progress.get('done', 0) + 1
                        _scan_progress['errors'] = len(errors)

        # ── Phase B: Scan movies (REST calls, I/O-bound) ──────────────────────
        # Separate phase so the ETA reflects only movie REST call speed.
        if use_zilean and movies:
            _scan_progress.update({'phase': 'scanning', 'done': 0, 'total': len(movies)})
            logger.info(f"[UPGRADE_HUB] Scanning {len(movies)} movies "
                        f"(DB={'yes' if used_db else 'no'}, workers={max_workers})…")
            movie_futures: Dict = {}
            with ThreadPoolExecutor(max_workers=max_workers) as rest_pool:
                for m in movies:
                    pf = movie_prefetch.get(m['imdb_id'], []) if movie_prefetch is not None else None
                    f = rest_pool.submit(_scan_movie, m, zilean_instance, zilean_settings,
                                         threshold, default_version, None, pf, after_date,
                                         _not_wanted_hashes)
                    movie_futures[f] = m
                for future in as_completed(movie_futures):
                    item = movie_futures[future]
                    try:
                        r = future.result()
                        if r:
                            upgrade_candidates.append(r)
                    except Exception as e:
                        errors.append(f"Movie '{item.get('title')}': {e}")
                    with _state_lock:
                        _scan_progress['done'] = _scan_progress.get('done', 0) + 1
                        _scan_progress['errors'] = len(errors)

        # ── Phase C: NZB indexer scan (all collected items vs latest NZBs) ──────
        # Fetches latest NZBs from enabled Newznab indexers in browse mode,
        # scores them with the same pipeline, and merges results.
        # Runs for ALL collected items (both debrid and NZB items) so cross-
        # protocol upgrades are possible (debrid→NZB and NZB→debrid via Zilean).
        try:
            from database.nzb_upgrade import scan_nzb_upgrades, get_newznab_scrapers
            if use_nzb and get_newznab_scrapers():
                logger.info('[UPGRADE_HUB] Starting NZB indexer scan phase…')
                _scan_progress.update({'phase': 'nzb_scan', 'done': 0, 'total': len(movies) + len(episodes) + len(season_groups)})

                def _nzb_progress(done, total):
                    with _state_lock:
                        _scan_progress['done'] = done

                nzb_upgrades, nzb_packs = scan_nzb_upgrades(
                    movies=movies,
                    episodes=episodes,
                    season_groups=dict(season_groups),
                    threshold=threshold,
                    pack_threshold=pack_threshold,
                    default_version=default_version,
                    not_wanted_hashes=_not_wanted_hashes,
                    max_workers=max_workers,
                    progress_callback=_nzb_progress,
                )
                # Merge: prefer whichever source gives the higher improvement_pct
                # for the same item_id. New items with no Zilean result are added directly.
                zilean_by_item = {c['item_id']: c for c in upgrade_candidates if 'item_id' in c}
                for c in nzb_upgrades:
                    iid = c.get('item_id')
                    existing = zilean_by_item.get(iid)
                    if existing is None:
                        upgrade_candidates.append(c)
                    elif c.get('improvement_pct', 0) > existing.get('improvement_pct', 0):
                        upgrade_candidates.remove(existing)
                        upgrade_candidates.append(c)

                zilean_packs_by_key = {(p['imdb_id'], p['season']): p for p in pack_candidates}
                for p in nzb_packs:
                    key = (p.get('imdb_id'), p.get('season'))
                    existing = zilean_packs_by_key.get(key)
                    if existing is None:
                        pack_candidates.append(p)
                    elif p.get('improvement_pct', 0) > existing.get('improvement_pct', 0):
                        pack_candidates.remove(existing)
                        pack_candidates.append(p)

                logger.info(f'[UPGRADE_HUB] After NZB merge: {len(upgrade_candidates)} upgrades, '
                            f'{len(pack_candidates)} packs')
        except Exception as _nzb_e:
            logger.warning(f'[UPGRADE_HUB] NZB scan phase failed: {_nzb_e}', exc_info=True)

        # Tag protocol on all candidates so the UI can show NZB/Debrid badges
        for c in upgrade_candidates + pack_candidates:
            if 'protocol' not in c:
                c['protocol'] = 'torrent'

        upgrade_candidates.sort(key=lambda x: -x.get('improvement_pct', 0))
        pack_candidates.sort(key=lambda x: -x.get('ratio_pct', 0))

        # ── Hide episodes already covered by a Season Pack candidate ─────────
        if get_setting('Upgrade Hub', 'hide_pack_episodes', False):
            pack_item_ids = set()
            for pack in pack_candidates:
                pack_item_ids.update(pack.get('item_ids', []))
            before = len(upgrade_candidates)
            upgrade_candidates = [c for c in upgrade_candidates if c['item_id'] not in pack_item_ids]
            logger.debug(f"[UPGRADE_HUB] hide_pack_episodes: removed {before - len(upgrade_candidates)} "
                         f"episodes already present in pack candidates")

        # ── Recency-aware selection ──────────────────────────────────────────
        # _not_wanted_hashes and _get_hash already loaded above before Phase A

        # Load filename filter-out terms once (same setting as adding_queue uses)
        try:
            _raw_filter = get_setting('Debug', 'filename_filter_out_list', '')
            _filename_filters = [f.strip().lower() for f in _raw_filter.split(',') if f.strip()] if _raw_filter else []
        except Exception:
            _filename_filters = []

        all_candidates = upgrade_candidates + pack_candidates
        recent_count = 0
        nw_filtered_alts = 0
        fn_filtered_alts = 0
        for c in all_candidates:
            alts = c.pop('_alts', [])
            # Filter out www-spam alts
            alts = [(score, r) for score, r in alts
                    if not (r.get('title', '') or '').lower().startswith('www')]
            # Filter out alts whose magnet or title is known-bad before selecting best
            if _not_wanted_hashes:
                before_alts = len(alts)
                from database.not_wanted_magnets import normalize_title as _norm_title
                alts = [(score, r) for score, r in alts
                        if _get_hash(r.get('magnet', '')) not in _not_wanted_hashes
                        and _norm_title(r.get('title', '')) not in _not_wanted_hashes]
                nw_filtered_alts += before_alts - len(alts)
            # Filter out alts whose title contains a filename filter-out term
            if _filename_filters:
                before_alts = len(alts)
                alts = [(score, r) for score, r in alts
                        if not any(term in r.get('title', '').lower() for term in _filename_filters)]
                fn_filtered_alts += before_alts - len(alts)
            chosen_score, chosen_result, is_recent = _pick_best_alt(alts)
            if chosen_result is None and alts:
                chosen_score, chosen_result = alts[0]
                is_recent = False
            if chosen_result:
                c['new_title']       = chosen_result.get('title', '')
                c['new_size_gb']     = chosen_result.get('size', 0)
                c['new_magnet']      = chosen_result.get('magnet', '')
                c['new_ingested_at'] = chosen_result.get('ingested_at', '')
                c['new_info_hash']   = chosen_result.get('info_hash', '')
                if 'current_score' in c:
                    c['new_score']       = round(chosen_score, 2)
                    c['improvement_pct'] = round(
                        (chosen_score - c['current_score']) / max(abs(c['current_score']), 1) * 100, 1
                    )
                elif 'best_score' in c:
                    c['best_score'] = round(chosen_score, 2)
            c['is_recent'] = is_recent
            if is_recent:
                recent_count += 1
            # Store all remaining (not-wanted-filtered) alt magnets so the frontend
            # can block the entire current set when the user clicks Ignore — not just
            # the best candidate. This prevents the next-best alt from reappearing.
            c['_alt_magnets'] = [r.get('magnet', '') for _, r in alts
                                  if r.get('magnet') and r.get('magnet') != c.get('new_magnet', '')]

        logger.info(f"[UPGRADE_HUB] Recency selection: {recent_count}/{len(all_candidates)} candidates recent"
                    + (f" | {nw_filtered_alts} alt(s) skipped (not-wanted)" if nw_filtered_alts else "")
                    + (f" | {fn_filtered_alts} alt(s) skipped (filename filter)" if fn_filtered_alts else ""))

        # Final pass: drop any candidate whose selected new_title still contains a filter term.
        # This catches cases where all alts were filtered (chosen_result=None) and the
        # pre-set new_title from the initial scan still holds a bad value.
        if _filename_filters:
            _before_u = len(upgrade_candidates)
            _before_p = len(pack_candidates)
            upgrade_candidates = [c for c in upgrade_candidates
                                  if not any(t in c.get('new_title', '').lower() for t in _filename_filters)]
            pack_candidates    = [c for c in pack_candidates
                                  if not any(t in c.get('new_title', '').lower() for t in _filename_filters)]
            _fn_dropped = (_before_u - len(upgrade_candidates)) + (_before_p - len(pack_candidates))
            if _fn_dropped:
                logger.info(f"[UPGRADE_HUB] Filename filter: dropped {_fn_dropped} candidate(s) with filtered term in new_title")

        # ── Phalanx RD cache check ────────────────────────────────────────────
        # Batch-query community cache data for all candidate info_hashes.
        # Old/established releases have high network coverage so hits are common.
        try:
            phalanx_enabled = get_setting('UI Settings', 'enable_phalanx_db', default=False)
            if phalanx_enabled and all_candidates:
                from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
                _phalanx = PhalanxDBClassManager()
                hashes = [c['new_info_hash'] for c in all_candidates if c.get('new_info_hash')]
                if hashes:
                    cache_map = _phalanx.get_multi_cache_status(hashes)
                    for c in all_candidates:
                        h = c.get('new_info_hash', '')
                        status = cache_map.get(h) if h else None
                        c['rd_cached'] = status['is_cached'] if status is not None else None
                    known_cached   = sum(1 for c in all_candidates if c.get('rd_cached') is True)
                    known_uncached = sum(1 for c in all_candidates if c.get('rd_cached') is False)
                    unknown        = sum(1 for c in all_candidates if c.get('rd_cached') is None)
                    logger.info(f"[UPGRADE_HUB] Phalanx RD cache: {known_cached} cached, "
                                f"{known_uncached} not cached, {unknown} unknown")
        except Exception as _pe:
            logger.debug(f"[UPGRADE_HUB] Phalanx cache check skipped: {_pe}")

        # ── Phalanx bypass filter ─────────────────────────────────────────────
        # Candidates flagged _phalanx_only had NO recent Zilean results; they
        # were scored against the full (older) pool.  We keep them only when
        # Phalanx confirms the chosen torrent is already cached on RD — without
        # that signal we cannot safely recommend old/untested releases.
        # If Phalanx is disabled entirely, _phalanx_only candidates are always
        # dropped (rd_cached is never populated → not True).
        if after_date:
            rescued = 0
            for c in all_candidates:
                if c.get('_phalanx_only') and c.get('rd_cached') is True:
                    c['rd_cached_bypass'] = True
                    rescued += 1
            before_u = len(upgrade_candidates)
            before_p = len(pack_candidates)
            upgrade_candidates = [c for c in upgrade_candidates
                                  if not c.get('_phalanx_only') or c.get('rd_cached') is True]
            pack_candidates    = [c for c in pack_candidates
                                  if not c.get('_phalanx_only') or c.get('rd_cached') is True]
            dropped = (before_u + before_p) - (len(upgrade_candidates) + len(pack_candidates))
            if rescued or dropped:
                logger.info(f"[UPGRADE_HUB] Phalanx bypass: {rescued} old candidates rescued "
                            f"(RD-cached), {dropped} dropped (not cached / Phalanx disabled)")
        # Clean up internal flag before storing results
        for c in upgrade_candidates + pack_candidates:
            c.pop('_phalanx_only', None)

        scanned_at = datetime.now().isoformat()
        result = {
            'upgrade_candidates': upgrade_candidates,
            'pack_candidates': pack_candidates,
            'scanned_movies': len(movies),
            'scanned_episodes': len(episodes),
            'used_db': used_db,
            'errors': errors,
            'scanned_at': scanned_at,
            'version_used': default_version,
        }
        _last_scan_results = result
        _last_scan_time = datetime.now()
        _scan_progress = {'phase': 'done', 'done': 0, 'total': 0, 'errors': len(errors)}

        # Persist results to SQLite so they survive restarts
        _save_scan_results_to_db(result, default_version, scanned_at)

        logger.info(f"[UPGRADE_HUB] Scan complete: {len(upgrade_candidates)} upgrades, "
                    f"{len(pack_candidates)} packs, {len(errors)} errors")

        # Log activity
        from database.upgrade_hub_activity import log_hub_activity
        log_hub_activity(
            'scan',
            triggered_by=triggered_by,
            result='success' if not errors else 'partial',
            title=(f"Scan: {len(upgrade_candidates)} upgrade{'s' if len(upgrade_candidates) != 1 else ''}, "
                   f"{len(pack_candidates)} pack{'s' if len(pack_candidates) != 1 else ''} "
                   f"({len(movies)} movies, {len(episodes)} episodes scanned)"),
            stats={
                'upgrades': len(upgrade_candidates),
                'packs': len(pack_candidates),
                'movies_scanned': len(movies),
                'episodes_scanned': len(episodes),
                'errors': len(errors),
                'scan_limit': scan_limit,
            },
        )

        # Release malloc arenas back to the OS — the scan creates/destroys hundreds of MB
        # of temporary objects; glibc holds those pages without this call.
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
            logger.debug("[UPGRADE_HUB] malloc_trim called after scan")
        except Exception:
            pass

        return result

    except Exception as e:
        logger.error(f"[UPGRADE_HUB] Scan failed: {e}", exc_info=True)
        _scan_progress = {'phase': 'error', 'done': 0, 'total': 0, 'errors': 1}
        try:
            from database.upgrade_hub_activity import log_hub_activity
            log_hub_activity('scan', triggered_by=triggered_by, result='failed',
                             title=f"Scan failed: {e}")
        except Exception:
            pass
        return {'error': str(e)}
    finally:
        _rr_logger.setLevel(_rr_old_level)
        with _state_lock:
            _scan_in_progress = False


def get_scan_status() -> Dict[str, Any]:
    _ensure_cache_initialized()
    with _state_lock:
        return {
            'in_progress': _scan_in_progress,
            'progress': dict(_scan_progress),
            'last_scan_at': _last_scan_time.isoformat() if _last_scan_time else None,
            'has_results': _last_scan_results is not None,
        }


def get_last_results() -> Optional[Dict[str, Any]]:
    _ensure_cache_initialized()
    return _last_scan_results


def queue_upgrade_candidates(item_ids: List[int], triggered_by: str = 'manual') -> Dict[str, Any]:
    """Move Collected items to Upgrading state, pre-seeding the known candidate
    magnet so the UpgradingQueue can skip re-scraping."""
    if not item_ids:
        return {'queued': 0, 'failed': 0, 'errors': []}

    # Build a lookup of pre-known candidates from the last scan
    known: Dict[int, Dict] = {}
    if _last_scan_results:
        for c in _last_scan_results.get('upgrade_candidates', []):
            if c.get('new_magnet'):
                known[c['item_id']] = c
        for p in _last_scan_results.get('pack_candidates', []):
            if p.get('new_magnet'):
                for iid in p.get('item_ids', []):
                    known[iid] = p

    conn = get_db_connection()
    queued = failed = 0
    errors = []
    try:
        for item_id in item_ids:
            try:
                cur = conn.execute(
                    "UPDATE media_items SET state='Upgrading', last_updated=? WHERE id=? AND state='Collected'",
                    (datetime.now(), item_id),
                )
                conn.commit()
                if cur.rowcount > 0:
                    queued += 1
                    if item_id in known:
                        candidate = dict(known[item_id])
                        candidate['triggered_by'] = triggered_by
                        _queued_magnets[item_id] = candidate
                        _save_queued_magnet_to_db(item_id, candidate)
                        logger.info(f"[UPGRADE_HUB] Pre-seeded magnet for item {item_id}: {known[item_id].get('new_title')}")
                else:
                    failed += 1
                    errors.append(f"Item {item_id}: not found or not Collected")
            except Exception as e:
                failed += 1
                errors.append(f"Item {item_id}: {e}")
    finally:
        conn.close()

    # Build human-readable title list from known candidates
    queued_titles = []
    for iid in item_ids:
        c = known.get(iid)
        if c:
            t = c.get('title', '')
            y = c.get('year')
            s = c.get('season')
            ep = c.get('episode')
            label = t
            if y:
                label += f' ({y})'
            if s is not None and ep is not None:
                label += f' S{str(s).zfill(2)}E{str(ep).zfill(2)}'
            elif s is not None:
                label += f' S{str(s).zfill(2)}'
            queued_titles.append(label)

    if queued_titles:
        names_str = ', '.join(queued_titles[:5])
        if len(queued_titles) > 5:
            names_str += f' +{len(queued_titles) - 5} more'
        log_title = f"Queued {queued} item{'s' if queued != 1 else ''}: {names_str}"
    else:
        log_title = f"Queued {queued} item{'s' if queued != 1 else ''} for upgrading"

    from database.upgrade_hub_activity import log_hub_activity
    log_hub_activity(
        'queue',
        triggered_by=triggered_by,
        result='success' if queued > 0 else 'failed',
        title=log_title,
        stats={'queued': queued, 'failed': failed, 'errors': errors[:10],
               'items': queued_titles},
    )

    return {'queued': queued, 'failed': failed, 'errors': errors}
