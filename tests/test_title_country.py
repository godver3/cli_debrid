#!/usr/bin/env python3
"""Tests for title country detection and source-title preservation."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from utilities.title_country import (
    extract_title_country_codes,
    prefer_source_title_on_country_conflict,
    primary_title_country_code,
)


class TestExtractTitleCountryCodes(unittest.TestCase):
    def test_parenthetical_pt(self):
        self.assertEqual(extract_title_country_codes('The Floor (PT)'), ['PT'])

    def test_parenthetical_us(self):
        self.assertEqual(primary_title_country_code('The Floor (US)'), 'US')

    def test_standalone_au_release(self):
        self.assertEqual(
            extract_title_country_codes('The Floor AU S01E01 1080p HDTV H264-DARKFLiX'),
            ['AU'],
        )

    def test_gb_aliases_to_uk(self):
        self.assertEqual(extract_title_country_codes('The Office (GB)'), ['UK'])

    def test_no_false_positive_from_common_words(self):
        self.assertEqual(extract_title_country_codes('Breaking Bad'), [])
        # "IN" / "NO" must not enable region filtering as bare tokens
        self.assertEqual(extract_title_country_codes('Once Upon A Time In Hollywood'), [])
        self.assertEqual(extract_title_country_codes('No Time To Die'), [])
        # Parenthetical forms of those codes are still intentional markers
        self.assertEqual(extract_title_country_codes('Some Show (IN)'), ['IN'])
        self.assertEqual(extract_title_country_codes('Some Show (NO)'), ['NO'])


class TestPreferSourceTitleOnCountryConflict(unittest.TestCase):
    def test_us_source_beats_pt_battery(self):
        self.assertEqual(
            prefer_source_title_on_country_conflict(
                'The Floor (US)',
                'The Floor (PT)',
            ),
            'The Floor (US)',
        )

    def test_matching_regions_keep_battery(self):
        self.assertEqual(
            prefer_source_title_on_country_conflict(
                'The Floor (US)',
                'The Floor (US)',
            ),
            'The Floor (US)',
        )

    def test_no_source_keeps_battery(self):
        self.assertEqual(
            prefer_source_title_on_country_conflict(None, 'The Floor (PT)'),
            'The Floor (PT)',
        )

    def test_battery_region_conflicts_with_country_field(self):
        self.assertEqual(
            prefer_source_title_on_country_conflict(
                'The Floor',
                'The Floor (PT)',
                metadata_country='us',
            ),
            'The Floor',
        )

    def test_plain_titles_unchanged(self):
        self.assertEqual(
            prefer_source_title_on_country_conflict('Breaking Bad', 'Breaking Bad'),
            'Breaking Bad',
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
