import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from database import movie_release_overrides
from queues.wanted_queue import WantedQueue


class TestMovieReleaseDateOverride(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = self.connect()
        conn.execute(
            """
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY,
                imdb_id TEXT,
                tmdb_id TEXT,
                title TEXT,
                release_date DATE,
                state TEXT,
                type TEXT,
                version TEXT,
                physical_release_date DATE,
                last_updated TIMESTAMP
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO media_items
                (id, imdb_id, tmdb_id, title, release_date, state, type, version, physical_release_date)
            VALUES (?, 'tt34966562', '1400357', 'The Christophers', 'Unknown', ?, 'movie', ?, ?)
            """,
            [
                (1, 'Unreleased', 'Default', None),
                (2, 'Unreleased', 'Physical', None),
            ],
        )
        conn.commit()
        conn.close()
        self.connection_patch = patch.object(
            movie_release_overrides,
            'get_db_connection',
            side_effect=self.connect,
        )
        self.connection_patch.start()

    def tearDown(self):
        self.connection_patch.stop()
        os.unlink(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def settings(section, key, default=None):
        if section == 'Scraping' and key == 'versions':
            return {
                'Default': {'require_physical_release': False},
                'Physical': {'require_physical_release': True},
            }
        if section == 'Debug' and key == 'use_alternate_scrape_time_strategy':
            return False
        if section == 'Queue' and key == 'movie_airtime_offset':
            return '0'
        return default

    def row(self, item_id):
        conn = self.connect()
        try:
            return dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone())
        finally:
            conn.close()

    def test_past_override_reaches_real_wanted_scrape_decision(self):
        with patch('utilities.settings.get_setting', side_effect=self.settings):
            result = movie_release_overrides.set_movie_release_override(
                'tt34966562',
                '2026-05-12',
                updated_by='test-admin',
                as_of=date(2026, 8, 21),
            )

        self.assertEqual(2, result['affected_count'])
        eligible_item = self.row(1)
        self.assertEqual('2026-05-12', eligible_item['release_date'])
        self.assertEqual('Wanted', eligible_item['state'])

        queue_manager = MagicMock()
        queue_manager.generate_identifier.return_value = 'The Christophers (2026)'
        with patch('queues.wanted_queue.check_existing_media_item', return_value=False), \
             patch('queues.wanted_queue.get_setting', side_effect=self.settings):
            decision = WantedQueue()._evaluate_item_readiness_and_act(
                eligible_item,
                datetime(2026, 8, 21, 12, 0, 0),
                queue_manager,
            )

        self.assertEqual('scrape', decision['status'])
        queue_manager.move_to_unreleased.assert_not_called()

    def test_future_override_stays_unreleased(self):
        with patch('utilities.settings.get_setting', side_effect=self.settings):
            movie_release_overrides.set_movie_release_override(
                '1400357',
                '2026-09-26',
                as_of=date(2026, 8, 21),
            )

        self.assertEqual('Unreleased', self.row(1)['state'])
        self.assertEqual('2026-09-26', self.row(1)['release_date'])

    def test_physical_required_version_is_not_unblocked(self):
        with patch('utilities.settings.get_setting', side_effect=self.settings):
            movie_release_overrides.set_movie_release_override(
                'tt34966562',
                '2026-05-12',
                as_of=date(2026, 8, 21),
            )

        self.assertEqual('Wanted', self.row(1)['state'])
        self.assertEqual('Unreleased', self.row(2)['state'])

    def test_clear_restores_provider_date_and_removes_override(self):
        with patch('utilities.settings.get_setting', side_effect=self.settings):
            movie_release_overrides.set_movie_release_override(
                'tt34966562',
                '2026-05-12',
                as_of=date(2026, 8, 21),
            )
            result = movie_release_overrides.clear_movie_release_override(
                'tt34966562',
                '2026-09-26',
                as_of=date(2026, 8, 21),
            )

        self.assertEqual('2026-09-26', result['release_date'])
        self.assertEqual('Unreleased', self.row(1)['state'])
        self.assertIsNone(
            movie_release_overrides.get_movie_release_override(imdb_id='tt34966562')
        )

    def test_new_movie_version_inherits_existing_override(self):
        with patch('utilities.settings.get_setting', side_effect=self.settings):
            movie_release_overrides.set_movie_release_override(
                'tt34966562',
                '2026-05-12',
                as_of=date(2026, 8, 21),
            )
            new_item = {
                'imdb_id': 'tt34966562',
                'tmdb_id': '1400357',
                'type': 'movie',
                'version': 'Default',
                'state': 'Unreleased',
                'release_date': 'Unknown',
            }
            conn = self.connect()
            try:
                movie_release_overrides.apply_movie_release_override_to_item(
                    new_item,
                    conn=conn,
                    as_of=date(2026, 8, 21),
                )
            finally:
                conn.close()

        self.assertEqual('2026-05-12', new_item['release_date'])
        self.assertEqual('Wanted', new_item['state'])


if __name__ == '__main__':
    unittest.main()
