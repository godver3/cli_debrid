#!/usr/bin/env python3
"""
Unit tests for season/episode recovery from a release filename.

Mirrors the self-contained style of test_filename_truncation.py: the functions
under test live in utilities/local_library_scan.py, which pulls heavy project
imports (database, settings, scraper). To keep the test runnable in isolation we
copy the two pure helpers here verbatim. They depend only on os, re and PTT
(parsett, already a project dependency).

Regression target: season packs labelled as a single episode (e.g. FLUX WEB-DLs)
and manually-assigned items reach get_symlink_path with season/episode == None/0,
which made the template render 'Season 00 / S00E00'. These helpers recover the
real SxxExx from the on-disk filename so the symlink lands in the correct folder.
"""

import unittest
import sys
import os
import re
from typing import Tuple, Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- functions copied verbatim from utilities/local_library_scan.py ---

def _se_is_missing(value: Any) -> bool:
    """A season/episode number counts as 'missing' if it is None or 0."""
    if value is None:
        return True
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


def recover_season_episode_from_filename(filename: str) -> Tuple[Optional[int], Optional[int]]:
    """Best-effort recovery of (season, episode) from a release filename."""
    if not filename:
        return None, None
    base = os.path.basename(str(filename))
    season = episode = None

    # Primary: PTT (already a dependency, handles the long tail of naming schemes)
    try:
        from PTT import parse_title
        parsed = parse_title(base)
        seasons = parsed.get('seasons') or []
        episodes = parsed.get('episodes') or []
        if seasons:
            season = int(seasons[0])
        if episodes:
            episode = int(episodes[0])
    except Exception:
        pass

    # Backstop: explicit SxxExx / Sxx regex for anything PTT missed
    if season is None or episode is None:
        m = re.search(r'(?i)\bS(\d{1,3})(?:E(\d{1,3}))?\b', base)
        if m:
            if season is None and m.group(1):
                season = int(m.group(1))
            if episode is None and m.group(2):
                episode = int(m.group(2))

    return season, episode


# --- tests ---

class TestSeMissing(unittest.TestCase):
    def test_missing_values(self):
        for v in (None, 0, "0", 0.0):
            self.assertTrue(_se_is_missing(v), f"{v!r} should be missing")

    def test_present_values(self):
        for v in (1, 5, "5", 12):
            self.assertFalse(_se_is_missing(v), f"{v!r} should not be missing")

    def test_unparseable_is_not_missing(self):
        # A non-numeric, non-None value (e.g. multi-ep string 'E17-E18') is left alone.
        self.assertFalse(_se_is_missing("E17-E18"))


class TestRecoverSeasonEpisode(unittest.TestCase):
    def test_flux_single_episode_label(self):
        # The exact release from the bug report.
        fn = ("For.All.Mankind.S05E10.This.Land.Is.Our.Land.2160p."
              "ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX")
        self.assertEqual(recover_season_episode_from_filename(fn), (5, 10))

    def test_e01(self):
        fn = "For.All.Mankind.S05E01.2160p.ATVP.WEB-DL.H.265-FLUX"
        self.assertEqual(recover_season_episode_from_filename(fn), (5, 1))

    def test_standard_sxxexx(self):
        self.assertEqual(
            recover_season_episode_from_filename("Severance.S02E07.1080p.WEB.h264-ETHEL"),
            (2, 7),
        )

    def test_season_pack_without_episode(self):
        # Whole-season pack: season recovered, episode stays None (template falls back to 0).
        self.assertEqual(
            recover_season_episode_from_filename("The.Bear.S03.1080p.WEB-DL-NTb"),
            (3, None),
        )

    def test_alternate_numbering(self):
        self.assertEqual(
            recover_season_episode_from_filename("Show.Name.2x05.HDTV.x264"),
            (2, 5),
        )

    def test_movie_returns_nothing(self):
        self.assertEqual(
            recover_season_episode_from_filename("Some.Movie.2019.1080p.BluRay.x264"),
            (None, None),
        )

    def test_full_path_uses_basename(self):
        fn = "/mnt/x/For All Mankind/Season 05/For.All.Mankind.S05E10-FLUX.mkv"
        self.assertEqual(recover_season_episode_from_filename(fn), (5, 10))

    def test_empty_and_none(self):
        self.assertEqual(recover_season_episode_from_filename(""), (None, None))
        self.assertEqual(recover_season_episode_from_filename(None), (None, None))

    def test_integration_shape(self):
        # Emulates the get_symlink_path branch: missing S/E get backfilled from filename.
        s_num_val, e_num_val = 0, None  # what a season-pack/manual-assign item carries
        fname = ("For.All.Mankind.S05E10.This.Land.Is.Our.Land.2160p."
                 "ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX")
        if _se_is_missing(s_num_val) or _se_is_missing(e_num_val):
            rec_s, rec_e = recover_season_episode_from_filename(fname)
            if _se_is_missing(s_num_val) and rec_s is not None:
                s_num_val = rec_s
            if _se_is_missing(e_num_val) and rec_e is not None:
                e_num_val = rec_e
        season_number = int(s_num_val if s_num_val is not None else 0)
        episode_number = int(e_num_val if e_num_val is not None else 0)
        # Before the fix this rendered Season 00 / S00E00.
        self.assertEqual((season_number, episode_number), (5, 10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
