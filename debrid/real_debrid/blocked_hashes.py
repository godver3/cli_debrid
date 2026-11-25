"""Real-Debrid-only memory of hashes it refused with 451 (legal block).

A 451 stays off the install-wide not-wanted list on purpose: another provider
may serve the hash. Without this memory every Adding attempt re-sent each
blocked magnet to RD (about 40 addMagnet calls per attempt for one item),
which pushed RD into 429s. Entries expire so a lifted block is retried.
Kept on disk so a restart does not start the burst again.
"""
import json
import logging
import os
import threading
import time

from utilities.atomic_io import atomic_write_bytes

TTL_SECONDS = 24 * 3600

_lock = threading.Lock()
_blocked = None  # hash -> unix time it was blocked


def _path():
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'rd_blocked_hashes.json')


def _load():
    global _blocked
    if _blocked is not None:
        return _blocked
    _blocked = {}
    try:
        with open(_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            now = time.time()
            _blocked = {str(h).lower(): float(t) for h, t in data.items()
                        if isinstance(t, (int, float)) and now - t < TTL_SECONDS}
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.debug(f"Could not read RD blocked-hash memory: {e}")
    return _blocked


def _save():
    try:
        atomic_write_bytes(_path(), json.dumps(_blocked).encode('utf-8'))
    except Exception as e:
        logging.debug(f"Could not save RD blocked-hash memory: {e}")


def mark_blocked(hash_value):
    if not hash_value:
        return
    with _lock:
        _load()[str(hash_value).lower()] = time.time()
        _save()


def is_blocked(hash_value):
    if not hash_value:
        return False
    key = str(hash_value).lower()
    with _lock:
        blocked = _load()
        at = blocked.get(key)
        if at is None:
            return False
        if time.time() - at >= TTL_SECONDS:
            del blocked[key]
            _save()
            return False
        return True


def _reset_for_tests():
    global _blocked
    with _lock:
        _blocked = None
