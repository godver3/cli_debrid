#!/usr/bin/env python3
"""NZB repair replacement scrape must tolerate non-JSON genres.

Reported 2026-09-14 (poison_dagger47): json.loads(item['genres']) raised
"Expecting value: line 1 column 1" for rows whose genres were a plain string,
so every automated repair scrape for those items (4,353 times in one log, all
anime) failed before searching and the broken file was never replaced.
"""

import os
import tempfile
import unittest

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
from usenet.repair_engine import _parse_item_genres


class TestParseItemGenres(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(_parse_item_genres('["anime", "Animation"]'), ['anime', 'Animation'])
        self.assertEqual(_parse_item_genres('Animation, Anime'), ['Animation', 'Anime'])
        self.assertEqual(_parse_item_genres('anime'), ['anime'])
        self.assertEqual(_parse_item_genres(['anime']), ['anime'])

    def test_empty(self):
        self.assertIsNone(_parse_item_genres(None))
        self.assertIsNone(_parse_item_genres(''))
        self.assertIsNone(_parse_item_genres(' , '))


if __name__ == '__main__':
    unittest.main()
