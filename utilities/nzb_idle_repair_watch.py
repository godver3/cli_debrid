"""Idle-safe polling of cli_mount's already-produced broken-entry state."""

import logging
import time


log = logging.getLogger(__name__)

REDISPATCH_SECONDS = 6 * 60 * 60
_watch_state = {"fingerprint": None, "dispatched_at": 0.0}


def _broken_fingerprint(entries):
    """Build a stable in-memory identity without logging provider data."""
    identities = []
    for entry in entries:
        identities.append(
            (
                str(entry.get("info_hash") or entry.get("hash") or ""),
                str(entry.get("entry_name") or entry.get("name") or ""),
                str(entry.get("file_name") or ""),
                str(entry.get("cli_debrid_id") or ""),
            )
        )
    return tuple(sorted(identities))


def run_idle_repair_watch(
    *,
    connection_factory=None,
    fetch_broken=None,
    run_repair=None,
    state=None,
    time_fn=time.time,
):
    """Start the existing repair engine only when NZB acquisition is idle.

    ``fetch_broken`` reads cli_mount's already-produced health state; it does
    not start a provider scan. Waiting until no NZB is Adding or Checking keeps
    the repair engine from observing a just-submitted job as broken.
    """
    if connection_factory is None:
        from database.core import get_db_connection

        connection_factory = get_db_connection
    if fetch_broken is None or run_repair is None:
        from usenet.repair_engine import fetch_broken_items, run_repair as repair

        fetch_broken = fetch_broken or fetch_broken_items
        run_repair = run_repair or repair
    state = _watch_state if state is None else state

    conn = connection_factory()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM media_items "
            "WHERE state IN ('Adding','Checking') "
            "AND filled_by_torrent_id LIKE 'nzb:%'"
        ).fetchone()
        in_flight = int(row[0] or 0)
    finally:
        conn.close()

    if in_flight:
        log.debug(
            "[NZBIdleRepair] Deferring passive broken-item poll; %s NZB item(s) are in flight",
            in_flight,
        )
        return {"outcome": "busy", "in_flight": in_flight}

    broken = fetch_broken() or []
    if not broken:
        state["fingerprint"] = None
        state["dispatched_at"] = 0.0
        return {"outcome": "clean", "broken": 0}

    now = float(time_fn())
    fingerprint = _broken_fingerprint(broken)
    if (
        fingerprint == state.get("fingerprint")
        and now - float(state.get("dispatched_at") or 0) < REDISPATCH_SECONDS
    ):
        return {"outcome": "unchanged", "broken": len(broken)}

    log.warning(
        "[NZBIdleRepair] cli_mount reports %s broken item(s) while acquisition is idle; "
        "starting the existing bounded repair workflow",
        len(broken),
    )
    result = run_repair(triggered_by="idle_watch")
    state["fingerprint"] = fingerprint
    state["dispatched_at"] = now
    return {"outcome": "repair_started", "broken": len(broken), "result": result}
