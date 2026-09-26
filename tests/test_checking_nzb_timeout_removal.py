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

REPO_ROOT = Path(__file__).resolve().parent.parent
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
