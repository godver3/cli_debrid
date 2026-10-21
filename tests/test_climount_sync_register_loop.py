"""cli_mount sync must not re-register unchanged entries every pass.

cli_mount saves an entry on every PATCH /cli_ids and addTags call, which sets
its UpdatedAt to now - later than the since-timestamp the sync pass records at
its start. Registering (or tagging) unconditionally therefore put every touched
entry back into the next pass's changes, forever: a live report showed ~100,000
"Registered cli_debrid IDs" calls in 6 hours pegging cli_mount's CPU.
"""
import importlib.util
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HASH = "abc123hash"


class FakeCliMount:
    """Mirrors cli_mount's behaviour: every save bumps UpdatedAt to now."""

    base_url = "http://cli_mount.test"

    def __init__(self):
        self.entries = {HASH: {
            "info_hash": HASH, "protocol": "nzb", "updated_at": time.time(),
            "folder_name": "Show.S01E01.1080p", "original_filename": "Show.S01E01.1080p",
            "provider_id": f"nzb:{HASH}", "bad": False, "file_count": 1,
            "files": [{"name": "Show.S01E01.1080p.mkv", "size": 1_000_000}],
            "cli_debrid_ids": {},
        }}
        self.register_calls = 0
        self.tag_calls = 0

    # client surface used by climount_sync
    def is_enabled(self):
        return True

    def _headers(self):
        return {}

    def register_cli_ids(self, info_hash, ids):
        self.register_calls += 1
        entry = self.entries[info_hash]
        entry["cli_debrid_ids"] = dict(ids)
        entry["updated_at"] = time.time()
        return True

    def push_tags(self, info_hash, tags):
        self.tag_calls += 1
        self.entries[info_hash]["updated_at"] = time.time()
        return True

    # GET /api/sync/changes?since=
    def changes(self, url):
        since = int(url.split("since=")[1]) if "since=" in url else 0
        out = []
        for e in self.entries.values():
            if since and not e["updated_at"] > since:
                continue
            row = dict(e)
            row["updated_at"] = int(e["updated_at"])
            out.append(row)
        return out


@pytest.fixture
def sync_env(monkeypatch, tmp_path):
    db_path = tmp_path / "media.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE media_items (
        id INTEGER PRIMARY KEY, title TEXT, type TEXT, state TEXT, version TEXT,
        season_number INTEGER, episode_number INTEGER, size REAL,
        filled_by_magnet TEXT, filled_by_torrent_id TEXT, filled_by_file TEXT,
        filled_by_title TEXT, location_on_disk TEXT, location_basename TEXT,
        debrid_folder_name TEXT, original_scraped_torrent_title TEXT,
        original_filename TEXT, nzb_segment_id TEXT, tags TEXT,
        tags_pushed_at TIMESTAMP, last_updated TIMESTAMP)""")
    conn.execute(
        "INSERT INTO media_items (id, title, type, state, version, season_number, episode_number, "
        "filled_by_torrent_id, filled_by_file, tags, last_updated) "
        "VALUES (1, 'Show', 'episode', 'Collected', '1080p', 1, 1, ?, 'Show.S01E01.1080p.mkv', "
        "'plex-tag', '2026-01-01 00:00:00')",
        (f"nzb:{HASH}",),
    )
    conn.commit()
    conn.close()

    mount = FakeCliMount()
    settings = {}

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    _stub("utilities.settings",
          get_setting=lambda section, key, default=None: settings.get((section, key), default),
          set_setting=lambda section, key, value: settings.__setitem__((section, key), value))
    _stub("database.core", get_db_connection=lambda: sqlite3.connect(db_path))
    _stub("usenet.climount_client", get_climount_client=lambda: mount)
    _stub("routes.api_tracker",
          api=types.SimpleNamespace(get=lambda url, **kw: _Resp(mount.changes(url))))
    for pkg in ("debrid", "debrid.common"):
        _stub(pkg, __path__=[str(REPO_ROOT / pkg.replace(".", "/"))])

    spec = importlib.util.spec_from_file_location(
        "_climount_sync_under_test", REPO_ROOT / "usenet" / "climount_sync.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - sandbox missing an extra dep
        pytest.skip(f"climount_sync import needs an unstubbed dependency: {exc}")
    return module, mount, settings


def _pass_after_clock_tick(sync):
    # cli_mount compares UpdatedAt against a whole-second since; step past the
    # current second so a pass that genuinely changed nothing reads as quiet.
    start = int(time.time())
    while int(time.time()) == start:
        time.sleep(0.05)
    return sync.sync_changes_from_climount()


def test_second_pass_sends_nothing_when_nothing_changed(sync_env):
    sync, mount, _ = sync_env

    sync.sync_changes_from_climount(force_full=True)
    assert mount.register_calls == 1
    assert mount.tag_calls == 1
    assert mount.entries[HASH]["cli_debrid_ids"] == {"Show.S01E01.1080p.mkv": 1}

    # Pass 1's own register/tag saves bumped UpdatedAt, so pass 2 still sees the
    # entry once - but must recognise there is nothing new to send.
    _pass_after_clock_tick(sync)
    assert (mount.register_calls, mount.tag_calls) == (1, 1)

    # With nothing re-saved, the entry has now dropped out of the changes feed.
    summary = _pass_after_clock_tick(sync)
    assert summary["fetched"] == 0
    assert (mount.register_calls, mount.tag_calls) == (1, 1)


def test_changed_ids_are_still_registered(sync_env):
    sync, mount, _ = sync_env
    sync.sync_changes_from_climount(force_full=True)
    assert mount.register_calls == 1

    # A second episode lands on the same pack job, and cli_mount's entry changes
    # (a new file appears) - the map now differs and must be sent.
    conn = sys.modules["database.core"].get_db_connection()
    conn.execute(
        "INSERT INTO media_items (id, title, type, state, version, season_number, episode_number, "
        "filled_by_torrent_id, filled_by_file, last_updated) "
        "VALUES (2, 'Show', 'episode', 'Collected', '1080p', 1, 2, ?, 'Show.S01E02.1080p.mkv', "
        "'2026-01-01 00:00:00')",
        (f"nzb:{HASH}",),
    )
    conn.commit()
    conn.close()
    entry = mount.entries[HASH]
    entry["files"].append({"name": "Show.S01E02.1080p.mkv", "size": 1_000_001})
    entry["updated_at"] = time.time()

    _pass_after_clock_tick(sync)
    assert mount.register_calls == 2
    assert entry["cli_debrid_ids"] == {"Show.S01E01.1080p.mkv": 1, "Show.S01E02.1080p.mkv": 2}


def test_same_cli_ids_normalises_types(sync_env):
    sync, _, _ = sync_env
    assert sync._same_cli_ids({"a.mkv": "5"}, {"a.mkv": 5})
    assert not sync._same_cli_ids(None, {"a.mkv": 5})
    assert not sync._same_cli_ids({"a.mkv": 5}, {"a.mkv": 5, "b.mkv": 6})


def _row(sync_mod_conn_factory, item_id):
    conn = sync_mod_conn_factory()
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM media_items WHERE id = ?", (item_id,)).fetchone())
    finally:
        conn.close()


def test_sync_keeps_nzb_url_and_own_segment_id(sync_env):
    """The NZB URL is what blacklisting reads to add the release's GUID; the stored
    segment id is what the pre-submit check compares against. The sync used to
    null the URL and overwrite the segment with cli_mount's (different) one."""
    sync, mount, _ = sync_env
    get_conn = sys.modules["database.core"].get_db_connection
    conn = get_conn()
    conn.execute("UPDATE media_items SET filled_by_magnet = ?, nzb_segment_id = ? WHERE id = 1",
                 ("https://api.indexer.test/getnzb/dd49a3a2.nzb&i=1&r=key", "oawhy-own@seg"))
    conn.commit()
    conn.close()
    mount.entries[HASH]["nzb_segment_id"] = "6dbXq-climount@seg"

    sync.sync_changes_from_climount(force_full=True)

    row = _row(get_conn, 1)
    assert row["filled_by_magnet"] == "https://api.indexer.test/getnzb/dd49a3a2.nzb&i=1&r=key"
    assert row["nzb_segment_id"] == "oawhy-own@seg"


def test_sync_still_clears_a_leftover_debrid_magnet(sync_env):
    sync, _, _ = sync_env
    get_conn = sys.modules["database.core"].get_db_connection
    conn = get_conn()
    conn.execute("UPDATE media_items SET filled_by_magnet = 'magnet:?xt=urn:btih:abc' WHERE id = 1")
    conn.commit()
    conn.close()

    sync.sync_changes_from_climount(force_full=True)
    assert _row(get_conn, 1)["filled_by_magnet"] is None


def test_abandoned_item_is_not_relinked_via_cli_debrid_ids(sync_env):
    """cli_mount keeps cli_debrid_ids after cli_debrid gives up on a job and moves
    the item back to Wanted (clearing its filled_by fields). The sync must not
    write the dead job back onto it."""
    sync, mount, _ = sync_env
    get_conn = sys.modules["database.core"].get_db_connection
    sync.sync_changes_from_climount(force_full=True)  # registers {file: 1}
    conn = get_conn()
    conn.execute("UPDATE media_items SET state = 'Wanted', filled_by_torrent_id = NULL, "
                 "filled_by_file = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    mount.entries[HASH]["updated_at"] = time.time()

    _pass_after_clock_tick(sync)

    row = _row(get_conn, 1)
    assert row["filled_by_torrent_id"] is None
    assert row["filled_by_file"] is None
