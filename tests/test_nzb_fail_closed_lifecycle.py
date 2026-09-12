from pathlib import Path

from utilities.nzb_idle_repair_watch import REDISPATCH_SECONDS, run_idle_repair_watch
from utilities.nzb_playability_guard import (
    BROKEN,
    HEALTHY,
    UNKNOWN,
    classify_new_nzb,
    nzb_job_ready,
)


class _Connection:
    def __init__(self, in_flight):
        self.in_flight = in_flight
        self.closed = False

    def execute(self, sql):
        assert "state IN ('Adding','Checking')" in sql
        return self

    def fetchone(self):
        return (self.in_flight,)

    def close(self):
        self.closed = True


def _classify(results, duration=100):
    values = iter(results)
    return classify_new_nzb(
        "/mount/item.mkv",
        attempts=len(results),
        duration_reader=lambda *_a, **_k: duration,
        packet_reader=lambda *_a, **_k: next(values),
        sleep_fn=lambda _seconds: None,
        fraction_reader=lambda: 0.5,
    )


def test_new_nzb_requires_one_conclusive_success():
    assert _classify([None, False, True]) == HEALTHY


def test_new_nzb_all_clean_failures_are_broken():
    assert _classify([False, False, False]) == BROKEN


def test_new_nzb_timeout_is_unknown_not_healthy_or_broken():
    assert _classify([False, None, False]) == UNKNOWN


def test_new_nzb_uses_one_stable_deep_offset_for_all_attempts():
    offsets = []
    verdict = classify_new_nzb(
        "/mount/item.mkv",
        attempts=3,
        duration_reader=lambda *_a, **_k: 200,
        packet_reader=lambda *_a, **kw: offsets.append(kw["offset_seconds"]) or False,
        sleep_fn=lambda _seconds: None,
        fraction_reader=lambda: 0.25,
    )
    assert verdict == BROKEN
    assert offsets == [50, 50, 50]


def test_nzb_job_must_be_conclusively_completed_before_probe():
    assert nzb_job_ready({"state": "completed", "progress": 100}) is True
    assert nzb_job_ready({"state": "downloading", "progress": 99}) is False
    assert nzb_job_ready({"state": "unknown", "progress": 100}) is False
    assert nzb_job_ready(None) is False


def test_idle_watch_does_not_poll_or_repair_while_nzb_is_in_flight():
    conn = _Connection(2)
    called = []
    result = run_idle_repair_watch(
        connection_factory=lambda: conn,
        fetch_broken=lambda: called.append("fetch"),
        run_repair=lambda **_kwargs: called.append("repair"),
    )
    assert result == {"outcome": "busy", "in_flight": 2}
    assert called == []
    assert conn.closed


def test_idle_watch_is_passive_when_health_state_is_clean():
    conn = _Connection(0)
    called = []
    result = run_idle_repair_watch(
        connection_factory=lambda: conn,
        fetch_broken=lambda: called.append("fetch") or [],
        run_repair=lambda **_kwargs: called.append("repair"),
    )
    assert result == {"outcome": "clean", "broken": 0}
    assert called == ["fetch"]


def test_idle_watch_invokes_existing_repair_once_for_reported_breakage():
    conn = _Connection(0)
    calls = []
    result = run_idle_repair_watch(
        connection_factory=lambda: conn,
        fetch_broken=lambda: [{"status": "broken"}, {"status": "broken"}],
        run_repair=lambda **kwargs: calls.append(kwargs) or {"replaced": 1},
    )
    assert result["outcome"] == "repair_started"
    assert result["broken"] == 2
    assert calls == [{"triggered_by": "idle_watch"}]


def test_idle_watch_suppresses_unchanged_broken_set_until_redispatch_window():
    state = {"fingerprint": None, "dispatched_at": 0.0}
    calls = []
    broken = [{"info_hash": "one", "file_name": "episode.mkv"}]

    first = run_idle_repair_watch(
        connection_factory=lambda: _Connection(0),
        fetch_broken=lambda: broken,
        run_repair=lambda **kwargs: calls.append(kwargs) or {},
        state=state,
        time_fn=lambda: 100.0,
    )
    unchanged = run_idle_repair_watch(
        connection_factory=lambda: _Connection(0),
        fetch_broken=lambda: list(reversed(broken)),
        run_repair=lambda **kwargs: calls.append(kwargs) or {},
        state=state,
        time_fn=lambda: 160.0,
    )
    redispatched = run_idle_repair_watch(
        connection_factory=lambda: _Connection(0),
        fetch_broken=lambda: broken,
        run_repair=lambda **kwargs: calls.append(kwargs) or {},
        state=state,
        time_fn=lambda: 100.0 + REDISPATCH_SECONDS,
    )

    assert first["outcome"] == "repair_started"
    assert unchanged == {"outcome": "unchanged", "broken": 1}
    assert redispatched["outcome"] == "repair_started"
    assert calls == [
        {"triggered_by": "idle_watch"},
        {"triggered_by": "idle_watch"},
    ]


def test_idle_watch_new_broken_identity_dispatches_immediately():
    state = {"fingerprint": None, "dispatched_at": 0.0}
    calls = []
    common = dict(
        connection_factory=lambda: _Connection(0),
        run_repair=lambda **kwargs: calls.append(kwargs) or {},
        state=state,
        time_fn=lambda: 100.0,
    )
    run_idle_repair_watch(fetch_broken=lambda: [{"info_hash": "one"}], **common)
    changed = run_idle_repair_watch(
        fetch_broken=lambda: [{"info_hash": "one"}, {"info_hash": "two"}],
        **common,
    )
    assert changed["outcome"] == "repair_started"
    assert len(calls) == 2


def test_run_program_wires_independent_idle_watch_and_fail_closed_gate():
    source = (Path(__file__).parents[1] / "queues" / "run_program.py").read_text()
    assert "'task_nzb_idle_repair_watch': 60" in source
    assert "'task_nzb_idle_repair_watch'," in source
    assert "def task_nzb_idle_repair_watch(self):" in source
    assert source.count("is not True:\n                        continue") >= 2
    queue_tasks = source.split("_QUEUE_TASKS = {", 1)[1].split("}", 1)[0]
    assert "task_nzb_idle_repair_watch" not in queue_tasks
    assert "if not nzb_job_ready(job_status):" in source
    assert "if is_nzb:\n                logging.info" in source
    assert "verdict = classify_new_nzb(actual_file_path)" in source
    assert "if verdict == UNKNOWN:" in source
    assert "from usenet.repair_engine import _verify_file_readable" in source


def test_checking_queue_wires_nzb_period_backstop():
    # CheckingQueue can't be imported here (heavy deps); pin the #499 backstop
    # call site so a refactor can't silently drop GUID blacklisting on timeout.
    source = (Path(__file__).parents[1] / "queues" / "checking_queue.py").read_text()
    assert "from utilities.nzb_checking_backstop import apply_nzb_checking_timeout" in source
    assert "apply_nzb_checking_timeout(" in source
    assert 'content not found within {checking_queue_limit}s — adding to not-wanted' in source


def test_inconclusive_probe_stays_in_checking_until_period_timeout_blacklists_guid():
    """Composition: #499 fail-closed defer + Checking period backstop.

    An inconclusive probe must not admit the item (gate contract: only True
    proceeds). Once time_in_queue exceeds checking_queue_period, the NZB GUID
    is blacklisted and the item returns to Wanted — the path that replaced the
    old tick-based force-collect.
    """
    from utilities.nzb_checking_backstop import (
        apply_nzb_checking_timeout,
        checking_period_exceeded,
    )

    # 1) Inconclusive probe → UNKNOWN → callers treat as "not True" and defer.
    assert _classify([False, None, False]) == UNKNOWN
    gate_result = None  # run_program maps UNKNOWN → None
    assert gate_result is not True

    item = {"id": 42, "filled_by_magnet": "https://indexer.example/guid-xyz"}
    not_wanted = []
    moved = []

    # 2) Still under the period limit — backstop must not fire yet.
    assert checking_period_exceeded(30.0, 120.0) is False
    assert (
        apply_nzb_checking_timeout(
            torrent_id="nzb:deadbeef",
            items=[item],
            time_in_queue=30.0,
            limit=120.0,
            add_to_not_wanted_nzb_guid=not_wanted.append,
            move_to_wanted=lambda it, state: moved.append((it["id"], state)),
            contains_item_id=lambda _id: True,
        )
        is False
    )
    assert not_wanted == []
    assert moved == []

    # 3) Time advances past the limit — GUID blacklisted, item → Wanted.
    assert checking_period_exceeded(130.0, 120.0) is True
    assert (
        apply_nzb_checking_timeout(
            torrent_id="nzb:deadbeef",
            items=[item],
            time_in_queue=130.0,
            limit=120.0,
            add_to_not_wanted_nzb_guid=not_wanted.append,
            move_to_wanted=lambda it, state: moved.append((it["id"], state)),
            contains_item_id=lambda _id: True,
        )
        is True
    )
    assert not_wanted == ["https://indexer.example/guid-xyz"]
    assert moved == [(42, "Checking")]


def test_checking_timeout_ignores_non_nzb_and_already_removed_items():
    from utilities.nzb_checking_backstop import apply_nzb_checking_timeout

    not_wanted = []
    moved = []
    assert (
        apply_nzb_checking_timeout(
            torrent_id="rd:abc123",
            items=[{"id": 1, "filled_by_magnet": "magnet:?xt=urn:btih:abc"}],
            time_in_queue=999.0,
            limit=120.0,
            add_to_not_wanted_nzb_guid=not_wanted.append,
            move_to_wanted=lambda it, state: moved.append(it["id"]),
            contains_item_id=lambda _id: True,
        )
        is False
    )
    assert not_wanted == []
    assert moved == []

    assert (
        apply_nzb_checking_timeout(
            torrent_id="nzb:abc",
            items=[{"id": 7, "filled_by_magnet": "https://indexer.example/guid-7"}],
            time_in_queue=999.0,
            limit=120.0,
            add_to_not_wanted_nzb_guid=not_wanted.append,
            move_to_wanted=lambda it, state: moved.append(it["id"]),
            contains_item_id=lambda _id: False,  # already removed from Checking
        )
        is True
    )
    assert not_wanted == ["https://indexer.example/guid-7"]
    assert moved == []
