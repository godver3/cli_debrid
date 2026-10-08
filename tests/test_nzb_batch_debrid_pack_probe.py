#!/usr/bin/env python3
"""ScrapingQueue NZB batch: "Disable NZB Season Packs" must not block debrid season packs.

Drives the real ScrapingQueue.process() against a temp SQLite DB with the scraper
stubbed out. A full season waiting in Scraping, with the Usenet Provider enabled:

- setting off: season-pack path, one scrape (unchanged)
- setting on, top result is a debrid season pack: season-pack path, the probe's
  results are reused (no second scrape)
- setting on, anything else (single episodes on top, a 2-episode release, a partial
  range, an NZB pack, no results, fall_back_to_single_scraper set): every episode is
  batched individually in the same tick, as before the probe existed
"""

import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

IMDB = 'tt0411008'
SEASON = 2
N_EPS = 24


def _res(title, protocol='torrent', episodes=None, season_pack='N/A'):
    return {
        'title': title, 'original_title': title, 'protocol': protocol,
        'magnet': None if protocol == 'nzb' else f'magnet:?xt=urn:btih:{title}',
        'nzb_url': f'https://idx/{title}.nzb' if protocol == 'nzb' else None,
        'parsed_info': {'season_episode_info': {'season_pack': season_pack, 'episodes': episodes or []}},
        'score_breakdown': {'total_score': 100},
    }


def _single_nzb(ep):
    return _res(f'Lost.S02E{ep:02d}.1080p', 'nzb', [ep])


DEBRID_PACK = _res('Lost.S02.1080p.BluRay', 'torrent', [], str(SEASON))
COMPLETE_PACK = _res('Lost.Complete.Series.1080p', 'torrent', [], 'Complete')
FULL_RANGE = _res('Lost.S02E01-E24.1080p', 'torrent', list(range(1, N_EPS + 1)), str(SEASON))
PARTIAL_RANGE = _res('Lost.S02.E01-E12.720p', 'torrent', list(range(1, 13)), str(SEASON))
DOUBLE_EP = _res('Lost.S02E01E02.720p', 'torrent', [1, 2], str(SEASON))
OTHER_SEASON_PACK = _res('Lost.S03.1080p', 'torrent', [], '3')
NZB_PACK = _res('Lost.S02.1080p.NZBPACK', 'nzb', [], str(SEASON))


class TestNzbBatchDebridPackProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other tests replace database / queues modules in sys.modules with stubs.
        # Import the real ones here and restore whatever was there afterwards.
        cls._modules = mock.patch.dict(sys.modules)
        cls._modules.start()
        for name in list(sys.modules):
            if name == 'database' or name.startswith('database.') or name == 'queues.scraping_queue':
                del sys.modules[name]
        global database, sq
        database = importlib.import_module('database')
        importlib.import_module('database.core')
        importlib.import_module('database.database_reading')
        sq = importlib.import_module('queues.scraping_queue')

    @classmethod
    def tearDownClass(cls):
        cls._modules.stop()

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.db = os.path.join(self._dir, 'media.db')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    # --- tiny DB layer ---------------------------------------------------
    def _conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def _rows(self, imdb_id=IMDB, **_):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute("SELECT * FROM media_items WHERE imdb_id=?", (imdb_id,))]
        finally:
            c.close()

    def _by_id(self, item_id):
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM media_items WHERE id=?", (item_id,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def _setup_db(self, collected=(), fallback_ids=()):
        c = self._conn()
        c.execute(
            """CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, tmdb_id TEXT, title TEXT,
            year INT, type TEXT, state TEXT, version TEXT, season_number INT, episode_number INT,
            release_date TEXT, filled_by_torrent_id TEXT, filled_by_file TEXT, filled_by_magnet TEXT,
            filled_by_title TEXT, original_scraped_torrent_title TEXT, nzb_segment_id TEXT,
            fall_back_to_single_scraper INT DEFAULT 0, genres TEXT, current_score REAL DEFAULT 0,
            resolution TEXT)"""
        )
        for ep in range(1, N_EPS + 1):
            c.execute(
                "INSERT INTO media_items (id, imdb_id, tmdb_id, title, year, type, state, version, "
                "season_number, episode_number, release_date, fall_back_to_single_scraper) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ep, IMDB, '4607', 'Lost', 2004, 'episode',
                 'Collected' if ep in collected else 'Scraping', '1080p', SEASON, ep,
                 '2005-09-21', 1 if ep in fallback_ids else 0),
            )
        c.commit()
        c.close()

    # --- driver ----------------------------------------------------------
    def _run(self, multi_results, disable=True, collected=(), fallback_ids=(), debrid='usable'):
        """debrid: 'usable', 'down' (provider configured but unavailable) or 'none' (usenet-only)."""
        self._setup_db(collected, fallback_ids)
        q = sq.ScrapingQueue.__new__(sq.ScrapingQueue)
        q.items = [self._by_id(ep) for ep in range(1, N_EPS + 1) if ep not in collected]
        q._item_ids = {it['id'] for it in q.items}
        scrape_calls = []

        def fake_scrape(_self, item, is_multi_pack, _qm, skip_filter=False, check_pack_wantedness=False):
            if self._by_id(item['id']).get('fall_back_to_single_scraper'):
                is_multi_pack = False
            scrape_calls.append(is_multi_pack)
            if is_multi_pack:
                return list(multi_results), []
            return [_single_nzb(item['episode_number'])], []

        def fake_get_setting(section, key=None, default=None):
            if section == 'Usenet Provider' and key == 'enabled':
                return True
            if section == 'Usenet Provider' and key == 'disable_nzb_season_packs':
                return disable
            if section == 'Scrapers':
                return {}
            if section == 'Scraping' and key == 'versions':
                return {'1080p': {}}
            return default

        provider = object()
        debrid_stub = types.ModuleType('debrid')
        debrid_stub.get_debrid_providers = lambda: [] if debrid == 'none' else [provider]
        import utilities.acquisition_health as acquisition_health

        qm = mock.MagicMock()
        qm.generate_identifier.side_effect = lambda it: f"Lost S02E{it['episode_number']:02d}"
        qm.queues = {}
        with mock.patch.object(sq, 'get_setting', fake_get_setting), \
                mock.patch.object(sq.DirectAPI, 'get_show_metadata', return_value=(None, None)), \
                mock.patch.object(sq.ScrapingQueue, 'scrape_with_fallback', fake_scrape), \
                mock.patch.object(sq.ScrapingQueue, 'reset_not_wanted_check', lambda _self, _id: None), \
                mock.patch.object(sq, 'is_magnet_not_wanted', return_value=False), \
                mock.patch.object(sq, 'is_url_not_wanted', return_value=False), \
                mock.patch.object(sq, 'is_nzb_guid_not_wanted', return_value=False), \
                mock.patch.object(database, 'get_db_connection', self._conn), \
                mock.patch.object(database.core, 'get_db_connection', self._conn), \
                mock.patch.object(database, 'get_all_media_items', self._rows), \
                mock.patch.object(database, 'get_media_item_by_id', self._by_id), \
                mock.patch.object(database.database_reading, 'get_all_media_items', self._rows), \
                mock.patch.dict(sys.modules, {'debrid': debrid_stub}), \
                mock.patch.object(acquisition_health, 'provider_available',
                                  lambda p: debrid == 'usable' and p is provider):
            q.process(qm)
        adds = [(c.args[0]['episode_number'], c.args[2]) for c in qm.move_to_adding.call_args_list]
        return scrape_calls, adds

    def _assert_season_path(self, multi_results, expected_title, **kw):
        calls, adds = self._run(multi_results, **kw)
        self.assertEqual(calls, [True], 'probe results must be reused, not scraped twice')
        self.assertEqual(adds, [(1, expected_title)])

    def _assert_per_episode_batch(self, multi_results, expected=N_EPS, **kw):
        _, adds = self._run(multi_results, **kw)
        self.assertEqual(len(adds), expected)
        for ep, title in adds:
            self.assertEqual(title, f'Lost.S02E{ep:02d}.1080p')

    # --- unchanged paths -------------------------------------------------
    def test_setting_off_uses_season_path(self):
        self._assert_season_path([DEBRID_PACK, _single_nzb(1)], DEBRID_PACK['title'], disable=False)

    def test_fall_back_flag_skips_probe(self):
        calls, adds = self._run([DEBRID_PACK], fallback_ids=(1,))
        self.assertNotIn(True, calls)
        self.assertEqual(len(adds), N_EPS)

    def test_partial_season_skips_probe(self):
        calls, adds = self._run([DEBRID_PACK], collected=(5,))
        self.assertNotIn(True, calls)
        self.assertEqual(len(adds), N_EPS - 1)

    # --- top result is a debrid season pack -> season path -----------------
    def test_debrid_season_pack_on_top(self):
        self._assert_season_path([DEBRID_PACK, _single_nzb(1)], DEBRID_PACK['title'])

    def test_complete_series_pack_on_top(self):
        self._assert_season_path([COMPLETE_PACK], COMPLETE_PACK['title'])

    def test_full_season_range_on_top(self):
        self._assert_season_path([FULL_RANGE], FULL_RANGE['title'])

    # --- anything else -> every episode batched in one tick -------------
    def test_single_episode_ranked_above_pack(self):
        self._assert_per_episode_batch([_single_nzb(1), DEBRID_PACK])

    def test_two_episode_release_is_not_a_season_pack(self):
        self._assert_per_episode_batch([DOUBLE_EP, _single_nzb(1)])

    def test_partial_range_is_not_a_season_pack(self):
        self._assert_per_episode_batch([PARTIAL_RANGE])

    def test_other_season_pack_is_not_a_season_pack(self):
        self._assert_per_episode_batch([OTHER_SEASON_PACK])

    def test_nzb_pack_is_not_a_debrid_pack(self):
        self._assert_per_episode_batch([NZB_PACK])

    def test_no_results(self):
        self._assert_per_episode_batch([])

    # --- no usable debrid provider -> no probe scrape at all ---------------
    def test_usenet_only_skips_probe(self):
        # A torrent scraper left enabled can put a torrent pack on top, but Adding
        # would drop it with no debrid provider, and E01 would go alone.
        calls, adds = self._run([DEBRID_PACK], debrid='none')
        self.assertNotIn(True, calls, 'usenet-only must not run the season-pack scrape')
        self.assertEqual(len(adds), N_EPS)

    def test_debrid_down_skips_probe(self):
        calls, adds = self._run([DEBRID_PACK], debrid='down')
        self.assertNotIn(True, calls)
        self.assertEqual(len(adds), N_EPS)


if __name__ == '__main__':
    unittest.main()
