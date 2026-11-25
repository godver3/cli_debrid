from unittest.mock import Mock

import pytest

from scraper.functions.filter_results import filter_results
from scraper.functions.file_processing import parse_torrent_info

HORIZONS = (
    "Pokemon.Horizons.The.Series.S01E82.Beyond.the.Shining.Rainbow."
    "1080p.NF.WEB-DL.MULTi.AAC2.0.H.264-VARYG"
)
ALIASES = {
    "us": ["Pokémon", "Pokémon: Indigo League", "Pokémon Horizons: The Series", "Pokémon the Series: XY"],
    "jp": ["Pocket Monsters"],
}
VERSION = {"max_resolution": "1080p", "min_bitrate_mbps": 0, "max_bitrate_mbps": 1000}


def _filter(titles, *, season_title="Indigo League", aliases=ALIASES, matching_aliases=None,
            translated_title=None, show="Pokémon", anime=True, original_season=None, season=1):
    api = Mock()
    api.get_show_aliases.return_value = (aliases, "battery")
    api.get_show_metadata.return_value = ({
        "seasons": {"1": {"title": season_title}, "2": {"name": "Adventures in the Orange Islands"}}
    }, "battery")
    results = [
        {"title": title, "original_title": title, "size": 1.5, "parsed_info": parse_torrent_info(title, 1.5),
         "scraper_type": "Newznab", "additional_metadata": {"filename": title}}
        for title in titles
    ]
    passed, _ = filter_results(
        results, "60572", show, 1997, "episode", season, 82, False, VERSION,
        22, 82, {1: 82, 2: 36}, ["anime"] if anime else ["drama"],
        matching_aliases=matching_aliases, imdb_id="tt0168366", direct_api=api,
        translated_title=translated_title, original_season=original_season,
    )
    api.get_show_metadata.assert_called_once_with("tt0168366")
    api.get_show_seasons.assert_not_called()
    return passed, results


@pytest.mark.parametrize("extra_aliases", [{}, {
    "matching_aliases": ALIASES["us"], "translated_title": "Pokémon Horizons: The Series",
}], ids=["metadata", "all-title-sources"])
def test_horizons_alias_rejected_for_pokemon_s01e82(extra_aliases):
    passed, results = _filter([HORIZONS, HORIZONS], **extra_aliases)
    assert not passed
    assert all(result["filter_reason"].startswith("Title similarity too low") for result in results)


@pytest.mark.parametrize("title", [
    "Pokemon.S01E82.1080p.WEB-DL",
    "Pokemon.Indigo.League.082.1080p.WEB-DL",
    "Pocket.Monsters.S01E82.1080p.WEB-DL",
])
def test_pokemon_main_title_and_genuine_aliases_pass(title):
    passed, results = _filter([title])
    assert passed == results, results[0]["filter_reason"]


def test_xem_remap_uses_the_items_original_season_title():
    title = "Pokemon.Indigo.League.S02E82.1080p.WEB-DL"
    passed, results = _filter([title], original_season=1, season=2)
    assert passed == results, results[0]["filter_reason"]


@pytest.mark.parametrize("season_title", [None, "Season 1"])
def test_sub_series_alias_keeps_existing_behavior_without_a_named_season(season_title):
    passed, results = _filter([HORIZONS], season_title=season_title)
    assert passed == results, results[0]["filter_reason"]


@pytest.mark.parametrize(("show", "alias", "release_title"), [
    ("Money Heist", "La Casa de Papel", "La.Casa.de.Papel"),
    ("The Office", "The Office (US)", "The.Office.US"),
])
def test_plain_alternate_and_country_tagged_aliases_still_pass(show, alias, release_title):
    passed, results = _filter([f"{release_title}.S01E82.1080p.WEB-DL"],
                              show=show, anime=False, aliases={"us": [alias]})
    assert passed == results, results[0]["filter_reason"]


def test_named_season_alias_does_not_accept_the_wrong_padded_episode():
    passed, results = _filter(["Pokemon.Indigo.League.083.1080p.WEB-DL"])
    assert not passed
    assert results[0]["filter_reason"].startswith("Episode mismatch")


def test_tmdb_season_title_from_alias_response_survives_battery_cache(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from cli_battery.app import direct_api, trakt_client
    from cli_battery.app.database import Base, Item
    from utilities import settings

    imdb_id = "tt0168366"
    show_response = Mock(status_code=200)
    show_response.json.return_value = {"title": "Pokémon", "ids": {"imdb": imdb_id, "tmdb": 60572, "slug": "pokemon"}}
    season_response = Mock(status_code=200)
    season_response.json.return_value = [{"number": 1, "episode_count": 82, "title": "Season 1"}]
    tmdb_response = Mock(status_code=200)
    tmdb_response.json.return_value = {
        "name": "Pokémon",
        "seasons": [{"season_number": 1, "name": "Indigo League"}],
        "alternative_titles": {"results": [{"iso_3166_1": "US", "title": "Pokémon Horizons: The Series"}]},
    }
    monkeypatch.setattr(settings, "get_setting", lambda *_: "test-key")
    monkeypatch.setattr(trakt_client, "_make_request", Mock(side_effect=[show_response, season_response]))
    monkeypatch.setattr(trakt_client, "get_show_aliases", Mock(return_value={}))
    tmdb_get = Mock(return_value=tmdb_response)
    monkeypatch.setattr(trakt_client._http, "get", tmdb_get)

    metadata = trakt_client.get_show_data(imdb_id)
    assert metadata["aliases"] == {"us": ["Pokémon Horizons: The Series"]}
    tmdb_get.assert_called_once()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        item = Item(imdb_id=imdb_id, title="Pokémon", type="show")
        session.add(item)
        session.flush()
        direct_api._upsert_seasons_and_episodes(item.id, metadata["seasons"], session)
        assert direct_api._build_show_metadata_dict(item)["seasons"][1]["title"] == "Indigo League"
