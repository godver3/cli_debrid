"""Survivor (US), 2026-11-26: TorBox had 'Survivor Borneo Complete Season 1' and
'Survivor Collection S01-S21' cached, but the title check scored them 0.45 and
0.38 against 'Survivor' (PTT keeps 'Borneo' / 'Collection' in the title)."""
from unittest.mock import Mock

import pytest

from scraper.functions.filter_results import _named_season_or_pack_title, filter_results
from scraper.functions.file_processing import parse_torrent_info

SEASONS = {"1": {"title": "Borneo"}, "2": {"title": "The Australian Outback"}, "4": {"title": "Marquesas"}}
VERSION = {"max_resolution": "1080p", "min_bitrate_mbps": 0, "max_bitrate_mbps": 1000}


def _filter(titles, *, season=1, episode=1, multi=False, scraper_type="Torrentio", size=3.0):
    api = Mock()
    api.get_show_aliases.return_value = ({}, "battery")
    api.get_show_metadata.return_value = ({"seasons": SEASONS}, "battery")
    results = [
        {"title": t, "original_title": t, "size": size, "parsed_info": parse_torrent_info(t, size),
         "scraper_type": scraper_type, "additional_metadata": {"filename": t},
         **({"protocol": "nzb", "nzb_url": "https://indexer/getnzb/1"} if scraper_type == "Newznab" else {})}
        for t in titles
    ]
    passed, _ = filter_results(
        results, "14658", "Survivor", 2000, "episode", season, episode, multi, VERSION,
        43, 14, {1: 14, 2: 16, 4: 15, 20: 14, 21: 15}, ["reality"],
        imdb_id="tt0239195", direct_api=api,
    )
    return passed, results


@pytest.mark.parametrize("title", [
    "Survivor Borneo Complete Season 1 - S01 - 2000 - lcpj",
    "Survivor.Borneo.S01.DVDRip.XviD",
])
def test_season_named_pack_passes_the_title_check(title):
    passed, results = _filter([title], multi=True)
    assert passed == results, results[0]["filter_reason"]


def test_season_named_episode_passes():
    passed, results = _filter(["Survivor.Borneo.S01E01.DVDRip.XviD"])
    assert passed == results, results[0]["filter_reason"]


def test_another_seasons_name_is_not_this_show():
    passed, results = _filter(["Survivor.Marquesas.S01E01.DVDRip.XviD"])
    assert not passed
    assert results[0]["filter_reason"].startswith("Title similarity too low")


@pytest.mark.parametrize(("parsed", "seasons", "episodes", "ok"), [
    ("survivor collection", list(range(1, 22)), [], True),          # S01-S21 pack
    ("survivor complete series", [1], [], True),
    ("survivor collection", [1], [1], False),                        # an episode isn't a pack
    ("survivor borneo", [1], [], True),
    ("survivor borneo", [], [], True),                                # names no season
    ("survivor borneo", [2], [], False),                              # Borneo is season 1
    ("survivor south africa", [1], [], False),
    ("survivor new zealand", list(range(1, 3)), [], False),
    ("survivor", [1], [], False),                                     # nothing extra: normal path
    ("designated survivor", [1], [], False),
])
def test_named_season_or_pack_title(parsed, seasons, episodes, ok):
    assert _named_season_or_pack_title(parsed, "survivor", {"borneo"}, seasons, episodes, 1) is ok


# --- Torrent packs for a single episode (rescrapes only ever scrape singles) ---

@pytest.mark.parametrize("title", [
    "Survivor.S01.DVDRip.XviD-GRP",
    "SURVIVOR COMPLETE COLLECTION-Seasons 1-22",
    "Survivor S01-S21 DVDRip",
])
def test_torrent_pack_covering_the_season_is_kept_for_a_single_episode(title):
    passed, results = _filter([title], size=40.0)
    assert passed == results, results[0]["filter_reason"]
    assert passed[0]["pack_for_single_episode"] is True


def test_torrent_pack_of_other_seasons_is_still_rejected():
    passed, results = _filter(["Survivor S02-S05 DVDRip"], size=40.0)
    assert not passed


def test_nzb_packs_still_rejected_for_a_single_episode():
    passed, results = _filter(["Survivor.S01.DVDRip.XviD-GRP", "Survivor S01-S21 DVDRip"],
                              scraper_type="Newznab", size=40.0)
    assert not passed
    assert "single episode" in results[0]["filter_reason"]
    assert "single episode" in results[1]["filter_reason"]


def test_single_episode_releases_are_not_marked_as_packs():
    passed, _ = _filter(["Survivor.S01E01.DVDRip.XviD-GRP"])
    assert passed and not passed[0].get("pack_for_single_episode")
