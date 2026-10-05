"""Acquisition backends are alternatives, not global prerequisites.

Only the existing scheduled connectivity check contacts providers. Queue-side
readers consume its in-memory verdicts; they never make extra health requests.
Unknown subscription data is unavailable, not proof of expiry. Provider
instances are tracked separately, including two accounts of the same type.
"""

import logging
import math
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_checks_lock = threading.Lock()
_availability = {}


def provider_available(provider):
    if provider is None:
        return False
    with _lock:
        # Before the first check, preserve the application's existing behavior.
        return _availability.get(id(provider), not _availability)


def usenet_available():
    with _lock:
        return _availability.get('usenet', True)


def usable_results(results, providers):
    torrent_ok = any(provider_available(p) for p in providers)
    nzb_ok = usenet_available()
    return [r for r in results if (
        nzb_ok if r.get('protocol') == 'nzb' or r.get('nzb_url') else torrent_ok
    )]


def subscription_failure(info, now=None):
    """Return an error kind, or None for a conclusively usable subscription."""
    if not isinstance(info, dict) or not info or info.get('error'):
        return 'SUBSCRIPTION_UNKNOWN'
    premium = info.get('premium')
    expiration = info.get('expiration')
    now = now or datetime.now(timezone.utc)
    if expiration:
        try:
            expiry = datetime.fromisoformat(str(expiration).replace('Z', '+00:00'))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= now:
                return 'SUBSCRIPTION_EXPIRED'
            # A positive exact lifetime covers the last partial day (days=0).
            return None if premium is True else 'SUBSCRIPTION_UNKNOWN'
        except (ValueError, TypeError, OverflowError):
            return 'SUBSCRIPTION_UNKNOWN'
    days = info.get('days_remaining')
    numeric = isinstance(days, (int, float)) and not isinstance(days, bool) and math.isfinite(days)
    if premium is False and numeric and days <= 0:
        return 'SUBSCRIPTION_EXPIRED'
    if numeric and days > 0 and premium is not False:
        return None
    # Providers can explicitly report lifetime/unlimited premium accounts.
    if premium is True and days is None:
        return None
    return 'SUBSCRIPTION_UNKNOWN'


def check_acquisition_providers(get_setting, providers_factory=None, usenet_factory=None):
    """Check every configured backend and publish verdicts atomically.

    Returned failures are blocking only when *no* backend is usable. Degraded
    alternatives remain observable in sanitized logs without pausing healthy
    acquisition. Shared prerequisites stay in check_service_connectivity.
    """
    with _checks_lock:
        return _check_acquisition_providers(get_setting, providers_factory, usenet_factory)


def _check_acquisition_providers(get_setting, providers_factory, usenet_factory):
    if providers_factory is None:
        from debrid import get_debrid_providers
        providers_factory = get_debrid_providers
    if usenet_factory is None:
        from usenet import get_usenet_client
        usenet_factory = get_usenet_client

    primary = bool(get_setting('Debrid Provider', 'provider', '') and
                   get_setting('Debrid Provider', 'api_key', ''))
    fallbacks = get_setting('Debrid Provider', 'fallback_providers', []) or []
    debrid_configured = primary or any(isinstance(f, dict) and f.get('provider') and
                                       f.get('api_key') for f in fallbacks)
    nzb_configured = bool(get_setting('Usenet Provider', 'enabled', False))
    statuses = {'usenet': False, 'checked': True}
    failures = []
    healthy = 0

    def failure(service, kind):
        # Never propagate exception strings: they may contain URLs/tokens.
        detail = {'service': service, 'type': kind, 'status_code': None,
                  'message': ('Subscription expiry confirmed' if kind == 'SUBSCRIPTION_EXPIRED'
                              else 'Provider unavailable or account status unverified')}
        failures.append(detail)
        logging.warning('Acquisition backend degraded: %s (%s)', service, kind)

    if debrid_configured:
        try:
            providers = list(providers_factory() or [])
        except Exception:
            providers = []
        if not providers:
            failure('Debrid Provider API', 'CONFIG_ERROR')
        for provider in providers:
            service = getattr(provider, 'PROVIDER_NAME', 'Debrid Provider')
            statuses[id(provider)] = False
            try:
                ok, detail = provider.check_connectivity()
                if not ok:
                    kind = detail.get('type', 'CONNECTION_ERROR') if isinstance(detail, dict) else 'CONNECTION_ERROR'
                    failure(service, kind)
                    continue
                subscription = getattr(provider, 'get_subscription_status', None)
                kind = subscription_failure(subscription()) if callable(subscription) else None
                if kind:
                    failure(service, kind)
                    continue
                statuses[id(provider)] = True
                healthy += 1
            except Exception:
                failure(service, 'CONNECTION_ERROR')

    if nzb_configured:
        if not get_setting('Usenet Provider', 'url', ''):
            failure('Usenet Provider', 'CONFIG_ERROR')
        else:
            try:
                client = usenet_factory()
                ok, _ = client.check_connectivity()
                if ok:
                    statuses['usenet'] = True
                    healthy += 1
                else:
                    failure('Usenet Provider', 'CONNECTION_ERROR')
            except Exception:
                failure('Usenet Provider', 'CONNECTION_ERROR')

    with _lock:
        _availability.clear()
        _availability.update(statuses)
    if healthy:
        return True, []
    if not debrid_configured and not nzb_configured:
        return False, [{'service': 'Provider', 'type': 'NO_PROVIDER_CONFIGURED',
                        'message': 'Configure at least one acquisition provider'}]
    return False, failures
