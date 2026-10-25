"""Changing a show's series type on the show page moves its collected symlinks.

With separate anime folders on, a show's folder follows each row's 'anime'
genre. Re-tagging the rows without moving the files left the collected
episodes in one root and every later episode in the other.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT')


def _fresh_app_modules():
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    return patcher


class _Conn:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def execute(self, sql, params):
        self.queries.append((sql, params))
        return iter(self.rows)

    def close(self):
        pass


class MoveSymlinksForSeriesTypeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        import routes.show_options_routes as so
        import database.core
        import utilities.settings
        import utilities.local_library_scan as lls
        self.so = so
        self.settings = {('File Management', 'file_collection_management'): 'Symlinked/Local',
                         ('Debug', 'enable_separate_anime_folders'): True}
        self.conn = _Conn([{'id': 5}, {'id': 6}])
        self.resync = mock.Mock(return_value={'symlinks_updated_count': 2, 'symlinks_created_count': 0,
                                              'error_count': 0})
        for patch in (
            mock.patch.object(utilities.settings, 'get_setting',
                              side_effect=lambda s, k, d=None: self.settings.get((s, k), d)),
            mock.patch.object(database.core, 'get_db_connection', return_value=self.conn),
            mock.patch.object(lls, 'resync_symlinks_with_new_settings', self.resync),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_collected_episodes_are_resynced(self):
        note = self.so._move_symlinks_for_series_type('tt1')
        self.resync.assert_called_once_with(item_ids=[5, 6])
        self.assertEqual(note, 'Symlinks: 2 moved, 0 created, 0 errors')
        self.assertEqual(self.conn.queries[0][1], ('tt1',))

    def test_nothing_without_separate_anime_folders(self):
        self.settings[('Debug', 'enable_separate_anime_folders')] = False
        self.assertIsNone(self.so._move_symlinks_for_series_type('tt1'))
        self.resync.assert_not_called()

    def test_nothing_outside_symlink_mode(self):
        self.settings[('File Management', 'file_collection_management')] = 'Plex'
        self.assertIsNone(self.so._move_symlinks_for_series_type('tt1'))
        self.resync.assert_not_called()

    def test_nothing_collected(self):
        self.conn.rows = []
        self.assertIsNone(self.so._move_symlinks_for_series_type('tt1'))
        self.resync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
