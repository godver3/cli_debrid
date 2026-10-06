#!/usr/bin/env python3
"""Scraper / indexer stats: grab logging, outcome tracking, aggregation, and the
source fields the scrapers stamp on their results (database/scraper_grabs.py)."""

import os
import tempfile
import unittest
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order)
from database import scraper_grabs as sg
from database.core import get_db_connection


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._prev_db = os.environ['USER_DB_CONTENT']
        os.environ['USER_DB_CONTENT'] = tempfile.mkdtemp()
        conn = get_db_connection()
        conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, state TEXT, collected_at TIMESTAMP)")
        conn.commit()
        conn.close()
        sg.create_scraper_grabs_table()

    def tearDown(self):
        os.environ['USER_DB_CONTENT'] = self._prev_db

    def set_item(self, item_id, state):
        conn = get_db_connection()
        conn.execute("INSERT OR REPLACE INTO media_items (id, state) VALUES (?, ?)", (item_id, state))
        conn.commit()
        conn.close()

    def delete_item(self, item_id):
        conn = get_db_connection()
        conn.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()

    def rows(self, item_id):
        conn = get_db_connection()
        out = [dict(r) for r in conn.execute(
            "SELECT * FROM scraper_grabs WHERE item_id = ? ORDER BY id", (item_id,))]
        conn.close()
        return out

    @staticmethod
    def result(title, instance='Torrentio', indexer='ThePirateBay', **extra):
        return {'title': title, 'scraper_type': 'Torrentio', 'scraper_instance': instance,
                'indexer': indexer, 'hash': title.lower().replace(' ', '') + 'h', **extra}


class TestSourceExtraction(unittest.TestCase):
    def test_split_source(self):
        self.assertEqual(sg.split_source('Torrentio - ThePirateBay'), ('Torrentio', 'ThePirateBay'))
        self.assertEqual(sg.split_source('AIO - Comet - NZBgeek'), ('AIO', 'NZBgeek'))
        self.assertEqual(sg.split_source('altHUB'), ('altHUB', ''))
        self.assertEqual(sg.split_source(''), ('', ''))

    def test_structured_fields_win(self):
        r = {'source': 'Prowlarr - X', 'scraper_type': 'Prowlarr', 'scraper_instance': 'Prowlarr', 'indexer': 'NZBgeek'}
        self.assertEqual(sg.result_source(r), ('Prowlarr', 'Prowlarr', 'NZBgeek'))

    def test_legacy_result_falls_back_to_source_string(self):
        self.assertEqual(sg.result_source({'source': 'Jackett - 1337x'}), ('', 'Jackett', '1337x'))

    def test_current_file_placeholder_has_no_source(self):
        self.assertEqual(sg.result_source({'source': '__current__'}), ('', '', ''))


class TestRecordAndOutcomes(_DbCase):
    def test_superseded_pending_grab_is_failed(self):
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.record_grab({'id': 1}, self.result('B'))
        a, b = self.rows(1)
        self.assertEqual((a['outcome'], a['outcome_reason']), ('failed', 'superseded before collect'))
        self.assertEqual(b['outcome'], 'pending')
        self.assertEqual((b['scraper_instance'], b['indexer'], b['kind']), ('Torrentio', 'ThePirateBay', 'torrent'))

    def test_grab_while_in_library_replaces(self):
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        self.set_item(1, 'Collected')
        sg.record_grab({'id': 1}, self.result('B'), trigger='upgrade')
        self.assertEqual([r['outcome'] for r in self.rows(1)], ['replaced', 'pending'])

    def test_upgrade_settles_unreconciled_previous_as_collected(self):
        # The upgrading queue moves the item to Adding before the grab is recorded.
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.record_grab({'id': 1}, self.result('B'), trigger='upgrade')
        self.assertEqual(self.rows(1)[0]['outcome'], 'replaced')

    def test_repair_marks_previous_repaired(self):
        self.set_item(1, 'Collected')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.record_grab({'id': 1}, self.result('B'), trigger='repair', kind='nzb')
        old, new = self.rows(1)
        self.assertEqual(old['outcome'], 'repaired')
        self.assertEqual((new['trigger'], new['kind']), ('repair', 'nzb'))

    def test_same_release_resubmitted_is_not_a_new_grab(self):
        self.set_item(1, 'Adding')
        first = sg.record_grab({'id': 1}, self.result('A'))
        again = sg.record_grab({'id': 1}, self.result('A'))
        self.assertEqual(first, again)
        self.assertEqual(len(self.rows(1)), 1)

    def test_failed_upgrade_restores_replaced_grab(self):
        self.set_item(1, 'Collected')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.record_grab({'id': 1}, self.result('B'), trigger='upgrade')
        sg.mark_current_grab(1, 'failed', 'no matching files')
        self.assertEqual([r['outcome'] for r in self.rows(1)], ['collected', 'failed'])

    def test_upgrade_restore_outside_adding_counts_as_failed(self):
        # e.g. a Checking timeout: UpgradingQueue.restore_item_state puts the old file back.
        from queues.upgrading_queue import UpgradingQueue
        self.set_item(1, 'Collected')
        sg.record_grab({'id': 1}, self.result('A'))
        self.set_item(1, 'Checking')
        sg.record_grab({'id': 1}, self.result('B'), trigger='upgrade')
        q = UpgradingQueue()
        q.upgrade_states = {1: [{'timestamp': 'then', 'state': {'state': 'Collected'}}]}
        with mock.patch.object(q, 'save_upgrade_states'):
            self.assertTrue(q.restore_item_state({'id': 1}))
        sg.reconcile()
        self.assertEqual([r['outcome'] for r in self.rows(1)], ['collected', 'failed'])
        self.assertEqual(self.rows(1)[1]['outcome_reason'], 'upgrade failed; previous file restored')

    def test_upgrade_restore_ignores_non_upgrade_grab(self):
        # An upgrade that failed before its grab was recorded must not fail the original grab.
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.mark_current_grab(1, 'failed', 'restored', only_trigger='upgrade')
        self.assertEqual(self.rows(1)[0]['outcome'], 'pending')

    def test_failed_auto_grab_does_not_touch_older_rows(self):
        self.set_item(1, 'Collected')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.record_grab({'id': 1}, self.result('B'))
        sg.mark_current_grab(1, 'failed', 'broken')
        self.assertEqual([r['outcome'] for r in self.rows(1)], ['replaced', 'failed'])

    def test_mark_failed_ignores_collected_grab(self):
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        self.set_item(1, 'Collected')
        sg.reconcile()
        sg.mark_current_grab(1, 'failed', 'x')
        self.assertEqual(self.rows(1)[0]['outcome'], 'collected')

    def test_mark_repaired_counts_as_collected(self):
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.mark_current_grab(1, 'repaired', 'debrid repair')
        row = self.rows(1)[0]
        self.assertEqual(row['outcome'], 'repaired')
        self.assertIsNotNone(row['collected_at'])

    def test_manual_grab_without_source(self):
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, {}, trigger='manual', release_title='X')
        self.assertEqual(self.rows(1)[0]['scraper_instance'], 'Manual')

    def test_never_raises(self):
        with mock.patch.object(sg, 'get_db_connection', side_effect=RuntimeError('db down')):
            self.assertIsNone(sg.record_grab({'id': 1}, self.result('A')))
            sg.mark_current_grab(1, 'failed', 'x')

    def test_reconcile(self):
        for item_id, state in ((1, 'Adding'), (2, 'Adding'), (3, 'Adding'), (4, 'Adding')):
            self.set_item(item_id, state)
            sg.record_grab({'id': item_id}, self.result(f'R{item_id}'))
        self.set_item(1, 'Collected')
        self.set_item(2, 'Sleeping')
        self.delete_item(3)
        sg.reconcile()
        self.assertEqual(self.rows(1)[0]['outcome'], 'collected')
        self.assertEqual(self.rows(2)[0]['outcome_reason'], 'item returned to Sleeping')
        self.assertEqual(self.rows(3)[0]['outcome_reason'], 'item removed')
        self.assertEqual(self.rows(4)[0]['outcome'], 'pending')


class TestAggregation(_DbCase):
    def test_per_scraper_and_indexer_totals(self):
        # Torrentio/TPB: 1 in library, 1 failed. Torrentio/1337x: repaired.
        # altHUB (no indexer): pending.
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1}, self.result('A'))
        sg.record_grab({'id': 1}, self.result('B'))           # A failed
        self.set_item(1, 'Collected')                         # B collected
        self.set_item(2, 'Collected')
        sg.record_grab({'id': 2}, self.result('C', indexer='1337x'))
        sg.mark_current_grab(2, 'repaired', 'broken')
        self.set_item(3, 'Adding')
        sg.record_grab({'id': 3}, {'title': 'D', 'scraper_type': 'Newznab', 'scraper_instance': 'altHUB',
                                   'protocol': 'nzb'})

        stats = sg.get_scraper_stats()
        by = {s['instance']: s for s in stats['scrapers']}
        t = by['Torrentio']
        self.assertEqual((t['grabs'], t['in_library'], t['failed'], t['repaired']), (3, 1, 1, 1))
        self.assertEqual(t['success_rate'], 66.7)
        self.assertEqual(t['repair_rate'], 50.0)
        idx = {i['indexer']: i for i in t['indexers']}
        self.assertEqual((idx['ThePirateBay']['grabs'], idx['ThePirateBay']['success_rate']), (2, 50.0))
        self.assertEqual(idx['1337x']['repaired'], 1)
        a = by['altHUB']
        self.assertEqual((a['grabs'], a['pending'], a['success_rate'], a['indexers']), (1, 1, None, []))
        self.assertIsNotNone(stats['tracking_since'])

        self.assertEqual([s['instance'] for s in sg.get_scraper_stats(kind='nzb')['scrapers']], ['altHUB'])

    def test_recent_grabs_filter(self):
        self.set_item(1, 'Adding')
        sg.record_grab({'id': 1, 'title': 'Show'}, self.result('A', indexer='1337x'))
        self.set_item(2, 'Adding')
        sg.record_grab({'id': 2}, self.result('B'))
        got = sg.get_recent_grabs(instance='Torrentio', indexer='1337x')
        self.assertEqual([g['release_title'] for g in got], ['A'])


_ITEM = '<item><title>{title}</title><guid>{guid}</guid><enclosure url="http://x/{guid}.nzb" length="1073741824"/>{extra}</item>'
_RSS = ('<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        '<channel>{items}</channel></rss>')


def _rss(*items):
    return _RSS.format(items=''.join(_ITEM.format(**i) for i in items))


class TestNewznabIndexerNames(unittest.TestCase):
    def setUp(self):
        from scraper import newznab
        self.nz = newznab

    def parse(self, extra):
        res = self.nz._parse_newznab_xml(_rss({'title': 'Show.S01E01.1080p.WEB-GRP', 'guid': 'g1', 'extra': extra}), 'Agg')
        self.assertEqual(len(res), 1)
        self.assertEqual((res[0]['scraper_type'], res[0]['scraper_instance']), ('Newznab', 'Agg'))
        return res[0]['indexer']

    def test_plain_indexer_has_no_sub_indexer(self):
        self.assertEqual(self.parse(''), '')

    def test_nzbhydra2(self):
        self.assertEqual(self.parse('<newznab:attr name="hydraIndexerName" value="altHUB"/>'), 'altHUB')

    def test_prowlarr(self):
        self.assertEqual(self.parse('<prowlarrindexer id="3" type="private">NZBPlanet</prowlarrindexer>'), 'NZBPlanet')

    def test_jackett(self):
        self.assertEqual(self.parse('<jackettindexer id="nzbgeek">NZBgeek</jackettindexer>'), 'NZBgeek')

    def test_aggregate_pack_takes_majority_source(self):
        # Instance A has E01+E02, instance B only E03 (same release group).
        feeds = {
            ('A', 1): 'Show.S01E01.1080p.WEB-GRP', ('A', 2): 'Show.S01E02.1080p.WEB-GRP',
            ('B', 3): 'Show.S01E03.1080p.WEB-GRP',
        }

        def fake_get(url, params=None, timeout=None):
            inst = 'A' if url.startswith('http://a') else 'B'
            q = (params or {}).get('q', '')
            ep = int(q[-2:]) if q[-3:-2] == 'E' else (params or {}).get('ep')
            title = feeds.get((inst, ep))
            body = _rss({'title': title, 'guid': f'{inst}{ep}',
                         'extra': f'<newznab:attr name="hydraIndexerName" value="idx{inst}"/>'}) if title else _rss()
            return mock.Mock(status_code=200, text=body)

        self.nz._NZB_CACHE.clear()
        with mock.patch.object(self.nz.api, 'get', side_effect=fake_get), \
             mock.patch.object(self.nz, '_get_retention_days', return_value=0):
            packs = self.nz.scrape_newznab_season_aggregate(
                [('A', {'url': 'http://a', 'api_key': 'k'}), ('B', {'url': 'http://b', 'api_key': 'k'})],
                imdb_id=None, title='Show', year=2024, season=1, episode_numbers=[1, 2, 3],
            )
        self.assertTrue(packs)
        p = packs[0]
        self.assertEqual((p['scraper_instance'], p['indexer'], p['scraper_type']), ('A', 'idxA', 'Newznab'))
        self.assertEqual(p['episode_sources'][3], {'scraper_instance': 'B', 'indexer': 'idxB'})


class TestScraperManagerStamping(unittest.TestCase):
    def test_results_carry_scraper_instance_and_type(self):
        from scraper.scraper_manager import ScraperManager
        cfg = {'Scrapers': {'My Torrentio': {'type': 'Torrentio', 'enabled': True}}}
        mgr = ScraperManager(cfg)
        mgr.scrapers['Torrentio'] = lambda **kw: [{'title': 'X', 'source': 'My Torrentio - TPB'}]
        def fake_setting(section, key=None, default=None):
            return cfg['Scrapers'] if (section, key) == ('Scrapers', None) else default

        with mock.patch('scraper.scraper_manager.get_setting', side_effect=fake_setting):
            results = mgr.scrape_all('tt1', 'X', 2020, 'movie')
        self.assertTrue(results)
        self.assertEqual((results[0]['scraper_type'], results[0]['scraper_instance']), ('Torrentio', 'My Torrentio'))


if __name__ == '__main__':
    unittest.main()
