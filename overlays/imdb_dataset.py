"""
IMDb Ratings Dataset

Downloads the public IMDb ratings dataset (title.ratings.tsv.gz) once per day and
exposes a fast in-memory lookup by IMDb ID.  Used as a zero-API-call supplement
when MDBList or Plex metadata doesn't return an IMDb rating.

Dataset URL: https://datasets.imdbws.com/title.ratings.tsv.gz
Format:      TSV, columns: tconst  averageRating  numVotes
Size:        ~3 MB gzipped, ~10 MB uncompressed, ~1.5 M rows
Memory:      ~150-200 MB for the in-memory dict (acceptable for a media-server host)
Refresh:     Daily — IMDb publishes updated files each day

The dataset is stored at /user/config/imdb_ratings.tsv.gz (same persistent volume
used by overlay_assets / overlay_templates) so it survives container restarts and
is only re-downloaded when the file is older than 24 hours.
"""

import gzip
import logging
import os
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DATASET_URL  = "https://datasets.imdbws.com/title.ratings.tsv.gz"
_DATASET_PATH = "/user/config/imdb_ratings.tsv.gz"
_TTL          = 86400  # re-download after 24 hours

# Module-level state — shared across all callers in the same process
_ratings: Dict[str, float] = {}   # {imdb_id: average_rating}
_loaded_at: float = 0.0           # timestamp of last successful parse
_lock = threading.Lock()


def _file_age() -> float:
    """Return seconds since _DATASET_PATH was last modified, or infinity if absent."""
    try:
        return time.time() - os.path.getmtime(_DATASET_PATH)
    except OSError:
        return float('inf')


def _download() -> bool:
    """Download the gzipped TSV to _DATASET_PATH. Returns True on success."""
    import requests
    try:
        logger.info(f"Downloading IMDb ratings dataset from {_DATASET_URL} ...")
        resp = requests.get(_DATASET_URL, stream=True, timeout=30)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(_DATASET_PATH), exist_ok=True)
        tmp = _DATASET_PATH + ".tmp"
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
        os.replace(tmp, _DATASET_PATH)
        logger.info("IMDb ratings dataset downloaded successfully")
        return True
    except Exception as exc:
        logger.warning(f"IMDb dataset download failed: {exc}")
        return False


def _parse() -> bool:
    """Parse _DATASET_PATH into the in-memory _ratings dict. Returns True on success."""
    global _ratings, _loaded_at
    try:
        new_ratings: Dict[str, float] = {}
        with gzip.open(_DATASET_PATH, "rt", encoding="utf-8") as fh:
            next(fh)  # skip header line: tconst\taverageRating\tnumVotes
            for line in fh:
                parts = line.split("\t")
                if len(parts) >= 2:
                    try:
                        new_ratings[parts[0]] = round(float(parts[1]), 1)
                    except ValueError:
                        pass
        _ratings = new_ratings
        _loaded_at = time.time()
        logger.info(f"IMDb ratings dataset loaded: {len(_ratings):,} entries")
        return True
    except Exception as exc:
        logger.warning(f"IMDb dataset parse failed: {exc}")
        return False


def _ensure_loaded() -> bool:
    """
    Ensure the dataset is loaded and current.  Thread-safe — only one thread
    downloads/parses at a time; others return from the existing dict immediately.
    Returns True if the dataset is available (may be slightly stale if download fails).
    """
    global _loaded_at

    # Fast path: dataset is fresh enough — no lock needed for the read
    if _ratings and (time.time() - _loaded_at) < _TTL:
        return True

    with _lock:
        # Re-check inside the lock in case another thread already refreshed
        if _ratings and (time.time() - _loaded_at) < _TTL:
            return True

        # Download if file is absent or older than TTL
        if _file_age() >= _TTL:
            if not _download():
                # Download failed — if we have stale data, keep using it
                if _ratings:
                    logger.debug("Using stale IMDb dataset (download failed)")
                    return True
                return False

        # Parse (or re-parse if the file was refreshed)
        return _parse()


def get_imdb_dataset_rating(imdb_id: str) -> Optional[float]:
    """
    Return the IMDb community rating for imdb_id, or None if unavailable.

    Triggers a lazy download/parse on first call (or when the daily TTL expires).
    Subsequent calls within the same day are instant dict lookups.
    Never raises — any failure returns None so callers degrade gracefully.
    """
    try:
        if not _ensure_loaded():
            return None
        return _ratings.get(imdb_id)
    except Exception as exc:
        logger.debug(f"IMDb dataset lookup failed for {imdb_id}: {exc}")
        return None


def prefetch_dataset() -> None:
    """
    Trigger dataset download/parse in the background so it is warm before the
    first overlay sync run.  Call this from the overlay task scheduler at startup.
    Safe to call multiple times — subsequent calls are no-ops if data is fresh.
    """
    def _bg():
        try:
            _ensure_loaded()
        except Exception:
            pass
    t = threading.Thread(target=_bg, daemon=True, name="imdb-dataset-prefetch")
    t.start()
