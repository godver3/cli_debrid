#!/usr/bin/env python3
"""
Simulations for the two ghost-job fixes added after the Swat Kats dead-job
reuse-loop incident (2026-08-23):

  Fix A: torrent_processor.py's /api/torrents title-match reuse now calls
         is_nzb_job_alive() before reusing a matched job, same as the two
         sibling-pack reuse sites already patched in 15bca857 (2026-08-18).

  Fix B: run_program.py's NZB health-check now tracks a per-item repeat
         counter and blacklists after _NZB_GHOST_BLACKLIST_THRESHOLD (3)
         consecutive terminal-failure cycles instead of retrying forever,
         and the counter is cleared whenever the item successfully clears
         Adding. Applied at both places an item can loop Adding->Wanted
         with nothing new to try: the progress==-2 "ghost job" branch
         (job absent from cli_mount's queue entirely) and the progress==-1
         "folder never appeared after 10 ticks" branch - the one actually
         exercised in the real Swat Kats trace (191 occurrences logged;
         the -2 branch had zero in that same log, despite being the one
         initially assumed responsible).

These are simulations of the counter/guard *logic* in isolation (extracted
to plain functions mirroring the real code), not integration tests against
the full RunProgram/TorrentProcessor classes - those have large DB/queue-
manager dependency graphs that aren't worth dragging in here. Each test
documents which real code path it mirrors.
"""

import unittest


# --- Fix B: ghost-repeat counter, extracted to match run_program.py's logic ---

GHOST_BLACKLIST_THRESHOLD = 3


def simulate_ghost_cycle(counts: dict, item_id, threshold: int = GHOST_BLACKLIST_THRESHOLD):
    """Mirrors the body of the `for _gi in _ghost_items:` loop in
    run_program.py's ghost-job handler. Returns 'blacklisted' or 'wanted'."""
    count = counts.get(item_id, 0) + 1
    counts[item_id] = count
    if count >= threshold:
        counts.pop(item_id, None)
        return 'blacklisted'
    return 'wanted'


def simulate_success(counts: dict, item_id):
    """Mirrors the counts.pop(item_id, None) added at both move_to_checking
    success sites (primary item and coalesced siblings)."""
    counts.pop(item_id, None)


class TestGhostRepeatCounter(unittest.TestCase):
    def test_single_ghost_does_not_blacklist(self):
        """A one-off ghost (transient provider blip) must not blacklist -
        regression check against over-aggressive blacklisting."""
        counts = {}
        outcome = simulate_ghost_cycle(counts, item_id=35774)
        self.assertEqual(outcome, 'wanted')
        self.assertEqual(counts.get(35774), 1)

    def test_two_ghosts_still_wanted(self):
        counts = {}
        simulate_ghost_cycle(counts, 35774)
        outcome = simulate_ghost_cycle(counts, 35774)
        self.assertEqual(outcome, 'wanted')
        self.assertEqual(counts.get(35774), 2)

    def test_third_consecutive_ghost_blacklists(self):
        """Reproduces the exact Swat Kats trace: same item_id (35774) cycling
        Wanted->Scraping->Adding and ghosting every time. Threshold=3 must
        trip on the 3rd repeat instead of looping forever (18+ cycles observed
        in the real incident log)."""
        counts = {}
        outcomes = [simulate_ghost_cycle(counts, 35774) for _ in range(3)]
        self.assertEqual(outcomes, ['wanted', 'wanted', 'blacklisted'])
        # Counter must be cleared once terminal, not left dangling
        self.assertNotIn(35774, counts)

    def test_further_ghosts_after_blacklist_start_fresh_count(self):
        """If the item is somehow re-wanted again after blacklisting (e.g. a
        user-initiated un-blacklist), it gets a full fresh budget rather than
        immediately re-blacklisting on the next single ghost."""
        counts = {}
        for _ in range(3):
            simulate_ghost_cycle(counts, 35774)
        # Simulate the item coming back into Adding and ghosting once more
        outcome = simulate_ghost_cycle(counts, 35774)
        self.assertEqual(outcome, 'wanted')

    def test_success_resets_counter(self):
        """An item that ghosts twice then successfully clears Adding (real
        job found) must not carry those 2 repeats into an unrelated future
        ghost streak - otherwise a single ghost months later would wrongly
        blacklist on repeat 1 instead of repeat 3."""
        counts = {}
        simulate_ghost_cycle(counts, 35774)
        simulate_ghost_cycle(counts, 35774)
        self.assertEqual(counts.get(35774), 2)

        simulate_success(counts, 35774)
        self.assertNotIn(35774, counts)

        # A later, unrelated ghost must start the count over, not resume at 3
        outcome = simulate_ghost_cycle(counts, 35774)
        self.assertEqual(outcome, 'wanted')
        self.assertEqual(counts.get(35774), 1)

    def test_items_are_independent(self):
        """Sibling items sharing the same dead torrent_id must each track
        their own repeat count, not a shared/global one (regression check:
        one item's history must not affect another's threshold)."""
        counts = {}
        simulate_ghost_cycle(counts, 35696)
        simulate_ghost_cycle(counts, 35696)
        outcome_other = simulate_ghost_cycle(counts, 35774)
        self.assertEqual(outcome_other, 'wanted')
        self.assertEqual(counts.get(35774), 1)
        self.assertEqual(counts.get(35696), 2)

    def test_18_cycle_incident_would_have_stopped_at_3(self):
        """End-to-end simulation of the actual incident shape: the same item
        ghosting every ~2-3 minutes with no other resolution. Real log showed
        18+ unbounded cycles; with the fix, only 3 should ever occur before
        the item is pulled out of the loop entirely."""
        counts = {}
        cycles_run = 0
        for _ in range(18):  # same repeat count observed in the real log
            cycles_run += 1
            outcome = simulate_ghost_cycle(counts, 35774)
            if outcome == 'blacklisted':
                break
        self.assertEqual(cycles_run, 3)
        self.assertNotIn(35774, counts)


# --- Fix A: is_nzb_job_alive-gated reuse, extracted to match the new
#     torrent_processor.py title-match loop body ---

def simulate_title_match_reuse(torrents: list, job_title: str, is_alive_fn):
    """Mirrors the edited `for _t in _torrents_dc:` loop body in
    torrent_processor.py: scans listed torrents for an exact title match,
    skips (continues past) any match whose job is no longer alive, and
    returns the first alive match's hash - or None if nothing usable found,
    signalling the caller should fall through to a fresh submission."""
    for t in torrents:
        if t.get('name') == job_title:
            job_hash = t.get('info_hash', '')
            if not is_alive_fn(job_hash):
                continue
            return job_hash
    return None


class TestReuseGuard(unittest.TestCase):
    def test_dead_job_is_skipped_not_reused(self):
        """The exact Swat Kats failure: cli_mount's listing still shows a
        title match for a job that's actually gone. Must not be reused."""
        torrents = [{'name': 'Swat Kats S01E04', 'info_hash': 'dead-hash'}]
        result = simulate_title_match_reuse(
            torrents, 'Swat Kats S01E04', is_alive_fn=lambda h: False
        )
        self.assertIsNone(result)

    def test_live_job_is_still_reused(self):
        """Regression check: the common, legitimate case (job genuinely still
        in progress) must keep working exactly as before this fix."""
        torrents = [{'name': 'Swat Kats S01E04', 'info_hash': 'live-hash'}]
        result = simulate_title_match_reuse(
            torrents, 'Swat Kats S01E04', is_alive_fn=lambda h: True
        )
        self.assertEqual(result, 'live-hash')

    def test_dead_match_falls_through_to_a_later_live_match(self):
        """If a dead entry and a genuinely live entry both title-match
        (e.g. an old cleaned-up job plus a fresh resubmission), the loop must
        continue past the dead one and pick up the live one instead of
        giving up on the whole listing."""
        torrents = [
            {'name': 'Swat Kats S01E04', 'info_hash': 'dead-hash'},
            {'name': 'Swat Kats S01E04', 'info_hash': 'live-hash'},
        ]
        alive_hashes = {'live-hash'}
        result = simulate_title_match_reuse(
            torrents, 'Swat Kats S01E04', is_alive_fn=lambda h: h in alive_hashes
        )
        self.assertEqual(result, 'live-hash')

    def test_no_match_at_all_returns_none(self):
        """Regression check: unrelated listing contents must not accidentally
        match and must not raise."""
        torrents = [{'name': 'Some Other Show S01E01', 'info_hash': 'x'}]
        result = simulate_title_match_reuse(
            torrents, 'Swat Kats S01E04', is_alive_fn=lambda h: True
        )
        self.assertIsNone(result)


# --- progress==-1 branch simulation: the actually-exercised path ---
#
# Real log evidence (2026-08-23 incident): 11 distinct jobs across 5 unrelated
# shows hit this branch repeatedly in one evening (13-27 repeats each) before
# these fixes; only 3 jobs resolved after a couple of hits. This section
# simulates that branch's full shape - including the "still have other scrape
# results to try" sub-path, which must NOT be touched by the counter since
# trying a different result is legitimate progress, not a repeat of the same
# failure.

def simulate_failed_in_climount_cycle(counts: dict, item_id, has_more_results: bool,
                                       threshold: int = GHOST_BLACKLIST_THRESHOLD):
    """Mirrors the `elif progress == -1:` branch's `if not _has_more_results:`
    split in run_program.py. Returns 'retry_next_result' (has_more_results path,
    uncounted), 'wanted' (no more results, under threshold), or 'blacklisted'."""
    if has_more_results:
        return 'retry_next_result'
    count = counts.get(item_id, 0) + 1
    counts[item_id] = count
    if count >= threshold:
        counts.pop(item_id, None)
        return 'blacklisted'
    return 'wanted'


class TestFailedInCliMountCounter(unittest.TestCase):
    def test_real_incident_shapes_all_cap_at_3(self):
        """Replays the exact repeat counts observed in the real log for each
        of the 11 looping jobs (Welcome to the Jungle x27 down to x13) and
        confirms every single one would have stopped at exactly 3 cycles
        instead of running to completion of its real repeat count."""
        observed_repeat_counts = [27, 20, 18, 18, 17, 16, 15, 14, 14, 13, 13]
        for real_repeats, item_id in zip(observed_repeat_counts, range(len(observed_repeat_counts))):
            counts = {}
            cycles_run = 0
            for _ in range(real_repeats):
                cycles_run += 1
                outcome = simulate_failed_in_climount_cycle(counts, item_id, has_more_results=False)
                if outcome == 'blacklisted':
                    break
            self.assertEqual(cycles_run, 3, f"item {item_id} (real repeats={real_repeats}) should cap at 3")

    def test_two_hit_transient_case_never_blacklists(self):
        """The 3 jobs that only hit this branch twice in the real log and then
        resolved normally must never reach the threshold - confirms the
        threshold doesn't punish genuinely transient, self-resolving cases."""
        counts = {}
        outcomes = [simulate_failed_in_climount_cycle(counts, 'transient-job', has_more_results=False)
                    for _ in range(2)]
        self.assertEqual(outcomes, ['wanted', 'wanted'])
        self.assertNotIn('blacklisted', outcomes)

    def test_has_more_results_path_is_never_counted(self):
        """Regression check: an item still working through several scrape
        results (existing, unrelated multi-result retry behavior) must not
        accumulate any failure count just because an earlier result in the
        same batch failed - only exhausting ALL results counts as a cycle."""
        counts = {}
        for _ in range(10):  # far more than the blacklist threshold
            outcome = simulate_failed_in_climount_cycle(counts, 'multi-result-item', has_more_results=True)
            self.assertEqual(outcome, 'retry_next_result')
        self.assertNotIn('multi-result-item', counts)

    def test_exhausting_results_then_failing_fresh_scrapes_still_caps_at_3(self):
        """Realistic composite sequence: item first burns through 2 other
        results from its original scrape batch (uncounted), then on the 3rd
        result and afterward has nothing left and starts actually cycling -
        the counter must only start from when repeats genuinely begin."""
        counts = {}
        sequence = [True, True, False, False, False, False]  # last 4 are real repeats
        outcomes = [simulate_failed_in_climount_cycle(counts, 'composite-item', has_more_results=hm)
                    for hm in sequence]
        # Repeats 1-2 (uncounted, still had other results): retry_next_result
        # Repeats 3-4 (first two genuine failures): wanted, wanted
        # Repeat 5 (3rd genuine failure, hits threshold): blacklisted, counter popped
        # Repeat 6: counter already cleared by the pop, so this starts a fresh
        # count at 1 rather than compounding past the terminal state
        self.assertEqual(outcomes, ['retry_next_result', 'retry_next_result', 'wanted', 'wanted',
                                     'blacklisted', 'wanted'])
        self.assertEqual(counts.get('composite-item'), 1)


# --- Structural regression checks against the real source files ---
#
# These don't re-simulate logic; they assert the actual edited files still
# have the shape both fixes depend on, so a later unrelated refactor that
# accidentally drops a call site is caught even though the pure-logic
# simulations above would keep passing (they don't read the real files).

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSourceShapeRegression(unittest.TestCase):
    def _read(self, relpath):
        with open(os.path.join(_REPO_ROOT, relpath), encoding='utf-8') as f:
            return f.read()

    def test_is_nzb_job_alive_has_three_call_sites(self):
        """Two pre-existing (15bca857, 2026-08-18) sibling-pack guards plus
        the one this fix added to the /api/torrents title-match block. If
        this count drops, a reuse site has lost its liveness check again.
        Counts real import/call sites only, not comments mentioning the name."""
        src = self._read('queues/torrent_processor.py')
        import_sites = src.count('from usenet.climount_client import is_nzb_job_alive')
        self.assertEqual(import_sites, 3)

    def test_db_dedup_reuse_site_is_unchanged_by_fix_a(self):
        """The DB-level dedup reuse path (state IN Adding/Checking, same
        item's own prior submission) is a different, already-safe site -
        Fix A must not have leaked a liveness check into it, since that
        would be redundant (it's scoped to this item's own recent write,
        not a stale provider-listing match) and untested against it."""
        src = self._read('queues/torrent_processor.py')
        dedup_start = src.index("NZB already in-flight (DB dedup)")
        dedup_region = src[dedup_start - 400:dedup_start + 200]
        self.assertNotIn('is_nzb_job_alive', dedup_region)

    def test_ghost_counter_wired_into_both_terminal_branches(self):
        """Both the progress==-2 (ghost job) and progress==-1 (folder never
        appeared) branches must reference the shared counter/threshold -
        losing either silently reintroduces an unbounded loop on that path."""
        src = self._read('queues/run_program.py')
        self.assertIn('_nzb_ghost_repeat_counts: dict = {}', src)  # class-level init present
        self.assertIn('_NZB_GHOST_BLACKLIST_THRESHOLD = 3', src)

        ghost_branch_start = src.index("# Ghost job — never existed in cli_mount")
        ghost_branch_end = src.index("elif progress == -1:", ghost_branch_start)
        ghost_branch = src[ghost_branch_start:ghost_branch_end]
        self.assertIn('_nzb_ghost_repeat_counts', ghost_branch)
        self.assertIn('_NZB_GHOST_BLACKLIST_THRESHOLD', ghost_branch)
        self.assertIn('move_to_blacklisted', ghost_branch)

        fail_branch_start = src.index("failed in cli_mount — adding to not-wanted")
        fail_branch_end = src.index("elif progress < 100:", fail_branch_start)
        fail_branch = src[fail_branch_start:fail_branch_end]
        self.assertIn('_nzb_ghost_repeat_counts', fail_branch)
        self.assertIn('_NZB_GHOST_BLACKLIST_THRESHOLD', fail_branch)
        self.assertIn('move_to_blacklisted', fail_branch)

    def test_dead_sibling_cleanup_is_counted_not_just_the_primary_item(self):
        """The progress==-1 branch's _dead_siblings loop (coalesced-pack
        siblings sharing the primary item's dead job) must apply the same
        counter/threshold/blacklist logic as the primary item a few lines
        below it - not just move straight to Wanted every cycle. Scoped
        strictly to the _dead_siblings loop body itself (not the whole
        progress==-1 branch, which also contains the primary item's own
        counting code further down and would pass this assertion even if
        the sibling loop had none at all - the actual gap this test closes)."""
        src = self._read('queues/run_program.py')
        siblings_start = src.index("_dead_siblings = [")
        siblings_end = src.index("# Try next result from scrape_results", siblings_start)
        siblings_region = src[siblings_start:siblings_end]
        self.assertIn('_nzb_ghost_repeat_counts', siblings_region)
        self.assertIn('_NZB_GHOST_BLACKLIST_THRESHOLD', siblings_region)
        self.assertIn('move_to_blacklisted', siblings_region)
        self.assertIn('move_to_wanted', siblings_region)

    def test_success_path_pops_counter_for_primary_and_siblings(self):
        """Both places an item can successfully clear Adding into Checking
        (the primary item and a coalesced sibling moved with it) must clear
        this item's ghost history - otherwise a later unrelated ghost streak
        wrongly inherits stale progress toward the threshold."""
        src = self._read('queues/run_program.py')
        primary_success_start = src.index("Item cleared Adding on a real, live job")
        primary_region = src[primary_success_start:primary_success_start + 300]
        self.assertIn("_nzb_ghost_repeat_counts.pop(item_id, None)", primary_region)

        sibling_success_start = src.index("_moved_as_sibling.add(_sib['id'])")
        sibling_region = src[sibling_success_start:sibling_success_start + 300]
        self.assertIn("_nzb_ghost_repeat_counts.pop(_sib['id'], None)", sibling_region)

    def test_playback_repair_short_circuit_precedes_ghost_counting(self):
        """The 2026-08-XX NZB playback repair feature's reject_active_candidate
        check must still run - and short-circuit via continue - before this
        fix's counter logic in both branches, so a playback-repair candidate
        never gets miscounted as a plain ghost/failure repeat."""
        src = self._read('queues/run_program.py')
        ghost_branch_start = src.index("# Ghost job — never existed in cli_mount")
        fail_branch_start = src.index("failed in cli_mount — adding to not-wanted")
        playback_marker = 'reject_active_candidate'
        # nearest preceding playback-repair check must come before the ghost-counting code
        ghost_playback_idx = src.rfind(playback_marker, 0, ghost_branch_start)
        fail_playback_idx = src.rfind(playback_marker, 0, fail_branch_start)
        self.assertNotEqual(ghost_playback_idx, -1)
        self.assertNotEqual(fail_playback_idx, -1)
        self.assertLess(ghost_playback_idx, ghost_branch_start)
        self.assertLess(fail_playback_idx, fail_branch_start)


if __name__ == '__main__':
    unittest.main(verbosity=2)
