"""
Tools -> NFO Files -> Check a show: does Plex show each library file's
episode the way cli_debrid numbers it?

For every episode file of the show, the Plex episode at the same
season/episode is compared with cli_debrid's: it counts as right when the
title matches closely or the air dates agree (Plex's own wording differs a
little on episodes it numbers the same way, "Raven Black - Part 1" vs
"Raven Black (1)"). Read-only; fix_show() rewrites the show's NFO files (or,
without NFO files, releases old title locks) and has Plex re-read it.
"""

import os
from typing import Dict, List



def library_shows(query: str = '', limit: int = 30) -> List[Dict]:
    """TV shows with library files, for the picker: imdb_id, title, files,
    episode order and the NFO override."""
    from database.core import get_db_connection
    from utilities import nfo_writer
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT imdb_id, MIN(title), COUNT(DISTINCT location_on_disk) FROM media_items "
            "WHERE type = 'episode' AND state IN ('Collected', 'Upgrading') AND location_on_disk IS NOT NULL "
            "AND location_on_disk != '' AND imdb_id IS NOT NULL AND title LIKE ? "
            "GROUP BY imdb_id ORDER BY MIN(title) LIMIT ?", (f'%{query.strip()}%', int(limit))).fetchall()
    finally:
        conn.close()
    from cli_battery.app.episode_order import show_episode_order
    out = []
    for imdb_id, title, files in rows:
        out.append({'imdb_id': imdb_id, 'title': title, 'files': files,
                    'order': show_episode_order(imdb_id) or 'default',
                    'override': nfo_writer.show_override(imdb_id),
                    'gets_nfos': nfo_writer.wants_show(imdb_id)})
    return out


def _nfo_kind(path: str) -> str:
    nfo = os.path.splitext(path)[0] + '.nfo'
    try:
        with open(nfo, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return 'none'
    return 'full' if '<title>' in text else 'minimal'


def check_show(imdb_id: str, plex=None) -> Dict:
    from utilities import nfo_writer
    from utilities.nfo_sources import _days, _similar
    from utilities.plex_order_metadata import _plex_show
    rows = [r for r in nfo_writer._episode_rows(imdb_id) if os.path.lexists(r['location_on_disk'])]
    out = {'imdb_id': imdb_id, 'files': len(rows), 'override': nfo_writer.show_override(imdb_id),
           'gets_nfos': nfo_writer.wants_show(imdb_id), 'mode': nfo_writer.mode(),
           'right': 0, 'wrong': [], 'missing': [], 'plex_show': None, 'error': None}
    show, problem = _plex_show(imdb_id, plex)
    if show is None:
        out['error'] = problem or 'show not found in Plex'
        return out
    out['plex_show'] = show.title
    if out['gets_nfos']:
        # The TV library must still use the NFO agent (editing the library's
        # settings with an older client can put it back on plain Plex Series)
        try:
            from utilities import plex_nfo_agent
            st = plex_nfo_agent.status()
            out['library_uses_nfo_agent'] = bool(st.get('configured'))
        except Exception as e:
            out['library_uses_nfo_agent'] = None
            out['agent_error'] = str(e)
    plex_eps = {(e.seasonNumber, e.index): e for e in show.episodes()}
    seen = set()
    for r in sorted(rows, key=lambda r: (r['season_number'] or 0, r['episode_number'] or 0)):
        key = (r['season_number'], r['episode_number'])
        if key in seen:
            continue
        seen.add(key)
        label = f"S{(key[0] or 0):02d}E{(key[1] or 0):02d}"
        e = plex_eps.get(key)
        if e is None:
            out['missing'].append({'episode': label, 'expected': r.get('episode_title')})
            continue
        plex_date = e.originallyAvailableAt.strftime('%Y-%m-%d') if e.originallyAvailableAt else None
        d = _days(r.get('release_date'), plex_date)
        if _similar(e.title, r.get('episode_title')) >= 0.8 or (d is not None and d <= 2):
            out['right'] += 1
        else:
            out['wrong'].append({'episode': label, 'plex': e.title, 'expected': r.get('episode_title'),
                                 'nfo': _nfo_kind(r['location_on_disk'])})
    out['checked'] = len(seen)
    return out


def fix_show(imdb_id: str) -> str:
    """Rewrite the show's NFO files (without NFO files: release any title
    locks older versions set) and have Plex re-read the show."""
    from utilities import nfo_writer
    from utilities.plex_order_metadata import _plex_show, release_for_nfo, sync_show
    if nfo_writer.wants_show(imdb_id):
        from utilities import plex_nfo_agent
        agent_note = ''
        if not plex_nfo_agent.status().get('configured'):
            plex_nfo_agent.apply(refresh=False)
            agent_note = 'TV library put back on the NFO agent. '
        c = nfo_writer.write_show(imdb_id)
        note = release_for_nfo(imdb_id)
        return f"{agent_note}NFO files: {c['written']} written, {c['unchanged']} unchanged. {note}".strip()
    removed = nfo_writer.write_show(imdb_id)['removed'] if nfo_writer.enabled() else 0
    show_note = sync_show(imdb_id)
    show, _problem = _plex_show(imdb_id)
    if show is not None and not show_note:
        show.refresh()
    return (f"{removed} NFO file(s) removed. " if removed else '') + (show_note or 'Plex refreshed the show')
