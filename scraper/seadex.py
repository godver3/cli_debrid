"""
SeaDex (releases.moe) best-release lookup for anime ranking priority.

Not a scraper — this module does not return scrape results. It resolves an
IMDb ID to the set of info_hashes SeaDex has confirmed as the best release for
that anime, so the ranking step can boost a matching scraped result above
cli-debrid's normal scoring. See rank_results.py's use of get_seadex_hashes().
"""
import logging
import time
from typing import Set

from routes.api_tracker import api

ANIZIP_URL = "https://api.ani.zip/mappings"
SEADEX_URL = "https://releases.moe/api/collections/entries/records"

_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours - AniList mappings and SeaDex entries change rarely

# imdb_id -> (timestamp, anilist_id or None)
_anilist_id_cache = {}
# anilist_id -> (timestamp, set of info_hash strings, lowercased)
_seadex_hash_cache = {}


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


def get_seadex_hashes(imdb_id: str) -> Set[str]:
    """Return the set of lowercased info_hashes SeaDex marks as best for this
    anime's IMDb ID. Empty set if not anime, not on SeaDex, or on any error —
    callers must treat this as "no boost available", never as an error to
    surface to the user.
    """
    anilist_id = _get_anilist_id(imdb_id)
    if not anilist_id:
        return set()

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
