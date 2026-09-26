"""godver3/cli_debrid#515: the stored debrid folder name can diverge from the
real folder on the mount, so the item's file is never found.

Covers both halves of the fix:
  - TorBox derives debrid_folder_name from its file paths' shared root folder,
    not the torrent's 'name' (which can be one episode's filename).
  - check_local_file_for_item's normalized folder-name key, used to find a
    folder whose name only differs by characters the mount dropped.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from tests.test_prefer_largest_nzb_source import lls  # noqa: F401  (shared fixture)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def torbox(monkeypatch):
    """Load the real debrid/torbox/client.py with its provider/DB imports stubbed."""
    class _Err(Exception):
        pass

    noop = lambda *a, **k: None
    stubs = {
        "bencodepy": {},
        "utilities.settings": {"get_setting": lambda *a, **k: k.get("default")},
        "debrid.base": {"DebridProvider": type("DebridProvider", (), {}),
                        "ProviderUnavailableError": _Err, "TooManyDownloadsError": _Err,
                        "TorrentAdditionError": _Err},
        "debrid.common": {"extract_hash_from_magnet": noop, "is_unwanted_file": noop,
                          "is_video_file": noop, "timed_lru_cache": lambda *a, **k: (lambda f: f)},
        "debrid.torbox.api": {"get_all_torrents": noop, "get_api_key": noop,
                              "get_data_payload": noop, "make_request": noop},
        "debrid.torbox.exceptions": {"TorboxAPIError": _Err, "TorboxAuthError": _Err},
        "database.not_wanted_magnets": {"add_to_not_wanted": noop},
        "utilities.phalanx_db_cache_manager": {"PhalanxDBClassManager": type("P", (), {})},
    }
    for pkg in ("debrid", "debrid.torbox"):
        mod = types.ModuleType(pkg)
        mod.__path__ = [str(REPO_ROOT / pkg.replace(".", "/"))]
        monkeypatch.setitem(sys.modules, pkg, mod)
    for name, attrs in stubs.items():
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
    # Real enum module - the status mapping in _normalize_torrent_info uses it.
    status_spec = importlib.util.spec_from_file_location("debrid.status", REPO_ROOT / "debrid" / "status.py")
    status_mod = importlib.util.module_from_spec(status_spec)
    monkeypatch.setitem(sys.modules, "debrid.status", status_mod)
    status_spec.loader.exec_module(status_mod)
    spec = importlib.util.spec_from_file_location(
        "debrid.torbox._client_under_test", REPO_ROOT / "debrid" / "torbox" / "client.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - sandbox missing an extra dep
        pytest.skip(f"torbox client import needs an unstubbed dependency: {exc}")
    return module


def _torbox_client(torbox):
    cls = next(v for v in vars(torbox).values()
               if isinstance(v, type) and hasattr(v, "_normalize_torrent_info"))
    return cls.__new__(cls)


def test_torbox_folder_comes_from_file_paths_not_name(torbox):
    client = _torbox_client(torbox)
    info = {
        "id": 1, "hash": "ABC", "download_state": "cached",
        "name": "The Rookie.S03E08 [1080p].mkv",  # one episode's name, not the pack
        "files": [
            {"id": 1, "name": "Rekrut - The Rookie 2018-2023 [S01-S05]/Season 3/The Rookie.S03E08 [1080p].mkv", "size": 1},
            {"id": 2, "name": "Rekrut - The Rookie 2018-2023 [S01-S05]/Season 3/The Rookie.S03E09 [1080p].mkv", "size": 1},
        ],
    }
    assert client._normalize_torrent_info(info)["debrid_folder_name"] == \
        "Rekrut - The Rookie 2018-2023 [S01-S05]"


def test_torbox_single_file_falls_back_to_name(torbox):
    client = _torbox_client(torbox)
    info = {"id": 1, "hash": "abc", "name": "Movie.2020.1080p.mkv",
            "files": [{"id": 1, "name": "Movie.2020.1080p.mkv", "size": 1}]}
    assert client._normalize_torrent_info(info)["debrid_folder_name"] == "Movie.2020.1080p.mkv"


def test_torbox_mixed_roots_falls_back_to_name(torbox):
    client = _torbox_client(torbox)
    info = {"id": 1, "hash": "abc", "name": "Pack",
            "files": [{"id": 1, "name": "A/x.mkv", "size": 1}, {"id": 2, "name": "B/y.mkv", "size": 1}]}
    assert client._normalize_torrent_info(info)["debrid_folder_name"] == "Pack"


def test_folder_key_ignores_characters_the_mount_stripped(lls):
    api_name = "[Anime Time] Jujutsu Kaisen (Season 1 & 2 + Movie + NCOP & NCED)"
    mount_name = "[Anime Time] Jujutsu Kaisen (Season 1  2 + Movie + NCOP  NCED)"
    assert lls._folder_match_key(api_name) == lls._folder_match_key(mount_name)


def test_folder_key_still_distinguishes_different_releases(lls):
    assert lls._folder_match_key("Show.S01.1080p-GRP") != lls._folder_match_key("Show.S02.1080p-GRP")
