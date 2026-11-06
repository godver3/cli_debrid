#!/usr/bin/env python3
"""Plex mode scans each release folder once per cooldown.

Reported 2026-11-01: a Plex-mode user's release folders were each scanned 4-6
times in 10 minutes (task_check_plex_files every ~2 min plus the Checking
queue), ~1,500 scans an hour, and Plex stopped answering for five hours.
"""

import os
import tempfile
import unittest
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
from utilities import plex_functions as pf

PATH = '/cli_mnt/Movies/Pinocchio.2019.UHD.BluRay.2160p-FraMeSToR'


def _mode(mode):
    return mock.patch.object(pf, 'get_setting', lambda section, key, *a, **k: mode)


class TestPlexModeScanCooldown(unittest.TestCase):
    def setUp(self):
        pf._plex_mode_scans.clear()
        self.now = 1_000_000.0
        patcher = mock.patch.object(pf.time, 'time', lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_scan_inside_cooldown_is_skipped(self):
        with _mode('Plex'):
            self.assertTrue(pf.claim_plex_mode_scan(PATH))
            self.now += 120
            self.assertFalse(pf.plex_mode_scan_due(PATH))
            self.assertFalse(pf.claim_plex_mode_scan(PATH))

    def test_scan_allowed_again_after_cooldown(self):
        with _mode('Plex'):
            pf.claim_plex_mode_scan(PATH)
            self.now += pf.PLEX_MODE_SCAN_COOLDOWN
            self.assertTrue(pf.claim_plex_mode_scan(PATH))

    def test_peek_does_not_claim(self):
        with _mode('Plex'):
            self.assertTrue(pf.plex_mode_scan_due(PATH))
            self.assertTrue(pf.claim_plex_mode_scan(PATH))

    def test_same_folder_different_spelling_shares_cooldown(self):
        with _mode('Plex'):
            pf.claim_plex_mode_scan(PATH + '/')
            self.assertFalse(pf.claim_plex_mode_scan(PATH))

    def test_other_folders_unaffected(self):
        with _mode('Plex'):
            pf.claim_plex_mode_scan(PATH)
            self.assertTrue(pf.claim_plex_mode_scan(PATH + '-other'))

    def test_symlink_mode_never_skips(self):
        # Season-pack episodes land in the same folder one at a time there.
        with _mode('Symlinked/Local'):
            for _ in range(5):
                self.assertTrue(pf.claim_plex_mode_scan(PATH))
                self.now += 30


if __name__ == '__main__':
    unittest.main()
