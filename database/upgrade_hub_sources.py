"""
Upgrade Hub — which sources a scan reads (Bulk Sources), and cache status for
the candidates it finds.
"""

import logging
from typing import Dict, List, Optional

from utilities.settings import get_setting

logger = logging.getLogger(__name__)

# Bulk Sources (Upgrade Hub.upgrade_source).
UPGRADE_SOURCES = ('both', 'zilean_only', 'nzb_only')
DEFAULT_UPGRADE_SOURCE = 'both'


def upgrade_source() -> str:
    """The Bulk Sources setting; unset or unknown means both (Zilean + NZB)."""
    raw = get_setting('Upgrade Hub', 'upgrade_source', None)
    return raw if raw in UPGRADE_SOURCES else DEFAULT_UPGRADE_SOURCE


def _result_hash(r: Dict) -> str:
    link = r.get('magnet') or ''
    if not link.startswith('magnet:'):
        return ''
    try:
        from debrid.common import extract_hash_from_magnet
        return (extract_hash_from_magnet(link) or '').lower()
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Cache status
# ---------------------------------------------------------------------------

def _provider_key() -> str:
    raw = get_setting('Debrid Provider', 'provider', '') or ''
    return raw.strip().lower().replace('-', '').replace(' ', '')


def annotate_cache_status(candidates: List[Dict]) -> Optional[str]:
    """Set ``cached`` (True/False/None) and ``cache_service`` on torrent candidates.

    Uses whatever can answer without adding the torrent to the account:
      * TorBox — its checkcached endpoint, in batches.
      * Real-Debrid — Phalanx community cache data, when Phalanx is enabled.
    Other providers (AllDebrid, Debrid-Link, Premiumize) and usenet candidates
    can only be checked by adding them, so they stay None and are checked when
    queued. Returns the name of the service that answered, or None.
    """
    for c in candidates:
        c['cached'] = None
        c.pop('cache_service', None)

    def _hash(c):
        h = (c.get('new_info_hash') or '').lower()
        if h:
            return h
        return _result_hash({'magnet': c.get('new_magnet') or ''})

    torrent = [(c, _hash(c)) for c in candidates if c.get('protocol') != 'nzb']
    torrent = [(c, h) for c, h in torrent if h]
    if not torrent:
        return None
    hashes = sorted({h for _, h in torrent})

    status: Dict[str, Optional[bool]] = {}
    service: Optional[str] = None
    provider_key = _provider_key()
    try:
        if provider_key == 'torbox':
            from debrid import get_debrid_provider
            provider = get_debrid_provider()
            if provider is not None:
                for i in range(0, len(hashes), 50):
                    chunk = hashes[i:i + 50]
                    res = provider.is_cached_sync([f'magnet:?xt=urn:btih:{h}' for h in chunk],
                                                  skip_phalanx_db=True)
                    if isinstance(res, dict):
                        for h in chunk:
                            v = res.get(h)
                            status[h] = v if isinstance(v, bool) else None
                service = 'TorBox'
        elif provider_key == 'realdebrid' and get_setting('UI Settings', 'enable_phalanx_db', default=False):
            from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
            cache_map = PhalanxDBClassManager().get_multi_cache_status(hashes)
            for h in hashes:
                st = cache_map.get(h)
                status[h] = bool(st['is_cached']) if st is not None else None
            service = 'Real-Debrid'
    except Exception as e:
        logger.debug(f"[UPGRADE_HUB] Cache check skipped: {e}")
        return None

    if service:
        for c, h in torrent:
            c['cached'] = status.get(h)
            c['cache_service'] = service
        known = [v for v in status.values() if v is not None]
        logger.info(f"[UPGRADE_HUB] {service} cache: {sum(known)} cached, "
                    f"{len(known) - sum(known)} not cached, {len(hashes) - len(known)} unknown")
    return service
