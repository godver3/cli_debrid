#!/usr/bin/env python3
"""A release name wrapped in brackets must not match a different show.

NinjaCentral returned "[ Below.Deck.Adventure.S01E05.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTb ] -"
for Below (tt35934755) S01E05. PTT returns an empty title for "[ ... ] -" names, so
filter_results compared the whole release name to "Below" with token_set_ratio, which
scores 1.0 when every word of the query appears anywhere in the name, and its extra-words
penalties only run when there is a parsed title. The same release without brackets was
rejected (similarity 0.36) but the wrapped copy was accepted and collected.

Two layers: PTT retries without the wrapper (both PTT wrappers), and filter_results uses
the title part of the name when PTT still finds no title.
"""

import importlib
import logging
import unittest
from unittest import mock

from scraper.functions.ptt_parser import (
    parse_with_ptt, title_before_markers, unwrap_release_title,
)
from scraper.functions.file_processing import _parse_with_ptt, _process_single_title

WRAPPED_WRONG = '[ Below.Deck.Adventure.S01E05.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTb ] -'
WRAPPED_RIGHT = '[ Below.S01E05.1080p.NF.WEB-DL.DD+5.1.Atmos.H.264-playWEB ] -'
PLAIN_RIGHT = 'Below.S01E05.The.Unfathering.1080p.NF.WEB-DL.DD+5.1.Atmos.H.264-playWEB'


class TestUnwrap(unittest.TestCase):
    def test_unwrap(self):
        self.assertEqual(unwrap_release_title(WRAPPED_WRONG),
                         'Below.Deck.Adventure.S01E05.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTb')
        self.assertEqual(unwrap_release_title('( Show.S01E01.720p )'), 'Show.S01E01.720p')
        for name in (PLAIN_RIGHT, '[sam] Vinland Saga [BD 1080p FLAC]', '[S08] Rick and Morty [CUK]'):
            self.assertIsNone(unwrap_release_title(name), name)

    def test_both_ptt_wrappers_read_the_wrapped_title(self):
        for parse in (parse_with_ptt, _parse_with_ptt):
            self.assertEqual(parse(WRAPPED_WRONG).get('title'), 'Below Deck Adventure')
            self.assertEqual(parse(WRAPPED_RIGHT).get('title'), 'Below')

    def test_names_ptt_already_titles_are_unchanged(self):
        for name, title in (('[sam] Vinland Saga [BD 1080p FLAC]', 'Vinland Saga'),
                            ('[Ex-torrenty.org]Suits.S09.PL.1080p.BluRay.DDP5.1.x264-Ralf', 'Suits'),
                            ('[S08] Rick and Morty [CUK]', 'Rick and Morty'),
                            (PLAIN_RIGHT, 'Below')):
            self.assertEqual(parse_with_ptt(name).get('title'), title, name)
            self.assertEqual(_parse_with_ptt(name).get('title'), title, name)

    def test_title_before_markers(self):
        self.assertEqual(title_before_markers(WRAPPED_WRONG), 'Below Deck Adventure')
        self.assertEqual(title_before_markers('[grp] Show Name S02E03 1080p'), 'Show Name')
        self.assertEqual(title_before_markers('Some.Movie.2019.1080p'), 'Some Movie')
        self.assertEqual(title_before_markers('abc123def456.mkv'), '')


class TestFilterResultsRejectsWrappedOtherShow(unittest.TestCase):
    def _filter(self, titles):
        fr = importlib.import_module('scraper.functions.filter_results')
        results = []
        for title in titles:
            results.append({
                'title': title, 'original_title': title, 'size': 3.4,
                'parsed_info': _process_single_title((title, 3.4)),
                'protocol': 'nzb', 'nzb_url': 'https://indexer/' + title, 'magnet': None,
                'seeders': 0, 'source': 'NinjaCentral_1',
            })
        version_settings = {'similarity_threshold': 0.95, 'max_resolution': '2160p',
                            'resolution_wanted': '<=', 'min_size_gb': 0.01}
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(fr, 'get_setting',
                                   lambda *a, **k: k.get('default', a[2] if len(a) > 2 else None)):
                kept, _ = fr.filter_results(
                    results, '285322', 'Below', 2026, 'episode', 1, 5, False, version_settings,
                    50, 6, {1: 6}, ['Drama'], imdb_id='tt35934755')
        finally:
            logging.disable(logging.NOTSET)
        return [r['title'] for r in kept]

    def test_wrapped_other_show_is_rejected(self):
        kept = self._filter([WRAPPED_WRONG, WRAPPED_RIGHT, PLAIN_RIGHT])
        self.assertNotIn(WRAPPED_WRONG, kept)
        self.assertIn(WRAPPED_RIGHT, kept)
        self.assertIn(PLAIN_RIGHT, kept)


if __name__ == '__main__':
    unittest.main()
