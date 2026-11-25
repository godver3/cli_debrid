"""Upgrades: same-release check, Upgrade Hub scraper search + cache status,
Upgrading queue pass budget, and the upgrading settings on the hub."""
from unittest import mock

import pytest

import database  # noqa: F401  (app import order)


# ── Same-release check (replaces the 95% fuzzy title filter) ─────────────────

def _item(**kw):
    base = {'id': 1, 'type': 'movie', 'title': 'The Long Walk', 'imdb_id': 'tt1',
            'version': '1080p', 'filled_by_magnet': '', 'filled_by_file': '',
            'original_scraped_torrent_title': ''}
    base.update(kw)
    return base


def test_4k_of_the_same_release_name_is_not_the_same_release():
    from queues.upgrading_queue import is_same_release
    item = _item(original_scraped_torrent_title='The.Long.Walk.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX')
    result = {'title': 'The.Long.Walk.2025.2160p.WEB-DL.DDP5.1.H.264-FLUX'}
    # The old SequenceMatcher ratio here is 0.959, above the 0.95 cut-off.
    assert not is_same_release(item, result)


def test_same_name_with_other_punctuation_or_extension_is_the_same_release():
    from queues.upgrading_queue import is_same_release
    item = _item(filled_by_file='The.Long.Walk.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX.mkv')
    assert is_same_release(item, {'title': 'The Long Walk 2025 1080p WEB-DL DDP5 1 H 264-FLUX'})


def test_same_info_hash_is_the_same_release():
    from queues.upgrading_queue import is_same_release
    h = 'a' * 40
    item = _item(filled_by_magnet=f'magnet:?xt=urn:btih:{h}&dn=x')
    assert is_same_release(item, {'title': 'Totally different name',
                                  'magnet': f'magnet:?xt=urn:btih:{h.upper()}'})


# ── Hub scraper search: rotation ─────────────────────────────────────────────

@pytest.fixture
def hub_db(app_db, monkeypatch):
    # Module-level caches remember tables and results from whichever database
    # the last test used; reset them (monkeypatch puts them back afterwards).
    import database.upgrade_hub_scrapers as uhs
    import database.zilean_upgrade as zu
    monkeypatch.setattr(uhs, '_tables_ready', False)
    for name, value in (('_cache_tables_ready', False), ('_cache_initialized', False),
                        ('_last_scan_results', None), ('_last_scan_time', None),
                        ('_scan_progress', {}), ('_queued_magnets', {}), ('_vs_cache', {})):
        monkeypatch.setattr(zu, name, value)
    return app_db


def test_never_checked_items_go_first_then_oldest_check(hub_db):
    import database.upgrade_hub_scrapers as uhs
    items = [{'id': i, 'type': 'movie'} for i in (1, 2, 3)]
    assert [i['id'] for i in uhs.pick_items_to_check(items, 3)] == [3, 2, 1]  # newest first among never-checked

    uhs.record_checked([3, 2])
    picked = uhs.pick_items_to_check(items, 2)
    assert picked[0]['id'] == 1  # never checked
    assert len(picked) == 2


def test_movies_before_episodes_when_both_unchecked(hub_db):
    import database.upgrade_hub_scrapers as uhs
    items = [{'id': 10, 'type': 'episode'}, {'id': 5, 'type': 'movie'}]
    assert [i['id'] for i in uhs.pick_items_to_check(items, 2)] == [5, 10]


def test_limit_zero_checks_nothing(hub_db):
    import database.upgrade_hub_scrapers as uhs
    assert uhs.pick_items_to_check([{'id': 1, 'type': 'movie'}], 0) == []


# ── Hub scraper search: one item ─────────────────────────────────────────────

def _insert(conn, **cols):
    cols.setdefault('state', 'Collected')
    cols.setdefault('version', '1080p')
    keys = ', '.join(cols)
    cur = conn.execute(f"INSERT INTO media_items ({keys}) VALUES ({', '.join('?' for _ in cols)})",
                       list(cols.values()))
    conn.commit()
    return cur.lastrowid


def _fake_score(current_filename, pool, *a, **kw):
    """Score 2160p above 1080p, current file at 50."""
    scored = [(100.0 if '2160p' in r['title'] else 40.0, r) for r in pool]
    scored.sort(key=lambda x: -x[0])
    return 50.0, scored


def test_scan_item_returns_the_better_release_and_skips_the_current_one(hub_db, app_settings):
    import database.upgrade_hub_scrapers as uhs
    conn = hub_db()
    iid = _insert(conn, type='movie', title='The Long Walk', imdb_id='tt1', tmdb_id='1', year=2025,
                  filled_by_file='The.Long.Walk.2025.1080p.WEB-DL-FLUX.mkv', size=5.0)
    conn.close()
    results = [
        {'title': 'The.Long.Walk.2025.1080p.WEB-DL-FLUX', 'magnet': 'magnet:?xt=urn:btih:' + 'b' * 40, 'size': 6.0},
        {'title': 'The.Long.Walk.2025.2160p.WEB-DL-FLUX', 'magnet': 'magnet:?xt=urn:btih:' + 'c' * 40, 'size': 15.0},
    ]
    with mock.patch('queues.scraping_queue.ScrapingQueue.scrape_with_fallback', return_value=(results, [])), \
         mock.patch('database.zilean_upgrade._score_results_with_current', side_effect=_fake_score) as score:
        c = uhs._scan_item({'id': iid, 'type': 'movie', 'title': 'The Long Walk'}, 0.1, '1080p', frozenset())
    pool_titles = [r['title'] for r in score.call_args[0][1]]
    assert 'The.Long.Walk.2025.1080p.WEB-DL-FLUX' not in pool_titles
    assert c['new_title'] == 'The.Long.Walk.2025.2160p.WEB-DL-FLUX'
    assert c['found_by'] == 'scrapers'
    assert c['protocol'] == 'torrent'
    assert c['new_info_hash'] == 'c' * 40
    assert c['improvement_pct'] == 100.0


def test_scan_item_nzb_result_carries_the_nzb_url(hub_db, app_settings):
    import database.upgrade_hub_scrapers as uhs
    conn = hub_db()
    iid = _insert(conn, type='movie', title='X', imdb_id='tt2', tmdb_id='2', year=2025,
                  filled_by_file='X.2025.1080p.WEB.mkv', size=4.0)
    conn.close()
    results = [{'title': 'X.2025.2160p.WEB', 'nzb_url': 'https://idx/get/42', 'size': 12.0}]
    with mock.patch('queues.scraping_queue.ScrapingQueue.scrape_with_fallback', return_value=(results, [])), \
         mock.patch('database.zilean_upgrade._score_results_with_current', side_effect=_fake_score):
        c = uhs._scan_item({'id': iid, 'type': 'movie', 'title': 'X'}, 0.1, '1080p', frozenset())
    assert c['protocol'] == 'nzb'
    assert c['new_magnet'] == 'https://idx/get/42'


def test_scan_item_ignores_smaller_and_non_upgrades(hub_db, app_settings):
    import database.upgrade_hub_scrapers as uhs
    conn = hub_db()
    iid = _insert(conn, type='movie', title='X', imdb_id='tt3', tmdb_id='3', year=2025,
                  filled_by_file='X.2025.1080p.WEB.mkv', size=20.0)
    conn.close()
    results = [{'title': 'X.2025.2160p.x265', 'magnet': 'magnet:?xt=urn:btih:' + 'd' * 40, 'size': 8.0}]
    with mock.patch('queues.scraping_queue.ScrapingQueue.scrape_with_fallback', return_value=(results, [])), \
         mock.patch('database.zilean_upgrade._score_results_with_current', side_effect=_fake_score):
        assert uhs._scan_item({'id': iid, 'type': 'movie', 'title': 'X'}, 0.1, '1080p', frozenset()) is None


def test_scan_with_scrapers_records_checks_even_without_upgrades(hub_db, app_settings):
    import database.upgrade_hub_scrapers as uhs
    with mock.patch.object(uhs, '_scan_item', return_value=None):
        out = uhs.scan_with_scrapers([{'id': 7, 'type': 'movie'}], 0.1, '1080p', limit=5)
    assert out == []
    assert 7 in uhs.last_checked_map([7])


def _days_ago(n):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


def test_full_scan_with_only_the_fresh_release_search(hub_db, app_settings, monkeypatch):
    import database.zilean_upgrade as zu
    import database.upgrade_hub_scrapers as uhs
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'fresh_search_days': 30, 'fresh_search_limit': 10}
    conn = hub_db()
    iid = _insert(conn, type='movie', title='The Long Walk', imdb_id='tt1', tmdb_id='1', year=2025,
                  filled_by_file='The.Long.Walk.2025.1080p.WEB-DL-FLUX.mkv', size=5.0, release_date=_days_ago(10))
    conn.close()
    monkeypatch.setattr(zu, '_save_scan_results_to_db', lambda *a, **k: None)
    results = [{'title': 'The.Long.Walk.2025.2160p.WEB-DL-FLUX',
                'magnet': 'magnet:?xt=urn:btih:' + 'c' * 40, 'size': 15.0}]
    with mock.patch('queues.scraping_queue.ScrapingQueue.scrape_with_fallback', return_value=(results, [])), \
         mock.patch('database.zilean_upgrade._score_results_with_current', side_effect=_fake_score), \
         mock.patch('database.upgrade_hub_activity.log_hub_activity'):
        out = zu.scan_for_upgrades()
    assert 'error' not in out, out
    [c] = out['upgrade_candidates']
    assert c['item_id'] == iid
    assert c['new_title'] == 'The.Long.Walk.2025.2160p.WEB-DL-FLUX'
    assert c['new_magnet'].startswith('magnet:?xt=urn:btih:ccc')
    assert c['found_by'] == 'scrapers' and c['protocol'] == 'torrent' and c['is_recent'] is True
    assert iid in uhs.last_checked_map([iid])


def test_fresh_search_covers_recent_items_only_up_to_its_cap(hub_db, app_settings, monkeypatch):
    # Recent items only, capped per scan, oldest-checked first; Scan Limit
    # (for Zilean / NZB) doesn't narrow it.
    import database.zilean_upgrade as zu
    import database.upgrade_hub_scrapers as uhs
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'fresh_search_days': 30, 'fresh_search_limit': 2}
    conn = hub_db()
    recent = [_insert(conn, type='movie', title=f'M{n}', imdb_id=f'tt{n}', tmdb_id=str(n), year=2025,
                      filled_by_file=f'M{n}.2025.1080p.WEB.mkv', size=5.0, release_date=_days_ago(n))
              for n in (1, 2, 3)]
    old = _insert(conn, type='movie', title='Old', imdb_id='tt9', tmdb_id='9', year=2010,
                  filled_by_file='Old.2010.1080p.mkv', size=5.0, release_date='2010-05-01')
    conn.close()
    monkeypatch.setattr(zu, '_save_scan_results_to_db', lambda *a, **k: None)
    seen = []
    with mock.patch.object(uhs, '_scan_item', side_effect=lambda it, *a, **k: seen.append(it['id'])), \
         mock.patch('database.upgrade_hub_activity.log_hub_activity'):
        out = zu.scan_for_upgrades(scan_limit=1)
        assert 'error' not in out, out
        assert len(seen) == 2 and set(seen) <= set(recent) and old not in seen
        zu.scan_for_upgrades(scan_limit=1)  # next scan reaches the one not searched yet
    assert set(seen) == set(recent)


def test_scan_with_no_source_at_all_says_so(hub_db, app_settings):
    import database.zilean_upgrade as zu
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'fresh_search_limit': 0}
    out = zu.scan_for_upgrades()
    assert 'No upgrade source' in out['error']


# ── Cache status ─────────────────────────────────────────────────────────────

def test_torbox_cache_check_marks_torrent_candidates_only(app_settings):
    import database.upgrade_hub_scrapers as uhs
    app_settings['Debrid Provider'] = {'provider': 'Torbox'}
    provider = mock.Mock()
    provider.is_cached_sync.return_value = {'a' * 40: True, 'b' * 40: False}
    cands = [
        {'protocol': 'torrent', 'new_info_hash': 'a' * 40},
        {'protocol': 'torrent', 'new_magnet': 'magnet:?xt=urn:btih:' + 'b' * 40},
        {'protocol': 'nzb', 'new_magnet': 'https://idx/1'},
    ]
    with mock.patch('debrid.get_debrid_provider', return_value=provider):
        assert uhs.annotate_cache_status(cands) == 'TorBox'
    assert [c['cached'] for c in cands] == [True, False, None]
    assert cands[0]['cache_service'] == 'TorBox'
    assert 'cache_service' not in cands[2]


def test_providers_without_a_cheap_check_leave_status_unknown(app_settings):
    import database.upgrade_hub_scrapers as uhs
    app_settings['Debrid Provider'] = {'provider': 'AllDebrid'}
    cands = [{'protocol': 'torrent', 'new_info_hash': 'a' * 40}]
    with mock.patch('debrid.get_debrid_provider') as gp:
        assert uhs.annotate_cache_status(cands) is None
        gp.assert_not_called()  # AllDebrid would have to add the torrent to check it
    assert cands[0]['cached'] is None


def test_real_debrid_uses_phalanx_only_when_enabled(app_settings):
    import database.upgrade_hub_scrapers as uhs
    app_settings['Debrid Provider'] = {'provider': 'RealDebrid'}
    cands = [{'protocol': 'torrent', 'new_info_hash': 'a' * 40}]
    assert uhs.annotate_cache_status(cands) is None
    app_settings['UI Settings'] = {'enable_phalanx_db': True}
    with mock.patch('utilities.phalanx_db_cache_manager.PhalanxDBClassManager') as P:
        P.return_value.get_multi_cache_status.return_value = {'a' * 40: {'is_cached': True}}
        assert uhs.annotate_cache_status(cands) == 'Real-Debrid'
    assert cands[0]['cached'] is True


# ── Upgrading queue: pass budget ─────────────────────────────────────────────

def test_upgrading_pass_stops_starting_searches_after_its_budget(hub_db, app_settings):
    import queues.upgrading_queue as uq
    app_settings['Scraping'] = {'enable_upgrading': True}
    app_settings['Debug'] = {'upgrade_queue_duration_hours': '24'}
    q = uq.UpgradingQueue()
    q.items = [{'id': i, 'title': f'T{i}', 'type': 'movie', 'imdb_id': f'tt{i}', 'version': '1080p'}
               for i in (1, 2, 3)]
    clock = [1000.0]
    searched = []

    def fake_scrape(item, queue_manager=None):
        searched.append(item['id'])
        clock[0] += uq.PASS_BUDGET_SECONDS + 1  # one search eats the whole budget

    with mock.patch.object(uq.time, 'monotonic', side_effect=lambda: clock[0]), \
         mock.patch.object(q, 'hourly_scrape', side_effect=fake_scrape), \
         mock.patch.object(q, 'save_upgrade_times'), mock.patch.object(q, 'save_upgrades_data'):
        q.process()
        assert searched == [1]
        # Next pass picks up where it left off: item 1 isn't due again for an hour.
        q.process()
        assert searched == [1, 2]


def _queue_with(app_settings, ids, duration='24'):
    import queues.upgrading_queue as uq
    app_settings['Scraping'] = {'enable_upgrading': True}
    app_settings['Debug'] = {'upgrade_queue_duration_hours': duration}
    q = uq.UpgradingQueue()
    q.items = [{'id': i, 'title': f'T{i}', 'type': 'movie', 'imdb_id': f'tt{i}', 'version': '1080p'}
               for i in ids]
    return uq, q


def test_items_cut_off_by_the_budget_go_first_next_time(hub_db, app_settings):
    # With a fixed order, an hour later item 1 was due again and took the whole
    # budget, so items after it were never searched.
    from datetime import datetime, timedelta
    uq, q = _queue_with(app_settings, (1, 2, 3))
    t0 = datetime(2026, 1, 1, 12, 0)
    q.upgrade_times = {i: {'start_time': t0, 'time_added': 'x'} for i in (1, 2, 3)}
    clock = [0.0]
    searched = []

    def fake_scrape(item, queue_manager=None):
        searched.append(item['id'])
        clock[0] += uq.PASS_BUDGET_SECONDS + 1

    class FakeDT(datetime):
        now_val = t0

        @classmethod
        def now(cls, tz=None):
            return cls.now_val

    with mock.patch.object(uq.time, 'monotonic', side_effect=lambda: clock[0]), \
         mock.patch.object(uq, 'datetime', FakeDT), \
         mock.patch.object(q, 'hourly_scrape', side_effect=fake_scrape), \
         mock.patch.object(q, 'save_upgrade_times'), mock.patch.object(q, 'save_upgrades_data'):
        for minutes in (0, 61, 122):  # item 1 is due again on every one of these passes
            FakeDT.now_val = t0 + timedelta(minutes=minutes)
            q.process()
    assert searched == [1, 2, 3]


def test_hub_queued_magnets_go_before_ordinary_searches(hub_db, app_settings):
    import database.zilean_upgrade as zu
    uq, q = _queue_with(app_settings, (1, 2, 3))
    zu._queued_magnets[3] = {'new_magnet': 'magnet:?xt=urn:btih:' + 'e' * 40}
    assert [i['id'] for i in q._pass_order()] == [3, 1, 2]


@pytest.mark.parametrize('stored', ['48', 48, 48.0])
def test_upgrade_window_reads_text_and_numbers(hub_db, app_settings, stored):
    # The old Settings page stored '48'; the Upgrade Hub stores 48.
    from datetime import datetime, timedelta
    uq, q = _queue_with(app_settings, (7,), duration=stored)
    q.upgrade_times = {7: {'start_time': datetime.now() - timedelta(hours=30), 'time_added': 'x'}}
    q.last_scrape_times = {7: datetime.now()}  # not due, so only the timeout check runs
    with mock.patch.object(q, 'save_upgrade_times'), mock.patch.object(q, 'save_upgrades_data'), \
         mock.patch('database.update_media_item_state') as set_state:
        q.process()
    assert not set_state.called  # 30h is inside a 48h window


def test_upgrading_queue_runs_every_five_minutes():
    from queues.runner.task_registry import SPECS
    spec = SPECS['Upgrading']
    assert spec.interval == 300


# ── Upgrading settings on the Upgrade Hub ────────────────────────────────────

def _call(view, method='GET', **ctx):
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context(method=method, **ctx):
        rv = view.__wrapped__()
    resp, status = (rv if isinstance(rv, tuple) else (rv, rv.status_code))
    return resp.get_json(), status


def test_hub_settings_read_and_write_the_upgrading_keys_in_their_own_sections():
    from routes import upgrade_hub_routes as r
    store = {('Scraping', 'enable_upgrading'): True, ('Debug', 'upgrade_queue_duration_hours'): '48',
             ('Scraping', 'upgrading_percentage_threshold'): '0.25'}

    def fake_get(section, key, default=None):
        return store.get((section, key), default)

    def fake_set(section, key, value):
        store[(section, key)] = value

    with mock.patch.object(r, 'get_setting', side_effect=fake_get), \
         mock.patch.object(r, 'set_setting', side_effect=fake_set):
        body, status = _call(r.get_settings)
        upg = body['settings']['upgrading']
        assert upg['enable_upgrading'] is True
        assert upg['upgrade_queue_duration_hours'] == 48
        assert upg['upgrading_percentage_threshold'] == 0.25
        assert upg['enable_upgrading_cleanup'] is True  # default
        assert upg['upgrade_max_release_age_days'] == 7  # default
        assert body['settings']['fresh_search_days'] == 30
        assert body['settings']['fresh_search_limit'] == 20

        _call(r.save_settings, method='POST', json={
            'fresh_search_days': 14, 'fresh_search_limit': 0,
            'upgrading': {'enable_upgrading': False, 'upgrading_percentage_threshold': 3,
                          'upgrade_queue_duration_hours': 0, 'delayed_upgrade_scrape_days': 7,
                          'upgrade_max_release_age_days': 60},
        })
    assert store[('Scraping', 'enable_upgrading')] is False
    assert store[('Scraping', 'upgrading_percentage_threshold')] == 1.0  # clamped
    assert store[('Debug', 'upgrade_queue_duration_hours')] == 1          # clamped
    assert store[('Scraping', 'delayed_upgrade_scrape_days')] == 7
    assert store[('Scraping', 'upgrade_max_release_age_days')] == 60
    assert store[('Upgrade Hub', 'fresh_search_days')] == 14
    assert store[('Upgrade Hub', 'fresh_search_limit')] == 0
    assert ('Upgrade Hub', 'enable_upgrading') not in store


def test_saving_without_upgrading_block_leaves_those_settings_alone():
    from routes import upgrade_hub_routes as r
    store = {}
    with mock.patch.object(r, 'set_setting', side_effect=lambda s, k, v: store.__setitem__((s, k), v)):
        _call(r.save_settings, method='POST', json={'max_upgrades_per_run': 5})
    assert not any(s in ('Scraping', 'Debug') for s, _ in store)


def test_settings_page_no_longer_defaults_enable_upgrading_off():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / 'static' / 'js' / 'settings.js').read_text()
    assert "settingsData['Scraping']['enable_upgrading'] = false" not in js


def test_status_counts_fresh_release_search_as_a_source(hub_db):
    from routes import upgrade_hub_routes as r
    with mock.patch('database.zilean_upgrade.get_zilean_config', return_value=None), \
         mock.patch('database.nzb_upgrade.get_newznab_scrapers', return_value=[]), \
         mock.patch('database.zilean_upgrade.get_last_results', return_value=None), \
         mock.patch('database.upgrade_hub_scrapers.fresh_search_settings',
                    return_value={'enabled': True, 'limit': 20, 'days': 30}):
        body, _ = _call(r.status)
    assert body['any_source_configured'] is True
    assert body['fresh_search_enabled'] is True


@pytest.mark.parametrize('stored, expected', [(None, 7), ('30', 30), (60, 60), ('', 7), ('x', 7), (-5, 0)])
def test_release_age_setting(app_settings, stored, expected):
    from utilities.settings import get_upgrade_max_release_age_days
    if stored is not None:
        app_settings['Scraping'] = {'upgrade_max_release_age_days': stored}
    assert get_upgrade_max_release_age_days() == expected


@pytest.mark.parametrize('path', ['database/collected_items.py', 'utilities/local_library_scan.py'])
def test_both_collection_paths_use_the_release_age_setting(path):
    # Items collected by the Plex/symlink check and by the local library scan.
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / path).read_text()
    assert 'days_since_release <= 7' not in src
    assert 'days_since_release <= get_upgrade_max_release_age_days()' in src


# ── Search recent releases ───────────────────────────────────────────────────

def test_released_within_keeps_only_known_dates_in_the_window():
    from datetime import date
    import database.upgrade_hub_scrapers as uhs
    today = date(2026, 11, 24)
    items = [{'id': 1, 'release_date': '2026-11-24'},   # today
             {'id': 2, 'release_date': '2026-10-25'},   # 30 days ago
             {'id': 3, 'release_date': '2026-10-24'},   # 31 days ago
             {'id': 4, 'release_date': '2026-12-01'},   # not out yet
             {'id': 5, 'release_date': None},
             {'id': 6, 'release_date': 'Unknown'}]
    assert [i['id'] for i in uhs.released_within(items, 30, today)] == [1, 2]
    assert [i['id'] for i in uhs.released_within(items, 0, today)] == [1]


def test_recent_search_covers_every_recent_item_past_the_cap_and_toggle(hub_db, app_settings, monkeypatch):
    from datetime import date, timedelta
    import database.zilean_upgrade as zu
    import database.upgrade_hub_scrapers as uhs
    # Scraper search off and capped at 1: the recent search ignores both.
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'fresh_search_limit': 0}
    recent = (date.today() - timedelta(days=3)).isoformat()
    old = (date.today() - timedelta(days=400)).isoformat()
    conn = hub_db()
    new_ids = [_insert(conn, type='episode', title='S', imdb_id='tt9', tmdb_id='9', season_number=1,
                       episode_number=n, filled_by_file=f'S.S01E0{n}.1080p.WEB.mkv', size=2.0,
                       release_date=recent) for n in (1, 2, 3)]
    old_id = _insert(conn, type='movie', title='Old', imdb_id='tt8', tmdb_id='8', year=2020,
                     filled_by_file='Old.2020.1080p.mkv', size=5.0, release_date=old)
    conn.close()
    monkeypatch.setattr(zu, '_save_scan_results_to_db', lambda *a, **k: None)
    seen = []
    with mock.patch.object(uhs, '_scan_item', side_effect=lambda it, *a, **k: seen.append(it['id'])), \
         mock.patch('database.upgrade_hub_activity.log_hub_activity'):
        out = zu.scan_for_upgrades(recent_release_days=7)
    assert 'error' not in out, out
    assert sorted(seen) == sorted(new_ids)
    assert old_id not in seen
    assert uhs.count_recent_items(7) == {'items': 3, 'movies': 0, 'episodes': 3, 'cap': uhs.MAX_CHECKS_PER_SCAN}


def test_recent_search_skips_zilean_and_nzb_and_keeps_other_candidates(hub_db, app_settings, monkeypatch):
    # A targeted run: only the scraper search runs, and the last results are
    # kept for every item it didn't search.
    from datetime import date, timedelta
    import database.zilean_upgrade as zu
    import database.upgrade_hub_scrapers as uhs
    import database.nzb_upgrade as nzb
    app_settings['Upgrade Hub'] = {'upgrade_source': 'both'}
    recent = (date.today() - timedelta(days=2)).isoformat()
    conn = hub_db()
    searched_no_upgrade = _insert(conn, type='movie', title='A', imdb_id='tt11', tmdb_id='11', year=2026,
                                  filled_by_file='A.2026.1080p.mkv', size=5.0, release_date=recent)
    searched_upgrade = _insert(conn, type='movie', title='B', imdb_id='tt12', tmdb_id='12', year=2026,
                               filled_by_file='B.2026.1080p.mkv', size=5.0, release_date=recent)
    old_item = _insert(conn, type='movie', title='C', imdb_id='tt13', tmdb_id='13', year=2010,
                       filled_by_file='C.2010.720p.mkv', size=2.0, release_date='2010-01-01')
    conn.close()
    monkeypatch.setattr(zu, '_save_scan_results_to_db', lambda *a, **k: None)
    monkeypatch.setattr(zu, '_cache_initialized', True)
    monkeypatch.setattr(zu, '_last_scan_results', {
        'upgrade_candidates': [{'item_id': old_item, 'improvement_pct': 50.0, 'title': 'C'},
                               {'item_id': searched_no_upgrade, 'improvement_pct': 20.0, 'title': 'A'}],
        'pack_candidates': [{'item_ids': [99], 'ratio_pct': 30.0}],
    })
    touched = []
    for name in ('_api_query', '_load_rest_cache_from_db', '_db_batch_fetch_movies', '_get_db_config'):
        monkeypatch.setattr(zu, name, lambda *a, _n=name, **k: touched.append(_n))
    monkeypatch.setattr(zu, 'get_zilean_config', lambda: (object(), {'url': 'http://zilean'}))
    monkeypatch.setattr(nzb, 'get_newznab_scrapers', lambda: [{'name': 'idx'}])
    monkeypatch.setattr(nzb, 'scan_nzb_upgrades', lambda *a, **k: touched.append('nzb') or {})

    def fake_scan(it, *a, **k):
        if it['id'] != searched_upgrade:
            return None
        return {'type': 'movie', 'item_id': it['id'], 'title': 'B', 'current_score': 50, 'new_score': 100,
                'improvement_pct': 100.0, 'new_title': 'B.2026.2160p', 'new_magnet': 'magnet:?xt=urn:btih:' + 'f' * 40,
                'protocol': 'torrent', 'found_by': 'scrapers', '_alts': [], '_phalanx_only': False}

    with mock.patch.object(uhs, '_scan_item', side_effect=fake_scan), \
         mock.patch('database.upgrade_hub_activity.log_hub_activity'):
        out = zu.scan_for_upgrades(recent_release_days=7)
    assert 'error' not in out, out
    assert touched == [] or touched == ['_get_db_config'], touched  # config read only, no queries
    ids = [c['item_id'] for c in out['upgrade_candidates']]
    assert ids == [searched_upgrade, old_item]          # A replaced (no upgrade now), C kept
    assert out['pack_candidates'] == [{'item_ids': [99], 'ratio_pct': 30.0}]
    assert (out['scanned_movies'], out['scanned_episodes']) == (2, 0)
    assert out['recent_release_days'] == 7


def _start_scan(json, **settings):
    from routes import upgrade_hub_routes as r
    started = {}

    class FakeThread:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            self.target()

    store = {('Upgrade Hub', 'upgrade_source'): 'none', ('Upgrade Hub', 'fresh_search_limit'): 0}
    store.update(settings)
    with mock.patch.object(r, 'get_setting', side_effect=lambda s, k, d=None: store.get((s, k), d)), \
         mock.patch('database.upgrade_hub_scrapers.get_setting', side_effect=lambda s, k, d=None: store.get((s, k), d)), \
         mock.patch('database.zilean_upgrade.get_scan_status', return_value={'in_progress': False}), \
         mock.patch('database.zilean_upgrade.get_zilean_config', return_value=None), \
         mock.patch('database.nzb_upgrade.get_newznab_scrapers', return_value=[]), \
         mock.patch('database.zilean_upgrade.scan_for_upgrades',
                    side_effect=lambda **kw: started.update(kw)), \
         mock.patch.object(r.threading, 'Thread', FakeThread):
        body, status = _call(r.start_scan, method='POST', json=json)
    return body, status, started


def test_recent_search_route_starts_even_with_no_other_source():
    body, status, started = _start_scan({'recent_release_days': 30})
    assert status == 200 and body['success'] is True
    assert started['recent_release_days'] == 30


def test_plain_scan_route_still_needs_a_source():
    body, status, started = _start_scan({})
    assert status == 400 and not started


@pytest.mark.parametrize('days', [-1, 'abc', 99999])
def test_recent_search_route_rejects_bad_days(days):
    body, status, started = _start_scan({'recent_release_days': days})
    assert status == 400 and not started


# ── Bulk Sources default ─────────────────────────────────────────────────────

@pytest.mark.parametrize('stored, expected', [
    (None, 'both'), ('', 'both'), ('garbage', 'both'),
    ('both', 'both'), ('zilean_only', 'zilean_only'), ('nzb_only', 'nzb_only'), ('none', 'none'),
])
def test_bulk_sources_default_to_both(app_settings, stored, expected):
    import database.upgrade_hub_scrapers as uhs
    app_settings['Upgrade Hub'] = {} if stored is None else {'upgrade_source': stored}
    assert uhs.upgrade_source() == expected
