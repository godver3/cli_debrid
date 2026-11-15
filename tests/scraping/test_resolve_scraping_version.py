"""A '**' row rescraped must keep a real version's filters (godver3/cli_debrid#576)."""
import unittest
from unittest import mock

import utilities.reverse_parser as rp

VERSIONS = {'1080p': {'filter_out': 'Jaskier'}, '2160p': {}}


def fake_settings(default_version=''):
    def get_setting(section, key, default=None):
        if (section, key) == ('Scraping', 'versions'):
            return VERSIONS
        if (section, key) == ('Reverse Parser', 'default_version'):
            return default_version
        return default
    return get_setting


class ResolveScrapingVersionTests(unittest.TestCase):
    def test_double_star_row_gets_first_version(self):
        with mock.patch.object(rp, 'get_setting', fake_settings('')):
            self.assertEqual(rp.resolve_scraping_version('**'), '1080p')
            self.assertEqual(rp.resolve_scraping_version(''), '1080p')
            self.assertEqual(rp.resolve_scraping_version(None), '1080p')

    def test_configured_reverse_parser_default_wins(self):
        with mock.patch.object(rp, 'get_setting', fake_settings('2160p')):
            self.assertEqual(rp.resolve_scraping_version('**'), '2160p')

    def test_known_version_is_only_stripped(self):
        with mock.patch.object(rp, 'get_setting', fake_settings('')):
            self.assertEqual(rp.resolve_scraping_version('2160p*'), '2160p')
            self.assertEqual(rp.resolve_scraping_version('1080p**'), '1080p')

    def test_deleted_version_falls_back(self):
        with mock.patch.object(rp, 'get_setting', fake_settings('')):
            self.assertEqual(rp.resolve_scraping_version('4K HDR*'), '1080p')

    def test_no_versions_configured_keeps_name(self):
        with mock.patch.object(rp, 'get_setting', lambda s, k, d=None: {} if k == 'versions' else d):
            self.assertEqual(rp.resolve_scraping_version('**'), '')


if __name__ == '__main__':
    unittest.main()
