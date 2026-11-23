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


class CleanupPreviewTests(PosterCleanupTests):
    """Dry run: same decisions as a real run, nothing deleted or written."""

    def test_planned_list_instead_of_delete(self):
        self.overlaid(1, '100')
        old = self.file('a1', 'b' * 40)
        planned = []
        res = scheduled_tasks._cleanup_overlay_upload_files(
            self.root, _FakePlex({'100': [_upload('a' * 40, True), _upload('b' * 40)]}),
            set(), planned=planned)
        self.assertEqual(res['deleted'], 1)
        self.assertTrue(os.path.exists(old))
        self.assertEqual([(p['path'], p['kind'], p['key']) for p in planned], [(old, 'overlay', '100')])

    def _run_task(self, plex, dry_run):
        from unittest.mock import patch
        settings = {('Overlay Settings', 'plex_data_path'): self.tmp,
                    ('Plex', 'token'): 'tok', ('Plex', 'url'): 'http://plex.invalid'}
        with patch.object(scheduled_tasks, 'get_setting',
                          lambda sec, key, default=None, **k: settings.get((sec, key), default)), \
             patch.object(scheduled_tasks, 'overlays_active', lambda: True), \
             patch.object(scheduled_tasks, 'is_jellyfin_mode', lambda: False), \
             patch.object(scheduled_tasks, '_collection_upload_hashes', lambda *a: set()), \
             patch('overlays.plex_client.PlexClient', lambda *a, **k: plex), \
             patch.object(scheduled_tasks, 'log_activity') as log_activity:
            res = scheduled_tasks.task_overlay_cleanup(triggered_by='manual', dry_run=dry_run)
        return res, log_activity

    def test_task_dry_run_changes_nothing(self):
        self.overlaid(1, '100')
        self.add_item(2, 'movie', 'tt2', '200')
        self.execute("INSERT INTO media_overlay_state (media_item_id, status, retry_count, last_retry) "
                     "VALUES (2, 'failed', 5, datetime('now', '-30 days'))")
        old = self.file('a1', 'b' * 40)
        plex = _FakePlex({'100': [_upload('a' * 40, True), _upload('b' * 40)]})

        res, log_activity = self._run_task(plex, dry_run=True)
        self.assertTrue(res['success'] and res['dry_run'])
        self.assertTrue(res['path_ok'])
        self.assertEqual(res['would_delete'], 1)
        self.assertEqual(res['by_kind']['overlay'], 1)
        self.assertEqual(res['files'][0]['path'],
                         os.path.relpath(old, self.root))
        self.assertEqual(res['records_would_reset'], 1)
        self.assertTrue(os.path.exists(old))
        log_activity.assert_not_called()
        self.assertEqual(self.execute("SELECT status FROM media_overlay_state WHERE media_item_id = 2")[0][0],
                         'failed')
        state = dict(self.execute("SELECT key, value FROM overlay_sync_state"))
        self.assertNotIn('last_poster_cleanup_at', state)
        self.assertEqual(state.get('cleanup_total_posters', '0'), '0')
        self.assertEqual(self.execute("SELECT last_plex_upload_hash FROM media_overlay_state "
                                      "WHERE media_item_id = 1")[0][0] or '', '')

        # The real run then does exactly what the preview listed
        res, log_activity = self._run_task(plex, dry_run=False)
        self.assertEqual(res['posters_deleted'], 1)
        self.assertFalse(os.path.exists(old))
        log_activity.assert_called_once()


class OverlayHealthTests(OverlayDbTestBase):
    def sync(self, **stats):
        import json
        self.execute("INSERT INTO overlay_activity (action_type, triggered_by, result, title, stats_json) "
                     "VALUES ('overlay_sync', 'scheduled', 'success', 't', ?)", (json.dumps(stats),))

    def test_empty(self):
        h = scheduled_tasks.get_overlay_health()
        self.assertIsNone(h['last'])
        self.assertFalse(h['loop_suspected'])

    def test_old_rows_without_counts_never_flag(self):
        for _ in range(3):
            self.sync(applied=170)
        h = scheduled_tasks.get_overlay_health()
        self.assertIsNone(h['last']['resets'])
        self.assertFalse(h['loop_suspected'])

    def test_loop_needs_every_recent_sync_high(self):
        self.sync(applied=5, quality_resets=0, content_resets=5800, thumb_resets=90)
        self.sync(applied=5, quality_resets=0, content_resets=5800, thumb_resets=90)
        self.sync(applied=2, seasons_applied=1, quality_resets=0, content_resets=16, thumb_resets=0)
        h = scheduled_tasks.get_overlay_health()
        self.assertEqual(h['last']['resets'], 16)
        self.assertEqual(h['last']['applied'], 3)
        self.assertFalse(h['loop_suspected'])
        self.sync(applied=170, quality_resets=2, content_resets=5800, thumb_resets=90)
        self.sync(applied=170, quality_resets=2, content_resets=5800, thumb_resets=90)
        self.sync(applied=170, quality_resets=2, content_resets=5800, thumb_resets=90)
        h = scheduled_tasks.get_overlay_health()
        self.assertTrue(h['loop_suspected'])
        self.assertEqual(h['last']['resets'], 5892)
        self.assertEqual(len(h['syncs']), scheduled_tasks.HEALTH_SYNCS)


class CleanupPermissionTests(PosterCleanupTests):
    """Plex owns its Metadata folders; cli_debrid's uid often can't delete there."""

    def test_permission_denied_is_counted_and_logged_once(self):
        from unittest.mock import patch
        self.overlaid(1, '100')
        a = self.file('a1', 'b' * 40)
        b = self.file('a1', 'c' * 40)
        plex = _FakePlex({'100': [_upload('a' * 40, True), _upload('b' * 40), _upload('c' * 40)]})
        with patch.object(scheduled_tasks.os, 'remove', side_effect=PermissionError(13, 'denied')), \
             self.assertLogs(scheduled_tasks.logger, 'WARNING') as logs:
            res = self.run_cleanup(plex)
        self.assertEqual((res['deleted'], res['denied'], res['errors']), (0, 2, 2))
        self.assertEqual(len(logs.output), 1)
        self.assertIn('no write access', logs.output[0])
        self.assertTrue(os.path.exists(a) and os.path.exists(b))

    def test_preview_marks_files_it_could_not_delete(self):
        from unittest.mock import patch
        self.overlaid(1, '100')
        self.file('a1', 'b' * 40)
        planned = []
        with patch.object(scheduled_tasks.os, 'access', lambda *a: False):
            scheduled_tasks._cleanup_overlay_upload_files(
                self.root, _FakePlex({'100': [_upload('b' * 40)]}), set(), planned=planned)
        self.assertEqual([p['deletable'] for p in planned], [False])


class StaleSeasonTests(PosterCleanupTests):
    def test_season_404_is_stale_not_an_error_and_row_is_dropped(self):
        self.add_item(1, 'episode', 'tt1', '300', season=1, episode=1)
        self.execute("INSERT INTO season_overlay_state (show_ms_item_id, season_ms_item_id, season_number, "
                     "status, overlay_applied_at) VALUES ('300', '301', 1, 'applied', datetime('now'))")
        res = self.run_cleanup(_FakePlex({'301': _http_error(404)}))
        self.assertEqual((res['errors'], res['stale_seasons']), (0, ['301']))
        scheduled_tasks._record_poster_cleanup(res, '2026-11-23 00:00:00')
        self.assertEqual(self.execute("SELECT COUNT(*) FROM season_overlay_state")[0][0], 0)
        self.assertEqual(self.execute("SELECT value FROM overlay_sync_state "
                                      "WHERE key = 'last_poster_cleanup_at'")[0][0], '2026-11-23 00:00:00')
