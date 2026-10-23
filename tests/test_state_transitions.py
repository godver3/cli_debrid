#!/usr/bin/env python3
"""Item state history (database/state_transitions.py): the trigger records every
state change, update_media_item_state tags who made it, and automated moves
outside the designed pipeline are flagged once."""

import logging
import os
import sys
import tempfile
import unittest

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())


def _is_db_module(name):
    return name == 'database' or name.startswith('database.')


def _import_real_database():
    """Import the real database package even if other test modules left stubs
    in sys.modules (same approach as test_scraper_grabs)."""
    stubs = {k: sys.modules.pop(k) for k in list(sys.modules) if _is_db_module(k)}
    try:
        import database  # noqa: F401  (app import order)
        import database.state_transitions  # noqa: F401
        import database.database_writing  # noqa: F401
        import database.core  # noqa: F401
        return {k: m for k, m in sys.modules.items() if _is_db_module(k)}
    finally:
        for k in [k for k in sys.modules if _is_db_module(k)]:
            del sys.modules[k]
        sys.modules.update(stubs)


_REAL_DB_MODULES = _import_real_database()
st = _REAL_DB_MODULES['database.state_transitions']
dw = _REAL_DB_MODULES['database.database_writing']
get_db_connection = _REAL_DB_MODULES['database.core'].get_db_connection


class _DbCase(unittest.TestCase):
    def setUp(self):
        saved = {k: sys.modules.get(k) for k in _REAL_DB_MODULES}
        sys.modules.update(_REAL_DB_MODULES)

        def restore():
            for k, m in saved.items():
                if m is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = m
        self.addCleanup(restore)

        self._prev_db = os.environ['USER_DB_CONTENT']
        os.environ['USER_DB_CONTENT'] = tempfile.mkdtemp()
        self.addCleanup(os.environ.__setitem__, 'USER_DB_CONTENT', self._prev_db)
        conn = get_db_connection()
        conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, state TEXT, last_updated TIMESTAMP, "
                     "scrape_results TEXT, version TEXT)")
        conn.commit()
        conn.close()
        st.create_state_transitions_table()
        st._warned.clear()

    def sql(self, query, *params):
        conn = get_db_connection()
        conn.execute(query, params)
        conn.commit()
        conn.close()

    def history(self, item_id):
        return list(reversed(st.get_item_history(item_id)))


class TestTriggerRecordsEveryPath(_DbCase):
    def test_insert_records_initial_state(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        h = self.history(1)
        self.assertEqual([(r['from_state'], r['to_state']) for r in h], [(None, 'Wanted')])

    def test_raw_sql_update_recorded_without_source(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        self.sql("UPDATE media_items SET state = 'Scraping' WHERE id = 1")
        last = self.history(1)[-1]
        self.assertEqual((last['from_state'], last['to_state'], last['source']), ('Wanted', 'Scraping', None))

    def test_unchanged_state_not_recorded(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        self.sql("UPDATE media_items SET state = 'Wanted', version = '4k' WHERE id = 1")
        self.sql("UPDATE media_items SET version = '1080p' WHERE id = 1")
        self.assertEqual(len(self.history(1)), 1)

    def test_history_newest_first(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        self.sql("UPDATE media_items SET state = 'Scraping' WHERE id = 1")
        self.sql("UPDATE media_items SET state = 'Adding' WHERE id = 1")
        self.assertEqual([r['to_state'] for r in st.get_item_history(1)], ['Adding', 'Scraping', 'Wanted'])


class TestUpdateMediaItemStateTagsSource(_DbCase):
    def test_source_is_calling_function(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        dw.update_media_item_state(1, 'Scraping')
        last = self.history(1)[-1]
        self.assertEqual((last['from_state'], last['to_state']), ('Wanted', 'Scraping'))
        self.assertEqual(last['source'], f"{__name__}.test_source_is_calling_function")

    def test_same_state_update_adds_no_row(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        dw.update_media_item_state(1, 'Wanted')
        self.assertEqual(len(self.history(1)), 1)


class TestUnexpectedTransitions(_DbCase):
    def test_expected_moves(self):
        self.assertTrue(st.is_expected('Wanted', 'Scraping'))
        self.assertTrue(st.is_expected('Checking', 'Collected'))
        self.assertTrue(st.is_expected('Blacklisted', 'Wanted'))  # Wanted reachable from anywhere
        self.assertTrue(st.is_expected('Sleeping', 'Collected'))  # library scans adopt files
        self.assertTrue(st.is_expected(None, 'Collected'))        # new item
        self.assertTrue(st.is_expected('SomeNewState', 'Adding')) # unknown state: no opinion

    def test_unexpected_moves(self):
        self.assertFalse(st.is_expected('Wanted', 'Checking'))
        self.assertFalse(st.is_expected('Collected', 'Scraping'))

    def test_unexpected_automated_move_warns_once(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted'), (2, 'Wanted')")
        with self.assertLogs(st.logger, level=logging.WARNING) as logs:
            dw.update_media_item_state(1, 'Checking')
            dw.update_media_item_state(2, 'Checking')
        self.assertEqual(len(logs.records), 1)
        self.assertIn('Wanted -> Checking', logs.output[0])
        self.assertFalse(self.history(1)[-1]['expected'])

    def test_manual_move_not_flagged(self):
        self.sql("INSERT INTO media_items (id, state) VALUES (1, 'Wanted')")
        conn = get_db_connection()
        conn.execute("UPDATE media_items SET state = 'Checking' WHERE id = 1")
        with self.assertNoLogs(st.logger, level=logging.WARNING):
            st.tag_transition(conn, 1, 'Wanted', 'Checking', 'routes.debug_routes.move_item_to_queue')
        conn.commit()
        conn.close()
        last = self.history(1)[-1]
        self.assertTrue(last['manual'])
        self.assertTrue(last['expected'])


if __name__ == '__main__':
    unittest.main()
