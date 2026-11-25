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


def test_full_scan_with_only_the_scraper_source(hub_db, app_settings, monkeypatch):
    import database.zilean_upgrade as zu
    import database.upgrade_hub_scrapers as uhs
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'use_scrapers': True, 'scraper_checks_per_scan': 10}
    conn = hub_db()
    iid = _insert(conn, type='movie', title='The Long Walk', imdb_id='tt1', tmdb_id='1', year=2025,
                  filled_by_file='The.Long.Walk.2025.1080p.WEB-DL-FLUX.mkv', size=5.0)
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


def test_scan_limit_does_not_pin_the_scraper_rotation_to_the_first_rows(hub_db, app_settings, monkeypatch):
    # Scan Limit caps the Zilean/NZB phases; the scraper phase picks its own
    # oldest-checked slice from the whole library.
    import database.zilean_upgrade as zu
    import database.upgrade_hub_scrapers as uhs
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'use_scrapers': True, 'scraper_checks_per_scan': 10}
    conn = hub_db()
    ids = [_insert(conn, type='movie', title=f'M{n}', imdb_id=f'tt{n}', tmdb_id=str(n), year=2025,
                   filled_by_file=f'M{n}.2025.1080p.WEB.mkv', size=5.0) for n in (1, 2, 3)]
    conn.close()
    monkeypatch.setattr(zu, '_save_scan_results_to_db', lambda *a, **k: None)
    seen = []
    with mock.patch.object(uhs, '_scan_item', side_effect=lambda it, *a, **k: seen.append(it['id'])), \
         mock.patch('database.upgrade_hub_activity.log_hub_activity'):
        out = zu.scan_for_upgrades(scan_limit=1)
    assert 'error' not in out, out
    assert sorted(seen) == sorted(ids)


def test_scan_with_no_source_at_all_says_so(hub_db, app_settings):
    import database.zilean_upgrade as zu
    app_settings['Upgrade Hub'] = {'upgrade_source': 'none', 'use_scrapers': False}
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
        assert body['settings']['use_scrapers'] is True
        assert body['settings']['scraper_checks_per_scan'] == 50

        _call(r.save_settings, method='POST', json={
            'use_scrapers': False, 'scraper_checks_per_scan': 200,
            'upgrading': {'enable_upgrading': False, 'upgrading_percentage_threshold': 3,
                          'upgrade_queue_duration_hours': 0, 'delayed_upgrade_scrape_days': 7,
                          'upgrade_max_release_age_days': 60},
        })
    assert store[('Scraping', 'enable_upgrading')] is False
    assert store[('Scraping', 'upgrading_percentage_threshold')] == 1.0  # clamped
    assert store[('Debug', 'upgrade_queue_duration_hours')] == 1          # clamped
    assert store[('Scraping', 'delayed_upgrade_scrape_days')] == 7
    assert store[('Scraping', 'upgrade_max_release_age_days')] == 60
    assert store[('Upgrade Hub', 'use_scrapers')] is False
    assert store[('Upgrade Hub', 'scraper_checks_per_scan')] == 200
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


def test_status_counts_scrapers_as_a_source(hub_db):
    from routes import upgrade_hub_routes as r
    with mock.patch('database.zilean_upgrade.get_zilean_config', return_value=None), \
         mock.patch('database.nzb_upgrade.get_newznab_scrapers', return_value=[]), \
         mock.patch('database.zilean_upgrade.get_last_results', return_value=None), \
         mock.patch('database.upgrade_hub_scrapers.scraper_settings', return_value={'enabled': True, 'limit': 50}):
        body, _ = _call(r.status)
    assert body['any_source_configured'] is True
    assert body['scrapers_enabled'] is True


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
