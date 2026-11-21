"""Proof that a debrid file is gone, for the built-in mount's repair.

The rules follow cli_mount's repair sweep (pkg/manager/repair_sweep.go and
each provider's CheckFile/fetchDownloadLink), so the built-in mount marks
exactly what cli_mount would and nothing more:

  Real-Debrid  /unrestrict/check answers 404, or /unrestrict/link fails with
               error_code 19 (hoster unavailable), 24 (file unavailable) or
               35 (infringing file)
  TorBox       the torrent's download is no longer present on TorBox
  AllDebrid    /link/infos returns an error for the file's link
  Premiumize   the item exists but has no link (an item lookup that fails
               proves nothing)
  Debrid-Link  the file's link answers 404 or 410
  HighWay      the file answers 404
  all          the file has no link on the provider at all
               (missing_provider_link: cli_debrid re-adds the torrent)

Anything else (timeouts, 5xx, rate limits, auth trouble) proves nothing.
"""

from typing import Tuple

HOSTER_UNAVAILABLE = 'hoster_unavailable'
MISSING_PROVIDER_LINK = 'missing_provider_link'

HEALTHY, BROKEN, UNKNOWN = 'healthy', 'broken', 'unknown'


class MountFileGone(Exception):
    """The provider proved the file can't be downloaded any more."""

    def __init__(self, reason: str, detail: str = ''):
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason, self.detail = reason, detail


def link_status(url: str, timeout: int = 20) -> int:
    """HTTP status of a one-byte read of a download link (0 when unreachable)."""
    import requests
    try:
        r = requests.get(url, headers={'Range': 'bytes=0-0'}, timeout=timeout, stream=True)
        r.close()
        return r.status_code
    except Exception:
        return 0


def from_link_status(status: int, what: str) -> Tuple[str, str]:
    """cli_mount's link-status rule: 404/410 = gone, 2xx = fine."""
    if status in (404, 410):
        return BROKEN, HOSTER_UNAVAILABLE
    if 200 <= status < 300:
        return HEALTHY, ''
    return UNKNOWN, f'{what} answered HTTP {status}' if status else f'{what} unreachable'
