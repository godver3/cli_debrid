#!/usr/bin/env python3
"""
Regression test for the manual "Assign" flow's movie-file auto-selection
(routes/magnet_routes.py::prepare_manual_assignment(), Phase 1 movie logic).

Reported: a magnet containing all 8 Harry Potter movies always got the
requested movie (e.g. "Chamber of Secrets") assigned to "Half-Blood Prince"
- the single largest file in the pack - no matter which film was actually
being assigned. The Phase 1 movie branch picked the largest *unused* file
with no title/year check at all, the same class of bug fixed in #501 for
the automatic-add path (queues/media_matcher.py), but in a completely
separate code path that #501's fix does not touch.

This test exercises the exact filter-then-pick-largest algorithm the route
now runs (title/year match via the real MediaMatcher.match_movie(), fall
back to largest-unused only when nothing matches), against parsed dicts
shaped like scraper.functions.ptt_parser.parse_with_ptt()'s real output
(flat 'title'/'year' keys, not media_matcher's nested 'parsed_info').

PTT (parsett) isn't installed in this environment, so it's stubbed before
queues/media_matcher.py is loaded from source - the test still runs against
the real MediaMatcher.match_movie() body.
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
    spec = importlib.util.spec_from_file_location('media_matcher_assign_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mm = _load_media_matcher()


def _pack_file(path, bytes_, title, year):
    """Shaped like magnet_routes.py's parsed_video_files entries: {'original': {...}, 'parsed': {...}, 'used': bool}."""
    return {
        'original': {'id': None, 'path': path, 'filename': os.path.basename(path), 'bytes': bytes_},
        'parsed': {'title': title, 'year': year},
        'used': False,
    }


def _assign_movie(matcher, item, parsed_video_files):
    """The Phase 1 movie-assignment algorithm from prepare_manual_assignment()."""
    unused_parsed_files = [f for f in parsed_video_files if not f['used']]
    if not unused_parsed_files:
        return None
    title_matched_files = [
        f for f in unused_parsed_files
        if matcher.match_movie(f['parsed'], item, f['original'].get('filename', ''))
    ]
    match_candidates = title_matched_files or unused_parsed_files
    largest = max(match_candidates, key=lambda f: f['original'].get('bytes', 0))
    largest['used'] = True
    return largest['original']['filename']


class TestMagnetAssignMovieMatch(unittest.TestCase):
    def setUp(self):
        self.matcher = mm.MediaMatcher()

    def test_harry_potter_pack_assigns_requested_film_not_largest(self):
        # Half-Blood Prince is the biggest file in the pack; assigning any
        # other film must not silently land on it.
        pack = [
            _pack_file('/pack/Harry.Potter.and.the.Chamber.of.Secrets.2002.mkv', 5_000_000_000, 'Harry Potter and the Chamber of Secrets', 2002),
            _pack_file('/pack/Harry.Potter.and.the.Half-Blood.Prince.2009.mkv', 9_000_000_000, 'Harry Potter and the Half-Blood Prince', 2009),
            _pack_file('/pack/Harry.Potter.and.the.Prisoner.of.Azkaban.2004.mkv', 6_000_000_000, 'Harry Potter and the Prisoner of Azkaban', 2004),
        ]

        item = {'type': 'movie', 'title': 'Harry Potter and the Chamber of Secrets', 'year': 2002}
        result = _assign_movie(self.matcher, item, pack)
        self.assertEqual(result, 'Harry.Potter.and.the.Chamber.of.Secrets.2002.mkv')

        item2 = {'type': 'movie', 'title': 'Harry Potter and the Prisoner of Azkaban', 'year': 2004}
        result2 = _assign_movie(self.matcher, item2, pack)
        self.assertEqual(result2, 'Harry.Potter.and.the.Prisoner.of.Azkaban.2004.mkv')

    def test_falls_back_to_largest_unused_when_nothing_title_matches(self):
        item = {'type': 'movie', 'title': 'Some Requested Movie', 'year': 2021}
        pack = [
            _pack_file('/pack/Totally.Different.Release.Name.mkv', 5_000_000_000, 'Totally Different Release Name', 2021),
        ]

        result = _assign_movie(self.matcher, item, pack)
        self.assertEqual(result, 'Totally.Different.Release.Name.mkv')


if __name__ == '__main__':
    unittest.main()
