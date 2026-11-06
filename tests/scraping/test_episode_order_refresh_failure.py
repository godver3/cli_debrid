"""A show kept in another episode order whose order can't be fetched.

seasons_in_show_order used to fall back to the default (aired) seasons, and
the refresh stored them over the show's DVD/absolute/group numbering. Now
nothing is stored, the stored seasons are returned, and the show stays stale
so the next read retries the order.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cli_battery.app import database as db
from cli_battery.app import direct_api
from cli_battery.app.database import Episode, Item, Season, managed_session

IMDB_ID = "tt0388629"
STALE = datetime.now(timezone.utc) - timedelta(days=365)


@pytest.fixture
def dvd_ordered_show(monkeypatch):
    db.init_db()
    with managed_session() as session:
        item = session.query(Item).filter_by(imdb_id=IMDB_ID).first()
        if item is None:
            item = Item(imdb_id=IMDB_ID, title="Show", type="show")
            session.add(item)
            session.flush()
        for season in session.query(Season).filter_by(item_id=item.id):
            session.query(Episode).filter_by(season_id=season.id).delete()
        session.query(Season).filter_by(item_id=item.id).delete()
        item.media_status = "returning series"
        item.last_trakt_fetch = STALE
        season = Season(item_id=item.id, season_number=15, episode_count=1)
        session.add(season)
        session.flush()
        session.add(Episode(season_id=season.id, episode_number=1, title="DVD S15E01"))

    class Client:
        @staticmethod
        def get_show_data(imdb_id):
            return {"title": "Show", "status": "returning series", "ids": {},
                    "seasons": {13: {"episode_count": 1, "episodes": {41: {"title": "aired S13E41"}}}}}

    monkeypatch.setattr(direct_api, "_get_metadata_client", lambda: Client)
    monkeypatch.setattr(direct_api, "_ensure_plex_guids_in_data", lambda *a, **k: None)
    monkeypatch.setattr(direct_api, "seasons_in_show_order", lambda imdb_id, seasons: None)


def _stored():
    with managed_session() as session:
        item = session.query(Item).filter_by(imdb_id=IMDB_ID).first()
        seasons = {s.season_number: sorted((e.episode_number, e.title) for e in s.episodes) for s in item.seasons}
        fetched = item.last_trakt_fetch
        if fetched is not None and fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return seasons, fetched


def test_unavailable_order_stores_no_default_seasons(dvd_ordered_show):
    with managed_session() as session:
        show = direct_api._fetch_and_persist_show(IMDB_ID, session)

    seasons, fetched = _stored()
    assert seasons == {15: [(1, "DVD S15E01")]}
    assert list(show["seasons"]) == [15]
    assert show["seasons"][15]["episodes"][1]["title"] == "DVD S15E01"
    # Still stale, so the next read tries the order again
    assert abs((fetched - STALE).total_seconds()) < 1


def test_available_order_is_stored(dvd_ordered_show, monkeypatch):
    dvd = {15: {"episode_count": 2, "episodes": {1: {"title": "DVD S15E01"}, 2: {"title": "DVD S15E02"}}}}
    monkeypatch.setattr(direct_api, "seasons_in_show_order", lambda imdb_id, seasons: dvd)
    with managed_session() as session:
        direct_api._fetch_and_persist_show(IMDB_ID, session)

    seasons, fetched = _stored()
    assert seasons == {15: [(1, "DVD S15E01"), (2, "DVD S15E02")]}
    assert fetched > STALE + timedelta(days=300)
