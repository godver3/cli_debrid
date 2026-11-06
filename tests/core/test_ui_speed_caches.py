"""Caches added to make pages load faster: they must share work without serving
wrong data (routes/base_routes.py, routes/queues_routes.py, routes/scraper_routes.py,
routes/statistics_routes.py, utilities/tmdb_cache.py)."""
import json
import sys
import threading
import time
from unittest import mock

import pytest

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT')


@pytest.fixture
def fresh_modules():
    """Import app modules against a fresh sys.modules (other tests stub some)."""
    with mock.patch.dict(sys.modules):
        for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
            del sys.modules[name]
        import database  # noqa: F401  (app import order; routes first is circular)
        yield


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.text = json.dumps(payload)
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# --- utilities/tmdb_cache.get_tmdb_json ------------------------------------------

@pytest.fixture
def tmdb_cache(fresh_modules):
    from utilities import tmdb_cache
    tmdb_cache._json_cache.clear()
    return tmdb_cache


def test_tmdb_json_is_fetched_once_and_each_caller_gets_its_own_copy(tmdb_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(tmdb_cache._json_session, 'get',
                        lambda url, timeout: calls.append(url) or _FakeResponse({'seasons': [1, 2]}))

    first = tmdb_cache.get_tmdb_json('https://tmdb/tv/1')
    first['seasons'].append('mutated by caller')
    second = tmdb_cache.get_tmdb_json('https://tmdb/tv/1')

    assert calls == ['https://tmdb/tv/1']
    assert second == {'seasons': [1, 2]}


def test_tmdb_json_expires_and_honours_ttl_for(tmdb_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(tmdb_cache._json_session, 'get',
                        lambda url, timeout: calls.append(url) or _FakeResponse({'results': []}))
    clock = [1000.0]
    monkeypatch.setattr(tmdb_cache.time, 'monotonic', lambda: clock[0])

    tmdb_cache.get_tmdb_json('https://tmdb/movie/1/release_dates', ttl_for=lambda data: 900)
    clock[0] += 899
    tmdb_cache.get_tmdb_json('https://tmdb/movie/1/release_dates')
    assert len(calls) == 1
    clock[0] += 2
    tmdb_cache.get_tmdb_json('https://tmdb/movie/1/release_dates')
    assert len(calls) == 2


def test_tmdb_json_errors_are_raised_and_not_cached(tmdb_cache, monkeypatch):
    responses = [_FakeResponse({}, status=500), _FakeResponse({'ok': True})]
    monkeypatch.setattr(tmdb_cache._json_session, 'get', lambda url, timeout: responses.pop(0))

    with pytest.raises(RuntimeError):
        tmdb_cache.get_tmdb_json('https://tmdb/tv/2')
    assert tmdb_cache.get_tmdb_json('https://tmdb/tv/2') == {'ok': True}
    assert tmdb_cache._json_inflight == {}


def test_tmdb_json_concurrent_misses_share_one_request(tmdb_cache, monkeypatch):
    calls = []
    release = threading.Event()

    def slow_get(url, timeout):
        calls.append(url)
        release.wait(5)
        return _FakeResponse({'id': 3})

    monkeypatch.setattr(tmdb_cache._json_session, 'get', slow_get)
    results = []
    threads = [threading.Thread(target=lambda: results.append(tmdb_cache.get_tmdb_json('https://tmdb/tv/3')))
               for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(5)

    assert calls == ['https://tmdb/tv/3']
    assert results == [{'id': 3}] * 5


# --- routes/base_routes task stream -------------------------------------------------

def test_task_stream_payload_is_built_once_per_second_for_all_clients(fresh_modules, monkeypatch):
    from routes import base_routes
    builds = []
    monkeypatch.setattr(base_routes, '_build_task_stream_payload', lambda: builds.append(1) or '{"n": %d}' % len(builds))
    monkeypatch.setattr(base_routes, '_task_stream_cache', {'built_at': 0.0, 'payload': None})
    clock = [500.0]
    monkeypatch.setattr(base_routes.time, 'monotonic', lambda: clock[0])

    assert [base_routes._get_task_stream_payload() for _ in range(4)] == ['{"n": 1}'] * 4
    clock[0] += 1.0
    assert base_routes._get_task_stream_payload() == '{"n": 2}'
    assert len(builds) == 2


# --- routes/queues_routes stream items ----------------------------------------------

def test_queue_stream_items_are_copies_without_scrape_results(fresh_modules, monkeypatch):
    from routes import queues_routes
    queues_routes._nzb_provider_display_cache.clear()
    parsed = []
    real = queues_routes._get_provider_display
    monkeypatch.setattr(queues_routes, '_get_provider_display', lambda item: parsed.append(item['id']) or real(item))

    live = {'id': 7, 'state': 'Checking', 'filled_by_torrent_id': 'nzb:abc',
            'scrape_results': json.dumps([{'protocol': 'nzb', 'source': 'NZBGeek_1'}])}
    before = dict(live)

    first = queues_routes._stream_item_copy(live, 'Checking')
    first['state'] = 'changed by the web thread'
    second = queues_routes._stream_item_copy(live, 'Checking')

    assert live == before
    assert 'scrape_results' not in first
    assert first['provider_display'] == second['provider_display'] == 'NZBGeek'
    assert parsed == [7]


def test_queue_stream_items_outside_adding_checking_get_no_provider(fresh_modules):
    from routes import queues_routes
    copy = queues_routes._stream_item_copy({'id': 1, 'scrape_results': '[]'}, 'Scraping')
    assert copy == {'id': 1}


# --- routes/statistics_routes.cache_for_seconds -------------------------------------

def test_cache_for_seconds_recomputes_once_for_concurrent_callers(fresh_modules):
    from routes.statistics_routes import cache_for_seconds
    calls = []

    @cache_for_seconds(60)
    def slow_count():
        calls.append(1)
        time.sleep(0.2)
        return 42

    results = []
    threads = [threading.Thread(target=lambda: results.append(slow_count())) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert results == [42] * 5
    assert len(calls) == 1


# --- routes/scraper_routes trending ---------------------------------------------------

def test_trending_is_served_stale_while_one_refresh_runs(fresh_modules, monkeypatch):
    from routes import scraper_routes
    fetches = []
    refreshed = threading.Event()

    def fetch():
        fetches.append(1)
        if len(fetches) > 1:
            refreshed.set()
        return {'trendingMovies': [len(fetches)], 'trendingShows': [], 'trendingAnime': []}

    monkeypatch.setattr(scraper_routes, '_fetch_all_trending_now', fetch)
    monkeypatch.setattr(scraper_routes, '_trending_state', {'data': None, 'fetched_at': 0.0, 'refreshing': False})

    assert scraper_routes.fetch_all_trending_parallel()['trendingMovies'] == [1]
    assert scraper_routes.fetch_all_trending_parallel()['trendingMovies'] == [1]
    assert len(fetches) == 1

    scraper_routes._trending_state['fetched_at'] -= scraper_routes._TRENDING_FRESH_SECONDS + 1
    # Stale: answered at once from the old data, one refresh in the background.
    assert scraper_routes.fetch_all_trending_parallel()['trendingMovies'] == [1]
    assert refreshed.wait(5)
    deadline = time.time() + 5
    while scraper_routes._trending_state['refreshing'] and time.time() < deadline:
        time.sleep(0.01)
    assert scraper_routes.fetch_all_trending_parallel()['trendingMovies'] == [2]
    assert len(fetches) == 2
