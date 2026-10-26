import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from utilities import ai_context


class AiContextWatchHistoryTest(unittest.TestCase):
    def test_reads_plex_watch_history_with_its_real_columns(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'USER_DB_CONTENT': tmp}):
            conn = sqlite3.connect(os.path.join(tmp, 'watch_history.db'))
            # Same columns as utilities/plex_watch_history_functions.py creates (no rating/genres)
            conn.execute(
                "CREATE TABLE watch_history (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                "type TEXT NOT NULL, watched_at TIMESTAMP, media_id TEXT, imdb_id TEXT, tmdb_id TEXT, "
                "tvdb_id TEXT, season INTEGER, episode INTEGER, show_title TEXT, duration INTEGER, "
                "watch_progress INTEGER, source TEXT)"
            )
            conn.executemany(
                "INSERT INTO watch_history (title, type, watched_at, show_title) VALUES (?, ?, ?, ?)",
                [('Pilot', 'episode', '2026-10-20 20:00:00', 'Severance'),
                 ('Good News About Hell', 'episode', '2026-10-21 20:00:00', 'Severance'),
                 ('Heat', 'movie', '2026-10-19 20:00:00', None)],
            )
            conn.commit()
            conn.close()

            text = ai_context._get_watch_history()

        self.assertEqual(text.splitlines(), [
            '  Source: Plex watch history',
            '  2026-10-21 show: Severance (2 episodes)',
            '  2026-10-19 movie: Heat',
        ])


if __name__ == '__main__':
    unittest.main()
