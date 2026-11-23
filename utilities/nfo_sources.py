"""
Where an episode NFO's details come from (utilities/nfo_writer.py).

The goal is Plex's own agent, but with cli_debrid's (TVDB) numbering:

* Plex numbers shows by TMDB after 2026-12-03. An episode whose number is
  the same in cli_debrid and TMDB ("aligned") gets a minimal NFO (season,
  episode, ids), so Plex Series fills in everything exactly as before:
  title, description, thumbnail, cast, ratings.
* A "moved" episode (DVD/absolute orders, anime, split seasons) would get
  the details of whatever episode Plex has at that number. Its NFO carries
  everything instead: cli_debrid's title/description/air date, and the
  thumbnail, directors, writers, guest stars and rating of the right TMDB
  episode (found by air date and title), or TVDB's when TMDB has none.

TMDB seasons are one request per show (seasons appended 20 at a time); TVDB
episode records are fetched only for moved episodes and cached on disk.
Without a TMDB key nothing can be compared: every episode gets cli_debrid's
title, description and air date.
"""

import difflib
import json
import logging
import os
import re
import threading
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

_LOG = '[NFO]'
TMDB_IMG = 'https://image.tmdb.org/t/p/original'
_TMDB_TTL = 6 * 3600
_TVDB_TTL = 60 * 86400

_tmdb_cache: Dict[str, Tuple[float, Optional[Dict]]] = {}
_tmdb_lock = threading.Lock()
_tvdb_lock = threading.Lock()


# ─── TMDB ────────────────────────────────────────────────────────────────────

def _tmdb_key() -> str:
    from utilities.settings import get_setting
    return (get_setting('TMDB', 'api_key', '') or '').strip()


def tmdb_episodes(tmdb_id) -> Optional[Dict[Tuple[int, int], Dict]]:
    """(season, episode) -> TMDB episode (name, overview, air_date, runtime,
    still_path, crew, guest_stars, vote_average, vote_count). None when
    unavailable (no key, no id, request failed)."""
    key = _tmdb_key()
    if not key or not tmdb_id:
        return None
    sid = str(tmdb_id)
    with _tmdb_lock:
        hit = _tmdb_cache.get(sid)
        if hit and time.time() - hit[0] < _TMDB_TTL:
            return hit[1]
    import requests
    out: Dict[Tuple[int, int], Dict] = {}
    try:
        show = requests.get(f'https://api.themoviedb.org/3/tv/{sid}', params={'api_key': key}, timeout=20)
        if show.status_code != 200:
            raise ValueError(f'HTTP {show.status_code}')
        numbers = [s.get('season_number') for s in show.json().get('seasons') or [] if s.get('season_number') is not None]
        for i in range(0, len(numbers), 20):
            chunk = numbers[i:i + 20]
            r = requests.get(f'https://api.themoviedb.org/3/tv/{sid}', timeout=30,
                             params={'api_key': key, 'append_to_response': ','.join(f'season/{n}' for n in chunk)})
            if r.status_code != 200:
                raise ValueError(f'HTTP {r.status_code}')
            data = r.json()
            for n in chunk:
                for ep in (data.get(f'season/{n}') or {}).get('episodes') or []:
                    if ep.get('season_number') is not None and ep.get('episode_number') is not None:
                        out[(int(ep['season_number']), int(ep['episode_number']))] = ep
    except Exception as e:
        logging.info(f"{_LOG} TMDB episodes for {sid} unavailable: {e}")
        out = None
    with _tmdb_lock:
        _tmdb_cache[sid] = (time.time(), out)
    return out


# ─── TVDB ────────────────────────────────────────────────────────────────────

def _tvdb_cache_path() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'nfo_tvdb_episodes.json')


def _load_tvdb_cache() -> Dict:
    try:
        with open(_tvdb_cache_path(), encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_tvdb_mem: Optional[Dict] = None


def tvdb_episode(tvdb_episode_id) -> Optional[Dict]:
    """TVDB's record of one episode (English title/overview, image,
    characters), cached on disk for 60 days."""
    global _tvdb_mem
    if not tvdb_episode_id:
        return None
    eid = str(tvdb_episode_id)
    with _tvdb_lock:
        if _tvdb_mem is None:
            _tvdb_mem = _load_tvdb_cache()
        hit = _tvdb_mem.get(eid)
        if hit and time.time() - hit.get('t', 0) < _TVDB_TTL:
            return hit.get('d')
    data = None
    try:
        from cli_battery.app import tvdb_client
        if tvdb_client.is_available():
            r = tvdb_client._make_request(f"{tvdb_client.TVDB_BASE_URL}/episodes/{eid}/extended?meta=translations")
            if r is not None and r.status_code == 200:
                raw = r.json().get('data') or {}
                data = {
                    'name': _translation(raw, 'nameTranslations') or raw.get('name'),
                    'overview': _translation(raw, 'overviewTranslations') or raw.get('overview'),
                    'image': raw.get('image'),
                    'characters': [{k: c.get(k) for k in ('name', 'personName', 'peopleType', 'image',
                                                         'personImgURL', 'sort')}
                                   for c in raw.get('characters') or []],
                }
    except Exception as e:
        logging.debug(f"{_LOG} TVDB episode {eid}: {e}")
    with _tvdb_lock:
        _tvdb_mem[eid] = {'t': time.time(), 'd': data}
        try:
            tmp = _tvdb_cache_path() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(_tvdb_mem, f)
            os.replace(tmp, _tvdb_cache_path())
        except OSError as e:
            logging.debug(f"{_LOG} TVDB cache not saved: {e}")
    return data


def _translation(raw: Dict, key: str) -> Optional[str]:
    for t in raw.get(key) or []:
        if isinstance(t, dict) and t.get('language') == 'eng':
            return t.get('name') or t.get('overview')
    return None


# ─── matching ────────────────────────────────────────────────────────────────

def _norm(s) -> str:
    s = re.sub(r'\(\s*(\d+)\s*\)|part\s*(\d+)', lambda m: ' ' + (m.group(1) or m.group(2)), (s or '').lower())
    return re.sub(r'[^a-z0-9]+', '', s)


def _similar(a, b) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _days(a, b) -> Optional[int]:
    try:
        da = date.fromisoformat(str(a)[:10])
        db = date.fromisoformat(str(b)[:10])
        return abs((da - db).days)
    except (TypeError, ValueError):
        return None


def aligned(ep: Dict, tm: Optional[Dict]) -> bool:
    """Is TMDB's episode at the same number the same episode?"""
    if not tm:
        return False
    d = _days(ep.get('aired'), tm.get('air_date'))
    sim = _similar(ep.get('title'), tm.get('name'))
    if d is not None and d <= 2:
        return True
    if d is None and sim >= 0.85:
        return True
    return sim >= 0.95


def find_tmdb_match(ep: Dict, episodes: Dict[Tuple[int, int], Dict]) -> Optional[Dict]:
    """The TMDB episode that is this episode, wherever TMDB numbers it."""
    best, best_score = None, 0.0
    for (_s, _e), tm in episodes.items():
        sim = _similar(ep.get('title'), tm.get('name'))
        d = _days(ep.get('aired'), tm.get('air_date'))
        if d is not None and d <= 1:
            score = 1.0 + sim
        elif sim >= 0.9:
            score = sim
        else:
            continue
        if score > best_score:
            best, best_score = tm, score
    # Same air date alone is ambiguous on days with several episodes: then
    # the title has to agree too
    if best is not None and best_score < 1.5:
        same_day = [tm for tm in episodes.values()
                    if _days(ep.get('aired'), tm.get('air_date')) is not None
                    and _days(ep.get('aired'), tm.get('air_date')) <= 1]
        if len(same_day) > 1 and _similar(ep.get('title'), best.get('name')) < 0.6:
            return None
    return best


def extras_from_tmdb(tm: Dict) -> Dict:
    crew = tm.get('crew') or []
    out = {
        'thumb': f"{TMDB_IMG}{tm['still_path']}" if tm.get('still_path') else None,
        'directors': [c.get('name') for c in crew if c.get('job') == 'Director' and c.get('name')],
        'writers': [c.get('name') for c in crew if c.get('department') == 'Writing' and c.get('name')],
        'actors': [{'name': g.get('name'), 'role': g.get('character'),
                    'thumb': f"{TMDB_IMG}{g['profile_path']}" if g.get('profile_path') else None,
                    'order': g.get('order')}
                   for g in tm.get('guest_stars') or [] if g.get('name')],
    }
    if tm.get('vote_count'):
        out['rating'] = {'name': 'themoviedb', 'value': tm.get('vote_average'), 'votes': tm.get('vote_count')}
    return out


def extras_from_tvdb(rec: Dict) -> Dict:
    chars = rec.get('characters') or []
    return {
        'thumb': rec.get('image'),
        'directors': [c.get('personName') for c in chars if c.get('peopleType') == 'Director' and c.get('personName')],
        'writers': [c.get('personName') for c in chars if c.get('peopleType') == 'Writer' and c.get('personName')],
        'actors': [{'name': c.get('personName'), 'role': c.get('name'), 'thumb': c.get('personImgURL') or c.get('image'),
                    'order': c.get('sort')}
                   for c in chars if c.get('peopleType') in ('Guest Star', 'Actor') and c.get('personName')],
    }


def plan_episodes(episodes: List[Dict], tmdb_id, force_full: bool = False) -> Dict[int, Dict]:
    """index -> {'mode': 'minimal'|'full', **extras} for a show's episodes
    (dicts from nfo_writer._episode_info). Without TMDB data every episode
    is 'full' with no extras (cli_debrid's title, description and date).
    force_full (a per-show override): every episode gets the full details."""
    tm_eps = tmdb_episodes(tmdb_id)
    plans: Dict[int, Dict] = {}
    for i, ep in enumerate(episodes):
        if tm_eps is None:
            plans[i] = {'mode': 'full'}
            continue
        here = tm_eps.get((ep.get('season'), ep.get('episode')))
        if aligned(ep, here) and not force_full:
            plans[i] = {'mode': 'minimal'}
            continue
        plan = {'mode': 'full'}
        match = here if aligned(ep, here) else find_tmdb_match(ep, tm_eps)
        if match:
            plan.update(extras_from_tmdb(match))
        if not plan.get('thumb') or not (plan.get('actors') or plan.get('directors')):
            rec = tvdb_episode(ep.get('tvdb_id'))
            if rec:
                tv = extras_from_tvdb(rec)
                for k in ('thumb', 'directors', 'writers', 'actors'):
                    if not plan.get(k) and tv.get(k):
                        plan[k] = tv[k]
        plans[i] = plan
    return plans

