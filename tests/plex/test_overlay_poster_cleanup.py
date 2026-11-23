"""Plex poster cleanup: which uploaded poster files the daily cleanup may delete.

It used to walk Plex's whole Metadata tree and delete every uploaded poster whose
hash wasn't recorded on an 'applied'/'pending' overlay row: posters uploaded by the
user or other tools, the active poster of an item whose last re-render failed, and
posters of items whose lookup failed all went. Now it only deletes uploads Plex
reports as not selected, on items cli_debrid overlaid.
"""
import os
import tempfile
import unittest

import requests

import overlays.scheduled_tasks as scheduled_tasks
from tests.plex.test_overlay_change_detection import OverlayDbTestBase


def _upload(h, selected=False):
    return {'ratingKey': f'upload://posters/{h}', 'selected': selected}


class _FakePlex:
    def __init__(self, lists):
        self.lists = lists
        self.asked = []

    def get_poster_list(self, rk, strict=False):
        assert strict, 'cleanup must not treat a failed lookup as "no posters"'
        self.asked.append(rk)
        value = self.lists[rk]
        if isinstance(value, Exception):
            raise value
        return value


def _http_error(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.exceptions.HTTPError(response=resp)


class PosterCleanupTests(OverlayDbTestBase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, 'Metadata')

    def file(self, bundle, name):
        d = os.path.join(self.root, 'Movies', bundle[0], f'{bundle}.bundle', 'Uploads', 'posters')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, 'wb') as f:
            f.write(b'x' * 10)
        return path

    def overlaid(self, item_id, ms_item_id, applied_at="datetime('now', '-1 hour')"):
        self.add_item(item_id, 'movie', f'tt{item_id}', ms_item_id)
        self.apply(item_id, applied_at=applied_at)

    def run_cleanup(self, plex, keep=(), since=None):
        return scheduled_tasks._cleanup_overlay_upload_files(self.root, plex, set(keep), since=since)

    def test_deletes_only_unselected_uploads_of_overlaid_items(self):
        self.overlaid(1, '100')
        active = self.file('a1', 'a' * 40)
        old = self.file('a1', 'b' * 40)
        foreign = self.file('z9', 'c' * 40)     # someone else's upload, not in any list
        res = self.run_cleanup(_FakePlex({'100': [_upload('a' * 40, True), _upload('b' * 40)]}))
        self.assertEqual(res['deleted'], 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(active))
        self.assertTrue(os.path.exists(foreign))
        self.assertEqual(res['item_hashes'], {'100': 'a' * 40})

    def test_protected_and_cross_selected_hashes_survive(self):
        self.overlaid(1, '100')
        self.overlaid(2, '200')
        recorded = self.file('a1', 'd' * 40)    # recorded on an overlay row (e.g. status failed)
        shared = self.file('b2', 'e' * 40)      # selected on another checked item
        res = self.run_cleanup(_FakePlex({
            '100': [_upload('d' * 40), _upload('e' * 40)],
            '200': [_upload('e' * 40, True)],
        }), keep={'d' * 40})
        self.assertEqual(res['deleted'], 0)
        self.assertTrue(os.path.exists(recorded))
        self.assertTrue(os.path.exists(shared))

    def test_failed_lookup_skips_item_and_404_is_stale(self):
        self.overlaid(1, '100')
        self.overlaid(2, '200')
        kept = self.file('a1', 'f' * 40)
        res = self.run_cleanup(_FakePlex({'100': _http_error(500), '200': _http_error(404)}))
        self.assertEqual(res['errors'], 1)
        self.assertEqual(res['stale_keys'], ['200'])
        self.assertTrue(os.path.exists(kept))

    def test_only_items_rendered_since_last_run(self):
        self.overlaid(1, '100', applied_at="'2026-01-01 00:00:00'")
        self.overlaid(2, '200')
        plex = _FakePlex({'100': [], '200': []})
        self.run_cleanup(plex, since='2026-06-01 00:00:00')
        self.assertEqual(plex.asked, ['200'])

    def test_season_upload_listed_with_subpath(self):
        self.add_item(1, 'episode', 'tt1', '300', season=1, episode=1)
        self.execute("INSERT INTO season_overlay_state (show_ms_item_id, season_ms_item_id, season_number, "
                     "status, overlay_applied_at) VALUES ('300', '301', 1, 'applied', datetime('now'))")
        old = self.file('c3', 'g' * 40)
        res = self.run_cleanup(_FakePlex({'301': [_upload('seasons/1/' + 'g' * 40)]}))
        self.assertEqual(res['deleted'], 1)
        self.assertFalse(os.path.exists(old))


class MetadataRootTests(unittest.TestCase):
    def test_path_one_level_too_high_is_not_guessed(self):
        base = tempfile.mkdtemp()
        os.makedirs(os.path.join(base, 'Application Support', 'Plex Media Server', 'Metadata'))
        with self.assertLogs(scheduled_tasks.logger, 'WARNING') as logs:
            self.assertIsNone(scheduled_tasks._resolve_plex_metadata_root(base))
        self.assertIn(os.path.join(base, 'Application Support', 'Plex Media Server'), logs.output[0])

    def test_check_suggests_the_folder_holding_metadata(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, 'Library', 'Application Support', 'Plex Media Server')
        os.makedirs(os.path.join(target, 'Metadata'))
        self.assertEqual(scheduled_tasks.check_plex_data_path(base), {'ok': False, 'suggestion': target})
        self.assertEqual(scheduled_tasks.check_plex_data_path(target), {'ok': True, 'suggestion': None})
        self.assertEqual(scheduled_tasks.check_plex_data_path(os.path.join(base, 'nope')),
                         {'ok': False, 'suggestion': None})
        self.assertEqual(scheduled_tasks.check_plex_data_path('  '), {'ok': False, 'suggestion': None})

    def test_correct_path(self):
        base = tempfile.mkdtemp()
        os.makedirs(os.path.join(base, 'Metadata'))
        self.assertEqual(scheduled_tasks._resolve_plex_metadata_root(base), os.path.join(base, 'Metadata'))


if __name__ == '__main__':
    unittest.main()
