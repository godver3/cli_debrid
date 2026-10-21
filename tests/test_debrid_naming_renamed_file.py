"""Debrid naming renames a single-file cli_mount entry's file as well as the entry.

With cli_mount's "Original name" folder naming the folder keeps its original name, so
the file lives at "<original folder>/<new name>.mkv" while filled_by_file still holds the
original filename - the item sat in Checking forever. And an item symlinked in the
seconds before the rename landed was left pointing at a path that no longer exists.
"""
import importlib.util
import os
import sqlite3
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.test_prefer_largest_nzb_source import lls  # noqa: F401  (shared fixture)

REPO_ROOT = Path(__file__).resolve().parent.parent

ORIG_FOLDER = "www.1TamilMV.cool - Dune (2021) UHD BluRay - 4K HDR10 - (DD+5.1 - 640Kbps) [Tam + Tel + Hin + Mal + Kan + Eng]"
ORIG_FILE = ORIG_FOLDER + ".mkv"
NEW_NAME = "Dune Part One (2021) - {imdb-tt1160419} - (www.1TamilMV.cool - Dune (2021) UHD BluRay - 4K HDR10 - (DD+5.1 - 640Kbps)"


class _Found(Exception):
    pass


def _check(lls, monkeypatch, mount, item):
    """Run check_local_file_for_item and return the source file it settled on (or None)."""
    found = []

    def _capture(source_file, _item):
        found.append(source_file)
        raise _Found

    monkeypatch.setattr(lls, "get_setting", lambda section, key, *a, **k: str(mount) if key == "original_files_path" else k.get("default"))
    monkeypatch.setattr(lls, "_apply_nzb_naming", _capture)
    try:
        lls.check_local_file_for_item(item)
    except _Found:
        pass
    return found[0] if found else None


def _item():
    return {"id": 25187, "type": "movie", "filled_by_file": ORIG_FILE, "filled_by_title": NEW_NAME,
            "debrid_folder_name": ORIG_FOLDER, "original_scraped_torrent_title": ORIG_FILE,
            "real_debrid_original_title": ORIG_FILE}


def test_renamed_file_found_under_original_folder(lls, monkeypatch, tmp_path):
    (tmp_path / ORIG_FOLDER).mkdir()
    (tmp_path / ORIG_FOLDER / (NEW_NAME + ".mkv")).write_bytes(b"x")
    item = _item()

    source = _check(lls, monkeypatch, tmp_path, item)

    assert source == str(tmp_path / ORIG_FOLDER / (NEW_NAME + ".mkv"))
    assert item["filled_by_file"] == NEW_NAME + ".mkv"
    assert (25187, {"filled_by_file": NEW_NAME + ".mkv"}) in lls._persisted


def test_renamed_file_found_under_renamed_folder(lls, monkeypatch, tmp_path):
    (tmp_path / NEW_NAME).mkdir()
    (tmp_path / NEW_NAME / (NEW_NAME + ".mkv")).write_bytes(b"x")
    item = dict(_item(), debrid_folder_name=NEW_NAME)

    assert _check(lls, monkeypatch, tmp_path, item) == str(tmp_path / NEW_NAME / (NEW_NAME + ".mkv"))


def test_original_file_still_preferred_and_untouched(lls, monkeypatch, tmp_path):
    (tmp_path / ORIG_FOLDER).mkdir()
    (tmp_path / ORIG_FOLDER / ORIG_FILE).write_bytes(b"x")
    item = _item()

    assert _check(lls, monkeypatch, tmp_path, item) == str(tmp_path / ORIG_FOLDER / ORIG_FILE)
    assert item["filled_by_file"] == ORIG_FILE
    assert lls._persisted == []


@pytest.fixture
def reconcile(monkeypatch):
    """Load utilities/debrid_rename_reconcile.py against an in-memory media_items table."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE media_items (id INTEGER, state TEXT, location_on_disk TEXT, "
               "filled_by_magnet TEXT, filled_by_torrent_id TEXT)")
    updates = []

    @contextmanager
    def _conn():
        yield db

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    settings = {"file_collection_management": "Symlinked/Local"}
    _stub("utilities.settings", get_setting=lambda section, key, *a, **k: settings.get(key))
    _stub("database.core", get_db_connection=_conn)
    _stub("database.database_writing", update_media_item=lambda item_id, **kw: updates.append((item_id, kw)))
    spec = importlib.util.spec_from_file_location("_reconcile_under_test", REPO_ROOT / "utilities" / "debrid_rename_reconcile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._db, module._updates, module._settings = db, updates, settings
    return module


HASH = "844a50d261343a8ead4cd2f2c9b159b24eae076e"


def _collected_symlink(reconcile, tmp_path):
    mount, links = tmp_path / "mnt", tmp_path / "symlinked"
    (mount / ORIG_FOLDER).mkdir(parents=True)
    links.mkdir()
    old_target = mount / ORIG_FOLDER / ORIG_FILE
    old_target.write_bytes(b"x")
    link = links / "Dune Part One (2021).mkv"
    os.symlink(old_target, link)
    reconcile._db.execute("INSERT INTO media_items VALUES (25187, 'Collected', ?, ?, NULL)",
                          (str(link), f"magnet:?xt=urn:btih:{HASH}"))
    return mount, link, old_target


def test_symlink_repointed_when_only_the_file_was_renamed(reconcile, tmp_path):
    mount, link, old_target = _collected_symlink(reconcile, tmp_path)
    new_target = mount / ORIG_FOLDER / (NEW_NAME + ".mkv")
    old_target.rename(new_target)  # what cli_mount did with "Original name" folder naming

    assert reconcile.repoint_symlinks_after_rename(HASH, NEW_NAME, attempts=1) == 1
    assert os.readlink(link) == str(new_target)
    assert reconcile._updates == [(25187, {"original_path_for_symlink": str(new_target),
                                           "filled_by_file": NEW_NAME + ".mkv",
                                           "debrid_folder_name": ORIG_FOLDER})]


def test_symlink_repointed_when_folder_and_file_were_renamed(reconcile, tmp_path):
    mount, link, old_target = _collected_symlink(reconcile, tmp_path)
    (mount / NEW_NAME).mkdir()
    new_target = mount / NEW_NAME / (NEW_NAME + ".mkv")
    old_target.rename(new_target)  # "File name" folder naming

    assert reconcile.repoint_symlinks_after_rename(HASH, NEW_NAME, attempts=1) == 1
    assert os.readlink(link) == str(new_target)


def test_working_symlink_and_other_modes_left_alone(reconcile, tmp_path):
    _, link, old_target = _collected_symlink(reconcile, tmp_path)
    assert reconcile.repoint_symlinks_after_rename(HASH, NEW_NAME, attempts=1) == 0
    assert os.readlink(link) == str(old_target)

    reconcile._settings["file_collection_management"] = "Plex"
    old_target.unlink()
    assert reconcile.repoint_symlinks_after_rename(HASH, NEW_NAME, attempts=1) == 0
    assert reconcile._updates == []
