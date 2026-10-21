"""Map a show's episodes from one episode order to another.

Both sides are cli_battery-shaped seasons dicts ({season: {'episodes':
{number: {...}}}}) for the same show. Episodes are joined, strongest key
first:

1. TVDB episode ID (TVDB orders share IDs across season types)
2. TMDB episode ID
3. a TMDB episode-group episode's position in TMDB's default order, but only
   when the current numbering is TMDB's default (Trakt as metadata source)
4. air date + title
5. air date alone, when exactly one episode on each side has it

Each episode is used at most once on each side. An unmatched current episode
is reported as confidently absent from the target only when it has an ID of a
kind the target's episodes carry and that ID isn't there; anything else
unmatched is just unknown (date/title joins can miss). Pure functions, no I/O.
"""
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

SE = Tuple[int, int]


def _date(value: Any) -> Optional[str]:
    text = str(value or '')
    return text[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', text) else None


def _title(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _entries(seasons: Optional[Dict[Any, Any]]) -> List[Dict[str, Any]]:
    entries = []
    for s_num, s_data in (seasons or {}).items():
        try:
            s_num = int(s_num)
        except (TypeError, ValueError):
            continue
        if not isinstance(s_data, dict):
            continue
        for e_num, ep in (s_data.get('episodes') or {}).items():
            try:
                e_num = int(e_num)
            except (TypeError, ValueError):
                continue
            if not isinstance(ep, dict):
                continue
            src = None
            if ep.get('source_season') is not None and ep.get('source_episode') is not None:
                src = (int(ep['source_season']), int(ep['source_episode']))
            entries.append({
                'se': (s_num, e_num),
                'tvdb': str(ep['tvdb_id']) if ep.get('tvdb_id') else None,
                'tmdb': str(ep['tmdb_id']) if ep.get('tmdb_id') else None,
                'src': src,
                'date': _date(ep.get('first_aired')),
                'title': _title(ep.get('title')),
                'name': ep.get('title') or '',
            })
    return entries


def plan_remap(current: Optional[Dict[Any, Any]], target: Optional[Dict[Any, Any]],
               current_is_tmdb_default: bool = False) -> Dict[str, Any]:
    """{'map': {old (s, e): new (s, e)}, 'absent': [old (s, e) confidently not in the target],
    'titles': {new (s, e): title},
    'unmatched_current': [...], 'unmatched_target': [...], 'joined_by': {key: count}}"""
    cur = _entries(current)
    tgt = _entries(target)
    mapping: Dict[SE, SE] = {}
    used_target = set()
    joined_by: Dict[str, int] = defaultdict(int)

    def join(kind, cur_key, tgt_key):
        index = defaultdict(list)
        for t in tgt:
            if t['se'] not in used_target:
                key = tgt_key(t)
                if key is not None:
                    index[key].append(t)
        for c in cur:
            if c['se'] in mapping:
                continue
            key = cur_key(c)
            if key is None:
                continue
            candidates = [t for t in index.get(key, []) if t['se'] not in used_target]
            if len(candidates) == 1:
                mapping[c['se']] = candidates[0]['se']
                used_target.add(candidates[0]['se'])
                joined_by[kind] += 1

    join('tvdb_id', lambda c: c['tvdb'], lambda t: t['tvdb'])
    join('tmdb_id', lambda c: c['tmdb'], lambda t: t['tmdb'])
    if current_is_tmdb_default:
        join('tmdb_default_position', lambda c: c['se'], lambda t: t['src'])
    join('air_date_title',
         lambda c: (c['date'], c['title']) if c['date'] and c['title'] else None,
         lambda t: (t['date'], t['title']) if t['date'] and t['title'] else None)
    # Air date alone only when it identifies a single episode on both sides
    cur_by_date = defaultdict(list)
    for c in cur:
        if c['se'] not in mapping and c['date']:
            cur_by_date[c['date']].append(c)
    tgt_by_date = defaultdict(list)
    for t in tgt:
        if t['se'] not in used_target and t['date']:
            tgt_by_date[t['date']].append(t)
    for date, cs in cur_by_date.items():
        ts = tgt_by_date.get(date, [])
        if len(cs) == 1 and len(ts) == 1:
            mapping[cs[0]['se']] = ts[0]['se']
            used_target.add(ts[0]['se'])
            joined_by['air_date'] += 1

    target_ids = {'tvdb': {t['tvdb'] for t in tgt if t['tvdb']}, 'tmdb': {t['tmdb'] for t in tgt if t['tmdb']}}
    absent = []
    for c in cur:
        if c['se'] in mapping:
            continue
        for kind in ('tvdb', 'tmdb'):
            # The target must carry this kind of ID broadly, not on a stray episode
            if c[kind] and len(target_ids[kind]) * 2 >= len(tgt) and c[kind] not in target_ids[kind]:
                absent.append(c['se'])
                break

    return {
        'map': mapping,
        'absent': sorted(absent),
        'titles': {t['se']: t['name'] for t in tgt},
        'unmatched_current': sorted(c['se'] for c in cur if c['se'] not in mapping),
        'unmatched_target': sorted(t['se'] for t in tgt if t['se'] not in used_target),
        'joined_by': dict(joined_by),
    }
