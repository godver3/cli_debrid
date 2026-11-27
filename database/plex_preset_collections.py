"""
Plex Preset Collections: ready-made collections built from the existing library.

Kometa-style categories (Christmas, Box Office Blockbusters, decades, genres,
award winners, studios) that a user switches on in Settings. Only titles that
are already in Plex are added; nothing is ever requested or downloaded.

Where each preset's titles come from:
  - TMDB keyword / company / chart lists (needs the TMDB API key)
  - Wikidata award winners (no key)
  - the Plex library itself (decades, genres)

Settings live in config.json under "Plex Preset Collections": one boolean per
preset key plus "min_items". State (managed collection ratingKeys, cached
candidate lists) lives in plex_preset_collection_state.json.

A collection this module created is reconciled fully and deleted when its
preset is switched off or drops below min_items. A collection that already
existed under the same name is taken over add-only: items the user put there
are never removed and the collection is never deleted.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from utilities.settings import get_setting

logger = logging.getLogger(__name__)

SETTINGS_SECTION = 'Plex Preset Collections'
DEFAULT_MIN_ITEMS = 3

_STATE_FILE = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'plex_preset_collection_state.json')
_state_lock = threading.Lock()
_run_lock = threading.Lock()

_TMDB = 'https://api.themoviedb.org/3'
_WIKIDATA_SPARQL = 'https://query.wikidata.org/sparql'
_WIKIDATA_HEADERS = {
    'User-Agent': 'cli_debrid-preset-collections/1.0 (https://github.com/godver3/cli_debrid)',
    'Accept': 'application/sparql-results+json',
}

_HOUR = 3600
_DAY = 24 * _HOUR

# ── Catalog ───────────────────────────────────────────────────────────────────
# name: '{kind}' becomes Movies/Shows. Presets with both types make one
# collection per library type.

GROUPS = (
    ('seasonal', 'Seasonal'),
    ('charts', 'Charts'),
    ('decades', 'Decades & Genres'),
    ('awards', 'Awards'),
    ('studios', 'Studios'),
)

PRESETS: Dict[str, dict] = {
    # Seasonal: TMDB keyword, resolved by exact name at run time
    'christmas': {'group': 'seasonal', 'label': 'Christmas', 'name': 'Christmas Movies',
                  'source': 'tmdb_keyword', 'keyword': 'christmas', 'types': ('movie',)},
    'halloween': {'group': 'seasonal', 'label': 'Halloween', 'name': 'Halloween Movies',
                  'source': 'tmdb_keyword', 'keyword': 'halloween', 'types': ('movie',)},
    'thanksgiving': {'group': 'seasonal', 'label': 'Thanksgiving', 'name': 'Thanksgiving Movies',
                     'source': 'tmdb_keyword', 'keyword': 'thanksgiving', 'types': ('movie',)},
    'valentines': {'group': 'seasonal', 'label': "Valentine's Day", 'name': "Valentine's Day Movies",
                   'source': 'tmdb_keyword', 'keyword': "valentine's day", 'types': ('movie',)},
    'new_years': {'group': 'seasonal', 'label': "New Year's Eve", 'name': "New Year's Eve Movies",
                  'source': 'tmdb_keyword', 'keyword': "new year's eve", 'types': ('movie',)},

    # Charts: ranked lists, kept in list order
    'blockbusters': {'group': 'charts', 'label': 'Box Office Blockbusters', 'name': 'Box Office Blockbusters',
                     'source': 'tmdb_discover', 'params': {'sort_by': 'revenue.desc'}, 'pages': 10,
                     'ordered': True, 'types': ('movie',)},
    'top_rated': {'group': 'charts', 'label': 'Top Rated', 'name': 'Top Rated {kind}',
                  'source': 'tmdb_chart', 'chart': 'top_rated', 'pages': 20,
                  'ordered': True, 'types': ('movie', 'show')},
    'trending': {'group': 'charts', 'label': 'Trending This Week', 'name': 'Trending {kind}',
                 'source': 'tmdb_chart', 'chart': 'trending', 'pages': 5, 'ttl': 12 * _HOUR,
                 'ordered': True, 'types': ('movie', 'show')},
    'popular': {'group': 'charts', 'label': 'Popular', 'name': 'Popular {kind}',
                'source': 'tmdb_chart', 'chart': 'popular', 'pages': 10, 'ttl': 12 * _HOUR,
                'ordered': True, 'types': ('movie', 'show')},

    # Library-derived, one collection per decade / genre
    'decades': {'group': 'decades', 'label': 'Decades (1980s, 1990s…)', 'name': '{decade}s {kind}',
                'source': 'plex_decades', 'types': ('movie', 'show')},
    # Optional: Plex already browses by genre, this adds one collection per genre
    'genres': {'group': 'decades', 'label': 'Genres (Action, Horror…)', 'name': '{genre} {kind}',
               'source': 'plex_genres', 'types': ('movie', 'show')},

    # Awards: Wikidata "award received" winners with an IMDb title ID
    'oscar_best_picture': {'group': 'awards', 'label': 'Oscar Best Picture', 'name': 'Oscar Best Picture Winners',
                           'source': 'wikidata_award', 'award': 'Q102427', 'types': ('movie',)},
    'oscar_animated': {'group': 'awards', 'label': 'Oscar Best Animated Feature',
                       'name': 'Oscar Best Animated Feature Winners',
                       'source': 'wikidata_award', 'award': 'Q106800', 'types': ('movie',)},
    'palme_dor': {'group': 'awards', 'label': "Palme d'Or", 'name': "Palme d'Or Winners",
                  'source': 'wikidata_award', 'award': 'Q179808', 'types': ('movie',)},
    'emmy_drama': {'group': 'awards', 'label': 'Emmy Outstanding Drama Series',
                   'name': 'Emmy Outstanding Drama Series Winners',
                   'source': 'wikidata_award', 'award': 'Q989438', 'types': ('show',)},
    'emmy_comedy': {'group': 'awards', 'label': 'Emmy Outstanding Comedy Series',
                    'name': 'Emmy Outstanding Comedy Series Winners',
                    'source': 'wikidata_award', 'award': 'Q2110156', 'types': ('show',)},

    # Studios: TMDB production company IDs
    'pixar': {'group': 'studios', 'label': 'Pixar', 'name': 'Pixar',
              'source': 'tmdb_company', 'companies': '3', 'types': ('movie',)},
    'disney_animation': {'group': 'studios', 'label': 'Walt Disney Animation', 'name': 'Walt Disney Animation Studios',
                         'source': 'tmdb_company', 'companies': '6125', 'types': ('movie',)},
    'dreamworks_animation': {'group': 'studios', 'label': 'DreamWorks Animation', 'name': 'DreamWorks Animation',
                             'source': 'tmdb_company', 'companies': '521', 'types': ('movie',)},
    'illumination': {'group': 'studios', 'label': 'Illumination', 'name': 'Illumination',
                     'source': 'tmdb_company', 'companies': '6704', 'types': ('movie',)},
    'ghibli': {'group': 'studios', 'label': 'Studio Ghibli', 'name': 'Studio Ghibli',
               'source': 'tmdb_company', 'companies': '10342', 'types': ('movie',)},
    'marvel': {'group': 'studios', 'label': 'Marvel Studios', 'name': 'Marvel Studios',
               'source': 'tmdb_company', 'companies': '420', 'types': ('movie',)},
    'dc': {'group': 'studios', 'label': 'DC', 'name': 'DC',
           'source': 'tmdb_company', 'companies': '128064|184898', 'types': ('movie',)},
    'lucasfilm': {'group': 'studios', 'label': 'Lucasfilm', 'name': 'Lucasfilm',
                  'source': 'tmdb_company', 'companies': '1', 'types': ('movie',)},
    'a24': {'group': 'studios', 'label': 'A24', 'name': 'A24',
            'source': 'tmdb_company', 'companies': '41077', 'types': ('movie',)},
    'blumhouse': {'group': 'studios', 'label': 'Blumhouse', 'name': 'Blumhouse',
                  'source': 'tmdb_company', 'companies': '3172', 'types': ('movie',)},
}

_KIND_WORD = {'movie': 'Movies', 'show': 'Shows'}
_TMDB_MEDIA = {'movie': 'movie', 'show': 'tv'}
_TMDB_SOURCES = {'tmdb_keyword', 'tmdb_discover', 'tmdb_chart', 'tmdb_company'}
_MAX_DISCOVER_PAGES = 250


def catalog_for_template() -> List[dict]:
    """Groups with their presets, in display order, for the settings card."""
    out = []
    for gkey, glabel in GROUPS:
        items = [{'key': k, 'label': p['label'], 'types': list(p['types'])}
                 for k, p in PRESETS.items() if p['group'] == gkey]
        out.append({'key': gkey, 'label': glabel, 'presets': items})
    return out


def enabled_presets() -> List[str]:
    return [k for k in PRESETS if bool(get_setting(SETTINGS_SECTION, k, False))]


def _min_items() -> int:
    try:
        return max(1, int(get_setting(SETTINGS_SECTION, 'min_items', DEFAULT_MIN_ITEMS) or DEFAULT_MIN_ITEMS))
    except (TypeError, ValueError):
        return DEFAULT_MIN_ITEMS


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    with _state_lock:
        try:
            with open(_STATE_FILE, 'r') as f:
                state = json.load(f)
                return state if isinstance(state, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def _save_state(state: dict) -> None:
    with _state_lock:
        tmp = _STATE_FILE + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f)
            os.replace(tmp, _STATE_FILE)
        except Exception as e:
            logger.error(f"[PresetCollections] Failed to save state: {e}")


def get_status() -> dict:
    state = _load_state()
    managed = state.get('managed') or {}
    return {
        'last_run': state.get('last_run'),
        'last_summary': state.get('last_summary') or {},
        'collections_managed': len(managed),
        'enabled': enabled_presets(),
    }


# ── Candidate sources ─────────────────────────────────────────────────────────
# Each returns {lib_type: [candidate, ...]} in list order, where a candidate is
# {'tmdb': str|None, 'imdb': str|None, 'title': str, 'year': str}.

def _tmdb_key() -> str:
    return str(get_setting('TMDB', 'api_key', '') or '').strip()


def _tmdb_get(path: str, params: dict) -> dict:
    params = dict(params, api_key=_tmdb_key())
    for attempt in range(4):
        resp = requests.get(f'{_TMDB}{path}', params=params, timeout=20)
        if resp.status_code == 429:
            time.sleep(min(10, int(resp.headers.get('Retry-After', '2') or 2)))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f'TMDB rate limited on {path}')


def _tmdb_candidate(row: dict) -> dict:
    return {
        'tmdb': str(row.get('id')) if row.get('id') else None,
        'imdb': None,
        'title': row.get('title') or row.get('name') or '',
        'year': str(row.get('release_date') or row.get('first_air_date') or '')[:4],
    }


def _tmdb_paged(path: str, params: dict, max_pages: int) -> List[dict]:
    out = []
    page = 1
    while page <= max_pages:
        data = _tmdb_get(path, dict(params, page=page))
        out.extend(_tmdb_candidate(r) for r in data.get('results') or [])
        total = int(data.get('total_pages') or 1)
        if page >= min(total, max_pages):
            break
        page += 1
    return out


def _resolve_keyword_id(keyword: str, state: dict) -> Optional[int]:
    cache = state.setdefault('keyword_ids', {})
    if keyword in cache:
        return cache[keyword]
    data = _tmdb_get('/search/keyword', {'query': keyword})
    for row in data.get('results') or []:
        if (row.get('name') or '').casefold() == keyword.casefold():
            cache[keyword] = int(row['id'])
            return cache[keyword]
    return None


def _fetch_tmdb(preset: dict, state: dict) -> Dict[str, List[dict]]:
    if not _tmdb_key():
        raise RuntimeError('TMDB API key is not set')
    out = {}
    source = preset['source']
    for lib_type in preset['types']:
        media = _TMDB_MEDIA[lib_type]
        if source == 'tmdb_keyword':
            kid = _resolve_keyword_id(preset['keyword'], state)
            if kid is None:
                raise RuntimeError(f"TMDB keyword '{preset['keyword']}' not found")
            out[lib_type] = _tmdb_paged(f'/discover/{media}',
                                        {'with_keywords': kid, 'sort_by': 'popularity.desc'},
                                        _MAX_DISCOVER_PAGES)
        elif source == 'tmdb_company':
            out[lib_type] = _tmdb_paged(f'/discover/{media}',
                                        {'with_companies': preset['companies'], 'sort_by': 'popularity.desc'},
                                        _MAX_DISCOVER_PAGES)
        elif source == 'tmdb_discover':
            out[lib_type] = _tmdb_paged(f'/discover/{media}', dict(preset.get('params') or {}),
                                        int(preset.get('pages', 10)))
        elif source == 'tmdb_chart':
            chart = preset['chart']
            path = f'/trending/{media}/week' if chart == 'trending' else f'/{media}/{chart}'
            out[lib_type] = _tmdb_paged(path, {}, int(preset.get('pages', 10)))
    return out


def _fetch_wikidata_award(preset: dict) -> Dict[str, List[dict]]:
    award = preset['award']
    if not (award.startswith('Q') and award[1:].isdigit()):
        raise ValueError(f'Bad Wikidata ID {award!r}')
    query = (
        'SELECT DISTINCT ?imdb WHERE { '
        f'?work p:P166 ?st . ?st ps:P166 wd:{award} . ?work wdt:P345 ?imdb . '
        'FILTER(STRSTARTS(?imdb, "tt")) }'
    )
    resp = requests.get(_WIKIDATA_SPARQL, params={'query': query, 'format': 'json'},
                        headers=_WIKIDATA_HEADERS, timeout=60)
    resp.raise_for_status()
    imdb_ids = sorted({b['imdb']['value'] for b in resp.json().get('results', {}).get('bindings', [])
                       if b.get('imdb', {}).get('value')})
    candidates = [{'tmdb': None, 'imdb': i, 'title': '', 'year': ''} for i in imdb_ids]
    return {lib_type: candidates for lib_type in preset['types']}


def _cached_candidates(key: str, preset: dict, state: dict) -> Dict[str, List[dict]]:
    """External candidates, cached so Run Now and settings saves don't refetch."""
    ttl = int(preset.get('ttl', 7 * _DAY))
    cache = state.setdefault('cache', {})
    entry = cache.get(key) or {}
    if entry.get('ts') and time.time() - float(entry['ts']) < ttl and isinstance(entry.get('items'), dict):
        return entry['items']
    if preset['source'] in _TMDB_SOURCES:
        items = _fetch_tmdb(preset, state)
    elif preset['source'] == 'wikidata_award':
        items = _fetch_wikidata_award(preset)
    else:
        raise ValueError(f"Unknown source {preset['source']}")
    cache[key] = {'ts': time.time(), 'items': items}
    return items


def match_candidates(candidates: List[dict], id_map: dict) -> List[str]:
    """Plex ratingKeys for candidates found in a section, in candidate order, no repeats."""
    from database.plex_collections import _title_year_key
    out, seen = [], set()
    for c in candidates:
        rk = None
        if c.get('tmdb'):
            rk = id_map.get('tmdb:' + str(c['tmdb']))
        if not rk and c.get('imdb'):
            rk = id_map.get(c['imdb'])
        if not rk and c.get('title'):
            tk = _title_year_key(c['title'], c.get('year'))
            if tk:
                rk = id_map.get(tk)
        if rk and rk not in seen:
            seen.add(rk)
            out.append(rk)
    return out


# ── Plex library data for decades ────────────────────────────────────

def _plex_get(plex_url: str, token: str, path: str, params: dict = None) -> dict:
    from database.plex_collections import _headers
    resp = requests.get(f'{plex_url}{path}', headers=_headers(token), params=params or {}, timeout=120)
    resp.raise_for_status()
    return resp.json().get('MediaContainer', {}) or {}


def group_by_decade(items: List[dict]) -> Dict[int, List[str]]:
    """{1990: [ratingKey, ...]} from Plex items with a 'year'."""
    out: Dict[int, List[str]] = {}
    for item in items:
        try:
            year = int(item.get('year') or 0)
        except (TypeError, ValueError):
            continue
        rk = str(item.get('ratingKey') or '')
        if year >= 1900 and rk:
            out.setdefault(year // 10 * 10, []).append(rk)
    return out


def _plex_decades(plex_url: str, token: str, section_key: str, lib_type: str) -> Dict[str, List[str]]:
    plex_type = 1 if lib_type == 'movie' else 2
    container = _plex_get(plex_url, token, f'/library/sections/{section_key}/all', {'type': plex_type})
    by_decade = group_by_decade(container.get('Metadata') or [])
    return {str(d): rks for d, rks in sorted(by_decade.items())}


def _plex_genres(plex_url: str, token: str, section_key: str, lib_type: str) -> Dict[str, List[str]]:
    """{genre title: [ratingKey, ...]} using Plex's own genre filter."""
    plex_type = 1 if lib_type == 'movie' else 2
    genres = _plex_get(plex_url, token, f'/library/sections/{section_key}/genre', {'type': plex_type})
    out = {}
    for g in genres.get('Directory') or []:
        title = (g.get('title') or '').strip()
        gid = g.get('key')
        if not title or gid is None:
            continue
        container = _plex_get(plex_url, token, f'/library/sections/{section_key}/all',
                              {'type': plex_type, 'genre': gid})
        rks = [str(m['ratingKey']) for m in container.get('Metadata') or [] if m.get('ratingKey')]
        if rks:
            out[title] = rks
    return out


# ── Plex collection reconcile ─────────────────────────────────────────────────

def collection_name(preset: dict, lib_type: str, **extra) -> str:
    return preset['name'].format(kind=_KIND_WORD[lib_type], **extra)


def _section_collections(plex_url: str, token: str, section_key: str) -> Dict[str, str]:
    container = _plex_get(plex_url, token, f'/library/sections/{section_key}/collections')
    return {m.get('title', ''): str(m['ratingKey']) for m in container.get('Metadata') or [] if m.get('ratingKey')}


def _delete_collection(plex_url: str, token: str, rk: str) -> None:
    resp = requests.delete(f'{plex_url}/library/collections/{rk}?X-Plex-Token={token}',
                           headers={'Accept': 'application/json'}, timeout=30)
    if resp.status_code not in (200, 204, 404):
        raise RuntimeError(f'Plex returned {resp.status_code} deleting collection {rk}')


def plan_changes(current: List[str], desired: List[str], adopted: bool) -> tuple:
    """(to_add, to_remove). Taken-over collections never lose items."""
    cur, want = set(current), set(desired)
    to_add = [rk for rk in desired if rk not in cur]
    to_remove = [] if adopted else [rk for rk in current if rk not in want]
    return to_add, to_remove


def _reconcile(ctx: dict, section_key: str, lib_type: str, name: str, desired: List[str],
               ordered: bool, preset_key: str, state: dict) -> Optional[str]:
    """Bring one collection in line with desired ratingKeys. Returns its state key."""
    from database.plex_collections import (
        _create_collection, _get_collection_items, _add_items_to_collection,
        _remove_item_from_collection, _arrange_items_in_order, _set_collection_sort_mode,
    )
    plex_url, token = ctx['plex_url'], ctx['token']
    managed = state.setdefault('managed', {})
    skey = f'{section_key}|{name}'
    entry = managed.get(skey)
    existing = ctx['collections'].get(section_key)
    if existing is None:
        existing = ctx['collections'][section_key] = _section_collections(plex_url, token, section_key)

    rk = existing.get(name)
    if rk and entry and entry.get('rk') != rk:
        entry = dict(entry, rk=rk)
    if rk and not entry:
        entry = {'rk': rk, 'adopted': True}
        logger.info(f"[PresetCollections] Taking over existing collection '{name}' (add-only)")
    if not rk:
        rk = _create_collection(plex_url, token, section_key, name, lib_type, sort_prefix='')
        if not ordered:
            _set_collection_sort_mode(plex_url, token, rk, 0)
        existing[name] = rk
        entry = {'rk': rk, 'adopted': False}
        ctx['summary']['created'] += 1

    current = _get_collection_items(plex_url, token, rk)
    to_add, to_remove = plan_changes(current, desired, bool(entry.get('adopted')))
    if to_add:
        _add_items_to_collection(plex_url, token, ctx['machine_id'], rk, to_add)
    for item_rk in to_remove:
        _remove_item_from_collection(plex_url, token, rk, item_rk)
    if ordered and not entry.get('adopted') and (to_add or to_remove or current != desired):
        _arrange_items_in_order(plex_url, token, rk, desired)
    ctx['summary']['added'] += len(to_add)
    ctx['summary']['removed'] += len(to_remove)

    entry.update({'preset': preset_key, 'section': section_key, 'name': name, 'type': lib_type,
                  'count': len(set(current) | set(desired)) if entry.get('adopted') else len(desired)})
    managed[skey] = entry
    return skey


def _drop_collection(ctx: dict, skey: str, state: dict) -> None:
    managed = state.get('managed') or {}
    entry = managed.get(skey)
    if not entry:
        return
    if entry.get('adopted'):
        managed.pop(skey, None)
        logger.info(f"[PresetCollections] No longer managing '{entry.get('name')}' (left in Plex, it existed before)")
        return
    _delete_collection(ctx['plex_url'], ctx['token'], entry['rk'])
    managed.pop(skey, None)
    ctx['summary']['deleted'] += 1
    logger.info(f"[PresetCollections] Removed collection '{entry.get('name')}'")


# ── Run ───────────────────────────────────────────────────────────────────────

def _desired_for_preset(key: str, preset: dict, ctx: dict, state: dict) -> List[tuple]:
    """[(section_key, lib_type, name, [ratingKeys], ordered)] for one preset."""
    from database.plex_collections import _get_section_id_map
    plex_url, token = ctx['plex_url'], ctx['token']
    out = []
    source = preset['source']
    candidates = None
    if source not in ('plex_decades', 'plex_genres'):
        candidates = _cached_candidates(key, preset, state)
    for section_key, info in ctx['sections'].items():
        lib_type = info['type']
        if lib_type not in preset['types']:
            continue
        if source == 'plex_decades':
            for decade, rks in _plex_decades(plex_url, token, section_key, lib_type).items():
                out.append((section_key, lib_type, collection_name(preset, lib_type, decade=decade), rks, False))
        elif source == 'plex_genres':
            for genre, rks in _plex_genres(plex_url, token, section_key, lib_type).items():
                out.append((section_key, lib_type, collection_name(preset, lib_type, genre=genre), rks, False))
        else:
            id_map = _get_section_id_map(plex_url, token, section_key, lib_type)
            rks = match_candidates(candidates.get(lib_type) or [], id_map)
            out.append((section_key, lib_type, collection_name(preset, lib_type), rks,
                        bool(preset.get('ordered'))))
    return out


def run_preset_collections() -> dict:
    """Sync every enabled preset and remove collections for disabled ones."""
    if not _run_lock.acquire(blocking=False):
        logger.info("[PresetCollections] Already running, skipping")
        return {'success': False, 'message': 'Already running'}
    try:
        return _run()
    finally:
        _run_lock.release()


def _run() -> dict:
    from database.plex_collections import _get_plex_credentials, _get_library_sections, _get_machine_id

    state = _load_state()
    enabled = enabled_presets()
    if not enabled and not state.get('managed'):
        return {'success': True, 'message': 'No preset collections enabled'}

    try:
        plex_url, token = _get_plex_credentials()
        sections = _get_library_sections(plex_url, token)
        machine_id = _get_machine_id(plex_url, token)
    except Exception as e:
        logger.error(f"[PresetCollections] Cannot reach Plex: {e}")
        return {'success': False, 'message': f'Cannot reach Plex: {e}'}

    ctx = {
        'plex_url': plex_url, 'token': token, 'machine_id': machine_id, 'sections': sections,
        'collections': {},
        'summary': {'created': 0, 'added': 0, 'removed': 0, 'deleted': 0, 'errors': []},
    }
    min_items = _min_items()
    keep = set()
    failed_presets = set()

    for key in enabled:
        preset = PRESETS[key]
        try:
            planned = _desired_for_preset(key, preset, ctx, state)
        except Exception as e:
            failed_presets.add(key)
            ctx['summary']['errors'].append(f"{preset['label']}: {e}")
            logger.warning(f"[PresetCollections] {preset['label']} skipped: {e}")
            continue
        for section_key, lib_type, name, rks, ordered in planned:
            if len(rks) < min_items:
                continue
            try:
                keep.add(_reconcile(ctx, section_key, lib_type, name, rks, ordered, key, state))
            except Exception as e:
                failed_presets.add(key)
                ctx['summary']['errors'].append(f"{name}: {e}")
                logger.warning(f"[PresetCollections] Sync of '{name}' failed: {e}")

    # Anything we manage that this run didn't produce is gone: preset switched off,
    # below min_items, or its library removed. A preset that errored keeps its collections.
    for skey, entry in list((state.get('managed') or {}).items()):
        if skey in keep or entry.get('preset') in failed_presets:
            continue
        try:
            _drop_collection(ctx, skey, state)
        except Exception as e:
            logger.warning(f"[PresetCollections] Could not remove '{entry.get('name')}': {e}")

    state['last_run'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    state['last_summary'] = ctx['summary']
    _save_state(state)
    s = ctx['summary']
    logger.info(f"[PresetCollections] Done: {len(keep)} collections, {s['created']} created, "
                f"{s['added']} items added, {s['removed']} removed, {s['deleted']} collections deleted, "
                f"{len(s['errors'])} errors")
    return {'success': True, 'collections': len(keep), **s}
