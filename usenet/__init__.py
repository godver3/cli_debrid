"""
Usenet provider factory.

cli-debrid historically supported only Decypharr as a usenet backend (see
decypharr_client.py). This factory allows alternative SAB-compatible backends
to be slotted in without changing any call sites.

Currently supported:
  - decypharr  (default; the original implementation)
  - nzbdav     (alternative; see nzbdav_client.py)

Selection is controlled by the `Usenet Provider.provider` config key. If unset,
the factory defaults to `decypharr` to preserve existing behaviour for users
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
      from usenet.decypharr_client import get_decypharr_client
      client = get_decypharr_client()

  New (provider-agnostic):
      from usenet import get_usenet_client
      client = get_usenet_client()
"""

from typing import Union, Optional

from utilities.settings import get_setting


# Type alias for any concrete usenet client.
# Both classes implement the same duck-typed interface (see module docstring).
UsenetClient = Union[
    'usenet.decypharr_client.DecypharrClient',
    'usenet.nzbdav_client.NzbdavClient',
]


def _get_provider_key() -> str:
    """Read the chosen provider from settings, defaulting to 'decypharr'.

    The key lives in the same 'Usenet Provider' section as the URL/token, so
    existing configs upgrade seamlessly (an absent `provider` key falls back
    to the decypharr default).
    """
    cfg = get_setting('Usenet Provider') or {}
    return (cfg.get('provider') or 'decypharr').strip().lower()


def get_usenet_client():
    """Return the singleton client for whichever provider is configured."""
    provider = _get_provider_key()
    if provider == 'nzbdav':
        from .nzbdav_client import get_nzbdav_client
        return get_nzbdav_client()
    # default + 'decypharr'
    from .decypharr_client import get_decypharr_client
    return get_decypharr_client()


def get_usenet_provider_display_name() -> str:
    """Return a short human-friendly name for the active provider.

    Used by toasts, log messages and template strings so the UI reads
    "NZB submitted to Decypharr (job: ...)" or "...to NzbDAV (job: ...)"
    instead of a generic "the Usenet provider".
    """
    provider = _get_provider_key()
    return {
        'decypharr': 'Decypharr',
        'nzbdav': 'NzbDAV',
    }.get(provider, 'Usenet provider')


def reset_usenet_client() -> None:
    """Reset the active provider's singleton (call after settings change)."""
    provider = _get_provider_key()
    if provider == 'nzbdav':
        from .nzbdav_client import reset_nzbdav_client
        reset_nzbdav_client()
    else:
        from .decypharr_client import reset_decypharr_client
        reset_decypharr_client()
