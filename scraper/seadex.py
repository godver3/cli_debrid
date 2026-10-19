"""
SeaDex (releases.moe) best-release lookup for anime ranking priority.

Not a scraper — this module does not return scrape results. It resolves an
IMDb ID (and season) to the set of info_hashes SeaDex has confirmed as the best
release for that anime, so the ranking step can boost a matching scraped result
above cli-debrid's normal scoring. See scraper.py's use of get_seadex_hashes().

AniList (and so SeaDex) has one entry per season or cour, while ani.zip maps an
IMDb ID only to the first one. For a later season the AniList SEQUEL chain is
walked from that entry, and ani.zip says which TVDB season each entry covers.
"""
import logging
import time
from typing import List, Optional, Set, Tuple

from routes.api_tracker import api

ANIZIP_URL = "https://api.ani.zip/mappings"
SEADEX_URL = "https://releases.moe/api/collections/entries/records"
ANILIST_URL = "https://graphql.anilist.co"

_SEQUEL_QUERY = """query ($id: Int) { Media(id: $id, type: ANIME) { title { romaji }
  relations { edges { relationType node { id type format } } } } }"""
_SEQUEL_FORMATS = ('TV', 'TV_SHORT', 'ONA')
_MAX_CHAIN = 15  # Long runners can have many cours; also bounds AniList calls (30/min)

_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours - AniList mappings and SeaDex entries change rarely

# imdb_id -> (timestamp, anilist_id or None)
_anilist_id_cache = {}
# anilist_id -> (timestamp, set of info_hash strings, lowercased)
_seadex_hash_cache = {}
# imdb_id -> (timestamp, [(anilist_id, TVDB seasons covered, romaji title)], chain_ended)
_season_chain_cache = {}


def _get_anilist_id(imdb_id: str):
    if not imdb_id:
        return None

    cached = _anilist_id_cache.get(imdb_id)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    # api.get() raises on any non-2xx status (raise_for_status), so a failed
    # lookup (e.g. 404 — no mapping for this IMDb ID) lands in the except below,
    # not as a non-200 response to branch on here.
    anilist_id = None
    try:
        response = api.get(ANIZIP_URL, params={'imdb_id': imdb_id}, timeout=10)
        data = response.json()
        anilist_id = data.get('mappings', {}).get('anilist_id')
    except Exception as e:
        logging.debug(f"[SeaDex] ani.zip lookup failed for {imdb_id}: {e}")

    _anilist_id_cache[imdb_id] = (time.time(), anilist_id)
    return anilist_id


def _anilist_entry(anilist_id: int) -> Tuple[Optional[str], Optional[int]]:
    """(romaji title, next TV sequel's AniList ID) for one AniList entry."""
    response = api.post(ANILIST_URL, json={'query': _SEQUEL_QUERY, 'variables': {'id': anilist_id}}, timeout=10)
    media = (response.json().get('data') or {}).get('Media') or {}
    romaji = (media.get('title') or {}).get('romaji')
    for edge in (media.get('relations') or {}).get('edges') or []:
        node = edge.get('node') or {}
        if (edge.get('relationType') == 'SEQUEL' and node.get('type') == 'ANIME'
                and node.get('format') in _SEQUEL_FORMATS):
            return romaji, node.get('id')
    return romaji, None


def _tvdb_seasons(anilist_id: int) -> Set[int]:
    """Regular TVDB seasons (no specials) ani.zip maps this AniList entry's episodes to."""
    response = api.get(ANIZIP_URL, params={'anilist_id': anilist_id}, timeout=10)
    episodes = (response.json().get('episodes') or {}).values()
    return {ep['seasonNumber'] for ep in episodes
            if isinstance(ep, dict) and isinstance(ep.get('seasonNumber'), int) and ep['seasonNumber'] > 0}


def _season_chain(imdb_id: str, first_id: int, season: int) -> List[Tuple[int, Set[int], Optional[str]]]:
    """The show's AniList entries in order, walked far enough to pass `season`."""
    cached = _season_chain_cache.get(imdb_id)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        chain, ended = cached[1], cached[2]
    else:
        chain, ended = [], False

    def covered_past(entries):
        return any(seasons and min(seasons) > season for _, seasons, _ in entries)

    if not ended and not covered_past(chain) and len(chain) < _MAX_CHAIN:
        # Start at the IMDb ID's entry, or resume after the last one walked
        next_id = _anilist_entry(chain[-1][0])[1] if chain else first_id
        while next_id and len(chain) < _MAX_CHAIN:
            romaji, sequel_id = _anilist_entry(next_id)
            chain.append((next_id, _tvdb_seasons(next_id), romaji))
            if covered_past(chain):
                break
            next_id = sequel_id
        else:
            ended = True
        _season_chain_cache[imdb_id] = (time.time(), chain, ended)
    return chain


def anilist_entries_for_season(imdb_id: str, season: Optional[int]) -> List[Tuple[int, Optional[str]]]:
    """[(AniList ID, romaji title)] for the entries covering this TVDB season.

    A split season (e.g. "Season 3 Part 2") has several. For movies, season 1,
    or any lookup failure this is just the entry ani.zip maps the IMDb ID to.
    """
    first_id = _get_anilist_id(imdb_id)
    if not first_id:
        return []
    if not season or season <= 1:
        return [(first_id, None)]
    try:
        entries = [(aid, romaji) for aid, seasons, romaji in _season_chain(imdb_id, first_id, season)
                   if season in seasons]
    except Exception as e:
        logging.debug(f"[SeaDex] AniList season lookup failed for {imdb_id} S{season}: {e}")
        entries = []
    return entries or [(first_id, None)]


def get_seadex_hashes(imdb_id: str, season: Optional[int] = None) -> Set[str]:
    """Return the set of lowercased info_hashes SeaDex marks as best for this
    anime (and season, for shows). Empty set if not anime, not on SeaDex, or on
    any error — callers must treat this as "no boost available", never as an
    error to surface to the user.
    """
    hashes = set()
    for anilist_id, _ in anilist_entries_for_season(imdb_id, season):
        hashes |= _seadex_hashes_for_anilist_id(anilist_id, imdb_id)
    return hashes


def _seadex_hashes_for_anilist_id(anilist_id: int, imdb_id: str) -> Set[str]:
    cached = _seadex_hash_cache.get(anilist_id)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    hashes = set()
    try:
        response = api.get(
            SEADEX_URL,
            params={'filter': f'(alID={anilist_id})', 'expand': 'trs'},
            timeout=10,
        )
        data = response.json()
        for entry in data.get('items', []):
            for torrent in entry.get('expand', {}).get('trs', []):
                if torrent.get('isBest') and torrent.get('infoHash'):
                    hashes.add(torrent['infoHash'].lower())
    except Exception as e:
        logging.debug(f"[SeaDex] Lookup failed for AniList ID {anilist_id}: {e}")

    _seadex_hash_cache[anilist_id] = (time.time(), hashes)
    if hashes:
        logging.debug(f"[SeaDex] Found {len(hashes)} best-release hash(es) for AniList ID {anilist_id} (IMDb {imdb_id})")
    return hashes
