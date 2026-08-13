#!/usr/bin/env python3
"""
Unit tests for repair_engine._verify_file_readable's conservative aggregation.

This guard decides whether the repair engine may delete + re-scrape an item the
provider flagged as broken: a False return permits deletion. The safety-critical
property is that transient/inconclusive read results on lazy debrid/FUSE mounts
must NEVER be reported as 'dead' (which would delete a good file) — only a
consistent CLEAN failure counts as unreadable.

The per-attempt probe (_probe_readable_once) is monkeypatched so we test the
retry/aggregation decision, not ffprobe itself.
"""

import unittest
import sys
import os
import types
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _permissive_module(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr: (lambda *a, **k: None)  # any imported name → dummy callable
    return m


def _load():
    # Stub third-party + cli-debrid imports repair_engine pulls at module scope.
    if 'requests' not in sys.modules:
        sys.modules['requests'] = _permissive_module('requests')
    for n in ['database', 'database.core', 'database.nzb_repair_activity',
              'database.not_wanted_magnets']:
        sys.modules[n] = _permissive_module(n)
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: (a[2] if len(a) > 2 else None)
    sys.modules['utilities.settings'] = uss
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'repair_engine.py')
    spec = importlib.util.spec_from_file_location('repair_engine_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


re_mod = _load()


class TestVerifyFileReadable(unittest.TestCase):
    def setUp(self):
        # real file so os.path.exists() is True; path must not start with /debrid/
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.write(b'x')
        self.tmp.close()
        self.addCleanup(lambda: os.unlink(self.tmp.name))
        # no-op sleeps to keep tests fast
        import time
        self._orig_sleep = time.sleep
        time.sleep = lambda *a, **k: None
        self.addCleanup(lambda: setattr(time, 'sleep', self._orig_sleep))
        self._orig_probe = re_mod._probe_readable_once
        self.addCleanup(lambda: setattr(re_mod, '_probe_readable_once', self._orig_probe))
        # keep tests hermetic: don't actually shell out for duration
        self._orig_dur = re_mod._media_duration_seconds
        re_mod._media_duration_seconds = lambda *a, **k: None
        self.addCleanup(lambda: setattr(re_mod, '_media_duration_seconds', self._orig_dur))

    def _stub_probe(self, sequence):
        seq = list(sequence)
        default = sequence[-1] if sequence else None
        self.calls = 0
        self.offsets = []
        def fake(file_path, offset_seconds=None, timeout=10):
            self.calls += 1
            self.offsets.append(offset_seconds)
            return seq.pop(0) if seq else default
        re_mod._probe_readable_once = fake

    def test_missing_path_is_unreadable(self):
        self.assertFalse(re_mod._verify_file_readable('/does/not/exist/file.mkv'))

    def test_empty_path_is_unreadable(self):
        self.assertFalse(re_mod._verify_file_readable(''))

    def test_success_first_attempt(self):
        self._stub_probe([True])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name))
        self.assertEqual(self.calls, 1)  # short-circuits on success

    def test_all_clean_failures_is_unreadable(self):
        self._stub_probe([False, False, False])
        self.assertFalse(re_mod._verify_file_readable(self.tmp.name, attempts=3))
        self.assertEqual(self.calls, 3)

    def test_transient_then_success(self):
        self._stub_probe([None, True])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name, attempts=3))

    def test_any_inconclusive_means_keep(self):
        # a clean fail mixed with a transient -> must NOT delete (conservative True)
        self._stub_probe([False, None, False])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name, attempts=3))

    def test_all_inconclusive_means_keep(self):
        self._stub_probe([None, None, None])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name, attempts=3))

    def test_offset_is_random_fraction_of_duration(self):
        # With a known duration, the probe offset must be a deep 20–80% point,
        # and constant across the retries within one call.
        re_mod._media_duration_seconds = lambda *a, **k: 1000.0
        self._stub_probe([False, False])  # force 2 attempts (no success)
        re_mod._verify_file_readable(self.tmp.name, attempts=2)
        self.assertTrue(self.offsets, 'probe was not called')
        for off in self.offsets:
            self.assertIsNotNone(off)
            self.assertGreaterEqual(off, 200.0)   # >= 20% of 1000
            self.assertLessEqual(off, 800.0)       # <= 80% of 1000
        self.assertEqual(len(set(self.offsets)), 1, 'offset must be constant across retries')

    def test_unknown_duration_falls_back_to_start(self):
        re_mod._media_duration_seconds = lambda *a, **k: None
        self._stub_probe([False])
        re_mod._verify_file_readable(self.tmp.name, attempts=1)
        self.assertEqual(self.offsets, [None])  # start-read (no offset)


class TestNZBPlaybackReplacementSelection(unittest.TestCase):
    def _selection_modules(self, provisional=None):
        attempts = []
        cleanup_module = types.ModuleType('database.mount_replacement_cleanup')
        cleanup_module.candidate_matches_episode = (
            lambda _item, result=None, entry=None: True
        )
        cleanup_module.get_provisional_mount_attempt = lambda _item_id: provisional
        cleanup_module.record_mount_replacement_attempt = (
            lambda item_id, **values: attempts.append((item_id, values)) or True
        )
        usenet_module = types.ModuleType('usenet')
        usenet_module.get_usenet_client = lambda: Mock()
        return attempts, cleanup_module, usenet_module

    def test_terminal_candidate_is_hidden_and_next_candidate_is_used(self):
        attempts, cleanup_module, usenet_module = self._selection_modules()
        candidates = [
            {'title': 'first.failed', 'nzb_url': 'https://indexer/first'},
            {'title': 'second.works', 'nzb_url': 'https://indexer/second'},
        ]
        submissions = [
            ('failed-uuid', 'first.failed', 'failed'),
            ('working-uuid', 'second.works', 'confirmed'),
        ]
        with patch.dict(sys.modules, {
                'database.mount_replacement_cleanup': cleanup_module,
                'usenet': usenet_module,
        }), patch.object(re_mod, '_submit_and_confirm_replacement', side_effect=submissions), \
                patch.object(re_mod, '_blacklist_broken_nzb') as blacklist:
            selected, job_id, state = re_mod._select_confirmed_replacement(
                candidates, 'Show', {'id': 75299},
            )

        self.assertEqual('working-uuid', job_id)
        self.assertEqual('confirmed', state)
        self.assertEqual('second.works', selected['title'])
        self.assertEqual('failed_submission', attempts[0][1]['status'])
        self.assertEqual('failed-uuid', attempts[0][1]['job_id'])
        blacklist.assert_called_once()

    def test_scrape_deduplicates_normalized_titles_within_one_attempt(self):
        scraper_pkg = types.ModuleType('scraper')
        scraper_module = types.ModuleType('scraper.scraper')
        scraper_module.scrape = lambda **_kwargs: ([
            {'protocol': 'nzb', 'title': 'Release.Name', 'nzb_url': 'https://one'},
            {'protocol': 'nzb', 'title': 'release-name', 'nzb_url': 'https://two'},
            {'protocol': 'nzb', 'title': 'Different.Release', 'nzb_url': 'https://three'},
        ], None)
        cleanup_module = types.ModuleType('database.mount_replacement_cleanup')
        cleanup_module.get_attempted_candidate_keys = lambda _item_id: {
            'segment_ids': set(), 'guids': set(), 'titles': set(),
        }
        response = types.SimpleNamespace(status_code=500, text='')
        with patch.dict(sys.modules, {
                'scraper': scraper_pkg,
                'scraper.scraper': scraper_module,
                'database.mount_replacement_cleanup': cleanup_module,
        }), patch.object(re_mod.requests, 'get', return_value=response):
            results = re_mod._scrape_for_replacement({
                'id': 75299, 'title': 'Show', 'type': 'episode',
                'season_number': 3, 'episode_number': 1,
            }, '')
        self.assertEqual(['Release.Name', 'Different.Release'], [
            result['title'] for result in results
        ])

    def test_terminal_add_response_returns_uuid_as_failed_without_polling(self):
        client = Mock()
        client.add_nzb.return_value = 'failed-uuid'
        client.last_submission_state = 'failed'
        usenet_module = types.ModuleType('usenet')
        usenet_module.reset_usenet_client = lambda: None
        usenet_module.get_usenet_client = lambda: client
        with patch.dict(sys.modules, {'usenet': usenet_module}), \
                patch.object(re_mod.time, 'sleep') as sleep:
            job_id, release_title, state = re_mod._submit_and_confirm_replacement(
                {'title': 'first.failed', 'nzb_url': 'https://indexer/first'},
                'Show', item=None,
            )
        self.assertEqual(('failed-uuid', 'first.failed', 'failed'),
                         (job_id, release_title, state))
        client.get_job_status.assert_not_called()
        sleep.assert_not_called()

    def test_inconclusive_candidate_blocks_duplicate_submission(self):
        attempts, cleanup_module, usenet_module = self._selection_modules()
        with patch.dict(sys.modules, {
                'database.mount_replacement_cleanup': cleanup_module,
                'usenet': usenet_module,
        }), patch.object(
                re_mod, '_submit_and_confirm_replacement',
                return_value=('pending-uuid', 'pending.release', 'unconfirmed'),
        ) as submit:
            selected, job_id, state = re_mod._select_confirmed_replacement(
                [{'title': 'pending'}, {'title': 'must.not.submit'}],
                'Show', {'id': 75299},
            )

        self.assertIsNone(selected)
        self.assertEqual('pending-uuid', job_id)
        self.assertEqual('unconfirmed', state)
        self.assertEqual(1, submit.call_count)
        self.assertEqual('provisional', attempts[0][1]['status'])

    def test_mismatched_provisional_candidate_is_rejected(self):
        provisional = {
            'job_id': 'wrong-uuid', 'release_title': 'Show.S05E01.Wrong',
            'normalized_title': 'shows05e01wrong', 'segment_id': 'segment-one',
            'nzb_guid': 'guid-one',
        }
        attempts, cleanup_module, usenet_module = self._selection_modules(provisional)
        cleanup_module.candidate_matches_episode = (
            lambda _item, result=None, entry=None:
            (result or {}).get('title') != 'Show.S05E01.Wrong'
        )
        candidate = {'title': 'Show.S05E02.Right', 'nzb_url': 'https://indexer/right'}
        with patch.dict(sys.modules, {
                'database.mount_replacement_cleanup': cleanup_module,
                'usenet': usenet_module,
        }), patch.object(
                re_mod, '_submit_and_confirm_replacement',
                return_value=('working-uuid', 'Show.S05E02.Right', 'confirmed'),
        ) as submit:
            selected, job_id, state = re_mod._select_confirmed_replacement(
                [candidate], 'Show', {
                    'id': 75299, 'type': 'episode',
                    'season_number': 5, 'episode_number': 2,
                },
            )

        self.assertEqual(('working-uuid', 'confirmed'), (job_id, state))
        self.assertEqual('Show.S05E02.Right', selected['title'])
        submit.assert_called_once()
        self.assertEqual('failed_submission', attempts[0][1]['status'])
        self.assertEqual('wrong-uuid', attempts[0][1]['job_id'])

    def test_mismatched_episode_candidate_is_rejected_before_submission(self):
        attempts, cleanup_module, usenet_module = self._selection_modules()
        cleanup_module.candidate_matches_episode = (
            lambda _item, result=None, entry=None:
            (result or {}).get('title') != 'Show.S05E01.Wrong'
        )
        candidates = [
            {'title': 'Show.S05E01.Wrong', 'nzb_url': 'https://indexer/wrong'},
            {'title': 'Show.S05E02.Right', 'nzb_url': 'https://indexer/right'},
        ]
        with patch.dict(sys.modules, {
                'database.mount_replacement_cleanup': cleanup_module,
                'usenet': usenet_module,
        }), patch.object(
                re_mod, '_submit_and_confirm_replacement',
                return_value=('working-uuid', 'Show.S05E02.Right', 'confirmed'),
        ) as submit:
            selected, job_id, state = re_mod._select_confirmed_replacement(
                candidates, 'Show', {
                    'id': 75299, 'type': 'episode',
                    'season_number': 5, 'episode_number': 2,
                },
            )

        self.assertEqual('working-uuid', job_id)
        self.assertEqual('confirmed', state)
        self.assertEqual('Show.S05E02.Right', selected['title'])
        submit.assert_called_once_with(candidates[1], 'Show', item={
            'id': 75299, 'type': 'episode',
            'season_number': 5, 'episode_number': 2,
        })
        self.assertEqual('rejected_identity', attempts[0][1]['status'])

    def test_playback_repair_never_calls_plex_delete(self):
        with patch.object(re_mod, '_bulk_delete_from_plex') as delete:
            result = re_mod._prepare_plex_for_replacement(
                [{'id': 75299}, {'id': 75300}], playback_cleanup=True,
            )
        delete.assert_not_called()
        self.assertEqual({75299: True, 75300: True}, result)


class TestPlaybackRepairFailClosed(unittest.TestCase):
    @staticmethod
    def playback_entry():
        return {
            'entry_name': 'Show.S01E01.Broken',
            'failure_reason': 'media_probe_failed',
            'broken_files': [{
                'entry_name': 'Show.S01E01.Broken',
                'file_name': 'Show.S01E01.Broken.mkv',
                'info_hash': 'old-uuid',
                'cli_debrid_id': 75299,
                'reason': 'media_probe_failed',
            }],
        }

    def test_cleanup_processing_lock_does_not_disable_classification(self):
        raw = self.playback_entry()
        classified = dict(raw, _playback_cleanup=True, cli_debrid_id=75299,
                          file_name='Show.S01E01.Broken.mkv', info_hash='old-uuid')
        cleanup_module = types.ModuleType('database.mount_replacement_cleanup')
        cleanup_module.process_pending_mount_cleanups = Mock(
            side_effect=RuntimeError('database is locked')
        )
        cleanup_module.adopt_stale_replaced_nzbs = lambda entries: entries
        cleanup_module.split_playback_cleanup_targets = Mock(
            return_value=[classified]
        )
        summary = {'errors': 0}

        with patch.dict(sys.modules, {
                'database.mount_replacement_cleanup': cleanup_module,
        }), patch.object(re_mod, 'fetch_broken_items', return_value=[raw]):
            result = re_mod._prepare_broken_entries_for_repair(summary)

        self.assertEqual([classified], result)
        cleanup_module.split_playback_cleanup_targets.assert_called_once()
        self.assertEqual(0, summary['errors'])

    def test_classifier_failure_defers_playback_but_keeps_legacy_entries(self):
        playback = self.playback_entry()
        legacy = {
            'entry_name': 'Show.S01E02.Missing',
            'failure_reason': 'usenet_segment_missing',
            'broken_files': [{'reason': 'usenet_segment_missing'}],
        }
        cleanup_module = types.ModuleType('database.mount_replacement_cleanup')
        cleanup_module.process_pending_mount_cleanups = lambda *a, **k: {}
        cleanup_module.adopt_stale_replaced_nzbs = lambda entries: entries
        cleanup_module.split_playback_cleanup_targets = Mock(
            side_effect=RuntimeError('database is locked')
        )
        summary = {'errors': 0}

        with patch.dict(sys.modules, {
                'database.mount_replacement_cleanup': cleanup_module,
        }), patch.object(re_mod, 'fetch_broken_items',
                         return_value=[playback, legacy]):
            result = re_mod._prepare_broken_entries_for_repair(summary)

        self.assertEqual([legacy], result)
        self.assertEqual(1, summary['errors'])

    def test_raw_playback_reason_is_always_recognized(self):
        self.assertTrue(re_mod._entry_has_protected_playback_failure(
            self.playback_entry()
        ))
        self.assertTrue(re_mod._entry_has_protected_playback_failure({
            'broken_files': [{'reason': 'media_no_playable_stream'}],
        }))
        self.assertFalse(re_mod._entry_has_protected_playback_failure({
            'failure_reason': 'usenet_segment_missing',
        }))

    def test_already_running_result_is_explicit(self):
        self.assertTrue(re_mod._repair_lock.acquire(blocking=False))
        try:
            self.assertTrue(re_mod.is_repair_running())
            self.assertEqual(
                {'skipped': 'already_running'},
                re_mod.run_repair(triggered_by='manual'),
            )
        finally:
            re_mod._repair_lock.release()

if __name__ == '__main__':
    unittest.main(verbosity=2)
