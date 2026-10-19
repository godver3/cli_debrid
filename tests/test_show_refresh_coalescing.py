"""Concurrent get_show_metadata calls for one stale show.

The parallel episode-refresh pass asks for the same show from several worker
threads at once. Each used to run its own _refresh_show, deleting and
rewriting the show's metadata rows under the others; a cache-hit thread
updating its loaded xem_mapping row then failed with StaleDataError.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from cli_battery.app import database as db
from cli_battery.app import direct_api
from cli_battery.app.database import Item, Metadata, managed_session


IMDB_ID = "tt7631058"


@pytest.fixture
def stale_show(monkeypatch):
    db.init_db()
    with managed_session() as session:
        item = session.query(Item).filter_by(imdb_id=IMDB_ID).first()
        if item is None:
            item = Item(imdb_id=IMDB_ID, title="Show", type="show")
            session.add(item)
            session.flush()
        session.query(Metadata).filter_by(item_id=item.id).delete()
        item.media_status = "returning series"
        item.last_trakt_fetch = datetime.now(timezone.utc) - timedelta(days=365)
        session.add(Metadata(item_id=item.id, key="ids", value=json.dumps({"tvdb": 1}), provider="t"))
        session.add(Metadata(item_id=item.id, key="xem_mapping", value="{}", provider="xem"))
    monkeypatch.setattr(direct_api, "fetch_xem_mapping", lambda tvdb_id: {"x": 1})
    return item


def test_concurrent_misses_refresh_once(stale_show, monkeypatch):
    calls = []

    def fake_refresh(imdb_id, session):
        calls.append(imdb_id)
        time.sleep(0.2)
        item = session.query(Item).filter_by(imdb_id=imdb_id).first()
        item.last_trakt_fetch = datetime.now(timezone.utc)
        session.query(Metadata).filter(Metadata.item_id == item.id).delete(synchronize_session="fetch")
        session.add(Metadata(item_id=item.id, key="ids", value=json.dumps({"tvdb": 1}), provider="t"))
        return {"ids": {"tvdb": 1}, "seasons": {}}

    monkeypatch.setattr(direct_api, "_refresh_show", fake_refresh)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(direct_api.DirectAPI.get_show_metadata(IMDB_ID)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == [IMDB_ID]
    assert len(results) == 4
    assert all(md is not None for md, _ in results)


def test_xem_store_survives_row_deleted_underneath(stale_show):
    """A concurrent refresh deletes the xem row between our write and commit.

    Mutating a loaded row deferred the UPDATE to commit, which then matched
    nothing and raised StaleDataError. The write now executes immediately, so
    the other writer either runs first or waits on SQLite's lock.
    """
    with managed_session() as session:
        item = session.query(Item).filter_by(imdb_id=IMDB_ID).first()
        direct_api._fetch_and_store_xem(item, session, {"ids": {"tvdb": 1}})
        other = sqlite3.connect(db.engine.url.database, timeout=0.2)
        try:
            other.execute("DELETE FROM metadata WHERE item_id = ?", (item.id,))
            other.commit()
        except sqlite3.OperationalError as e:
            assert "locked" in str(e)
        finally:
            other.close()

    with managed_session() as session:
        item = session.query(Item).filter_by(imdb_id=IMDB_ID).first()
        rows = session.query(Metadata).filter_by(item_id=item.id, key="xem_mapping").all()
        assert [json.loads(r.value) for r in rows] == [{"x": 1}]
