#!/usr/bin/env python3
"""
Regression test for GitHub issue #501: MediaMatcher.find_best_match_from_parsed()
picked the largest video file in a torrent for movies with no title/year check
at all, so a multi-movie "Collection" torrent (several unrelated films bundled
as separate files) silently matched whichever film happened to be biggest,
regardless of what was actually requested.

PTT (parsett) isn't installed in this environment, so it and the small set of
other heavy imports media_matcher.py pulls in are stubbed before the real
module is loaded from source - the test still runs against the actual
find_best_match_from_parsed()/match_movie() bodies.
"""

import unittest
import sys
import os
import types
import importlib.util


def _load_media_matcher():
    if 'PTT' not in sys.modules:
        ptt = types.ModuleType('PTT')
        ptt.parse_title = lambda filename: {}
        sys.modules['PTT'] = ptt

    if 'scraper' not in sys.modules:
        sys.modules['scraper'] = types.ModuleType('scraper')
    if 'scraper.functions' not in sys.modules:
        sys.modules['scraper.functions'] = types.ModuleType('scraper.functions')
    if 'scraper.functions.anime_utils' not in sys.modules:
        au = types.ModuleType('scraper.functions.anime_utils')
        au.detect_absolute_numbering = lambda *a, **k: None
        sys.modules['scraper.functions.anime_utils'] = au

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'queues', 'media_matcher.py')
    spec = importlib.util.spec_from_file_location('media_matcher_collection_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mm = _load_media_matcher()


def _file(path, bytes_, title, year):
    return {
        'path': path,
        'bytes': bytes_,
        'parsed_info': {'title': title, 'year': year, 'original_filename': os.path.basename(path)},
    }


class TestMovieCollectionTitleMatch(unittest.TestCase):
    def setUp(self):
        self.matcher = mm.MediaMatcher()

    def test_picks_title_matched_file_not_largest_in_collection(self):
        # Franchise box-set: "The Last Stand (2019)" is the biggest file, but
        # the item actually requested is "The Great Heist (2018)".
        item = {'type': 'movie', 'title': 'The Great Heist', 'year': 2018}
        parsed_files = [
            _file('/torrent/The.Great.Heist.2018.1080p.mkv', 4_000_000_000, 'The Great Heist', 2018),
            _file('/torrent/The.Last.Stand.2019.1080p.mkv', 9_000_000_000, 'The Last Stand', 2019),
            _file('/torrent/Midnight.Express.2020.1080p.mkv', 6_000_000_000, 'Midnight Express', 2020),
        ]

        result = self.matcher.find_best_match_from_parsed(parsed_files, item)

        self.assertIsNotNone(result)
        self.assertEqual(result[0], 'The.Great.Heist.2018.1080p.mkv')

    def test_falls_back_to_largest_when_nothing_title_matches(self):
        # Preserve prior behavior for a single-file torrent whose release name
        # doesn't fuzzy-match the item title cleanly - don't fail closed.
        item = {'type': 'movie', 'title': 'Some Requested Movie', 'year': 2021}
        parsed_files = [
            _file('/torrent/Totally.Different.Release.Name.mkv', 5_000_000_000, 'Totally Different Release Name', 2021),
        ]

        result = self.matcher.find_best_match_from_parsed(parsed_files, item)

        self.assertIsNotNone(result)
        self.assertEqual(result[0], 'Totally.Different.Release.Name.mkv')

    def test_single_movie_torrent_still_matches(self):
        item = {'type': 'movie', 'title': 'The Great Heist', 'year': 2018}
        parsed_files = [
            _file('/torrent/The.Great.Heist.2018.1080p.mkv', 4_000_000_000, 'The Great Heist', 2018),
        ]

        result = self.matcher.find_best_match_from_parsed(parsed_files, item)

        self.assertIsNotNone(result)
        self.assertEqual(result[0], 'The.Great.Heist.2018.1080p.mkv')


if __name__ == '__main__':
    unittest.main()
