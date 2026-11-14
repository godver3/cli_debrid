"""Database page Source column: where an item's file came from."""
import unittest

import database  # noqa: F401  (app import order)
from routes.database_routes import item_source_label


class SourceLabelTests(unittest.TestCase):
    def test_usenet(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': 'nzb:abc'}), 'Usenet')

    def test_debrid_with_provider(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': 'X1', 'debrid_provider': 'Torbox'}), 'Debrid (Torbox)')

    def test_debrid_without_provider(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': 'X1'}), 'Debrid')

    def test_no_file(self):
        self.assertEqual(item_source_label({'filled_by_torrent_id': None}), '')


if __name__ == '__main__':
    unittest.main()
