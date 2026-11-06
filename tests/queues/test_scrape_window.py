"""The alternate scrape time window shared by the Scraping, Unreleased and Wanted queues."""
from datetime import datetime, time as dt_time

from queues.scrape_window import in_alternate_scrape_window

NOW = datetime(2026, 10, 24, 12, 0)
ANCHOR = dt_time(20, 0)  # window: 2026-10-23 20:00 .. 2026-10-24 20:00


def check(release_date, airtime=None):
    return in_alternate_scrape_window({'release_date': release_date, 'airtime': airtime}, NOW, ANCHOR)


def test_inside_the_window():
    assert check('2026-10-24', '08:00')
    assert check('2026-10-23', '21:30:00')


def test_after_the_window_waits():
    assert not check('2026-10-24', '21:00')
    assert not check('2026-10-25')


def test_older_than_the_window_is_always_allowed():
    assert check('2026-10-01')


def test_unknown_or_bad_dates_are_not_in_the_window():
    for date in (None, '', 'unknown', 'None', 'garbage'):
        assert not check(date)


def test_unparseable_airtime_counts_as_midnight():
    assert check('2026-10-24', 'bad')


def test_anchor_comes_from_settings_when_not_given(app_settings):
    app_settings['Debug'] = {'alternate_scrape_time_24h': '06:00'}
    # window: 2026-10-23 06:00 .. 2026-10-24 06:00, so 08:00 today is after it
    assert not in_alternate_scrape_window({'release_date': '2026-10-24', 'airtime': '08:00'}, NOW)
