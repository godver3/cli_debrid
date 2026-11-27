"""_upsert_seasons_and_episodes preserves real season titles against later
generic ('Season N', 'Specials', 'Season 0') or empty/None refreshes."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cli_battery.app import direct_api
from cli_battery.app.database import Base, Item, Season


def _session_with_show():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    item = Item(imdb_id="tt0168366", title="Pokémon", type="show")
    session.add(item)
    session.flush()
    return session, item


@pytest.mark.parametrize("incoming_title", [
    None,
    "",
    "   ",
    "Season 1",
    "season 1",
    "Season 0",
    "Season 01",
    "Specials",
    "specials",
])
def test_real_season_title_survives_generic_or_empty_upsert(incoming_title):
    session, item = _session_with_show()
    with session:
        direct_api._upsert_seasons_and_episodes(
            item.id,
            {1: {"number": 1, "title": "Indigo League", "episode_count": 82}},
            session,
        )
        direct_api._upsert_seasons_and_episodes(
            item.id,
            {1: {"number": 1, "title": incoming_title, "episode_count": 82}},
            session,
        )
        season = session.query(Season).filter_by(item_id=item.id, season_number=1).one()
        assert season.title == "Indigo League"


@pytest.mark.parametrize("initial_title", [None, "", "Season 1", "Specials", "Season 0"])
def test_generic_or_empty_season_title_is_replaced_by_real_name(initial_title):
    session, item = _session_with_show()
    with session:
        direct_api._upsert_seasons_and_episodes(
            item.id,
            {1: {"number": 1, "title": initial_title, "episode_count": 82}},
            session,
        )
        direct_api._upsert_seasons_and_episodes(
            item.id,
            {1: {"number": 1, "title": "Indigo League", "episode_count": 82}},
            session,
        )
        season = session.query(Season).filter_by(item_id=item.id, season_number=1).one()
        assert season.title == "Indigo League"


def test_real_title_containing_season_prefix_replaces_generic():
    session, item = _session_with_show()
    with session:
        direct_api._upsert_seasons_and_episodes(
            item.id,
            {1: {"number": 1, "title": "Season 1", "episode_count": 10}},
            session,
        )
        direct_api._upsert_seasons_and_episodes(
            item.id,
            {1: {"number": 1, "title": "Season 1: Indigo League", "episode_count": 10}},
            session,
        )
        season = session.query(Season).filter_by(item_id=item.id, season_number=1).one()
        assert season.title == "Season 1: Indigo League"
