#!/usr/bin/env python3
"""
Regression test for routes/magnet_routes.py::confirm_manual_assignment()'s
duplicate-submission guard.

Two rounds of live testing surfaced two distinct gaps in the same guard:

1. Assigning a second movie from the same collection magnet (e.g. movie 2 of
   an 8-movie Harry Potter magnet, right after movie 1) failed - the guard
   was scoped to torrent_hash alone, but a multi-movie collection magnet is
   legitimately re-assigned multiple times for different target movies in
   quick succession, each a separate Assign flow sharing one magnet. Fixed
   by scoping to (torrent_hash, tmdb_id) instead, using the tmdb_id already
   embedded in each item_key ("movie_{tmdb_id}" / "ep_{tmdb_id}_s..e..") and
   in the historical record's stored item_data.

2. Even with fix #1, re-adding the *same* show (Vinland Saga) after
   deleting a bad first attempt still failed - the guard had no way to know
   the prior assignment had been deleted, so within its 120s window it kept
   blocking a legitimate, deliberate retry. Fixed by checking whether any
   media_items row still references the prior assignment's torrent_id +
   tmdb_id before blocking; if the user already deleted it, there's nothing
   left to duplicate, so the retry is allowed regardless of how recently
   the deleted attempt happened.

This test exercises the actual matching algorithm as written in
confirm_manual_assignment() (item_key parsing + prior-record tmdb_id
comparison + still-present check), not the full Flask route.
"""

import unittest
from datetime import datetime, timedelta


def _current_tmdb_ids(assignment_item_keys):
    current_tmdb_ids = set()
    for item_key in assignment_item_keys:
        parts = item_key.split('_')
        if len(parts) >= 2:
            current_tmdb_ids.add(parts[1])
    return current_tmdb_ids


def _is_duplicate(history_records, current_tmdb_ids, now=None, still_present_fn=None):
    """Mirrors the exact guard logic in confirm_manual_assignment().

    still_present_fn(torrent_id, tmdb_id) -> bool defaults to always True
    (matches the real code's "assume still present" fallback when the
    presence check itself fails, erring toward blocking over letting a
    real duplicate through).
    """
    now = now or datetime.now()
    still_present_fn = still_present_fn or (lambda torrent_id, tmdb_id: True)
    for record in history_records:
        if record.get('trigger_source') != 'manual_assign_confirm':
            continue
        prior_item_data = record.get('item_data') or {}
        prior_tmdb_id = str(prior_item_data.get('tmdb_id') or '')
        if prior_tmdb_id and current_tmdb_ids and prior_tmdb_id not in current_tmdb_ids:
            continue
        record_time = record['timestamp']
        if (now - record_time).total_seconds() < 120:
            prior_torrent_id = prior_item_data.get('torrent_id')
            if prior_torrent_id and prior_tmdb_id and not still_present_fn(prior_torrent_id, prior_tmdb_id):
                continue
            return True
    return False


class TestManualAssignDedupScoping(unittest.TestCase):
    def test_different_movie_from_same_collection_magnet_not_blocked(self):
        # Movie 1 (tmdb 671, Harry Potter and the Philosopher's Stone) was just
        # confirmed for this magnet; now assigning movie 2 (tmdb 672) from the
        # SAME magnet moments later must succeed.
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=5),
            'item_data': {'tmdb_id': '671', 'title': "Harry Potter and the Philosopher's Stone"},
        }]
        current_tmdb_ids = _current_tmdb_ids(['movie_672'])
        self.assertFalse(_is_duplicate(history, current_tmdb_ids, now=now))

    def test_same_movie_resubmitted_moments_later_is_blocked(self):
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=5),
            'item_data': {'tmdb_id': '671', 'title': "Harry Potter and the Philosopher's Stone"},
        }]
        current_tmdb_ids = _current_tmdb_ids(['movie_671'])
        self.assertTrue(_is_duplicate(history, current_tmdb_ids, now=now))

    def test_same_movie_resubmitted_after_window_not_blocked(self):
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=200),
            'item_data': {'tmdb_id': '671', 'title': "Harry Potter and the Philosopher's Stone"},
        }]
        current_tmdb_ids = _current_tmdb_ids(['movie_671'])
        self.assertFalse(_is_duplicate(history, current_tmdb_ids, now=now))

    def test_legacy_record_with_no_tmdb_id_still_blocks_as_a_safe_fallback(self):
        # A record predating this fix (or any malformed item_data) has no
        # tmdb_id to compare against - fall back to the old hash-only behavior
        # rather than risk letting a real duplicate through.
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=5),
            'item_data': {},
        }]
        current_tmdb_ids = _current_tmdb_ids(['movie_671'])
        self.assertTrue(_is_duplicate(history, current_tmdb_ids, now=now))

    def test_episode_item_keys_parse_tmdb_id_correctly(self):
        # ep_{tmdb_id}_s{season}e{episode} - tmdb_id is still parts[1].
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=5),
            'item_data': {'tmdb_id': '88803'},
        }]
        same_show_current = _current_tmdb_ids(['ep_88803_s01e02'])
        self.assertTrue(_is_duplicate(history, same_show_current, now=now))

        different_show_current = _current_tmdb_ids(['ep_99999_s01e02'])
        self.assertFalse(_is_duplicate(history, different_show_current, now=now))

    def test_reassigning_same_show_after_deletion_not_blocked(self):
        # Exact reported scenario: Vinland Saga (tmdb 88803) was assigned, the
        # user deleted the bad result, and tried to redo the same show's
        # assignment moments later - must succeed since nothing is left to
        # duplicate.
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=30),
            'item_data': {'tmdb_id': '88803', 'torrent_id': 'SKOA4GRP5UHQK'},
        }]
        current_tmdb_ids = _current_tmdb_ids(['ep_88803_s01e01'])
        deleted_still_present = lambda torrent_id, tmdb_id: False  # user already deleted every episode
        self.assertFalse(_is_duplicate(history, current_tmdb_ids, now=now, still_present_fn=deleted_still_present))

    def test_reassigning_same_show_still_present_is_blocked(self):
        # Same as above but the prior assignment's items are still in the DB
        # (nothing was deleted) - a genuine near-immediate duplicate resubmit
        # must still be blocked.
        now = datetime.now()
        history = [{
            'trigger_source': 'manual_assign_confirm',
            'timestamp': now - timedelta(seconds=30),
            'item_data': {'tmdb_id': '88803', 'torrent_id': 'SKOA4GRP5UHQK'},
        }]
        current_tmdb_ids = _current_tmdb_ids(['ep_88803_s01e01'])
        still_present = lambda torrent_id, tmdb_id: True
        self.assertTrue(_is_duplicate(history, current_tmdb_ids, now=now, still_present_fn=still_present))


if __name__ == '__main__':
    unittest.main()
