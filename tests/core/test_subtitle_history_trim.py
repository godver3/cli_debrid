"""Trimming subtitle_history never drops a sidecar row with a path: it is the
only record that Clyde wrote that file, and deletes/resync/orphan cleanup only
touch recorded files."""
import sqlite3
from unittest import mock

import database  # noqa: F401 - app import order

from database import subtitle_history


class _Conn:
    """sqlite3 connection whose close() is a no-op so the test can reuse it."""
    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass


def test_trim_keeps_sidecar_paths(monkeypatch):
    conn = _Conn(sqlite3.connect(':memory:'))
    monkeypatch.setattr(subtitle_history, '_ready', False)
    monkeypatch.setattr(subtitle_history, '_KEEP_ROWS', 3)
    with mock.patch.object(subtitle_history, 'get_db_connection', return_value=conn):
        subtitle_history.record(1, 'A', 'en', 'p', 1, 1, [], 'r', 'srt', 'sidecar', '/lib/A.en.srt', 'auto')
        subtitle_history.record(2, 'B', 'en', 'p', 1, 1, [], 'r', 'srt', 'plex', None, 'auto')
        for i in range(10):
            subtitle_history.record(10 + i, 'X', 'en', 'p', 1, 1, [], 'r', 'srt', 'plex', None, 'auto')
        assert subtitle_history.is_clyde_path('/lib/A.en.srt') is True
        rows = conn.execute("SELECT COUNT(*) FROM subtitle_history").fetchone()[0]
    assert rows == 4  # 3 newest + the kept sidecar row
