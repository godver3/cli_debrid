import logging
from typing import Optional, List
from utilities.settings import get_setting, ensure_settings_file
from .base import DebridProvider, TooManyDownloadsError, ProviderUnavailableError
from .real_debrid import RealDebridProvider
from .alldebrid import AllDebridProvider
from .premiumize import PremiumizeProvider
from .torbox import TorboxProvider
from .debridlink import DebridLinkProvider
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
    'extract_hash_from_magnet',
    'download_and_extract_hash',
    'timed_lru_cache',
    'torrent_to_magnet',
    'is_video_file',
    'is_unwanted_file'
]
