"""Provider selection for personalized Discover recommendations."""

import logging

from utilities.settings import get_setting


def get_available_recommendation_providers():
    """Connected providers in default order (``trakt``, ``scrob``, ``library``).

    Checks settings only and never contacts a service. ``library`` builds
    recommendations from the watch history and the collection through TMDB,
    so it only needs the TMDB key.
    """
    providers = []
    try:
        from content_checkers.trakt import get_trakt_config

        # Current settings are authoritative. Do not let credentials left in
        # legacy .pytrakt.json select Trakt after it was disabled in Settings.
        client_id = str(get_setting('Trakt', 'client_id', '') or '').strip()
        client_secret = str(get_setting('Trakt', 'client_secret', '') or '').strip()
        access_token = str(get_trakt_config().get('OAUTH_TOKEN', '') or '').strip()
        if client_id and client_secret and access_token:
            providers.append('trakt')
    except Exception as e:
        logging.debug(f"Recommendations: Trakt availability check failed: {e}")

    try:
        from content_checkers.scrob import get_scrob_config
        if get_scrob_config():
            providers.append('scrob')
    except Exception as e:
        logging.debug(f"Recommendations: Scrob availability check failed: {e}")

    if str(get_setting('TMDB', 'api_key', '') or '').strip():
        providers.append('library')

    return providers


def get_recommendation_provider():
    """The default provider: ``trakt``, ``scrob``, ``library`` or ``None``."""
    providers = get_available_recommendation_providers()
    return providers[0] if providers else None
