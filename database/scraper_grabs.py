"""Scraper / indexer stats: an append-only log of every grab and how it ended.

One row per release cli_debrid commits to for a media item (torrent added, NZB
submitted, upgrade, manual add, NZB repair replacement). An item's current grab
is simply its latest row. Outcomes:

  pending   - grabbed, not yet seen in the library
  collected - the item reached Collected/Upgrading with this grab
  failed    - never made it (adding/health-check failure, or superseded first)
  replaced  - was collected, later swapped for a better release by an upgrade
  removed   - was collected, later left the library without an upgrade or
              repair (deleted, or reset to Wanted because the file went missing)
  repaired  - was collected, later found broken and repaired away

Recording is strictly best-effort: every public writer swallows its own errors
so a stats problem can never break the grab pipeline.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from database.core import get_db_connection

logger = logging.getLogger(__name__)

_PRUNE_DAYS = 365

# Item states that mean the current grab landed in the library.
_LIBRARY_STATES = ('Collected', 'Upgrading')
# Item states that mean the current grab is no longer in flight. A pending row
# for an item sitting in one of these never made it.
_RETURNED_STATES = ('Wanted', 'Scraping', 'Sleeping', 'Blacklisted', 'Ghostlisted')

# Placeholder sources used for "the file we already have" in upgrade ranking.
_NON_SCRAPER_SOURCES = {'__current__', 'Current Item'}


def create_scraper_grabs_table() -> None:
    conn = get_db_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scraper_grabs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                grabbed_at        TIMESTAMP NOT NULL,
                item_id           INTEGER,
                media_type        TEXT,
                imdb_id           TEXT,
                title             TEXT,
                season            INTEGER,
                episode           INTEGER,
                version           TEXT,
                kind              TEXT,
                scraper_type      TEXT,
                scraper_instance  TEXT,
                indexer           TEXT,
                release_title     TEXT,
                info_hash         TEXT,
                nzb_guid          TEXT,
                size_gb           REAL,
                score             REAL,
                trigger           TEXT NOT NULL DEFAULT 'auto',
                outcome           TEXT NOT NULL DEFAULT 'pending',
                outcome_reason    TEXT,
                outcome_at        TIMESTAMP,
                collected_at      TIMESTAMP,
                source_detail     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scraper_grabs_item ON scraper_grabs (item_id);
            CREATE INDEX IF NOT EXISTS idx_scraper_grabs_grabbed ON scraper_grabs (grabbed_at);
            CREATE INDEX IF NOT EXISTS idx_scraper_grabs_instance ON scraper_grabs (scraper_instance);
        """)
        # Settle a grab the moment its item enters the library, whichever of the
        # many code paths (queues, Plex/local scans, raw SQL) sets the state, so
        # a file deleted before anyone opens the stats page still counts as
        # collected. Also revives a 'removed' grab whose file was found again.
        # Removals stay lazy (reconcile): they only depend on current state.
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_scraper_grabs_collected
            AFTER UPDATE OF state ON media_items
            WHEN NEW.state IN ('Collected', 'Upgrading') AND OLD.state IS NOT NEW.state
            BEGIN
                UPDATE scraper_grabs
                SET outcome = 'collected',
                    outcome_reason = NULL,
                    outcome_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'),
                    collected_at = COALESCE(collected_at, strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'))
                WHERE id = (SELECT MAX(id) FROM scraper_grabs WHERE item_id = NEW.id)
                  AND outcome IN ('pending', 'removed');
            END
        """)
        # Runs at startup: keep the log bounded.
        conn.execute(
            "DELETE FROM scraper_grabs WHERE grabbed_at < ?",
            (_ts(datetime.now() - timedelta(days=_PRUNE_DAYS)),),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[ScraperStats] Could not create scraper_grabs table: {e}")
    finally:
        conn.close()


def _ts(dt: Optional[datetime] = None) -> str:
    """Timestamp text as stored in the table (same format sqlite3's deprecated
    default datetime adapter wrote, so comparisons and parsing stay consistent)."""
    return (dt or datetime.now()).isoformat(sep=' ')


# -- source extraction -----------------------------------------------------

def split_source(source: str) -> Tuple[str, str]:
    """Split a legacy free-text source ("Instance - Site", "Instance - Addon - Indexer")
    into (instance, indexer). The indexer is the last component."""
    parts = [p.strip() for p in (source or '').split(' - ') if p.strip()]
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], parts[-1]


def result_source(result: Optional[Dict[str, Any]]) -> Tuple[str, str, str]:
    """(scraper_type, scraper_instance, indexer) for a scrape result.

    Prefers the structured fields stamped by ScraperManager and the scrapers;
    falls back to parsing the legacy `source` string for results scraped before
    those fields existed. Returns empty strings when nothing is known.
    """
    if not isinstance(result, dict):
        return '', '', ''
    source = result.get('source') or ''
    if source in _NON_SCRAPER_SOURCES:
        return '', '', ''
    scraper_type = result.get('scraper_type') or ''
    instance = result.get('scraper_instance') or ''
    indexer = result.get('indexer') or ''
    if not instance:
        legacy_instance, legacy_indexer = split_source(source)
        instance = legacy_instance
        indexer = indexer or legacy_indexer
    return scraper_type, instance, indexer


def _result_kind(result: Dict[str, Any], kind: Optional[str]) -> str:
    if kind:
        return kind
    pi = result.get('parsed_info') or {}
    if result.get('protocol') == 'nzb' or pi.get('protocol') == 'nzb' or result.get('is_nzb_season_pack'):
        return 'nzb'
    return 'torrent'


def _score(result: Dict[str, Any]) -> Optional[float]:
    try:
        return float((result.get('score_breakdown') or {}).get('total_score'))
    except (TypeError, ValueError):
        return None


def _size(result: Dict[str, Any]) -> Optional[float]:
    try:
        return float(result.get('size'))
    except (TypeError, ValueError):
        return None


# -- writers -----------------------------------------------------------------

def queue_grab_trigger(item: Dict[str, Any]) -> str:
    """Trigger for a grab made by the automatic queues: an item carrying the
    upgrade markers (same check as AddingQueue._handle_failed_item) is an upgrade."""
    return 'upgrade' if item.get('upgrading') or item.get('upgrading_from') is not None else 'auto'


def _latest_row(conn, item_id):
    return conn.execute(
        "SELECT id, outcome, release_title, info_hash, nzb_guid, grabbed_at, trigger "
        "FROM scraper_grabs WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def _item_state(conn, item_id) -> Optional[str]:
    row = conn.execute("SELECT state FROM media_items WHERE id = ?", (item_id,)).fetchone()
    return row[0] if row else None


def _close_row(conn, row_id, outcome, reason, now, mark_collected=False):
    conn.execute(
        "UPDATE scraper_grabs SET outcome = ?, outcome_reason = ?, outcome_at = ?, "
        "collected_at = CASE WHEN ? THEN COALESCE(collected_at, ?) ELSE collected_at END "
        "WHERE id = ?",
        (outcome, reason, now, 1 if mark_collected else 0, now, row_id),
    )


def record_grab(
    item: Dict[str, Any],
    result: Optional[Dict[str, Any]],
    *,
    trigger: str = 'auto',
    kind: Optional[str] = None,
    release_title: Optional[str] = None,
    info_hash: Optional[str] = None,
    nzb_guid: Optional[str] = None,
) -> Optional[int]:
    """Log a grab for `item` and close out its previous grab. Never raises."""
    try:
        item_id = item.get('id') if isinstance(item, dict) else None
        if item_id is None:
            return None
        result = result if isinstance(result, dict) else {}
        scraper_type, instance, indexer = result_source(result)
        if not instance:
            instance = 'Manual' if trigger == 'manual' else 'Unknown'
        pi = result.get('parsed_info') or {}
        release_title = (release_title or result.get('original_title')
                         or result.get('title') or item.get('filled_by_title') or '')
        info_hash = (info_hash or result.get('hash') or result.get('info_hash') or '').lower() or None
        nzb_guid = nzb_guid or pi.get('guid') or None
        source_detail = None
        if result.get('episode_sources'):
            source_detail = json.dumps({str(k): v for k, v in result['episode_sources'].items()})

        now = _ts()
        conn = get_db_connection()
        try:
            prev = _latest_row(conn, item_id)
            if prev is not None:
                prev_id, prev_outcome, prev_title, prev_hash, prev_guid, _, _ = prev
                same_release = (
                    prev_title == release_title
                    and (prev_hash or None) == info_hash
                    and (prev_guid or None) == nzb_guid
                )
                if prev_outcome == 'pending' and same_release:
                    # The same release re-submitted for the same item (e.g. a
                    # stale in-memory queue entry): not a new grab.
                    return prev_id
                if prev_outcome == 'pending':
                    # Settle it from the item's state right now: the stats page
                    # may never have loaded while it sat in the library. Upgrades
                    # and repairs only start from an item that was in the library
                    # (its state has already moved to Adding by the time we get here).
                    if trigger in ('upgrade', 'repair') or _item_state(conn, item_id) in _LIBRARY_STATES:
                        prev_outcome = 'collected'
                        conn.execute(
                            "UPDATE scraper_grabs SET collected_at = COALESCE(collected_at, ?) WHERE id = ?",
                            (now, prev_id),
                        )
                    else:
                        _close_row(conn, prev_id, 'failed', 'superseded before collect', now)
                if prev_outcome == 'removed' and trigger == 'upgrade':
                    prev_outcome = 'collected'  # reconcile caught the upgrade mid-flight
                if prev_outcome == 'collected':
                    if trigger == 'repair':
                        _close_row(conn, prev_id, 'repaired', 'replaced by repair', now)
                    elif trigger == 'upgrade':
                        _close_row(conn, prev_id, 'replaced', 'replaced by upgrade', now)
                    else:
                        # Auto/manual grabs only happen once the item has left
                        # the library, so the old file was removed, not upgraded.
                        _close_row(conn, prev_id, 'removed', f'left the library before {trigger} re-grab', now)

            cur = conn.execute(
                """INSERT INTO scraper_grabs (
                       grabbed_at, item_id, media_type, imdb_id, title, season, episode,
                       version, kind, scraper_type, scraper_instance, indexer,
                       release_title, info_hash, nzb_guid, size_gb, score, trigger,
                       outcome, source_detail
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    now, item_id, item.get('type'), item.get('imdb_id'), item.get('title'),
                    item.get('season_number'), item.get('episode_number'), item.get('version'),
                    _result_kind(result, kind), scraper_type or None, instance, indexer or None,
                    release_title, info_hash, nzb_guid, _size(result), _score(result), trigger,
                    source_detail,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"[ScraperStats] record_grab failed (non-fatal): {e}")
        return None


def mark_current_grab(item_id, outcome: str, reason: str = '', *, only_trigger: Optional[str] = None) -> None:
    """Settle an item's latest grab explicitly. Never raises.

    'failed' only applies to a grab still pending. 'repaired' applies to a
    pending or collected grab and also counts it as collected (a repair means
    the file reached the library and turned out to be broken).
    only_trigger: act only if the latest grab was made with this trigger.
    """
    if item_id is None or outcome not in ('failed', 'repaired'):
        return
    try:
        now = _ts()
        conn = get_db_connection()
        try:
            row = _latest_row(conn, item_id)
            if row is None:
                return
            row_id, current, trigger = row[0], row[1], row[6]
            if only_trigger and trigger != only_trigger:
                return
            if outcome == 'failed' and current == 'pending':
                _close_row(conn, row_id, 'failed', (reason or '')[:300], now)
                # A failed upgrade leaves the old file in place: put back the
                # grab this one marked as replaced.
                if trigger == 'upgrade':
                    conn.execute(
                        "UPDATE scraper_grabs SET outcome = 'collected', outcome_reason = NULL, outcome_at = ? "
                        "WHERE id = (SELECT id FROM scraper_grabs WHERE item_id = ? AND id < ? "
                        "ORDER BY id DESC LIMIT 1) AND outcome = 'replaced'",
                        (now, item_id, row_id),
                    )
            elif outcome == 'repaired' and current in ('pending', 'collected'):
                _close_row(conn, row_id, 'repaired', (reason or '')[:300], now, mark_collected=True)
            else:
                return
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"[ScraperStats] mark_current_grab failed (non-fatal): {e}")


def reconcile(conn=None) -> None:
    """Settle pending grabs from the current media_items state.

    Run before reading stats instead of hooking every collect path (Plex scan,
    symlink, local scan, ...).
    """
    own = conn is None
    conn = conn or get_db_connection()
    try:
        now = _ts()
        latest = "SELECT MAX(id) FROM scraper_grabs GROUP BY item_id"
        lib = ','.join('?' * len(_LIBRARY_STATES))
        ret = ','.join('?' * len(_RETURNED_STATES))
        conn.execute(
            f"""UPDATE scraper_grabs
                SET outcome = 'collected', outcome_at = ?,
                    collected_at = COALESCE(collected_at,
                        (SELECT m.collected_at FROM media_items m WHERE m.id = scraper_grabs.item_id), ?)
                WHERE outcome = 'pending' AND id IN ({latest})
                  AND item_id IN (SELECT id FROM media_items WHERE state IN ({lib}))""",
            (now, now, *_LIBRARY_STATES),
        )
        conn.execute(
            f"""UPDATE scraper_grabs
                SET outcome = 'failed', outcome_at = ?,
                    outcome_reason = 'item returned to ' ||
                        (SELECT m.state FROM media_items m WHERE m.id = scraper_grabs.item_id)
                WHERE outcome = 'pending' AND id IN ({latest})
                  AND item_id IN (SELECT id FROM media_items WHERE state IN ({ret}))""",
            (now, *_RETURNED_STATES),
        )
        conn.execute(
            """UPDATE scraper_grabs SET outcome = 'failed', outcome_at = ?, outcome_reason = 'item removed'
               WHERE outcome = 'pending' AND item_id NOT IN (SELECT id FROM media_items)""",
            (now,),
        )
        # A collected file whose item has left the library (deleted, or reset
        # to Wanted etc.) without an upgrade or repair was removed. Repairs mark
        # their grab 'repaired' before resetting the item; an upgrade in flight
        # can pass through Scraping, so items carrying the upgrade markers
        # (same check as queue_grab_trigger) are left alone.
        conn.execute(
            f"""UPDATE scraper_grabs
                SET outcome = 'removed', outcome_at = ?,
                    outcome_reason = COALESCE('item moved to ' ||
                        (SELECT m.state FROM media_items m WHERE m.id = scraper_grabs.item_id),
                        'item deleted')
                WHERE outcome = 'collected' AND id IN ({latest})
                  AND (item_id NOT IN (SELECT id FROM media_items)
                       OR item_id IN (SELECT id FROM media_items WHERE state IN ({ret})
                                      AND COALESCE(upgrading, 0) = 0 AND upgrading_from IS NULL))""",
            (now, *_RETURNED_STATES),
        )
        # ...and comes back if the item returns to the library with no new grab
        # (e.g. a library scan finds the same file again).
        conn.execute(
            f"""UPDATE scraper_grabs SET outcome = 'collected', outcome_reason = NULL, outcome_at = ?
                WHERE outcome = 'removed' AND id IN ({latest})
                  AND item_id IN (SELECT id FROM media_items WHERE state IN ({lib}))""",
            (now, *_LIBRARY_STATES),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[ScraperStats] reconcile failed: {e}")
    finally:
        if own:
            conn.close()


# -- readers -----------------------------------------------------------------

def _filters(days: Optional[int], kind: Optional[str]) -> Tuple[str, list]:
    where, params = [], []
    if days:
        where.append("g.grabbed_at >= ?")
        params.append(_ts(datetime.now() - timedelta(days=int(days))))
    if kind in ('torrent', 'nzb'):
        where.append("g.kind = ?")
        params.append(kind)
    return ((' WHERE ' + ' AND '.join(where)) if where else ''), params


def get_scraper_stats(days: Optional[int] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    """Per-scraper totals with nested per-indexer rows."""
    conn = get_db_connection()
    try:
        reconcile(conn)
        where, params = _filters(days, kind)
        lib = ','.join('?' * len(_LIBRARY_STATES))
        rows = conn.execute(
            f"""SELECT COALESCE(g.scraper_instance, 'Unknown') AS instance,
                       MAX(g.scraper_type) AS scraper_type,
                       COALESCE(g.indexer, '') AS indexer,
                       COUNT(*) AS grabs,
                       SUM(g.outcome = 'pending') AS pending,
                       SUM(g.outcome = 'failed') AS failed,
                       SUM(g.outcome = 'replaced') AS replaced,
                       SUM(g.outcome = 'removed') AS removed,
                       SUM(g.outcome = 'repaired') AS repaired,
                       SUM(g.outcome IN ('collected', 'replaced', 'repaired', 'removed')) AS ever_collected,
                       SUM(g.outcome = 'collected' AND m.state IN ({lib})) AS in_library,
                       MAX(g.grabbed_at) AS last_grab
                FROM scraper_grabs g
                LEFT JOIN media_items m ON m.id = g.item_id
                {where}
                GROUP BY instance, indexer""",
            (*_LIBRARY_STATES, *params),
        ).fetchall()
        first = conn.execute("SELECT MIN(grabbed_at) FROM scraper_grabs").fetchone()[0]
    finally:
        conn.close()

    counters = ('grabs', 'pending', 'failed', 'replaced', 'removed', 'repaired', 'ever_collected', 'in_library')
    scrapers: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        row = dict(r)
        s = scrapers.setdefault(row['instance'], {
            'instance': row['instance'], 'scraper_type': row['scraper_type'] or '',
            'indexers': [], 'last_grab': None, **{c: 0 for c in counters},
        })
        if not s['scraper_type'] and row['scraper_type']:
            s['scraper_type'] = row['scraper_type']
        for c in counters:
            s[c] += row[c] or 0
        s['last_grab'] = max(filter(None, [s['last_grab'], row['last_grab']]), default=None)
        if row['indexer']:
            s['indexers'].append(_with_rates({
                'indexer': row['indexer'], 'last_grab': row['last_grab'],
                **{c: row[c] or 0 for c in counters},
            }))
    out = [_with_rates(s) for s in scrapers.values()]
    for s in out:
        s['indexers'].sort(key=lambda i: i['grabs'], reverse=True)
    out.sort(key=lambda s: s['grabs'], reverse=True)
    return {'scrapers': out, 'tracking_since': first}


def _with_rates(row: Dict[str, Any]) -> Dict[str, Any]:
    settled = row['grabs'] - row['pending']
    row['success_rate'] = round(100.0 * row['ever_collected'] / settled, 1) if settled else None
    row['repair_rate'] = (round(100.0 * row['repaired'] / row['ever_collected'], 1)
                          if row['ever_collected'] else None)
    return row


def get_recent_grabs(instance: Optional[str] = None, indexer: Optional[str] = None,
                     days: Optional[int] = None, kind: Optional[str] = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        where, params = _filters(days, kind)
        clauses = [where[len(' WHERE '):]] if where else []
        if instance is not None:
            clauses.append("COALESCE(g.scraper_instance, 'Unknown') = ?")
            params.append(instance)
        if indexer is not None:
            clauses.append("COALESCE(g.indexer, '') = ?")
            params.append(indexer)
        sql_where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = conn.execute(
            f"""SELECT g.id, g.grabbed_at, g.item_id, g.media_type, g.title, g.season, g.episode,
                       g.version, g.kind, g.scraper_instance, g.indexer, g.release_title,
                       g.trigger, g.outcome, g.outcome_reason, g.size_gb, g.source_detail
                FROM scraper_grabs g{sql_where}
                ORDER BY g.id DESC LIMIT ?""",
            (*params, int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get('source_detail'):
                try:
                    d['source_detail'] = json.loads(d['source_detail'])
                except ValueError:
                    d['source_detail'] = None
            out.append(d)
        return out
    finally:
        conn.close()
