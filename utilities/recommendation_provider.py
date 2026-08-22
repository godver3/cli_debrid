"""Provider selection for personalized Discover recommendations."""

import logging

from utilities.settings import get_setting


def get_recommendation_provider():
    """Return ``trakt``, ``scrob``, or ``None`` without contacting either service."""
    try:
        from content_checkers.trakt import get_trakt_config

        # Current settings are authoritative. Do not let credentials left in
        # legacy .pytrakt.json select Trakt after it was disabled in Settings.
        client_id = str(get_setting('Trakt', 'client_id', '') or '').strip()
        client_secret = str(get_setting('Trakt', 'client_secret', '') or '').strip()
        access_token = str(get_trakt_config().get('OAUTH_TOKEN', '') or '').strip()
        if client_id and client_secret and access_token:
            return 'trakt'
    except Exception as e:
        logging.debug(f"Recommendations: Trakt availability check failed: {e}")

    try:
        from content_checkers.scrob import get_scrob_config
        if get_scrob_config():
            return 'scrob'
    except Exception as e:
        logging.debug(f"Recommendations: Scrob availability check failed: {e}")

    return None
