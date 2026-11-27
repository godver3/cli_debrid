"""
Point Plex's TV libraries at an agent that reads Clyde's NFO files.

Plex (PMS 1.43+) builds agents from "metadata agent provider groups"
(GET/POST /media/providers/metadata/group, documented in the PMS OpenAPI
spec). The built-in agents are groups too ("Plex Series" is the group a TV
library uses by default). This makes one custom group:

    primary:  Plex Series          (items keep their plex:// guids, so watch
                                    state, Discover and collections stay)
    order:    Plex NFO Series      (Clyde's titles/plots/dates win)
              Plex Series          (everything an NFO doesn't say)
              Plex Local Media     (local artwork/tags)

and switches the TV libraries that read the symlinked folder to it
(PUT /library/sections/{id}?agent=&metadataAgentProviderGroupId=). The
previous agent of each library is saved, so restore() puts it back.
Shows without NFO files keep matching through Plex Series as before.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from utilities.settings import get_setting

_LOG = '[PlexNFOAgent]'
GROUP_TITLE = 'Clyde NFO + Plex Series'
LEGACY_GROUP_TITLES = ('cli_debrid NFO + Plex Series',)
SERIES = 'tv.plex.agents.series'
NFO_SERIES = 'tv.plex.agents.nfo.series'
LOCAL_MEDIA = 'tv.plex.agents.localmedia'
DEFAULT_SERIES_GROUP_TITLE = 'Plex Series'


def _state_path() -> str:
    from utilities.settings import get_config_dir
    return os.path.join(get_config_dir(), 'plex_nfo_agent.json')


def _load_state() -> Dict:
    try:
        with open(_state_path(), encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict) -> None:
    path = _state_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ─── Plex calls ──────────────────────────────────────────────────────────────

def _server():
    from utilities.episode_order_change import _plex_server
    return _plex_server()


def _q(plex, path: str, method: str = 'get', params: Optional[List[Tuple[str, str]]] = None):
    if params:
        path = f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"
    fn = getattr(plex._session, method)
    return plex.query(path, method=fn)


def _providers(plex) -> Dict[str, int]:
    """identifier -> provider id."""
    root = _q(plex, '/media/providers/metadata')
    return {p.get('identifier'): int(p.get('id')) for p in root.iter('MetadataAgentProvider')
            if p.get('identifier') and p.get('id')}


def _groups(plex) -> List[Dict]:
    root = _q(plex, '/media/providers/metadata/group')
    out = []
    for g in root.iter('MetadataAgentProviderGroup'):
        items = sorted(({'id': int(i.get('id')), 'provider': int(i.get('metadataAgentProviderId')),
                         'order': float(i.get('order') or 0)}
                        for i in g if i.tag.startswith('MetadataAgent') and i.get('id')),
                       key=lambda i: i['order'])
        out.append({'id': int(g.get('id')), 'title': g.get('title') or '',
                    'primary': g.get('primaryIdentifier') or '', 'builtIn': g.get('builtIn') in ('1', 'true'),
                    'items': items})
    return out


def _group_by_id(plex, gid: int) -> Optional[Dict]:
    return next((g for g in _groups(plex) if g['id'] == gid), None)


def _wanted_order(providers: Dict[str, int]) -> List[int]:
    return [providers[NFO_SERIES], providers[SERIES]] + ([providers[LOCAL_MEDIA]] if LOCAL_MEDIA in providers else [])


def ensure_group(plex=None) -> Dict:
    """The Clyde NFO group, created or repaired so its providers are
    NFO Series, Plex Series, Local Media in that order."""
    plex = plex or _server()
    if plex is None:
        raise RuntimeError('Plex is not configured')
    providers = _providers(plex)
    missing = [i for i in (SERIES, NFO_SERIES) if i not in providers]
    if missing:
        raise RuntimeError(f"this Plex server doesn't offer the {', '.join(missing)} agent. It needs Plex Media "
                           "Server 1.43.1 or newer, and reached Plex Pass (beta) servers first; a freshly "
                           "claimed server only lists it after a restart. The NFO files are written anyway: "
                           "Jellyfin, Emby and Kodi read them without this step")
    want = _wanted_order(providers)

    state = _load_state()
    group = _group_by_id(plex, state['group_id']) if state.get('group_id') else None
    if group is None:
        group = next((g for g in _groups(plex)
                      if g['title'] in (GROUP_TITLE,) + LEGACY_GROUP_TITLES and not g['builtIn']), None)
    if group is None:
        _q(plex, '/media/providers/metadata/group', 'post',
           [('title', GROUP_TITLE), ('primaryIdentifier', SERIES)])
        group = next((g for g in _groups(plex) if g['title'] == GROUP_TITLE and not g['builtIn']), None)
        if group is None:
            raise RuntimeError('Plex did not create the agent')
        logging.info(f"{_LOG} created Plex agent '{GROUP_TITLE}' (group {group['id']})")

    gid = group['id']
    have = {i['provider']: i['id'] for i in group['items']}
    for pid in want:
        if pid not in have:
            _q(plex, f'/media/providers/metadata/group/{gid}/items/{pid}', 'put')
    group = _group_by_id(plex, gid)
    item_of = {i['provider']: i['id'] for i in group['items']}
    # Order: each wanted item placed after the previous one; the first at -1.
    if [i['provider'] for i in group['items'] if i['provider'] in want] != want:
        prev = -1
        for pid in want:
            _q(plex, f'/media/providers/metadata/group/{gid}/items/{pid}', 'put', [('after', str(prev))])
            prev = item_of[pid]
        group = _group_by_id(plex, gid)
    order = [i['provider'] for i in group['items'] if i['provider'] in want]
    if order != want:
        raise RuntimeError(f'Plex kept the agent providers in the wrong order ({order}, wanted {want})')
    state['group_id'] = gid
    _save_state(state)
    return group


def _norm(p: str) -> str:
    return os.path.normpath(p or '').rstrip('/\\').replace('\\', '/')


def tv_sections(plex=None) -> List:
    """TV libraries that read Clyde's symlinked folder. Plex may see
    the folder under another path (another container): then a location
    ending in the same last two folders counts too."""
    plex = plex or _server()
    root = _norm(get_setting('File Management', 'symlinked_files_path', ''))
    if not root:
        return []
    tail = '/'.join(root.split('/')[-2:])
    out = []
    for s in plex.library.sections():
        if s.type != 'show':
            continue
        for loc in s.locations:
            loc = _norm(loc)
            if loc == root or loc.startswith(root + '/') or root.startswith(loc + '/') \
                    or (tail and ('/' + tail + '/') in ('/' + loc + '/')):
                out.append(s)
                break
    return out


def _section_group(plex, key) -> Tuple[str, Optional[int]]:
    """A library's agent and group, from the section list (GET
    /library/sections/{id} lists the library's contents, not its settings)."""
    return _sections_with_group(plex).get(str(key), ('', None))


def _sections_with_group(plex) -> Dict[str, Tuple[str, Optional[int]]]:
    out = {}
    root = _q(plex, '/library/sections')
    for d in root.iter('Directory'):
        gid = d.get('metadataAgentProviderGroupId')
        out[d.get('key')] = (d.get('agent') or '', int(gid) if gid else None)
    return out


def _set_section(plex, section, agent: str, gid: Optional[int]) -> None:
    params = [('agent', agent)]
    if gid is not None:
        params.append(('metadataAgentProviderGroupId', str(gid)))
    _q(plex, f'/library/sections/{section.key}', 'put', params)


def applied() -> bool:
    """Clyde switched at least one TV library to its NFO agent (and hasn't
    put it back). From the saved state: no Plex request."""
    state = _load_state()
    previous = state.get('previous') or {}
    return bool(state.get('group_id') and any(
        isinstance(v, dict) and v.get('switched', True) for v in previous.values()
    ))


def status(plex=None) -> Dict:
    """For the UI: is the agent set up, and which TV libraries use it."""
    out = {'configured': False, 'group_id': None, 'sections': [], 'error': None}
    try:
        plex = plex or _server()
        if plex is None:
            out['error'] = 'Plex is not configured'
            return out
        state = _load_state()
        gid = state.get('group_id')
        out['group_id'] = gid
        current = _sections_with_group(plex)
        for s in tv_sections(plex):
            agent, sgid = current.get(str(s.key), ('', None))
            out['sections'].append({'key': s.key, 'title': s.title, 'agent': agent, 'group_id': sgid,
                                    'uses_nfo_agent': gid is not None and sgid == gid,
                                    'locations': list(s.locations)})
        out['configured'] = bool(out['sections']) and all(x['uses_nfo_agent'] for x in out['sections'])
    except Exception as e:
        out['error'] = str(e)
    return out


def apply(refresh: bool = True, plex=None) -> Dict:
    """Make the NFO agent and switch the symlinked TV libraries to it.
    refresh: ask Plex to refresh each switched library's metadata so the
    shows already there pick the NFOs up."""
    plex = plex or _server()
    if plex is None:
        raise RuntimeError('Plex is not configured (File Management → Plex URL/token for symlinks)')
    group = ensure_group(plex)
    gid = group['id']
    sections = tv_sections(plex)
    if not sections:
        raise RuntimeError('no Plex TV library reads the symlinked folder '
                           f"({get_setting('File Management', 'symlinked_files_path', '')})")
    state = _load_state()
    previous = state.setdefault('previous', {})
    current = _sections_with_group(plex)
    done = []
    for s in sections:
        agent, sgid = current.get(str(s.key), (s.agent, None))
        locations_before = sorted(_norm(x) for x in s.locations)
        if sgid != gid:
            previous[str(s.key)] = {'agent': agent, 'group_id': sgid, 'title': s.title, 'switched': False}
            _save_state(state)
            _set_section(plex, s, SERIES, gid)
        new_agent, new_gid = _section_group(plex, s.key)
        if new_gid != gid:
            raise RuntimeError(f"Plex did not switch '{s.title}' to the NFO agent (still group {new_gid})")
        if str(s.key) in previous and not previous[str(s.key)].get('switched'):
            previous[str(s.key)]['switched'] = True
            _save_state(state)
        s.reload()
        if sorted(_norm(x) for x in s.locations) != locations_before:
            raise RuntimeError(f"Plex changed the folders of '{s.title}' while switching agents; check the library")
        if refresh:
            _q(plex, f'/library/sections/{s.key}/refresh', 'get', [('force', '1')])
        done.append(s.title)
        logging.info(f"{_LOG} '{s.title}' now uses '{GROUP_TITLE}' (group {gid})"
                     + ('; full metadata refresh started' if refresh else ''))
    return {'group_id': gid, 'sections': done}


def restore(refresh: bool = True, plex=None) -> Dict:
    """Put each switched library back on the agent it had before."""
    plex = plex or _server()
    if plex is None:
        raise RuntimeError('Plex is not configured')
    state = _load_state()
    previous = state.get('previous') or {}
    gid = state.get('group_id')
    current = _sections_with_group(plex)
    default_gid = next((g['id'] for g in _groups(plex) if g['builtIn'] and g['primary'] == SERIES), None)
    restored = []
    for s in plex.library.sections():
        if s.type != 'show':
            continue
        key = str(s.key)
        _agent, sgid = current.get(key, ('', None))
        if gid is None or sgid != gid:
            previous.pop(key, None)
            continue
        prev = previous.get(key) or {'agent': SERIES, 'group_id': default_gid}
        _set_section(plex, s, prev.get('agent') or SERIES, prev.get('group_id') or default_gid)
        if refresh:
            _q(plex, f'/library/sections/{s.key}/refresh', 'get', [('force', '1')])
        restored.append(s.title)
        previous.pop(key, None)
        logging.info(f"{_LOG} '{s.title}' back on its previous agent")
    state['previous'] = previous
    _save_state(state)
    return {'sections': restored}
