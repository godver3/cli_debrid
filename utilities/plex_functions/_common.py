"""Plex tuning constants, HTTP timeout and resolution normalising shared by the plex_functions modules."""

import aiohttp
import logging
import re

logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 50  # Back to original but with small delays between batches

OPTIMAL_PAGE_SIZE = 500  # Reduced from 2500 for gentler scanning

EPISODE_BATCH_SIZE = 15  # Balanced batch size - prevents NAS timeouts while keeping good performance

EPISODE_BATCH_DELAY = 0.5  # Moderate delay between episode batches

CHUNK_SIZE = 10

MAX_RETRIES = 3

def normalize_plex_resolution(resolution: str) -> str:
    """
    Normalize Plex videoResolution to standard format.

    Plex returns values like "4k", "1080", "720", "sd" but we want standardized
    "p" format like "2160p", "1080p", etc.

    Backwards compatible: if already normalized (e.g., "2160p"), returns as-is.

    Args:
        resolution: Raw resolution from Plex videoResolution

    Returns:
        Normalized resolution string (e.g., "2160p", "1080p")
    """
    if not resolution:
        return None

    resolution_lower = resolution.lower().strip()

    # Normalization map for Plex-specific formats
    resolution_map = {
        '4k': '2160p',
        'uhd': '2160p',
        '8k': '4320p',
        '2k': '1440p',
        'qhd': '1440p',
        'fhd': '1080p',
        'hd': '720p',
        'sd': '480p',
        # Map numeric-only to standard "p" format
        '2160': '2160p',
        '1440': '1440p',
        '1080': '1080p',
        '720': '720p',
        '576': '576p',
        '480': '480p',
    }

    # Check if it needs normalization
    if resolution_lower in resolution_map:
        return resolution_map[resolution_lower]

    # Already in correct format (e.g., "2160p", "1080i") - return as-is
    # Supports both progressive (p) and interlaced (i) formats
    if re.match(r'^\d{3,4}[pi]$', resolution_lower):
        return resolution_lower

    # Unknown format - return original value
    logger.debug(f"Unknown resolution format from Plex: {resolution}")
    return resolution

RETRY_DELAY = 1

BATCH_DELAY = 0.2  # Small delay between batch fetches to reduce Plex CPU spikes

# HTTP timeout configuration for Plex API requests
# Prevents hanging when Plex stops responding, works with existing retry logic
PLEX_HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=None,      # No total timeout (allow long-running operations)
    connect=10,      # 10 seconds to establish connection
    sock_read=30     # 30 seconds max per read operation (prevents hanging)
)
