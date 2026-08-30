#!/usr/bin/env python3
"""Regression test for GitHub issue #485: NZB season-pack coalescing must be version-aware.

The sibling lookup in queues/scraping_queue.py previously matched only on
imdb_id/season_number/state/filled_by_torrent_id, so an episode requesting
version '2160p' could inherit an in-flight season-pack job that was actually
submitted for version '1080p'. This test exercises the exact SQL query
(including the version-normalization fix) against a real SQLite connection
to confirm cross-version reuse is blocked while same-version reuse
(decorated or not, e.g. '1080p*' vs '1080p') still works.
"""

import os
import re
import sqlite3
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SIBLING_QUERY = (
    "SELECT filled_by_torrent_id, filled_by_file, filled_by_magnet, "
    "filled_by_title, original_scraped_torrent_title, nzb_segment_id "
    "FROM media_items WHERE imdb_id=? AND season_number=? AND type='episode' "
    "AND state IN ('Adding','Checking','Collected','Upgrading') "
    "AND filled_by_torrent_id LIKE 'nzb:%' "
    "AND REPLACE(COALESCE(version, ''), '*', '') = ? LIMIT 1"
)


def _make_conn():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        """
        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY,
            imdb_id TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            type TEXT,
            state TEXT,
            version TEXT,
            filled_by_torrent_id TEXT,
            filled_by_file TEXT,
            filled_by_magnet TEXT,
            filled_by_title TEXT,
            original_scraped_torrent_title TEXT,
            nzb_segment_id TEXT
        )
        """
    )
    return conn


def _insert_sibling(conn, episode_number, version, torrent_id='nzb:job-1'):
    conn.execute(
        "INSERT INTO media_items (imdb_id, season_number, episode_number, type, state, version, "
        "filled_by_torrent_id, filled_by_file, filled_by_magnet, filled_by_title, "
        "original_scraped_torrent_title, nzb_segment_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ('tt1234567', 1, episode_number, 'episode', 'Adding', version,
         torrent_id, '', 'https://indexer/getnzb/pack.nzb', 'Show.S01.PACK', '', ''),
    )
    conn.commit()


class TestSiblingLookupIsVersionAware(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()

    def tearDown(self):
        self.conn.close()

    def test_2160p_request_does_not_reuse_1080p_sibling_job(self):
        _insert_sibling(self.conn, episode_number=1, version='1080p')
        row = self.conn.execute(SIBLING_QUERY, ('tt1234567', 1, '2160p')).fetchone()
        self.assertIsNone(row)

    def test_1080p_request_reuses_1080p_sibling_job(self):
        _insert_sibling(self.conn, episode_number=1, version='1080p')
        row = self.conn.execute(SIBLING_QUERY, ('tt1234567', 1, '1080p')).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'nzb:job-1')

    def test_decorated_sibling_version_still_matches_plain_request(self):
        # '1080p*' marks an item pending upgrade to a better release of the same version.
        _insert_sibling(self.conn, episode_number=1, version='1080p*')
        row = self.conn.execute(SIBLING_QUERY, ('tt1234567', 1, '1080p')).fetchone()
        self.assertIsNotNone(row)

    def test_plain_sibling_version_matches_decorated_request(self):
        _insert_sibling(self.conn, episode_number=1, version='1080p')
        requested = 'tt1234567', 1, '1080p*'.rstrip('*')
        row = self.conn.execute(SIBLING_QUERY, requested).fetchone()
        self.assertIsNotNone(row)


class TestProductionQueryMatchesFixedSql(unittest.TestCase):
    """Guards against the fix regressing back to a version-blind query in scraping_queue.py."""

    def test_scraping_queue_query_includes_version_normalization(self):
        with open(os.path.join(PROJECT_ROOT, "queues", "scraping_queue.py"), encoding="utf-8") as f:
            source = f.read()
        marker = "FROM media_items WHERE imdb_id=? AND season_number=? AND type='episode'"
        idx = source.index(marker)
        snippet = source[idx:idx + 400]
        self.assertIn("REPLACE(COALESCE(version, ''), '*', '') = ?", snippet)
        self.assertIn("_coalesce_version = (item_to_process.get('version') or '').rstrip('*')", source)


if __name__ == "__main__":
    unittest.main()
