"""Common status flags and utilities for debrid providers"""

from enum import Enum, auto
from dataclasses import dataclass
from typing import Dict, Optional

class TorrentStatus(Enum):
    QUEUED = 'queued'
    DOWNLOADING = 'downloading'
    DOWNLOADED = 'downloaded'  # Cached
    ERROR = 'error'
    UNKNOWN = auto()
    SELECTING = 'selecting'
    REMOVED = 'removed'
    ADDED = 'added'
    CACHED = 'cached'
    NOT_CACHED = 'not_cached'

def get_status_flags(status: TorrentStatus) -> Dict[str, bool]:
    """Return a dictionary of boolean flags based on the torrent status"""
    is_cached = status == TorrentStatus.DOWNLOADED.value
    is_queued = status in [TorrentStatus.QUEUED.value, TorrentStatus.DOWNLOADING.value]
    is_error = status == TorrentStatus.ERROR.value

    return {
        'is_cached': is_cached,
        'is_queued': is_queued,
        'is_error': is_error
    }

class TorrentFetchStatus(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"  # HTTP 404
    RATE_LIMITED = "rate_limited"  # HTTP 429
    CLIENT_ERROR = "client_error"  # Other 4xx errors
    SERVER_ERROR = "server_error"  # 5xx errors
    REQUEST_ERROR = "request_error" # Error during request preparation or network issue (e.g., DNS failure, connection refused)
    PROVIDER_HANDLED_ERROR = "provider_handled_error" # Error was handled and logged by the provider's make_request, returned None
    UNKNOWN_ERROR = "unknown_error" # Other non-HTTP errors or unparseable ones

@dataclass
class TorrentInfoStatus:
    status: TorrentFetchStatus
    data: Optional[Dict] = None
    message: Optional[str] = None
    http_status_code: Optional[int] = None
