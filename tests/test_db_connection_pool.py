"""Per-thread connection pooling in database.core.get_db_connection."""
import importlib.util
import os
import sqlite3
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_real_core():
    # Other test modules stub database.core in sys.modules; load the real file.
    spec = importlib.util.spec_from_file_location(
        "_real_database_core", os.path.join(ROOT, "database", "core.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConnectionPoolTests(unittest.TestCase):
    def setUp(self):
        self.core = _load_real_core()
        self.tmp = tempfile.mkdtemp(prefix="dbpool_")
        self.db = os.path.join(self.tmp, "media_items.db")
        conn = self.core.get_db_connection(self.db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        conn.close()

    def _raw(self, handle):
        return object.__getattribute__(handle, "_conn")

    def test_close_returns_connection_for_reuse(self):
        a = self.core.get_db_connection(self.db)
        raw = self._raw(a)
        a.close()
        b = self.core.get_db_connection(self.db)
        self.assertIs(self._raw(b), raw)
        b.close()

    def test_nested_checkouts_get_separate_connections(self):
        outer = self.core.get_db_connection(self.db)
        outer.execute("BEGIN")
        outer.execute("SELECT COUNT(*) FROM t").fetchone()
        inner = self.core.get_db_connection(self.db)
        self.assertIsNot(self._raw(inner), self._raw(outer))
        inner.execute("INSERT INTO t (v) VALUES ('inner')")
        inner.commit()
        inner.close()
        # The inner commit/close must not have ended the outer transaction.
        self.assertTrue(outer.in_transaction)
        outer.rollback()
        outer.close()
        check = self.core.get_db_connection(self.db)
        rows = [r["v"] for r in check.execute("SELECT v FROM t")]
        check.close()
        self.assertEqual(rows, ["inner"])

    def test_close_discards_uncommitted_work_like_a_real_close(self):
        conn = self.core.get_db_connection(self.db)
        conn.execute("INSERT INTO t (v) VALUES ('lost')")
        conn.close()
        conn = self.core.get_db_connection(self.db)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        conn.execute("BEGIN")  # would fail if a transaction leaked through
        conn.rollback()
        conn.close()

    def test_per_caller_state_is_reset(self):
        conn = self.core.get_db_connection(self.db)
        conn.row_factory = None
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()
        conn = self.core.get_db_connection(self.db)
        self.assertIs(conn.row_factory, sqlite3.Row)
        conn.execute("INSERT INTO t (v) VALUES ('writable')")
        conn.commit()
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        conn.close()

    def test_closed_handle_raises(self):
        conn = self.core.get_db_connection(self.db)
        conn.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        conn.close()  # idempotent

    def test_context_manager_commits_and_yields_wrapper(self):
        with self.core.get_db_connection(self.db) as conn:
            conn.execute("INSERT INTO t (v) VALUES ('ctx')")
        self.assertIsInstance(conn, self.core.PooledConnection)
        conn.close()
        check = self.core.get_db_connection(self.db)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)
        check.close()

    def test_replaced_database_file_is_not_served_stale(self):
        conn = self.core.get_db_connection(self.db)
        conn.close()
        os.remove(self.db)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        fresh = sqlite3.connect(self.db)
        fresh.execute("CREATE TABLE other (x)")
        fresh.commit()
        fresh.close()
        conn = self.core.get_db_connection(self.db)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual(tables, ["other"])

    def test_connections_are_not_shared_across_threads(self):
        main = self.core.get_db_connection(self.db)
        main_raw = self._raw(main)
        main.close()
        seen = {}

        def worker():
            c = self.core.get_db_connection(self.db)
            seen["raw"] = self._raw(c)
            c.execute("SELECT 1").fetchone()
            c.close()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertIsNot(seen["raw"], main_raw)

    def test_garbage_collected_handle_returns_to_pool(self):
        conn = self.core.get_db_connection(self.db)
        raw = self._raw(conn)
        del conn
        again = self.core.get_db_connection(self.db)
        self.assertIs(self._raw(again), raw)
        again.close()


if __name__ == "__main__":
    unittest.main()
