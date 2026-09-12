#!/usr/bin/env python3
"""Tests for the shared season-pack/episode identity helper (godver3/cli_debrid#496 item 2).

Several reuse paths classified a sibling as a season pack using only one or
two of filled_by_file, filled_by_title, and original_scraped_torrent_title,
with a narrow [Ss]\\d{2}[Ee]\\d{2} pattern that misses 1-digit seasons and
3-digit episode numbers. debrid/common/utils.py's is_likely_season_pack /
release_identity_has_episode_marker replace that with one helper checking
every available identity field and a broader [Ss]\\d{1,2}[Ee]\\d{1,3} pattern.

queues/scraping_queue.py, queues/torrent_processor.py and queues/run_program.py
can't be imported in this test environment (bencodepy / plexapi missing, same
constraint as the rest of this suite), so their wiring is asserted against
source text instead.
"""

import importlib.util
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_utils():
    spec = importlib.util.spec_from_file_location('_debrid_common_utils_under_test',
                                                    PROJECT_ROOT / 'debrid' / 'common' / 'utils.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


utils = _load_utils()


class TestReleaseIdentityHasEpisodeMarker(unittest.TestCase):
    def test_detects_marker_in_any_field(self):
        self.assertTrue(utils.release_identity_has_episode_marker('Show.S03.WEB-DL', 'Show.S03E01.mkv'))
        self.assertTrue(utils.release_identity_has_episode_marker(None, None, 'Show.S03E01.mkv'))

    def test_no_marker_when_absent_everywhere(self):
        self.assertFalse(utils.release_identity_has_episode_marker('Show.S03.WEB-DL', 'abc123.mkv'))

    def test_one_digit_season_detected(self):
        # The old [Ss]\d{2}[Ee]\d{2} pattern missed this.
        self.assertTrue(utils.release_identity_has_episode_marker('Show.S1E1.WEB-DL'))

    def test_three_digit_episode_detected(self):
        # Long-running anime, e.g. S01E101 — the old pattern capped at 2 digits.
        self.assertTrue(utils.release_identity_has_episode_marker('Show.S01E101.WEB-DL'))


class TestIsLikelySeasonPack(unittest.TestCase):
    def test_pack_title_with_no_episode_marker(self):
        self.assertTrue(utils.is_likely_season_pack('Show.S03.WEB-DL', ''))

    def test_episode_marker_in_filename_blocks_pack_classification(self):
        # The exact regression this issue described: an obfuscated/season-level
        # title with the real episode marker only in the downloaded filename.
        self.assertFalse(utils.is_likely_season_pack('Show.S03.REPACK.WEB-DL', 'Show.S03E01.mkv'))

    def test_episode_marker_only_in_filled_by_title_blocks_pack_classification(self):
        # A provider can obfuscate the filename while filled_by_title (the
        # structured job title cli_debrid built) still carries the marker —
        # checking only title+filename (as every fixed call site used to)
        # missed this.
        self.assertFalse(utils.is_likely_season_pack('abc123.mkv', '', 'Show - S03E01 - Title'))

    def test_all_fields_empty_is_never_a_pack(self):
        # An unresolved sibling (fields not written back yet) must not be
        # mistaken for pack evidence — several call sites' comments describe
        # this exact wrong-job-reuse bug when the default was flipped.
        self.assertFalse(utils.is_likely_season_pack('', '', ''))
        self.assertFalse(utils.is_likely_season_pack())


class TestCallSiteWiring(unittest.TestCase):
    def _source(self, relpath):
        return (PROJECT_ROOT / relpath).read_text()

    def test_scraping_queue_uses_shared_helper_with_all_three_fields(self):
        src = self._source('queues/scraping_queue.py')
        self.assertIn('from debrid.common import is_likely_season_pack', src)
        self.assertIn('is_likely_season_pack(_coal_title, _coal_file, _coal_job_title)', src)
        self.assertNotIn(r"[Ss]\d{2}[Ee]\d{2}", src)

    def test_torrent_processor_uses_shared_helper_everywhere(self):
        src = self._source('queues/torrent_processor.py')
        self.assertEqual(src.count('from debrid.common import is_likely_season_pack'), 3)
        self.assertIn('filled_by_title: str = ', src)
        self.assertNotIn(r"[Ss]\d{2}[Ee]\d{2}", src)

    def test_run_program_uses_shared_helper_at_every_stale_check(self):
        src = self._source('queues/run_program.py')
        self.assertEqual(src.count('from debrid.common import is_likely_season_pack'), 5)
        self.assertNotIn(r"[Ss]\d{2}[Ee]\d{2}", src)

    def test_debrid_common_exports_the_helper(self):
        src = self._source('debrid/common/__init__.py')
        self.assertIn('is_likely_season_pack', src)
        self.assertIn('release_identity_has_episode_marker', src)


if __name__ == '__main__':
    unittest.main()
