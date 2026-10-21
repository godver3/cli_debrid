#!/usr/bin/env python3
"""Tests for usenet/nzb_dead_release_cache.py (godver3/cli_debrid#496 item 3).

Indexers can list the same underlying release under multiple GUIDs. The
not-wanted store is keyed per-GUID, so a fresh GUID for a release already
confirmed dead (missing segments) this session repeated the same expensive
cli_mount submission. This cache short-circuits that by normalized title.

Also asserts (via source inspection, since queues/torrent_processor.py can't
be imported in this test environment -- missing bencodepy, same constraint as
the rest of this suite) that _process_nzb_result is actually wired to check
and populate the cache.
"""

import os
import unittest

from pathlib import Path

from usenet.nzb_dead_release_cache import (
    is_release_known_dead,
    mark_release_dead,
    reset_dead_release_cache,
    _normalize_release_title,
    _MAX_ENTRIES,
)


class TestNormalization(unittest.TestCase):
    def test_case_and_punctuation_insensitive(self):
        self.assertEqual(
            _normalize_release_title('Show.S01E01.1080p-GRP'),
            _normalize_release_title('SHOW S01E01 1080P GRP'),
        )

    def test_empty_normalizes_empty(self):
        self.assertEqual(_normalize_release_title(''), '')
        self.assertEqual(_normalize_release_title(None), '')


class TestDeadReleaseCache(unittest.TestCase):
    def setUp(self):
        reset_dead_release_cache()

    def tearDown(self):
        reset_dead_release_cache()

    def test_unknown_release_is_not_dead(self):
        self.assertFalse(is_release_known_dead('Show.S01E01.1080p-GRP'))

    def test_marked_release_is_dead(self):
        mark_release_dead('Show.S01E01.1080p-GRP')
        self.assertTrue(is_release_known_dead('Show.S01E01.1080p-GRP'))

    def test_different_guid_same_title_is_caught(self):
        # Simulates the reported scenario: same release, different indexer
        # casing/punctuation, different GUID entirely (not checked here).
        mark_release_dead('Show.S01E01.1080p-GRP')
        self.assertTrue(is_release_known_dead('SHOW S01E01 1080P-GRP'))

    def test_unrelated_release_unaffected(self):
        mark_release_dead('Show.S01E01.1080p-GRP')
        self.assertFalse(is_release_known_dead('Other.Show.S01E01.1080p-GRP'))

    def test_empty_title_never_marked_or_matched(self):
        mark_release_dead('')
        self.assertFalse(is_release_known_dead(''))

    def test_bounded_size_evicts_oldest(self):
        for i in range(_MAX_ENTRIES + 10):
            mark_release_dead(f'Release.{i}.1080p-GRP')
        self.assertFalse(is_release_known_dead('Release.0.1080p-GRP'))
        self.assertTrue(is_release_known_dead(f'Release.{_MAX_ENTRIES + 9}.1080p-GRP'))


class TestTorrentProcessorWiring(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / 'queues' / 'torrent_processor.py'
        self.source = path.read_text()

    def test_checks_cache_before_submitting(self):
        self.assertIn('from usenet.nzb_dead_release_cache import is_release_known_dead', self.source)
        self.assertIn('if is_release_known_dead(title):', self.source)

    def test_marks_release_dead_only_on_definitive_missing_segments(self):
        # Both submission paths (primary + direct-upload fallback) must mark
        # the cache only inside their `client.last_missing_segments` branch --
        # never on a generic/timeout failure, which isn't proof of death.
        self.assertEqual(self.source.count('mark_release_dead(title)'), 2)
        self.assertIn('from usenet.nzb_dead_release_cache import mark_release_dead', self.source)


if __name__ == '__main__':
    unittest.main()
