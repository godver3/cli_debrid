import copy
import hashlib
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Dict, Any, Optional
from routes.api_tracker import api
from utilities.settings import get_setting
from PTT import parse_title as ptt_parse
from rapidfuzz import fuzz as _fuzz
from scraper.request_dedup import SingleFlightGuard

def _get_retention_days() -> int:
    """Get configured usenet retention days. 0 = disabled."""
    try:
        return int(get_setting('Usenet Provider', 'retention_days', 1500))
    except (ValueError, TypeError):
        return 1500


def _is_within_retention_days(pub_date_str: str, retention_days: int) -> bool:
    """Return True if pub_date_str is within retention_days. True if no date or retention=0."""
    if not pub_date_str or not retention_days:
        return True
    try:
        pub_dt = parsedate_to_datetime(pub_date_str)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - pub_dt).days
        return age_days <= retention_days
    except Exception:
        return True


# Newznab category IDs
# Explicit subcategories are required — many indexers do not return subcategory
# items when querying only the parent (e.g. 2000 won't return 2030/HD results
# on all indexers). Include all common subcategories to maximise coverage.
_CAT_MOVIE = '2000,2030,2040,2045,2050,2060'  # Movie All + HD, SD, UHD, BluRay, Foreign
_CAT_TV    = '5000,5010,5020,5030,5040,5045,5060,5070'  # TV All + Foreign, SD, SD, HD, UHD, BluRay, Sport

_SANITIZE_RE = re.compile(r"[!?:&,;\"'()\[\]{}]")

# Query result cache — reduces API hits for duplicate searches within TTL window
_NZB_CACHE: Dict[str, tuple] = {}   # key -> (timestamp, results)
_NZB_CACHE_LOCK = threading.Lock()
_NZB_CACHE_TTL = 600  # 10 minutes

# Coalesces concurrent identical searches (e.g. the same ID-based query fired
# by two title-variant threads within milliseconds of each other) so only one
# of them actually hits the indexer — see scraper/request_dedup.py.
_NZB_INFLIGHT = SingleFlightGuard()


def _cache_key(endpoint: str, params: dict) -> str:
    params_no_key = {k: v for k, v in params.items() if k != 'apikey'}
    stable = f"{endpoint}|{sorted(params_no_key.items())}"
    return hashlib.sha256(stable.encode()).hexdigest()


def _cache_get(key: str) -> Optional[List]:
    with _NZB_CACHE_LOCK:
        entry = _NZB_CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _NZB_CACHE_TTL:
            return entry[1]
        if entry:
            del _NZB_CACHE[key]
    return None


def _cache_set(key: str, results: List) -> None:
    with _NZB_CACHE_LOCK:
        _NZB_CACHE[key] = (time.monotonic(), results)


def _build_params_list(
    api_key: str,
    clean_imdb: str,
    title: str,
    year: int,
    content_type: str,
    season: Optional[int],
    episode: Optional[int],
    multi: bool,
) -> List[Dict[str, Any]]:
    """
    Always build BOTH an ID-based query AND a title-based query.
    Both run simultaneously — results are merged and deduped by GUID.
    """
    params_list = []

    if content_type.lower() == 'movie':
        cats = _CAT_MOVIE
        # 1. ID-based movie search
        if clean_imdb:
            params_list.append({
                'apikey': api_key, 't': 'movie',
                'imdbid': clean_imdb, 'cat': cats, 'limit': 100,
            })
        # 2. Title+year text search
        raw = _SANITIZE_RE.sub('', f'{title} {year or ""}'.strip()).strip()
        params_list.append({'apikey': api_key, 't': 'search', 'q': raw, 'cat': cats, 'limit': 100})

    elif content_type.lower() == 'episode':
        cats = _CAT_TV
        # 1. ID-based tvsearch
        if clean_imdb and season is not None:
            id_params: Dict[str, Any] = {
                'apikey': api_key, 't': 'tvsearch',
                'imdbid': clean_imdb, 'season': season,
                'cat': cats, 'limit': 100,
            }
            if episode is not None and not multi:
                id_params['ep'] = episode
            params_list.append(id_params)
        # 2. Title text search
        query_parts = [title]
        if season is not None:
            if episode is not None and not multi:
                query_parts.append(f'S{season:02d}E{episode:02d}')
            else:
                query_parts.append(f'S{season:02d}')
        raw = _SANITIZE_RE.sub('', ' '.join(query_parts)).strip()
        params_list.append({'apikey': api_key, 't': 'search', 'q': raw, 'cat': cats, 'limit': 100})

    else:
        cats = f'{_CAT_MOVIE},{_CAT_TV}'
        raw = _SANITIZE_RE.sub('', title).strip()
        params_list.append({'apikey': api_key, 't': 'search', 'q': raw, 'cat': cats, 'limit': 100})

    return params_list


def scrape_newznab_instance(
    instance: str,
    settings: Dict[str, Any],
    imdb_id: Optional[str],
    title: str,
    year: int,
    content_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    multi: bool = False,
    tmdb_id: Optional[str] = None,
    **kwargs
) -> List[Dict[str, Any]]:
    logging.info(f"Scraping Newznab instance: {instance} for '{title}' ({year})")

    url = settings.get('url', '').rstrip('/')
    api_key = settings.get('api_key', '').strip()

    if not url:
        logging.error(f"Newznab instance '{instance}' is missing URL.")
        return []

    clean_imdb = imdb_id.replace('tt', '') if imdb_id else ''
    endpoint = f'{url}/api'
    # scraper_timeout's own setting description says "0 to disable" — requests
    # treats timeout=None (not 0) as no timeout, so 0/falsy must map to None.
    timeout = get_setting('Scraping', 'scraper_timeout', 30) or None

    params_list = _build_params_list(
        api_key, clean_imdb, title, year, content_type, season, episode, multi
    )

    def _fetch(params):
        ck = _cache_key(endpoint, params)
        cached = _cache_get(ck)
        if cached is not None:
            logging.info(f"Newznab '{instance}' cache hit for {params.get('t')} query")
            return cached
        try:
            r = api.get(endpoint, params=params, timeout=timeout)
            logging.info(f"Newznab '{instance}' full request URL: {r.url}")
            if r.status_code != 200:
                logging.error(f"Newznab '{instance}' HTTP {r.status_code}: {r.text[:200]}")
                # Cache empty on rate limit or server errors to avoid hammering
                if r.status_code in (429, 503, 502, 500):
                    _cache_set(ck, [])
                return []
            results = _parse_newznab_xml(r.text, instance)
            _cache_set(ck, results)  # Cache even empty results to avoid hammering indexer
            return results
        except Exception as exc:
            logging.error(f"Newznab '{instance}' request error: {exc}")
            # Cache empty result on rate limit or server errors to avoid immediate retry
            _exc_str = str(exc)
            if any(code in _exc_str for code in ('429', '503', '502', '500')):
                _cache_set(ck, [])
            return []

    # scrape_all() runs one _do_scrape per title variant (original, translated,
    # anime aliases, ...) concurrently, and this ID-based query doesn't depend
    # on title at all — so two variants can fire the identical request within
    # milliseconds of each other, before either has cached a result. Route
    # every call through the single-flight guard so only the first one
    # actually hits the indexer; the rest reuse its result.
    _wait_timeout = (timeout + 5) if timeout else 35

    def _fetch_coalesced(params):
        return _NZB_INFLIGHT.call(
            _cache_key(endpoint, params),
            lambda: _fetch(params),
            wait_timeout=_wait_timeout,
            copy_fn=copy.deepcopy,
        )

    # Run both queries in parallel if there are 2, otherwise just run one
    all_results: List[Dict[str, Any]] = []
    if len(params_list) == 1:
        all_results = _fetch_coalesced(params_list[0])
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(_fetch_coalesced, p) for p in params_list]
            for f in as_completed(futures):
                all_results.extend(f.result())

    # For movie searches that return nothing, retry across TV categories too.
    # Some content (UFC/WWE/boxing PPVs, sports events) is typed as movie in
    # TMDB/Plex but indexed under TV categories (5000) on NZB indexers.
    if not all_results and content_type.lower() == 'movie':
        tv_params_list = _build_params_list(
            api_key, clean_imdb, title, year, 'search', season, episode, multi
        )
        logging.info(f"Newznab '{instance}': 0 movie results — retrying with cross-category search (movie+TV)")
        retry_results: List[Dict[str, Any]] = []
        if len(tv_params_list) == 1:
            retry_results = _fetch_coalesced(tv_params_list[0])
        else:
            with ThreadPoolExecutor(max_workers=2) as ex:
                futures = [ex.submit(_fetch_coalesced, p) for p in tv_params_list]
                for f in as_completed(futures):
                    retry_results.extend(f.result())
        if retry_results:
            logging.info(f"Newznab '{instance}': cross-category retry found {len(retry_results)} results")
        all_results = retry_results

    # Deduplicate by GUID — ID search and title search may return the same NZB
    seen_guids = set()
    deduped = []
    for r in all_results:
        guid = r.get('parsed_info', {}).get('guid') or r.get('nzb_url', '')
        if guid and guid not in seen_guids:
            seen_guids.add(guid)
            deduped.append(r)
        elif not guid:
            deduped.append(r)

    # Filter by retention age
    _retention = _get_retention_days()
    _retention_filtered = 0
    if _retention:
        before = len(deduped)
        deduped = [r for r in deduped if _is_within_retention_days(
            r.get('parsed_info', {}).get('publish_date', ''), _retention)]
        _retention_filtered = before - len(deduped)

    _msg = f"Newznab '{instance}': {len(all_results)} total results, {len(deduped)} after dedup"
    if _retention_filtered:
        _msg += f", {_retention_filtered} filtered by retention ({_retention}d)"
    logging.info(_msg)
    return deduped


def _parse_newznab_xml(xml_text: str, instance: str) -> List[Dict[str, Any]]:
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logging.error(f"Newznab XML parse error for '{instance}': {exc}")
        return results

    ns = {'newznab': 'http://www.newznab.com/DTD/2010/feeds/attributes/'}
    channel = root.find('channel')
    if channel is None:
        return results

    for item in channel.findall('item'):
        title_el = item.find('title')
        if title_el is None or not title_el.text:
            continue
        title = title_el.text.strip()

        # NZB download link is in <link> or <enclosure>
        link_el = item.find('link')
        enclosure_el = item.find('enclosure')
        nzb_url = None
        if enclosure_el is not None:
            nzb_url = enclosure_el.get('url', '')
        if not nzb_url and link_el is not None:
            nzb_url = (link_el.text or '').strip()
        if not nzb_url:
            continue

        guid_el = item.find('guid')
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else nzb_url

        # Size from enclosure or newznab:attr
        size_bytes = 0
        if enclosure_el is not None:
            try:
                size_bytes = int(enclosure_el.get('length', 0))
            except (ValueError, TypeError):
                pass
        nzb_files_count = 0
        if not size_bytes:
            for attr in item.findall('newznab:attr', ns):
                name = attr.get('name')
                if name == 'size':
                    try:
                        size_bytes = int(attr.get('value', 0))
                    except (ValueError, TypeError):
                        pass
                elif name == 'files':
                    try:
                        nzb_files_count = int(attr.get('value', 0))
                    except (ValueError, TypeError):
                        pass
        else:
            for attr in item.findall('newznab:attr', ns):
                if attr.get('name') == 'files':
                    try:
                        nzb_files_count = int(attr.get('value', 0))
                    except (ValueError, TypeError):
                        pass
                    break

        size_gb = round(size_bytes / (1024 ** 3), 2) if size_bytes else 0.0

        cats = []
        for cat_el in item.findall('category'):
            if cat_el.text:
                cats.append(cat_el.text.strip())

        pub_date_el = item.find('pubDate')
        pub_date = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ''

        try:
            ptt = ptt_parse(title)
        except Exception:
            ptt = {}

        parsed_info = {
            'guid': guid,
            'protocol': 'nzb',
            'publish_date': pub_date,
            'categories_newznab': cats,
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

        result = {
            'title': title,
            'original_title': title,
            'size': size_gb,
            'source': instance,
            'seeders': 0,
            'hash': '',
            'parsed_info': parsed_info,
            'magnet': None,
            'torrent_url': None,
            'magnet_link': None,
            'nzb_url': nzb_url,
            'protocol': 'nzb',
            'nzb_files': nzb_files_count,
        }
        results.append(result)

    logging.info(f"Newznab '{instance}': parsed {len(results)} NZB results")
    return results


# ---------------------------------------------------------------------------
# NZB episode aggregation — virtual season pack from per-episode searches
# ---------------------------------------------------------------------------

_NORM_RE = re.compile(r'[^a-z0-9]')


def _norm_group_key(title: str, resolution: str, group: str, hdr_flag: str = '') -> str:
    """Normalise title+res+hdr+group into a stable grouping key."""
    t = _NORM_RE.sub('', (title or '').lower())
    r = _NORM_RE.sub('', (resolution or '').lower())
    g = _NORM_RE.sub('', (group or '').lower())
    h = _NORM_RE.sub('', (hdr_flag or '').lower())
    return f'{t}|{r}|{h}|{g}'


def _hdr_flag(hdr_list) -> str:
    """Normalise HDR tags to a consistent flag for grouping.
    DV > HDR10 > HDR > SDR — captures the highest tier present."""
    tags = [h.lower() for h in (hdr_list or [])]
    if any(t in ('dv', 'dolby vision', 'dovi') for t in tags):
        return 'dv'
    if any(t in ('hdr10+', 'hdr10plus') for t in tags):
        return 'hdr10plus'
    if any(t in ('hdr10', 'hdr') for t in tags):
        return 'hdr'
    return 'sdr'


def scrape_newznab_season_aggregate(
    scrapers: List[tuple],          # [(instance_name, settings_dict), ...]
    imdb_id: Optional[str],
    title: str,
    year: int,
    season: int,
    episode_count: int = 0,
    timeout: int = 20,
    episode_numbers: Optional[List[int]] = None,  # explicit list overrides range(1, episode_count+1)
    version_settings: Optional[Dict] = None,      # version filter settings for resolution/quality
) -> List[Dict[str, Any]]:
    """
    Search each episode individually across all indexers in parallel.
    Group results by (parsed_title, resolution, group).
    Only return groups that cover ALL episodes — no partial packs.
    Each returned result is a virtual season pack with episode_nzb_urls
    containing {episode_number: nzb_url} and fallback_urls {episode_number: [alt_url, ...]}.

    episode_numbers: if provided, searches exactly those episode numbers (arbitrary range/gaps).
                     If not provided, searches range(1, episode_count+1).
    """
    # Resolve the list of episodes to fetch
    if episode_numbers:
        _ep_list = sorted(set(episode_numbers))
    elif episode_count:
        _ep_list = list(range(1, episode_count + 1))
    else:
        return []

    if not scrapers or not _ep_list:
        return []

    cats = _CAT_TV
    clean_imdb = imdb_id.replace('tt', '') if imdb_id else ''

    def _fetch_episode(ep_num: int, instance: str, cfg: dict) -> List[Dict[str, Any]]:
        url = cfg.get('url', '').rstrip('/')
        api_key = cfg.get('api_key', '').strip()
        if not url or not api_key:
            return []
        ep_endpoint = f'{url}/api'
        results = []
        # ID-based tvsearch first
        if clean_imdb:
            id_params = {
                'apikey': api_key, 't': 'tvsearch', 'imdbid': clean_imdb,
                'season': season, 'ep': ep_num, 'cat': cats, 'limit': 20,
            }
            ck = _cache_key(ep_endpoint, id_params)
            cached = _cache_get(ck)
            if cached is not None:
                results.extend(cached)
            else:
                try:
                    r = api.get(ep_endpoint, params=id_params, timeout=timeout)
                    if r.status_code == 200:
                        parsed = _parse_newznab_xml(r.text, instance)
                        _cache_set(ck, parsed)  # Cache even empty
                        results.extend(parsed)
                    elif r.status_code == 429:
                        _cache_set(ck, [])  # Cache 429 to avoid retry
                except Exception:
                    pass
        # Title text search
        q = _SANITIZE_RE.sub('', f'{title} S{season:02d}E{ep_num:02d}').strip()
        txt_params = {'apikey': api_key, 't': 'search', 'q': q, 'cat': cats, 'limit': 20}
        ck = _cache_key(ep_endpoint, txt_params)
        cached = _cache_get(ck)
        if cached is not None:
            results.extend(cached)
        else:
            try:
                r = api.get(ep_endpoint, params=txt_params, timeout=timeout)
                if r.status_code == 200:
                    parsed = _parse_newznab_xml(r.text, instance)
                    _cache_set(ck, parsed)  # Cache even empty
                    results.extend(parsed)
                elif r.status_code == 429:
                    _cache_set(ck, [])  # Cache 429 to avoid retry
            except Exception:
                pass
        # Deduplicate by guid
        seen, deduped = set(), []
        for res in results:
            guid = res.get('parsed_info', {}).get('guid') or res.get('nzb_url', '')
            if guid not in seen:
                seen.add(guid)
                deduped.append(res)
        return deduped

    # Build all tasks: (ep_num, instance, cfg)
    tasks = [(ep, inst, cfg) for ep in _ep_list for inst, cfg in scrapers]

    # ep_results: {ep_num: [result, ...]}
    ep_results: Dict[int, List[Dict]] = {ep: [] for ep in _ep_list}

    with ThreadPoolExecutor(max_workers=min(len(tasks), 16)) as ex:
        future_map = {ex.submit(_fetch_episode, ep, inst, cfg): ep for ep, inst, cfg in tasks}
        for fut in as_completed(future_map):
            ep = future_map[fut]
            try:
                ep_results[ep].extend(fut.result())
            except Exception:
                pass

    logging.info(f'[NZBAggregate] {title} S{season:02d}: fetched results for {len(_ep_list)} episodes')

    # Title similarity threshold — reject results whose parsed title doesn't match target
    _norm_target = _NORM_RE.sub('', title.lower())

    # Retention cutoff — uses shared helper and Usenet Provider setting
    _retention_days = _get_retention_days()

    def _is_within_retention(pub_date_str: str) -> bool:
        return _is_within_retention_days(pub_date_str, _retention_days)

    # Group by key across episodes
    # group_data[key] = {ep_num: [result, ...]}
    # Each result also stored in broader keys for waterfall fallback
    group_data: Dict[str, Dict[int, List[Dict]]] = {}
    group_meta: Dict[str, Dict] = {}  # key -> first result's parsed fields

    for ep_num, results in ep_results.items():
        for res in results:
            pi = res.get('parsed_info', {})
            # Only include results that actually contain this episode
            ep_nums_in_result = pi.get('episodes', [])
            if ep_nums_in_result and ep_num not in ep_nums_in_result:
                continue
            # Retention check — skip expired NZBs
            if not _is_within_retention(pi.get('publish_date', '')):
                logging.debug(f'[NZBAggregate] Skipping expired NZB: {res.get("title","")!r} (pub={pi.get("publish_date","")})')
                continue
            # Title similarity check — reject wrong shows
            parsed_title = pi.get('title', '')
            if parsed_title:
                _norm_parsed = _NORM_RE.sub('', parsed_title.lower())
                _sim = _fuzz.ratio(_norm_target, _norm_parsed) / 100.0
                if _sim < 0.6:
                    logging.debug(f'[NZBAggregate] Rejecting title mismatch: {parsed_title!r} vs {title!r} (sim={_sim:.2f})')
                    continue
            res_hdr = _hdr_flag(pi.get('hdr', []))
            res_resolution = pi.get('resolution', '')
            res_group = pi.get('group', '')
            # Store under all waterfall key levels
            for key in [
                _norm_group_key(parsed_title, res_resolution, res_group, res_hdr),  # level 1: res+hdr+group
                _norm_group_key(parsed_title, res_resolution, '',        res_hdr),  # level 2: res+hdr
                _norm_group_key(parsed_title, res_resolution, '',        ''),       # level 3: res only
            ]:
                if not key.replace('|', ''):
                    continue
                if key not in group_data:
                    group_data[key] = {}
                    group_meta[key] = pi
                group_data[key].setdefault(ep_num, []).append(res)

    # Waterfall: try each level in order, stop at first level that yields complete packs.
    # Level 1: (title, resolution, hdr, group) — most specific
    # Level 2: (title, resolution, hdr)        — any group, same quality tier
    # Level 3: (title, resolution)             — any group, any HDR variant
    def _key_level(k: str) -> int:
        parts = k.split('|')
        # parts = [title, resolution, hdr, group]
        if len(parts) == 4 and parts[3]:  # group present
            return 1
        if len(parts) == 4 and parts[2]:  # hdr present, no group
            return 2
        return 3  # resolution only

    complete_keys_by_level: Dict[int, list] = {1: [], 2: [], 3: []}
    for key, ep_map in group_data.items():
        if len(ep_map) >= len(_ep_list):
            complete_keys_by_level[_key_level(key)].append(key)

    # Pick the best level that has complete packs
    active_keys = []
    for lvl in (1, 2, 3):
        if complete_keys_by_level[lvl]:
            active_keys = complete_keys_by_level[lvl]
            logging.info(f'[NZBAggregate] {title} S{season:02d}: using waterfall level {lvl} ({len(active_keys)} complete group(s))')
            break

    virtual_packs = []
    for key in active_keys:
        ep_map = group_data[key]
        if len(ep_map) < len(_ep_list):
            continue

        pi = group_meta[key]
        # Primary NZB per episode (best result = first), fallbacks = rest
        episode_nzb_urls: Dict[int, str] = {}
        fallback_urls: Dict[int, List[str]] = {}
        episode_sizes: Dict[int, float] = {}
        episode_filenames: Dict[int, str] = {}
        total_size = 0.0
        # Use the median episode's result as representative (avoids outliers)
        rep_result = ep_map[sorted(ep_map.keys())[len(ep_map) // 2]][0]

        for ep_num in _ep_list:
            ep_list = ep_map[ep_num]
            ep_result = ep_list[0]
            episode_nzb_urls[ep_num] = ep_result.get('nzb_url', '')
            fallback_urls[ep_num] = [r.get('nzb_url', '') for r in ep_list[1:] if r.get('nzb_url')]
            ep_size = ep_result.get('size', 0.0)
            episode_sizes[ep_num] = ep_size
            total_size += ep_size
            _raw_title = ep_result.get('original_title') or ep_result.get('title') or ''
            # If the title doesn't contain this episode's SxxExx (e.g. PTT couldn't parse
            # episode numbers so a season-pack-style title was accepted for all episodes),
            # inject the correct SxxExx so quality tags are preserved but episode is correct.
            if _raw_title and not re.search(
                    rf'[Ss]{season:02d}[Ee]{ep_num:02d}', _raw_title):
                _ep_marker = re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', _raw_title)
                if _ep_marker:
                    # Replace existing SxxExx with correct one
                    _raw_title = _raw_title[:_ep_marker.start()] + \
                                 f'S{season:02d}E{ep_num:02d}' + \
                                 _raw_title[_ep_marker.end():]
                else:
                    # No SxxExx at all — inject after season marker if present
                    _s_marker = re.search(r'([Ss]\d{1,2})(?![Ee]\d)', _raw_title)
                    if _s_marker:
                        _raw_title = _raw_title[:_s_marker.end()] + \
                                     f'E{ep_num:02d}' + \
                                     _raw_title[_s_marker.end():]
            episode_filenames[ep_num] = _raw_title

        # Build display title: strip episode number AND episode title words,
        # keeping show title + season + quality tags only.
        # e.g. "Young.Sheldon.S01E11.A.Computer.a.Plastic.Pony.1080p.BluRay.REMUX-EPSiLON"
        #   → "Young.Sheldon.S01.1080p.BluRay.REMUX-EPSiLON"
        rep_orig = rep_result.get('original_title') or rep_result.get('title') or ''
        _QUALITY_START = re.compile(
            r'[._\s-]('
            r'(?:\d{3,4}[pP])'          # resolution: 1080p 2160p 720p
            r'|(?:BluRay|BDRip|WEB(?:-?DL)?|WEBRip|HDTV|DVDRip|REMUX|BRRip)'
            r'|(?:x264|x265|H\.?264|H\.?265|HEVC|AVC|XviD)'
            r'|(?:DTS|AC3|AAC|DDP?5?\.?1?|TrueHD|FLAC|Atmos)'
            r'|(?:HDR\d*|DV|DoVi|Dolby)'
            r')',
            re.IGNORECASE,
        )
        # Find where quality tags begin after the episode marker
        _ep_match = re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', rep_orig)
        if _ep_match:
            _after_ep = rep_orig[_ep_match.end():]
            _qual_match = _QUALITY_START.search(_after_ep)
            if _qual_match:
                # show_title + S01 + quality_tags
                ep_stripped = (
                    rep_orig[:_ep_match.start()] +
                    _ep_match.group(0)[:3] +   # just S01
                    _after_ep[_qual_match.start():]
                ).strip(' .-_')
            else:
                # No quality tags found — just strip E11 and leave the rest
                ep_stripped = re.sub(r'([Ss]\d{1,2})[Ee]\d{1,2}', lambda m: m.group(1), rep_orig).strip(' .-_')
        else:
            ep_stripped = rep_orig.strip(' .-_')
        display_title = f"{ep_stripped} [NZB Pack · {len(_ep_list)} eps]" if ep_stripped else (
            f"{pi.get('title', title)} S{season:02d} "
            f"[NZB Pack · {len(_ep_list)} eps · "
            f"{pi.get('resolution', '') or 'unknown res'}"
            f"{' · ' + pi.get('group', '') if pi.get('group') else ''}]"
        )

        # Run the display title through the full file_processing pipeline so that
        # parsed_info gets resolution_rank, is_hdr, season_episode_info etc. —
        # identical to what normal scraper results receive before filter/rank
        try:
            from scraper.functions.file_processing import _process_single_title
            enriched_pi = _process_single_title((display_title, round(total_size, 2)))
            if enriched_pi and 'parsing_error' not in enriched_pi:
                enriched_pi.update({
                    'original_title': display_title,
                    'seasons': [season],
                    'episodes': [],
                    'protocol': 'nzb',
                    'guid': f'nzb_agg_{key}',
                    'source_instance': 'NZB Aggregate',
                })
            else:
                enriched_pi = rep_result.get('parsed_info', pi).copy()
                enriched_pi.update({'original_title': display_title, 'seasons': [season],
                                    'episodes': [], 'protocol': 'nzb'})
        except Exception:
            enriched_pi = rep_result.get('parsed_info', pi).copy()
            enriched_pi.update({'original_title': display_title, 'seasons': [season],
                                'episodes': [], 'protocol': 'nzb'})

        virtual_packs.append({
            'title': display_title,
            'original_title': display_title,
            'resolution': enriched_pi.get('resolution', 'Unknown'),
            'parsed_title': enriched_pi.get('title', title),
            'size': round(total_size, 2),
            'source': 'NZB Aggregate',
            'seeders': 0,
            'hash': '',
            'magnet': None,
            'torrent_url': None,
            'magnet_link': None,
            'nzb_url': '',
            'protocol': 'nzb',
            'is_nzb_season_pack': True,
            'episode_nzb_urls': episode_nzb_urls,
            'fallback_nzb_urls': fallback_urls,
            'episode_sizes': episode_sizes,
            'episode_filenames': episode_filenames,
            'episode_count': len(_ep_list),
            'parsed_info': enriched_pi,
            'waterfall_level': lvl,
        })

    logging.info(f'[NZBAggregate] {title} S{season:02d}: {len(virtual_packs)} complete virtual packs found')

    # Apply version resolution filter and sort by quality preference
    if version_settings and virtual_packs:
        from scraper.functions.filter_results import get_resolution_value, resolution_filter
        max_res = version_settings.get('max_resolution', '2160p')
        res_wanted = version_settings.get('resolution_wanted', '<=')

        before = len(virtual_packs)
        virtual_packs = [
            p for p in virtual_packs
            if resolution_filter(p.get('resolution') or p.get('parsed_info', {}).get('resolution') or '', max_res, res_wanted)
        ]
        if len(virtual_packs) < before:
            logging.info(f'[NZBAggregate] {title} S{season:02d}: filtered {before - len(virtual_packs)} packs by resolution ({res_wanted} {max_res}), {len(virtual_packs)} remaining')

        # Sort highest resolution first for both <= and >= — user always wants the best
        # quality that passes the filter. == is already exact so order doesn't matter.
        if virtual_packs and res_wanted != '==':
            virtual_packs.sort(
                key=lambda p: get_resolution_value(p.get('resolution') or p.get('parsed_info', {}).get('resolution') or ''),
                reverse=True
            )

    return virtual_packs
