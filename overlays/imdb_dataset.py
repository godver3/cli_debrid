"""
IMDb Ratings Dataset

Downloads the public IMDb ratings dataset (title.ratings.tsv.gz) once per day and
exposes a fast file-based lookup by IMDb ID via SQLite.  Used as a zero-API-call
supplement when MDBList or Plex metadata doesn't return an IMDb rating.

Dataset URL: https://datasets.imdbws.com/title.ratings.tsv.gz
Format:      TSV, columns: tconst  averageRating  numVotes
Size:        ~3 MB gzipped, ~10 MB uncompressed, ~1.5 M rows
Memory:      Negligible — all data lives in a SQLite file; lookups are indexed queries
Refresh:     Daily — IMDb publishes updated files each day

Files stored at /user/config/:
  imdb_ratings.tsv.gz  — downloaded source file
  imdb_ratings.db      — SQLite DB built from the TSV; survives container restarts
"""

import gzip
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DATASET_URL  = "https://datasets.imdbws.com/title.ratings.tsv.gz"
_DATASET_PATH = "/user/config/imdb_ratings.tsv.gz"
_DB_PATH      = "/user/config/imdb_ratings.db"
_TTL          = 86400  # re-download after 24 hours

# Tracks when the DB was last successfully built in this process.
# On restart this is 0; _ensure_loaded() then checks the DB file mtime instead.
_loaded_at: float = 0.0
_lock = threading.Lock()


def _file_age() -> float:
    """Return seconds since _DATASET_PATH was last modified, or infinity if absent."""
    try:
        return time.time() - os.path.getmtime(_DATASET_PATH)
    except OSError:
        return float('inf')


def _db_age() -> float:
    """Return seconds since _DB_PATH was last modified, or infinity if absent."""
    try:
        return time.time() - os.path.getmtime(_DB_PATH)
    except OSError:
        return float('inf')


def _db_ready() -> bool:
    """Return True if the SQLite DB exists and contains at least one row."""
    if not os.path.exists(_DB_PATH):
        return False
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        count = conn.execute("SELECT COUNT(*) FROM imdb_ratings").fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


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
    """Parse _DATASET_PATH into a SQLite DB at _DB_PATH. Returns True on success."""
    global _loaded_at

    tmp_db = _DB_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        if os.path.exists(tmp_db):
            os.remove(tmp_db)

        conn = sqlite3.connect(tmp_db, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE imdb_ratings (imdb_id TEXT PRIMARY KEY, rating REAL)"
        )

        def _rows():
            with gzip.open(_DATASET_PATH, "rt", encoding="utf-8") as fh:
                next(fh)  # skip header: tconst\taverageRating\tnumVotes
                for line in fh:
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        try:
                            yield parts[0], round(float(parts[1]), 1)
                        except ValueError:
                            pass

        conn.executemany("INSERT OR REPLACE INTO imdb_ratings VALUES (?, ?)", _rows())
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM imdb_ratings").fetchone()[0]
        conn.close()

        os.replace(tmp_db, _DB_PATH)
        _loaded_at = time.time()
        logger.info(f"IMDb ratings dataset loaded into SQLite: {count:,} entries")
        return True

    except Exception as exc:
        logger.warning(f"IMDb dataset parse failed: {exc}")
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass
        return False


def _ensure_loaded() -> bool:
    """
    Ensure the SQLite DB is built and current.  Thread-safe — only one thread
    downloads/parses at a time.  Returns True if the DB is available (may be
    slightly stale if download fails).
    """
    global _loaded_at

    # Fast path: built recently in this process
    if _loaded_at and (time.time() - _loaded_at) < _TTL:
        return True

    with _lock:
        # Re-check inside the lock
        if _loaded_at and (time.time() - _loaded_at) < _TTL:
            return True

        # On process restart: DB may already be fresh on disk — use it as-is
        if not _loaded_at and _db_age() < _TTL and _db_ready():
            _loaded_at = time.time() - _db_age()
            logger.debug("IMDb ratings DB is fresh on disk, skipping rebuild")
            return True

        # Download TSV if absent or stale
        if _file_age() >= _TTL:
            if not _download():
                if _db_ready():
                    logger.debug("Using stale IMDb dataset (download failed)")
                    return True
                return False

        # Build (or rebuild) the SQLite DB from the TSV
        return _parse()


def get_imdb_dataset_rating(imdb_id: str) -> Optional[float]:
    """
    Return the IMDb community rating for imdb_id, or None if unavailable.

    Triggers a lazy download/parse on first call (or when the daily TTL expires).
    Subsequent calls are fast indexed SQLite lookups with no memory overhead.
    Never raises — any failure returns None so callers degrade gracefully.
    """
    try:
        if not _ensure_loaded():
            return None
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        row = conn.execute(
            "SELECT rating FROM imdb_ratings WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
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
