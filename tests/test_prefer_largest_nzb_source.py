"""_prefer_largest_nzb_source must only second-guess NZB job folders.

Regression: a movie manually assigned from a multi-movie debrid collection pack
(8 Harry Potter films in one folder) was correctly assigned its own file, then
swapped at symlink time for the pack's largest film (Half-Blood Prince).
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

HP_FILES = {
    "1. Harry Potter and the Sorcerer's Stone 2001 Extended 10bit BluRay x265.mkv": 3100,
    "3. Harry Potter and the Prisoner of Azkaban 2004 10bit BluRay x265.mkv": 2981,
    "6. Harry Potter and the Half-Blood Prince 2009 10bit Bluray x265.mkv": 3478,
}
AZKABAN = "3. Harry Potter and the Prisoner of Azkaban 2004 10bit BluRay x265.mkv"
HALF_BLOOD = "6. Harry Potter and the Half-Blood Prince 2009 10bit Bluray x265.mkv"


@pytest.fixture
def lls(monkeypatch):
    """Load the real utilities/local_library_scan.py with its heavy imports stubbed."""
    persisted = []

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    noop = lambda *a, **k: None
    _stub("utilities.settings", get_setting=lambda *a, **k: k.get("default"))
    _stub("utilities.anidb_functions", format_filename_with_anidb=noop)
    _stub("database", get_db_connection=noop)
    _stub("database.database_writing", update_media_item_state=noop,
          update_media_item=lambda item_id, **kw: persisted.append((item_id, kw)))
    _stub("utilities.post_processing", handle_state_change=noop)
    _stub("database.symlink_verification", add_symlinked_file_for_verification=noop,
          add_path_for_removal_verification=noop, remove_verification_by_media_item_id=noop)
    _stub("database.database_reading", get_all_media_items=noop,
          get_media_item_by_id=noop, get_season_year=noop)
    _stub("scraper.functions.ptt_parser", parse_with_ptt=noop)
    # Real debrid/common/utils.py, without debrid/__init__.py's provider imports.
    for pkg in ("debrid", "debrid.common"):
        _stub(pkg, __path__=[str(REPO_ROOT / pkg.replace(".", "/"))])

    spec = importlib.util.spec_from_file_location(
        "_lls_under_test", REPO_ROOT / "utilities" / "local_library_scan.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - sandbox missing an extra dep
        pytest.skip(f"local_library_scan import needs an unstubbed dependency: {exc}")
    module._persisted = persisted
    return module


@pytest.fixture
def pack_folder(tmp_path):
    for name, mib in HP_FILES.items():
        with open(tmp_path / name, "wb") as fh:
            fh.truncate(mib * 1024 * 1024)
    return tmp_path


def test_debrid_movie_keeps_its_assigned_file(lls, pack_folder):
    item = {"id": 42557, "type": "movie", "filled_by_torrent_id": "sauug71y9c1vwagw04kswy9z",
            "filled_by_file": AZKABAN}
    source = os.path.join(pack_folder, AZKABAN)

    assert lls._prefer_largest_nzb_source(item, str(pack_folder), source) == source
    assert item["filled_by_file"] == AZKABAN
    assert lls._persisted == []


def test_nzb_folder_still_prefers_largest_candidate(lls, pack_folder):
    item = {"id": 1, "type": "movie", "filled_by_torrent_id": "nzb:abc123",
            "filled_by_file": AZKABAN}
    source = os.path.join(pack_folder, AZKABAN)

    assert lls._prefer_largest_nzb_source(item, str(pack_folder), source) == \
        os.path.join(pack_folder, HALF_BLOOD)
    assert lls._persisted == [(1, {"filled_by_file": HALF_BLOOD})]
