"""
Usenet provider factory.

cli-debrid historically supported only cli_mount as a usenet backend (see
climount_client.py). This factory allows alternative SAB-compatible backends
to be slotted in without changing any call sites.

Currently supported:
  - climount  (default; the original implementation)
  - nzbdav     (alternative; see nzbdav_client.py)
  - builtin    (the optional built-in mount engine, engine/ + builtin_client.py)
  - zurg       (Zurg 2.0 / other flat-layout SAB-emulation mounts; reuses
                nzbdav_client.py's submit/poll/health-check logic unchanged,
                only the WebDAV browse helpers resolve folders differently —
                see NzbdavClient.flat_layout)

Selection is controlled by the `Usenet Provider.provider` config key. If unset,
the factory defaults to `climount` to preserve existing behaviour for users
who haven't opted into the new switch.

Both client classes expose an identical public method set:
    PROVIDER_NAME           is_enabled()              check_connectivity()
    add_nzb_content()       add_nzb()
    get_nzb_file_info()     get_nzb_folder_all_files()
    remove_nzb()            get_job_status()
    trigger_health_check()  poll_health_result()      check_entry_health()
    wait_for_completion()

Caller migration path:
  Old (still works):
      from usenet.climount_client import get_climount_client
      client = get_climount_client()

  New (provider-agnostic):
      from usenet import get_usenet_client
      client = get_usenet_client()
"""

from typing import TYPE_CHECKING, Union, Optional

from utilities.settings import get_setting

if TYPE_CHECKING:
    from usenet.climount_client import CliMountClient
    from usenet.nzbdav_client import NzbdavClient


# Type alias for any concrete usenet client.
# Both classes implement the same duck-typed interface (see module docstring).
UsenetClient = Union['CliMountClient', 'NzbdavClient']

# Providers served by NzbdavClient (SABnzbd-style API). Every factory and
# reset path must use this one tuple; climount_client.get_climount_client()
# delegates on it too, so a provider added here can't silently fall back to
# CliMountClient at the legacy call sites.
SAB_STYLE_PROVIDERS = ('nzbdav', 'zurg')

# The optional built-in mount engine (usenet/builtin_client.py).
BUILTIN_PROVIDER = 'builtin'


def _get_provider_key() -> str:
    """Read the chosen provider from settings, defaulting to 'climount'.

    The key lives in the same 'Usenet Provider' section as the URL/token, so
    existing configs upgrade seamlessly (an absent `provider` key falls back
    to the climount default).
    """
    cfg = get_setting('Usenet Provider') or {}
    return (cfg.get('provider') or 'climount').strip().lower()


def usenet_configured(get=None) -> bool:
    """Usenet is enabled and has somewhere to send jobs: a URL for external
    backends, or the built-in engine (which needs no URL).

    get: the caller's own get_setting(section, key, default), so modules that
    read settings through their own reference keep doing so.
    """
    get = get or get_setting
    if not get('Usenet Provider', 'enabled', False):
        return False
    if (get('Usenet Provider', 'provider', '') or '').strip().lower() == BUILTIN_PROVIDER:
        return True
    return bool((get('Usenet Provider', 'url', '') or '').strip())


def get_usenet_client():
    """Return the singleton client for whichever provider is configured."""
    provider = _get_provider_key()
    if provider in SAB_STYLE_PROVIDERS:
        from .nzbdav_client import get_nzbdav_client
        return get_nzbdav_client()
    if provider == BUILTIN_PROVIDER:
        from .builtin_client import get_builtin_client
        return get_builtin_client()
    # default + 'climount'
    from .climount_client import get_climount_client
    return get_climount_client()


def get_usenet_provider_display_name() -> str:
    """Return a short human-friendly name for the active provider.

    Used by toasts, log messages and template strings so the UI reads
    "NZB submitted to cli_mount (job: ...)" or "...to NzbDAV (job: ...)"
    instead of a generic "the Usenet provider".
    """
    provider = _get_provider_key()
    return {
        'climount': 'cli_mount',
        'nzbdav': 'NzbDAV',
        'zurg': 'Zurg',
        'builtin': 'Built-in mount',
    }.get(provider, 'Usenet provider')


def reset_usenet_client() -> None:
    """Reset the active provider's singleton (call after settings change)."""
    provider = _get_provider_key()
    if provider in SAB_STYLE_PROVIDERS:
        from .nzbdav_client import reset_nzbdav_client
        reset_nzbdav_client()
    elif provider == BUILTIN_PROVIDER:
        from .builtin_client import reset_builtin_client
        reset_builtin_client()
    else:
        from .climount_client import reset_climount_client
        reset_climount_client()


def is_nzb_job_alive(job_hash: str) -> bool:
    """Check whether a shared season-pack job is still queryable on the active
    provider before reusing its reference for a different episode, via
    whichever provider is actually configured.

    climount keeps its own short-lived "recently deleted by us" cache so a
    provider round-trip isn't needed right after we delete a job ourselves;
    that logic lives in climount_client.is_nzb_job_alive and is reused as-is.
    nzbdav/zurg have no equivalent cache yet, so this falls back to a plain
    get_job_status() round-trip for them.
    """
    provider = _get_provider_key()
    if provider in SAB_STYLE_PROVIDERS or provider == BUILTIN_PROVIDER:
        import logging
        if provider == BUILTIN_PROVIDER:
            from .climount_client import _was_recently_deleted_by_us
            if _was_recently_deleted_by_us(job_hash):
                return False
        try:
            status = get_usenet_client().get_job_status(job_hash)
            return bool(status and status.get('raw'))
        except Exception as exc:
            logging.debug(f'[{provider}] is_nzb_job_alive check failed for {job_hash!r}: {exc}')
            # Unknown due to a transient error - don't block a legitimate reuse over it.
            return True
    from .climount_client import is_nzb_job_alive as _climount_is_nzb_job_alive
    return _climount_is_nzb_job_alive(job_hash)
