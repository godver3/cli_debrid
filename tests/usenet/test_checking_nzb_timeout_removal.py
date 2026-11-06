"""When the Checking queue gives up on an NZB job it must remove that job from the
usenet provider - unless another item still uses it (a shared season pack).

Leaving it behind meant every abandoned attempt stayed on the mount; in Plex mode
each showed up as another copy of the episode (a user saw 4 copies of one).
"""
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
JOB = "nzb:abc-123"


class FakeUsenetClient:
    def __init__(self):
        self.removed = []

    def is_enabled(self):
        return True

    def remove_nzb(self, info_hash, entry_name=""):
        self.removed.append((info_hash, entry_name))
        return True


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_path = tmp_path / "media.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, state TEXT, filled_by_torrent_id TEXT)")
    conn.commit()
    conn.close()
    client = FakeUsenetClient()
    noop = lambda *a, **k: None

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    class _Any:
        def __init__(self, *a, **k):
            pass

    _stub("queues.run_program", get_and_add_recent_collected_from_plex=noop, run_recent_local_library_scan=noop)
    _stub("utilities.local_library_scan", check_local_file_for_item=noop)
    _stub("utilities.plex_functions", plex_update_item=noop)
    _stub("utilities.emby_functions", emby_update_item=noop)
    _stub("database.not_wanted_magnets", add_to_not_wanted=noop, add_to_not_wanted_urls=noop)
    _stub("queues.adding_queue", AddingQueue=_Any)
    _stub("debrid", get_debrid_provider=noop)
    _stub("utilities.settings", get_setting=lambda *a, **k: k.get("default"))
    _stub("debrid.common", timed_lru_cache=lambda *a, **k: (lambda f: f),
          extract_hash_from_magnet=noop, download_and_extract_hash=noop)
    _stub("utilities.phalanx_db_cache_manager", PhalanxDBClassManager=_Any)
    _stub("queues.upgrading_queue", UpgradingQueue=_Any)
    _stub("routes.notifications", send_upgrade_failed_notification=noop)
    _stub("debrid.status", TorrentFetchStatus=_Any)
    _stub("debrid.base", ProviderUnavailableError=Exception)
    _stub("database.database_reading", get_media_item_by_id=noop)
    _stub("database.core", get_db_connection=lambda: sqlite3.connect(db_path))
    _stub("database", get_db_connection=lambda: sqlite3.connect(db_path))
    _stub("usenet", get_usenet_client=lambda: client)

    spec = importlib.util.spec_from_file_location(
        "_checking_queue_under_test", REPO_ROOT / "queues" / "checking_queue.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - sandbox missing an extra dep
        pytest.skip(f"checking_queue import needs an unstubbed dependency: {exc}")
    queue = module.CheckingQueue.__new__(module.CheckingQueue)

    def add_rows(*rows):
        c = sqlite3.connect(db_path)
        c.executemany("INSERT INTO media_items (id, state, filled_by_torrent_id) VALUES (?, ?, ?)", rows)
        c.commit()
        c.close()

    return queue, client, add_rows


def test_unshared_timed_out_job_is_removed(env):
    queue, client, add_rows = env
    add_rows((1, "Checking", JOB))
    queue._remove_abandoned_nzb_job(JOB, [{"id": 1, "filled_by_title": "Show.S01E05.1080p"}])
    assert client.removed == [("abc-123", "Show.S01E05.1080p")]


def test_job_shared_with_a_collected_episode_is_kept(env):
    queue, client, add_rows = env
    add_rows((1, "Checking", JOB), (2, "Collected", JOB))  # season pack: E02 already collected
    queue._remove_abandoned_nzb_job(JOB, [{"id": 1, "filled_by_title": "Show.S01"}])
    assert client.removed == []


def test_unverifiable_sharing_keeps_the_job(env, monkeypatch):
    queue, client, _ = env

    def _broken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sys.modules["database"], "get_db_connection", _broken)
    queue._remove_abandoned_nzb_job(JOB, [{"id": 1}])
    assert client.removed == []


def test_non_nzb_ids_are_ignored(env):
    queue, client, _ = env
    queue._remove_abandoned_nzb_job("RDTORRENTID", [{"id": 1}])
    assert client.removed == []


def _prime_for_removal(queue, items, uncached=None):
    queue.items = list(items)
    queue.checking_queue_times = {}
    queue.progress_checks = {}
    queue.unknown_strikes = {}
    queue.missing_strikes = {}
    queue.uncached_torrents = uncached or {}


def test_remove_item_does_not_download_the_nzb_again(env, monkeypatch):
    # Every item leaving Checking used to re-download its NZB from the indexer just
    # to look for a torrent hash it never has - one wasted grab per attempt.
    queue, _, _ = env
    fetched = []
    monkeypatch.setitem(queue.remove_item.__globals__, "download_and_extract_hash",
                        lambda url, *a, **k: fetched.append(url))
    item = {"id": 1, "filled_by_torrent_id": JOB,
            "filled_by_magnet": "https://indexer.example/getnzb?id=abc.nzb&apikey=x"}
    _prime_for_removal(queue, [item])
    queue.remove_item(item)
    assert fetched == []
    assert queue.items == []


def test_remove_item_still_clears_uncached_tracking(env):
    queue, _, _ = env
    item = {"id": 1, "filled_by_torrent_id": "RDTORRENTID",
            "filled_by_magnet": "https://tracker.example/file.torrent"}
    _prime_for_removal(queue, [item], uncached={
        "a" * 40: {"last_check_time": 0, "item_ids": [1]},
        "b" * 40: {"last_check_time": 0, "item_ids": [1, 2]},
    })
    queue.remove_item(item)
    assert queue.uncached_torrents == {"b" * 40: {"last_check_time": 0, "item_ids": [2]}}


FOLDER = "The.War.Wagon.1967.2160p.UHD.BluRay.x265-B0MBARDiERS"
HOUR = 3600.0


@pytest.fixture
def stall(env, monkeypatch, tmp_path):
    """A finished Plex-mode NZB whose folder is on the mount, and a fresh plex_health."""
    queue, _, _ = env
    from utilities import plex_health
    plex_health._reset_for_tests()
    mount = tmp_path / "mnt" / "__all__"
    (mount / FOLDER).mkdir(parents=True)
    settings = {("File Management", "file_collection_management"): "Plex",
                ("Plex", "mounted_file_location"): str(mount)}
    monkeypatch.setitem(queue._hold_finished_nzb_for_stuck_plex.__globals__, "get_setting",
                        lambda s, k, *a, **kw: settings.get((s, k), kw.get("default", a[0] if a else "")))
    layout = types.ModuleType("usenet.mount_layout")
    spec = importlib.util.spec_from_file_location("_mount_layout", REPO_ROOT / "usenet" / "mount_layout.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    layout.resolve_nzb_job_dir = real.resolve_nzb_job_dir
    monkeypatch.setitem(sys.modules, "usenet.mount_layout", layout)
    queue._plex_stall_held_at = {}
    items = [{"id": 1, "debrid_folder_name": FOLDER, "filled_by_title": FOLDER}]
    return queue, plex_health, settings, mount, items


def _hold(queue, items, entered, now, tid=JOB):
    return queue._hold_finished_nzb_for_stuck_plex(tid, items, entered, now)


def _waiting(queue, *entered_times, tid_prefix="nzb:ahead"):
    """Put other finished NZB jobs in Checking that arrived at the given times."""
    queue.items = [{"id": 100 + n, "filled_by_torrent_id": f"{tid_prefix}-{n}"} for n in range(len(entered_times))]
    queue.checking_queue_times = {100 + n: t for n, t in enumerate(entered_times)}


@pytest.fixture
def clock(monkeypatch):
    from utilities import plex_health
    now = [100 * HOUR]
    monkeypatch.setattr(plex_health.time, "time", lambda: now[0])
    return now


def test_held_while_plex_is_timing_out(stall, clock):
    queue, health, _, _, items = stall
    _waiting(queue)
    health.record_contact_failure()
    clock[0] += 300  # failed 5 min ago, inside the window
    entered = clock[0] - 900
    assert _hold(queue, items, entered=entered, now=clock[0])
    assert queue._plex_stall_held_at[JOB] == clock[0]


def test_not_held_once_failures_are_old(stall, clock):
    queue, health, _, _, items = stall
    _waiting(queue)
    health.record_contact_failure()
    clock[0] += health.FAILURE_WINDOW + 1
    assert not _hold(queue, items, entered=clock[0] - 900, now=clock[0])


def test_held_when_plex_is_behind_on_earlier_releases(stall, clock):
    # 3 releases finished before this one and are still waiting; Plex has added 1 since.
    queue, health, _, _, items = stall
    entered = clock[0] - 900
    _waiting(queue, entered - 300, entered - 200, entered - 100)
    health.record_recently_added("1", [entered + 60, entered - 4 * HOUR])
    assert _hold(queue, items, entered=entered, now=clock[0])


def test_not_held_when_plex_got_past_this_release(stall, clock):
    # Plex added more than were waiting ahead, so it skipped this file: old behavior.
    queue, health, _, _, items = stall
    entered = clock[0] - 900
    _waiting(queue, entered - 300)
    health.record_recently_added("1", [entered + 60, entered + 120])
    assert not _hold(queue, items, entered=entered, now=clock[0])


def test_lone_release_plex_ignores_still_times_out(stall, clock):
    # Nothing ahead of it and Plex reachable: a file Plex won't add is replaced as before.
    queue, health, _, _, items = stall
    entered = clock[0] - 900
    _waiting(queue)
    health.record_recently_added("1", [entered - HOUR])
    assert not _hold(queue, items, entered=entered, now=clock[0])


def test_later_arrivals_do_not_count_as_ahead(stall, clock):
    queue, health, _, _, items = stall
    entered = clock[0] - 900
    _waiting(queue, entered + 60, entered + 120)
    health.record_recently_added("1", [])
    assert not _hold(queue, items, entered=entered, now=clock[0])


def test_not_held_after_restart_with_nothing_known(stall, clock):
    queue, _, _, _, items = stall
    entered = clock[0] - 900
    _waiting(queue, entered - 300)
    assert not _hold(queue, items, entered=entered, now=clock[0])


def test_not_held_past_the_cap(stall, clock):
    queue, health, _, _, items = stall
    _waiting(queue)
    health.record_contact_failure()
    assert not _hold(queue, items, entered=clock[0] - queue.PLEX_STALL_MAX_WAIT - 1, now=clock[0])


def test_not_held_for_torrents(stall, clock):
    queue, health, _, _, items = stall
    _waiting(queue)
    health.record_contact_failure()
    assert not _hold(queue, items, entered=clock[0] - 900, now=clock[0], tid="RDTORRENTID")


def test_not_held_in_symlink_mode(stall, clock):
    queue, health, settings, _, items = stall
    settings[("File Management", "file_collection_management")] = "Symlinked/Local"
    _waiting(queue)
    health.record_contact_failure()
    assert not _hold(queue, items, entered=clock[0] - 900, now=clock[0])


def test_not_held_when_folder_is_not_on_the_mount(stall, clock):
    queue, health, _, mount, items = stall
    (mount / FOLDER).rmdir()
    _waiting(queue)
    health.record_contact_failure()
    assert not _hold(queue, items, entered=clock[0] - 900, now=clock[0])


def test_hold_is_forgotten_when_the_job_leaves_checking(stall):
    queue, _, _, _, _ = stall
    item = {"id": 1, "filled_by_torrent_id": JOB}
    _prime_for_removal(queue, [item])
    queue._plex_stall_held_at = {JOB: HOUR}
    queue.remove_item(item)
    assert JOB not in queue._plex_stall_held_at
