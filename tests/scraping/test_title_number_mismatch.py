#!/usr/bin/env python3
"""A release title missing the show's number is a different show.

Discord report 2026-11-16: every new 9-1-1: Nashville episode (TMDB 284838) grabbed
the whole season 2 of Nashville (2012). The alias "911 Nashville" scored the parsed
title "Nashville" 0.91 (token_set_ratio is 1.0 when every release word is in the
alias), and the squashed acronym check scored "nashville" / "911nashville" 0.86, so
Nashville.S02 packs passed the 0.8 threshold.
"""

import importlib
import logging
import unittest
from unittest import mock

from scraper.functions.file_processing import _process_single_title
from scraper.functions.filter_results import _drops_title_number

ALIASES = ['911 Nashville', '9-1-1 Nashville', '911 - Nashville', '9-1-1 : Nashville']


def _filter(title, names, multi, aliases=()):
    fr = importlib.import_module('scraper.functions.filter_results')
    results = [{
        'title': n, 'original_title': n, 'size': 30.0,
        'parsed_info': _process_single_title((n, 30.0)),
        'protocol': 'torrent', 'magnet': 'magnet:?xt=urn:btih:' + 'a' * 40,
        'seeders': 10, 'source': 'Torrentio',
    } for n in names]
    version_settings = {'similarity_threshold': 0.8, 'max_resolution': '2160p',
                        'resolution_wanted': '<=', 'min_size_gb': 0.01}
    logging.disable(logging.CRITICAL)
    try:
        with mock.patch.object(fr, 'get_setting',
                               lambda *a, **k: k.get('default', a[2] if len(a) > 2 else None)):
            kept, _ = fr.filter_results(
                results, '284838', title, 2025, 'episode', 2, None if multi else 5, multi,
                version_settings, 45, 18, {1: 18, 2: 18}, ['Drama'],
                matching_aliases=list(aliases), imdb_id='tt33550053')
    finally:
        logging.disable(logging.NOTSET)
    return [r['title'] for r in kept]


class TestDropsTitleNumber(unittest.TestCase):
    def test_helper(self):
        self.assertTrue(_drops_title_number('nashville', '9-.1.-1.nashville'))
        self.assertTrue(_drops_title_number('911.nashville', 'nashville'))
        self.assertFalse(_drops_title_number('911.nashville', '9-.1.-1.nashville'))
        self.assertFalse(_drops_title_number('doctor.who', 'doctor.who.2005'))  # years don't count
        self.assertFalse(_drops_title_number('nashville', 'nashville'))
        self.assertFalse(_drops_title_number(None, '911'))


class TestNumberedShowFilter(unittest.TestCase):
    def test_season_pack_of_unnumbered_show_rejected(self):
        kept = _filter('9-1-1: Nashville', [
            'Nashville.S02.1080p.WEB-DL.DD5.1.H.264-NTb',
            '9-1-1.Nashville.S02.1080p.WEB.h264',
            '911.Nashville.S02.1080p.WEB.h264',
        ], multi=True, aliases=ALIASES)
        self.assertEqual(kept, ['9-1-1.Nashville.S02.1080p.WEB.h264', '911.Nashville.S02.1080p.WEB.h264'])

    def test_episode_of_unnumbered_show_rejected(self):
        kept = _filter('9-1-1: Nashville', [
            'Nashville.S02E05.720p.HDTV',
            '9-1-1.Nashville.S02E05.1080p.WEB.h264-ETHEL',
            '911 Nashville S02E05 1080p',
        ], multi=False, aliases=ALIASES)
        self.assertEqual(kept, ['9-1-1.Nashville.S02E05.1080p.WEB.h264-ETHEL', '911 Nashville S02E05 1080p'])

    def test_unnumbered_show_rejects_numbered_one(self):
        kept = _filter('Nashville', [
            'Nashville.S02E05.720p.HDTV',
            '911.Nashville.S02E05.1080p.WEB.h264',
        ], multi=False)
        self.assertEqual(kept, ['Nashville.S02E05.720p.HDTV'])


if __name__ == '__main__':
    unittest.main()
