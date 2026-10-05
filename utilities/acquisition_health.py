"""Acquisition backends are alternatives, not global prerequisites.

Only the existing scheduled connectivity check contacts providers. Queue-side
readers consume its in-memory verdicts; they never make extra health requests.
Unknown subscription data is unavailable, not proof of expiry. Provider
instances are tracked separately, including two accounts of the same type.
"""

import hashlib
import logging
import math
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_checks_lock = threading.Lock()
_availability = {}
_deferred_logged = set()


def _provider_key(provider):
    """Identify a provider by type and account rather than by object.

    reset_provider() builds new instances on every settings save, so an id()-keyed
    verdict would call any instance a queue still held from before "unavailable".
    """
    name = str(getattr(provider, 'PROVIDER_NAME', type(provider).__name__)).strip().lower()
    try:
        # The property loads a lazily-unset key from settings, so the identity is the
        # same before and after the provider first uses it.
        api_key = str(getattr(provider, 'api_key', '') or '')
    except Exception:
        api_key = ''
    return f"{name}:{hashlib.sha256(api_key.encode()).hexdigest()[:12]}"


def provider_available(provider):
    if provider is None:
        return False
    with _lock:
        # Before the first check, preserve the application's existing behavior.
        return _availability.get(_provider_key(provider), not _availability)


def usenet_available():
    with _lock:
        return _availability.get('usenet', True)


def log_deferred(item_identifier, reason):
    """Log a deferred Adding item at INFO once per outage, then at DEBUG.

    A deferred item never fails while its backend is down, so without this the
    only trace of an item waiting in Adding was a DEBUG line every tick.
    """
    with _lock:
        first = item_identifier not in _deferred_logged
        _deferred_logged.add(item_identifier)
    message = f"Adding deferred for {item_identifier}: {reason}; keeping its releases until the backend recovers"
    if first:
        logging.info(message)
    else:
        logging.debug(message)


def usable_results(results, providers):
    torrent_ok = any(provider_available(p) for p in providers)
    nzb_ok = usenet_available()
    return [r for r in results if (
        nzb_ok if r.get('protocol') == 'nzb' or r.get('nzb_url') else torrent_ok
    )]


def subscription_failure(info, now=None):
    """Return an error kind, or None for a usable subscription.

    Missing or unreadable account data is SUBSCRIPTION_UNKNOWN (the provider is
    unavailable), never proof of expiry. A positive day count is enough on its own,
    as before: some providers report premium=False for accounts with time left
    (TorBox derives it from is_subscribed).
    """
    if not isinstance(info, dict) or not info or info.get('error'):
        return 'SUBSCRIPTION_UNKNOWN'
    premium = info.get('premium')
    expiration = info.get('expiration')
    now = now or datetime.now(timezone.utc)
    bad_expiration = False
    if expiration:
        try:
            expiry = datetime.fromisoformat(str(expiration).replace('Z', '+00:00'))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            # An exact future expiry also covers the last partial day (days_remaining=0).
            return 'SUBSCRIPTION_EXPIRED' if expiry <= now else None
        except (ValueError, TypeError, OverflowError):
            bad_expiration = True  # Fall back to the day count.
    days = info.get('days_remaining')
    numeric = isinstance(days, (int, float)) and not isinstance(days, bool) and math.isfinite(days)
    if numeric:
        return None if days > 0 else 'SUBSCRIPTION_EXPIRED'
    # Providers can explicitly report lifetime/unlimited premium accounts: premium
    # with no day count at all, not a garbled one.
    if premium is True and days is None and not bad_expiration:
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

    def failure(service, kind, message=None, status_code=None):
        # Keep the real reason (bad key vs. network vs. expiry) for the UI and logs,
        # scrubbed of configured secrets since provider errors can echo URLs/tokens.
        from utilities.log_redaction import scrub
        default = ('Subscription expired' if kind == 'SUBSCRIPTION_EXPIRED'
                   else 'Provider unavailable or account status unverified')
        detail = {'service': service, 'type': kind, 'status_code': status_code,
                  'message': scrub(str(message)) if message else default}
        failures.append(detail)
        logging.warning('Acquisition backend degraded: %s (%s): %s', service, kind, detail['message'])

    if debrid_configured:
        try:
            providers = list(providers_factory() or [])
        except Exception:
            providers = []
        if not providers:
            failure('Debrid Provider API', 'CONFIG_ERROR')
        for provider in providers:
            service = getattr(provider, 'PROVIDER_NAME', 'Debrid Provider')
            statuses[_provider_key(provider)] = False
            try:
                ok, detail = provider.check_connectivity()
                if not ok:
                    detail = detail if isinstance(detail, dict) else {}
                    failure(service, detail.get('type', 'CONNECTION_ERROR'),
                            detail.get('message'), detail.get('status_code'))
                    continue
                subscription = getattr(provider, 'get_subscription_status', None)
                info = subscription() if callable(subscription) else None
                kind = subscription_failure(info) if callable(subscription) else None
                if isinstance(info, dict):
                    logging.info(f"{service} subscription status: days_remaining={info.get('days_remaining')}, "
                                 f"premium={info.get('premium')}, expiration={info.get('expiration')}")
                if kind:
                    summary = (f"days_remaining={info.get('days_remaining')}, premium={info.get('premium')}, "
                               f"expiration={info.get('expiration')}" if isinstance(info, dict) else 'no account data')
                    if isinstance(info, dict) and info.get('error'):
                        summary = f"{info['error']} ({summary})"
                    failure(service, kind, f"{service} subscription "
                            f"{'expired' if kind == 'SUBSCRIPTION_EXPIRED' else 'status could not be verified'}: {summary}")
                    continue
                statuses[_provider_key(provider)] = True
                healthy += 1
            except Exception as e:
                failure(service, 'CONNECTION_ERROR', f'Connectivity check error: {e}')

    if nzb_configured:
        usenet_url = get_setting('Usenet Provider', 'url', '')
        if not usenet_url:
            failure('Usenet Provider', 'CONFIG_ERROR', 'Usenet provider is enabled but no URL is configured')
        else:
            try:
                client = usenet_factory()
                label = getattr(client, 'PROVIDER_NAME', 'Usenet Provider')
                ok, err = client.check_connectivity()
                if ok:
                    statuses['usenet'] = True
                    healthy += 1
                    logging.info(f"Usenet provider ({label}) reachable at {usenet_url}")
                else:
                    failure(f'Usenet Provider ({label})', 'CONNECTION_ERROR', f'Cannot reach {label} at {usenet_url}: {err}')
            except Exception as e:
                failure('Usenet Provider', 'CONNECTION_ERROR', f'Connectivity check error: {e}')

    with _lock:
        if _availability != statuses:
            _deferred_logged.clear()  # backend health changed: a new outage logs again
        _availability.clear()
        _availability.update(statuses)
    if healthy:
        return True, []
    if not debrid_configured and not nzb_configured:
        return False, [{'service': 'Provider', 'type': 'NO_PROVIDER_CONFIGURED',
                        'message': 'Configure at least one acquisition provider'}]
    return False, failures
