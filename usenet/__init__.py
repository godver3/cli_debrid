"""
Usenet provider factory.

cli-debrid historically supported only cli_mount as a usenet backend (see
climount_client.py). This factory allows alternative SAB-compatible backends
to be slotted in without changing any call sites.

Currently supported:
  - climount  (default; the original implementation)
  - nzbdav     (alternative; see nzbdav_client.py)
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

from typing import Union, Optional

from utilities.settings import get_setting


# Type alias for any concrete usenet client.
# Both classes implement the same duck-typed interface (see module docstring).
UsenetClient = Union[
    'usenet.climount_client.CliMountClient',
    'usenet.nzbdav_client.NzbdavClient',
]


def _get_provider_key() -> str:
    """Read the chosen provider from settings, defaulting to 'climount'.

    The key lives in the same 'Usenet Provider' section as the URL/token, so
    existing configs upgrade seamlessly (an absent `provider` key falls back
    to the climount default).
    """
    cfg = get_setting('Usenet Provider') or {}
    return (cfg.get('provider') or 'climount').strip().lower()


def get_usenet_client():
    """Return the singleton client for whichever provider is configured."""
    provider = _get_provider_key()
    if provider in ('nzbdav', 'zurg'):
        from .nzbdav_client import get_nzbdav_client
        return get_nzbdav_client()
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
    }.get(provider, 'Usenet provider')


def reset_usenet_client() -> None:
    """Reset the active provider's singleton (call after settings change)."""
    provider = _get_provider_key()
    if provider in ('nzbdav', 'zurg'):
        from .nzbdav_client import reset_nzbdav_client
        reset_nzbdav_client()
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
    if provider in ('nzbdav', 'zurg'):
        import logging
        from .nzbdav_client import get_nzbdav_client
        try:
            status = get_nzbdav_client().get_job_status(job_hash)
            return bool(status and status.get('raw'))
        except Exception as exc:
            logging.debug(f'[{provider}] is_nzb_job_alive check failed for {job_hash!r}: {exc}')
            # Unknown due to a transient error - don't block a legitimate reuse over it.
            return True
    from .climount_client import is_nzb_job_alive as _climount_is_nzb_job_alive
    return _climount_is_nzb_job_alive(job_hash)
