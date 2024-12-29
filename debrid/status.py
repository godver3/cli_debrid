"""Common status flags and utilities for debrid providers"""

from enum import Enum
from typing import Dict

class TorrentStatus(Enum):
    QUEUED = 'queued'
    DOWNLOADING = 'downloading'
    DOWNLOADED = 'downloaded'  # Cached
    ERROR = 'error'
    UNKNOWN = 'unknown'

def get_status_flags(status: str) -> Dict[str, bool]:
    """Convert a status string to standardized status flags"""
    is_cached = status == TorrentStatus.DOWNLOADED.value
    is_queued = status in [TorrentStatus.QUEUED.value, TorrentStatus.DOWNLOADING.value]
    is_error = status == TorrentStatus.ERROR.value

    return {
        'is_cached': is_cached,
        'is_queued': is_queued,
        'is_error': is_error
    }
