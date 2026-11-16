import logging
from typing import Optional, List
from utilities.settings import get_setting, ensure_settings_file
from .base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
from .real_debrid import RealDebridProvider
from .alldebrid import AllDebridProvider
from .premiumize import PremiumizeProvider
from .torbox import TorboxProvider
from .debridlink import DebridLinkProvider
from .highway import HighwayProvider
from .common import (
    extract_hash_from_magnet,
    download_and_extract_hash,
    timed_lru_cache,
    torrent_to_magnet,
    is_video_file,
    is_unwanted_file
)

_provider_instance: Optional[DebridProvider] = None
_provider_list: Optional[List[DebridProvider]] = None

_NAME_MAP = {
    'realdebrid':  'Real-Debrid',
    'alldebrid':   'AllDebrid',
    'torbox':      'Torbox',
    'premiumize':  'Premiumize',
    'debridlink':  'Debrid-Link',
    'debrid-link': 'Debrid-Link',
    'highway':     'HighWay',
}


def _instantiate_provider(provider_name_raw: str, api_key: Optional[str] = None) -> DebridProvider:
    """Instantiate a provider by name, optionally overriding its API key."""
    provider_name = provider_name_raw.lower().strip()
    logging.info(f"[DEBRID FACTORY] Instantiating provider: {provider_name_raw}")
    if provider_name == 'realdebrid':
        p = RealDebridProvider()
    elif provider_name == 'alldebrid':
        p = AllDebridProvider()
    elif provider_name == 'premiumize':
        p = PremiumizeProvider()
    elif provider_name == 'torbox':
        p = TorboxProvider()
    elif provider_name in ('debridlink', 'debrid-link'):
        p = DebridLinkProvider()
    elif provider_name == 'highway':
        p = HighwayProvider()
    else:
        raise ValueError(f"Unknown debrid provider: {provider_name_raw}")
    if api_key:
        p._api_key = api_key
    return p


def get_debrid_provider() -> Optional[DebridProvider]:
    """Return the primary configured debrid provider (singleton, backward-compatible).
    Returns None if no debrid provider is configured (usenet-only setup)."""
    global _provider_instance
    if _provider_instance is not None:
        return _provider_instance
    ensure_settings_file()
    provider_name_raw = get_setting("Debrid Provider", "provider", "")
    if not provider_name_raw or not provider_name_raw.strip():
        return None
    try:
        _provider_instance = _instantiate_provider(provider_name_raw)
        return _provider_instance
    except ValueError:
        return None


def get_debrid_providers() -> List[DebridProvider]:
    """Return all configured debrid providers in priority order.

    Index 0 is always the primary provider.  Additional fallback providers
    come from ``Debrid Provider.fallback_providers`` — a list of dicts with
    ``provider`` and ``api_key`` keys stored in config.json.

    Falls back gracefully to a single-element list when no fallbacks are set.
    """
    global _provider_list
    if _provider_list is not None:
        return _provider_list

    primary = get_debrid_provider()
    # primary is None on a usenet-only setup (no debrid key); never put None in
    # the chain — downstream iterates/derefs these providers.
    providers = [primary] if primary is not None else []

    fallbacks = get_setting("Debrid Provider", "fallback_providers", []) or []
    for fb in fallbacks:
        if not isinstance(fb, dict):
            continue
        name = fb.get("provider", "")
        key  = fb.get("api_key", "")
        if not name or not key:
            continue
        try:
            p = _instantiate_provider(name, api_key=key)
            providers.append(p)
            logging.info(f"[DEBRID FACTORY] Fallback provider loaded: {name}")
        except Exception as e:
            logging.warning(f"[DEBRID FACTORY] Could not load fallback provider '{name}': {e}")

    _provider_list = providers
    logging.info(f"[DEBRID FACTORY] Provider chain: {[p.PROVIDER_NAME for p in providers]}")
    return _provider_list


def get_provider_by_name(provider_name: Optional[str]) -> Optional[DebridProvider]:
    """Return the already-instantiated provider from the chain whose
    PROVIDER_NAME matches, or None.

    Resolves against get_debrid_providers() rather than building a fresh
    instance so the fallback's configured api_key comes along with it -- a
    bare _instantiate_provider(name) would come back holding the primary's
    key (or none at all) and 401 on every call.
    """
    if not provider_name:
        return None
    wanted = provider_name.strip().lower()
    if not wanted:
        return None
    for p in get_debrid_providers():
        if p.PROVIDER_NAME.strip().lower() == wanted:
            return p
    # Also accept the raw settings spelling ('realdebrid' -> 'Real-Debrid')
    mapped = _NAME_MAP.get(wanted)
    if mapped:
        for p in get_debrid_providers():
            if p.PROVIDER_NAME.strip().lower() == mapped.strip().lower():
                return p
    return None


def provider_for_item(item, default=None) -> Optional[DebridProvider]:
    """The provider that holds this item's torrent.

    media_items.debrid_provider records which provider in the chain added it; fall
    back to ``default`` (or the primary) when it is empty or no longer configured.
    """
    name = None
    if isinstance(item, dict):
        name = item.get('debrid_provider')
    elif isinstance(item, str):
        name = item
    provider = get_provider_by_name(name) if name else None
    if provider is not None:
        return provider
    return default if default is not None else get_debrid_provider()


def provider_holding_torrent(torrent_id) -> Optional[DebridProvider]:
    """The provider in the chain that has this torrent ID, for callers that don't
    know which provider added it. With a single provider this is just the primary."""
    providers = get_debrid_providers()
    if len(providers) <= 1:
        return providers[0] if providers else None
    for provider in providers:
        try:
            if provider.get_torrent_info(torrent_id):
                return provider
        except Exception as e:
            logging.debug(f"[DEBRID FACTORY] {provider.PROVIDER_NAME} has no torrent {torrent_id}: {e}")
    return providers[0]


def reset_provider() -> None:
    """Reset all provider instances, forcing reinitialization on next use."""
    global _provider_instance, _provider_list
    _provider_instance = None
    _provider_list = None


def get_provider_display_name() -> str:
    """Return the human-readable display name for the primary debrid provider."""
    if _provider_instance is not None:
        return _provider_instance.PROVIDER_NAME
    provider_name = get_setting("Debrid Provider", "provider", "").lower()
    return _NAME_MAP.get(provider_name, provider_name.title() or 'Debrid')

# Export public interface
__all__ = [
    'get_debrid_provider',
    'get_debrid_providers',
    'get_provider_by_name',
    'provider_for_item',
    'provider_holding_torrent',
    'get_provider_display_name',
    'reset_provider',
    'DebridProvider',
    'TooManyDownloadsError',
    'ProviderUnavailableError',
    'RealDebridProvider',
    'AllDebridProvider',
    'PremiumizeProvider',
    'TorboxProvider',
    'DebridLinkProvider',
    'HighwayProvider',
    'extract_hash_from_magnet',
    'download_and_extract_hash',
    'timed_lru_cache',
    'torrent_to_magnet',
    'is_video_file',
    'is_unwanted_file'
]
