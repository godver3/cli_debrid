"""Movies with no air time: searched from 00:00 on the release date, and their
release-day window (queues/release_timing.py)."""
from datetime import datetime, timedelta
from unittest import mock

import pytest

import database  # noqa: F401  (app import order)
from queues import release_timing as rt


def _movie(**kw):
    base = {'id': 1, 'type': 'movie', 'title': 'Breeder', 'version': '1080p',
            'release_date': '2026-11-24', 'airtime': None, 'content_source': 'content_requester'}
    base.update(kw)
    return base


# ── The window ──────────────────────────────────────────────────────────────

def test_window_is_release_day_from_midnight_plus_offset(app_settings):
    app_settings['Queue'] = {'movie_airtime_offset': '0'}
    assert rt.release_day_window(_movie()) == (datetime(2026, 11, 24), datetime(2026, 11, 25))
    app_settings['Queue'] = {'movie_airtime_offset': '6'}
    assert rt.release_day_window(_movie())[0] == datetime(2026, 11, 24, 6)


@pytest.mark.parametrize('item', [
    _movie(type='episode'), _movie(airtime='20:00'), _movie(release_date='Unknown'), _movie(release_date=None),
])
def test_no_window_for_episodes_timed_items_and_unknown_dates(app_settings, item):
    assert rt.release_day_window(item) is None
    assert rt.in_release_day_window(item) is False


def test_versions_requiring_a_physical_release_use_the_physical_date(app_settings):
    app_settings['Scraping'] = {'versions': {'1080p': {'require_physical_release': True}}}
    item = _movie(physical_release_date='2026-12-10')
    assert rt.release_day_window(item)[0] == datetime(2026, 12, 10)
    assert rt.release_day_window(_movie()) is None  # no physical date yet


def test_in_window_boundaries(app_settings):
    m = _movie()
    assert not rt.in_release_day_window(m, datetime(2026, 11, 23, 23, 59))
    assert rt.in_release_day_window(m, datetime(2026, 11, 24, 0, 1))
    assert rt.in_release_day_window(m, datetime(2026, 11, 24, 23, 59))
    assert not rt.in_release_day_window(m, datetime(2026, 11, 25, 0, 0))


# ── Wanted: ready on the release day, not the next ──────────────────────────

def _readiness(item, now, app_settings):
    from queues.wanted_queue import WantedQueue
    app_settings.setdefault('Queue', {'movie_airtime_offset': '0'})
    qm = mock.Mock()
    qm.generate_identifier.side_effect = lambda i: i['title']
    return WantedQueue()._evaluate_item_readiness_and_act(item, now, qm)['status']


def test_movie_is_searched_from_midnight_on_its_release_date(app_settings):
    assert _readiness(_movie(), datetime(2026, 11, 24, 0, 1), app_settings) == 'scrape'
    assert _readiness(_movie(), datetime(2026, 11, 23, 23, 0), app_settings) == 'wait'


def test_episodes_and_timed_items_are_unchanged(app_settings):
    ep = _movie(type='episode', airtime='21:00', title='Show')
    assert _readiness(ep, datetime(2026, 11, 24, 20, 0), app_settings) == 'wait'
    assert _readiness(ep, datetime(2026, 11, 24, 21, 1), app_settings) == 'scrape'


# ── Sleeping: hourly, not counted ───────────────────────────────────────────

def _sleep_pass(items, asleep_for, now, app_settings):
    from queues import sleeping_queue
    app_settings['Queue'] = {'movie_airtime_offset': '0'}
    q = sleeping_queue.SleepingQueue()
    q.items = items
    q.sleeping_queue_times = {i['id']: now - asleep_for for i in items}
    qm = mock.Mock()
    qm.generate_identifier.side_effect = lambda i: str(i['id'])

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    with mock.patch.object(sleeping_queue, 'datetime', FakeDT), \
         mock.patch.object(sleeping_queue, 'load_config', return_value={}), \
         mock.patch.object(sleeping_queue, '_get_int_setting',
                           side_effect=lambda s, k, default=None: {'wake_limit': 2, 'sleep_duration_minutes': 30}.get(k, default)), \
         mock.patch('database.get_wake_count', side_effect=lambda iid: 5), \
         mock.patch('database.increment_wake_count', side_effect=lambda iid: 6) as inc, \
         mock.patch.object(q, 'blacklist_items') as bl:
        q.process(qm)
    woken = [c.args[0]['id'] for c in qm.move_to_wanted.call_args_list]
    blacklisted = [i['id'] for c in bl.call_args_list for i in c.args[1]]
    return woken, inc.call_count, blacklisted


def test_release_day_movie_wakes_hourly_past_its_wake_limit_without_counting(app_settings):
    now = datetime(2026, 11, 24, 15, 0)
    item = _movie(wake_count=5)  # already past the wake limit of 2
    assert _sleep_pass([dict(item)], timedelta(minutes=45), now, app_settings) == ([], 0, [])
    assert _sleep_pass([dict(item)], timedelta(minutes=61), now, app_settings) == ([1], 0, [])


def test_after_the_release_day_the_normal_rules_apply(app_settings):
    now = datetime(2026, 11, 25, 1, 0)
    item = _movie(wake_count=5)
    woken, increments, blacklisted = _sleep_pass([dict(item)], timedelta(hours=3), now, app_settings)
    assert (woken, increments, blacklisted) == ([], 0, [1])  # over the limit: blacklist path as before


# ── Scraping: nothing found on the release day ──────────────────────────────

def test_no_results_on_release_day_sleeps_even_for_never_sleep_versions(app_settings):
    from queues.scraping_queue import ScrapingQueue
    app_settings['Queue'] = {'movie_airtime_offset': '0'}
    app_settings['Scraping'] = {'versions': {'1080p': {'wake_count': -1, 'fallback_at_scrape_time': True,
                                                       'fallback_version': '720p'}}}
    item = _movie(release_date=datetime.now().strftime('%Y-%m-%d'))
    q = ScrapingQueue()
    qm = mock.Mock()
    qm.generate_identifier.side_effect = lambda i: i['title']
    with mock.patch.object(q, '_try_scrape_time_fallback') as fallback, \
         mock.patch.object(q, 'reset_not_wanted_check'):
        q.handle_no_results(item, qm)
    qm.move_to_sleeping.assert_called_once()
    qm.initiate_final_check_or_blacklist.assert_not_called()
    fallback.assert_not_called()  # no switch to the fallback version on the release day
