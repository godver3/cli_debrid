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
