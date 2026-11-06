"""Calendar page status mapping and data queries (routes/statistics_routes.py)."""
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT')


def _load_statistics_routes(test):
    """Import statistics_routes against a fresh set of app modules (other tests stub some)."""
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    from routes import statistics_routes
    return statistics_routes


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE media_items (type TEXT, title TEXT, season_number INT, episode_number INT,"
        " release_date TEXT, airtime TEXT, imdb_id TEXT, tmdb_id TEXT, state TEXT,"
        " upgrading_from TEXT, year INT)"
    )
    conn.executemany("INSERT INTO media_items VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return conn


class CalendarStatusTests(unittest.TestCase):
    def setUp(self):
        self.sr = _load_statistics_routes(self)

    def test_same_situation_same_status_for_movies_and_episodes(self):
        for released, expected in ((True, "premiered_uncollected"), (False, "upcoming")):
            self.assertEqual(self.sr._calendar_movie_status([("Wanted", None)], released)[0], expected)
            self.assertEqual(self.sr._calendar_episode_group_status([("Wanted", None)], released)[0], expected)
            self.assertEqual(self.sr._calendar_episode_group_status([("Unreleased", None)], released)[0], expected)

    def test_in_flight_and_upgrade_states(self):
        self.assertEqual(self.sr._calendar_status("Scraping", None, True), "in_progress")
        self.assertEqual(self.sr._calendar_status("Checking", None, False), "in_progress")
        self.assertEqual(self.sr._calendar_status("Checking", "old.mkv", True), "checking_upgrade")
        self.assertEqual(self.sr._calendar_status("Upgrading", None, True), "collected")
        self.assertEqual(self.sr._calendar_status("Collected", None, False), "upcoming_collected")

    def test_movie_with_collected_and_wanted_versions_is_collected(self):
        status, label = self.sr._calendar_movie_status([("Wanted", None), ("Collected", None)], True)
        self.assertEqual((status, label), ("collected", "Collected"))

    def test_partially_collected_episode_drop(self):
        states = [("Collected", None)] * 3 + [("Wanted", None)] * 5
        self.assertEqual(self.sr._calendar_episode_group_status(states, True), ("premiered_uncollected", "3/8 Collected"))
        self.assertEqual(self.sr._calendar_episode_group_status([("Collected", None)] * 8, True), ("collected", "Collected"))


class CalendarQueryTests(unittest.TestCase):
    def setUp(self):
        self.sr = _load_statistics_routes(self)

    def test_movies_keep_every_version_state_and_skip_blacklisted(self):
        conn = _db([
            ("movie", "Two Versions", None, None, "2026-10-20", None, "tt1", "1", "Collected", None, 2026),
            ("movie", "Two Versions", None, None, "2026-10-20", None, "tt1", "1", "Wanted", None, 2026),
            ("movie", "Blocked", None, None, "2026-10-21", None, "tt2", "2", "Blacklisted", None, 2026),
        ])
        with mock.patch("database.get_db_connection", return_value=conn):
            movies = self.sr.get_movies_for_calendar(start_date_override_iso="2026-10-01", end_date_override_iso="2026-10-31")
        self.assertEqual([m["title"] for m in movies], ["Two Versions"])
        self.assertEqual(sorted(movies[0]["states"]), [("Collected", None), ("Wanted", None)])

    def test_calendar_keeps_in_flight_upcoming_episodes(self):
        tz = ZoneInfo("UTC")
        future = (datetime.now(tz) + timedelta(days=3)).date().isoformat()
        rows = [
            ("episode", "Show", 1, 1, future, "20:00", "tt9", "9", "Scraping", None, 2026),
            ("episode", "Show", 1, 2, future, "20:00", "tt9", "9", "Unreleased", None, 2026),
        ]
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=10)).isoformat()
        with mock.patch("metadata.metadata._get_local_timezone", return_value=tz):
            with mock.patch("database.get_db_connection", side_effect=lambda: _db(rows)):
                _, home = self.sr.get_recently_aired_and_airing_soon(start_date_override_iso=start, end_date_override_iso=end)
                _, cal = self.sr.get_recently_aired_and_airing_soon(
                    start_date_override_iso=start, end_date_override_iso=end, include_all_upcoming=True)
        self.assertEqual(home, [])
        self.assertEqual(len(cal), 1)
        self.assertEqual(cal[0]["episode_states"], [("Scraping", None), ("Unreleased", None)])
        self.assertEqual(self.sr._calendar_episode_group_status(cal[0]["episode_states"], False), ("in_progress", "In Progress"))


if __name__ == "__main__":
    unittest.main()
