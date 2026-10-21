"""Add a show's episodes that have no library entry to Wanted."""
import logging
from typing import Dict, Iterable, Optional, Tuple

from database.core import get_db_connection

# A version counts as one the show is collected in when it has at least this
# share of the largest version's episodes (Dragon Ball Kai: 159 x 1080p, 3 x
# "720 and below" -> 1080p only).
MAIN_VERSION_SHARE = 0.5


def show_versions(imdb_id: str) -> Tuple[Optional[str], Optional[str], Dict[str, bool]]:
    """(tmdb_id, title, {version: True}) for the versions the show is collected in."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT version, MAX(tmdb_id) AS tmdb_id, MAX(title) AS title, COUNT(*) AS n FROM media_items "
            "WHERE imdb_id = ? AND type = 'episode' AND version IS NOT NULL AND version != '' "
            "GROUP BY version", (imdb_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None, None, {}
    counts: Dict[str, int] = {}
    for r in rows:  # "1080p*" (mid-upgrade) is still 1080p
        name = r['version'].rstrip('*').strip()
        counts[name] = counts.get(name, 0) + r['n']
    largest = max(counts.values())
    versions = {name: True for name, n in counts.items() if name and n >= largest * MAIN_VERSION_SHARE}
    return rows[0]['tmdb_id'], rows[0]['title'], versions


def queue_missing_episodes(imdb_id: str, source_detail: str,
                           positions: Optional[Iterable[Tuple[int, int]]] = None) -> int:
    """Add the show's episodes that have no entry in its versions to Wanted.
    `positions` limits it to those (season, episode) pairs. Returns how many
    entries were added."""
    from metadata.metadata import process_metadata
    from database.wanted_items import add_wanted_items

    tmdb_id, title, versions = show_versions(imdb_id)
    if not versions:
        return 0
    source = {'content_source': 'content_requester', 'content_source_detail': source_detail}
    processed = process_metadata([dict(source, imdb_id=imdb_id, tmdb_id=tmdb_id, media_type='tv',
                                       title=title, versions=versions)])
    episodes = (processed or {}).get('episodes') or []
    if positions is not None:
        wanted = {(int(s), int(e)) for s, e in positions}
        episodes = [ep for ep in episodes
                    if (ep.get('season_number'), ep.get('episode_number')) in wanted]
    for ep in episodes:
        ep.update(source)
    added = add_wanted_items(episodes, versions) if episodes else 0
    logging.info(f"Queued {added or 0} missing episode(s) of {imdb_id} in {sorted(versions)} ({source_detail})")
    return added or 0
