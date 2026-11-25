"""
Upgrade Hub — NZB feed history.

The NZB phase used to read each indexer's newest releases (up to 1000 per
category) only when a scan ran, and forgot them afterwards, so anything posted
and pushed out of that list between scans was never seen. Instead, a sync
(every 30 minutes while the Upgrade Hub scan task is on, and at the start of
each scan) reads only what's new on each indexer since the last sync and keeps
it here for HISTORY_DAYS. Scans match against this history.

Only releases that can belong to the library are kept: the indexer's IMDb id
(extended=1) or the parsed title matches a library item (any state but
Blacklisted, so Wanted items count and are covered once collected). That keeps
the table small on busy indexers.
"""

import json
import logging
import re
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Set, Tuple

from database.core import get_db_connection

logger = logging.getLogger(__name__)

HISTORY_DAYS = 90
PAGE_SIZE = 100
# Pages read per indexer and category in one sync. 10 x 100 matches the old
# "latest 1000"; an indexer posting more than that between syncs is capped.
MAX_PAGES = 10

_tables_ready = False
_tables_lock = threading.Lock()
_sync_lock = threading.Lock()


def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        conn = get_db_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS nzb_feed_releases ("
                " instance TEXT NOT NULL,"
                " guid TEXT NOT NULL,"
                " category TEXT NOT NULL,"
                " imdb_id TEXT,"
                " title_key TEXT,"
                " pub_ts REAL,"
                " result_json TEXT NOT NULL,"
                " PRIMARY KEY (instance, guid))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nzb_feed_releases_pub "
                         "ON nzb_feed_releases (category, pub_ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS nzb_feed_state ("
                " instance TEXT NOT NULL,"
                " category TEXT NOT NULL,"
                " newest_pub_ts REAL,"
                " synced_at REAL,"
                " PRIMARY KEY (instance, category))"
            )
            conn.commit()
            _tables_ready = True
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Library keys: which releases are worth keeping
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r'[^a-z0-9]+')


def title_key(title: str) -> str:
    """Lower-case words, punctuation as word breaks, sorted (word order doesn't
    matter): 'Dexter: Resurrection' and 'Dexter-Resurrection' give the same key."""
    return ' '.join(sorted(_NON_ALNUM.sub(' ', (title or '').lower()).split()))


def library_keys() -> Tuple[Set[str], Set[str]]:
    """(IMDb ids as bare digits, title keys) of every library item that isn't Blacklisted."""
    from scraper.newznab import _normalize_imdb
    imdb_ids: Set[str] = set()
    keys: Set[str] = set()
    conn = get_db_connection()
    try:
        for imdb_id, title in conn.execute(
                "SELECT DISTINCT imdb_id, title FROM media_items WHERE state != 'Blacklisted'"):
            norm = _normalize_imdb(str(imdb_id or ''))
            if norm:
                imdb_ids.add(norm)
            if title:
                keys.add(title_key(title))
    finally:
        conn.close()
    keys.discard('')
    return imdb_ids, keys


def _release_imdb(result: Dict) -> str:
    return ((result.get('parsed_info') or {}).get('newznab_imdb') or '').lower()


def _wanted(result: Dict, imdb_ids: Set[str], keys: Set[str]) -> bool:
    """Keep a release if its IMDb id or its title matches the library. Either is
    enough: indexers sometimes tag a release with the wrong id (seen on NZBGeek
    TV), and the scan's matching decides what it really is."""
    imdb = _release_imdb(result)
    if imdb and imdb in imdb_ids:
        return True
    pi = result.get('parsed_info') or {}
    return title_key(pi.get('title') or result.get('title', '')) in keys


def _pub_ts(pub_date: str) -> Optional[float]:
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _get_state(conn, instance: str, category: str) -> Optional[float]:
    row = conn.execute("SELECT newest_pub_ts FROM nzb_feed_state WHERE instance = ? AND category = ?",
                       (instance, category)).fetchone()
    return row[0] if row else None


def _sync_one(instance: str, settings: Dict, category: str, cats: str,
              imdb_ids: Set[str], keys: Set[str]) -> Dict[str, int]:
    """Read one indexer's feed for one category back to the last sync."""
    from database.nzb_upgrade import _fetch_browse_page, _parse_items
    url = (settings.get('url') or '').rstrip('/')
    api_key = (settings.get('api_key') or '').strip()
    if not url or not api_key:
        return {'read': 0, 'kept': 0, 'calls': 0}

    conn = get_db_connection()
    try:
        since = _get_state(conn, instance, category)
    finally:
        conn.close()

    newest: Optional[float] = None
    rows = []
    read = calls = 0
    reached_known = False
    for page in range(MAX_PAGES):
        items, total = _fetch_browse_page(url, api_key, cats, PAGE_SIZE, page * PAGE_SIZE)
        calls += 1
        if not items:
            break
        for r in _parse_items(items, instance):
            ts = _pub_ts(r.get('ingested_at') or '')
            if ts is not None:
                newest = ts if newest is None else max(newest, ts)
                if since is not None and ts <= since:
                    reached_known = True
                    continue
            read += 1
            if _wanted(r, imdb_ids, keys):
                pi = r.get('parsed_info') or {}
                rows.append((instance, pi.get('guid') or r.get('nzb_url'), category,
                             _release_imdb(r) or None, title_key(pi.get('title') or r.get('title', '')),
                             ts, json.dumps(r)))
        if reached_known or len(items) < PAGE_SIZE or (total and (page + 1) * PAGE_SIZE >= total):
            break

    conn = get_db_connection()
    try:
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO nzb_feed_releases "
                "(instance, guid, category, imdb_id, title_key, pub_ts, result_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        if newest is not None and (since is None or newest > since):
            conn.execute(
                "INSERT OR REPLACE INTO nzb_feed_state (instance, category, newest_pub_ts, synced_at) "
                "VALUES (?, ?, ?, ?)", (instance, category, newest, time.time()))
        conn.commit()
    finally:
        conn.close()
    if calls >= MAX_PAGES and not reached_known and since is not None:
        logger.info(f"[NZB_FEED] {instance} {category}: more than {MAX_PAGES * PAGE_SIZE} new releases "
                    f"since the last sync; older ones in between were not read")
    return {'read': read, 'kept': len(rows), 'calls': calls}


def prune(days: int = HISTORY_DAYS) -> int:
    _ensure_tables()
    cutoff = time.time() - days * 86400
    conn = get_db_connection()
    try:
        cur = conn.execute("DELETE FROM nzb_feed_releases WHERE pub_ts IS NOT NULL AND pub_ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def sync_feeds(scrapers: Optional[List[Tuple[str, Dict]]] = None) -> Dict[str, int]:
    """Read what's new on every enabled Newznab indexer and keep the library's
    releases. Runs one sync at a time; a caller arriving mid-sync waits for it."""
    from database.nzb_upgrade import get_newznab_scrapers, _CAT_MOVIE, _CAT_TV
    _ensure_tables()
    scrapers = get_newznab_scrapers() if scrapers is None else scrapers
    totals = {'read': 0, 'kept': 0, 'calls': 0, 'pruned': 0}
    if not scrapers:
        return totals
    with _sync_lock:
        imdb_ids, keys = library_keys()
        for instance, settings in scrapers:
            for category, cats in (('movie', _CAT_MOVIE), ('tv', _CAT_TV)):
                try:
                    got = _sync_one(instance, settings, category, cats, imdb_ids, keys)
                except Exception as e:
                    logger.warning(f"[NZB_FEED] Sync failed for {instance} {category}: {e}")
                    continue
                for k in ('read', 'kept', 'calls'):
                    totals[k] += got[k]
        totals['pruned'] = prune()
    logger.info(f"[NZB_FEED] Synced {len(scrapers)} indexer(s): {totals['read']} new release(s), "
                f"{totals['kept']} kept for the library, {totals['calls']} API call(s)"
                + (f", {totals['pruned']} older than {HISTORY_DAYS} days removed" if totals['pruned'] else ''))
    return totals


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load_pool(days: int = HISTORY_DAYS) -> Dict[str, List[Dict]]:
    """Kept releases from the last ``days`` days, as {'movie': [...], 'tv': [...]}
    in the same shape the NZB phase scores."""
    _ensure_tables()
    cutoff = time.time() - days * 86400
    pool: Dict[str, List[Dict]] = {'movie': [], 'tv': []}
    conn = get_db_connection()
    try:
        for category, result_json in conn.execute(
                "SELECT category, result_json FROM nzb_feed_releases "
                "WHERE pub_ts IS NULL OR pub_ts >= ? ORDER BY pub_ts DESC", (cutoff,)):
            try:
                pool.setdefault(category, []).append(json.loads(result_json))
            except ValueError:
                continue
    finally:
        conn.close()
    return pool
