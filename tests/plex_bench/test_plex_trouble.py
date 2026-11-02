"""Plex mode when Plex is down, stuck or behind, and when a file is bad.

Users report items sitting in Checking for ages, then the same release being
grabbed again. In Plex mode an item is only collected once Plex adds it, so a
Plex that is slow, stuck or unreachable looks to the Checking queue exactly like
a broken release, and the Checking timeout not-wants it. These scenarios pin
down which of those cases cli_debrid tells apart (the stuck-Plex hold from
#555) and which it still doesn't (xfail, with the reason).

Checking period is 600 s here, the value from the 2026-11-01 report.
"""
import os

import pytest

from tests.plex_bench.fake_plex import Fault

PERIOD = 600


def _wanted_during(bench, item_ids, start, end):
    """Item ids that went back to Wanted between two simulated times."""
    return [i for i in item_ids for t, s in bench.history[i] if s == 'Wanted' and start <= t < end]


def test_plex_down_keeps_finished_releases_in_checking(plex_bench):
    bench = plex_bench(checking_queue_period=PERIOD)
    outage_end = bench.clock.time() + 3 * 3600
    bench.plex.behavior.faults.append(Fault('.*', 'drop', until=outage_end))
    releases = [bench.add_movie(f'Movie {n}', 2010, f'tt60000{n:02d}', download_s=60) for n in range(3)]
    ids = [i for r in releases for i in r.item_ids]

    bench.run(hours=3)

    assert _wanted_during(bench, ids, bench.start_time, outage_end) == []
    assert bench.logs('Plex is unreachable or timing out; keeping it in Checking')
    assert bench.metrics()['not_wanted_entries'] == 0


@pytest.mark.xfail(strict=True, reason=(
    "Bug: the Checking queue's targeted Plex scan is sent once per job (_nzb_plex_scan_triggered) "
    "and is marked sent even when Plex refused it (plex_update_item returns False, the log still "
    "says 'Triggered'). After an outage nothing rescans the folder, Plex never adds it, and once "
    "the hold lets go the release is not-wanted ~10 min after Plex comes back."))
def test_release_finished_during_outage_is_collected_after_plex_recovers(plex_bench):
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.faults.append(Fault('.*', 'drop', until=bench.clock.time() + 3600))
    release = bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(hours=2)

    assert bench.state_of(release) == ['Collected']


def test_bad_file_still_times_out_when_plex_is_healthy(plex_bench):
    # The hold must not shield a release Plex looked at and rejected.
    bench = plex_bench(checking_queue_period=PERIOD)
    release = bench.add_movie('Broken', 2010, 'tt5000001', download_s=60)
    bench.plex.behavior.never_index.add(release.files[0])

    bench.run(hours=1)

    assert bench.state_of(release) == ['Wanted']
    assert bench.usenet.removed  # the abandoned job is removed from the provider
    assert bench.plex.scans_completed  # Plex did scan it
    assert not bench.logs('keeping it in Checking')


def _plex_four_minutes_a_scan(plex_bench, count):
    # Releases reach Checking two minutes apart; Plex needs four minutes a scan,
    # so its queue grows by one every four minutes.
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.scan_overhead_s = 240
    for n in range(count):
        bench.add_movie(f'Movie {n}', 2000 + n, f'tt70000{n:02d}', download_s=60)
        bench.run(minutes=2)
    bench.run(hours=1)
    return bench


@pytest.mark.xfail(strict=True, reason=(
    "Bug: the stuck-Plex hold only counts releases still in Checking ahead of this one. Once "
    "the earlier ones are collected that count is 0, so a Plex a few scans behind isn't seen "
    "as behind. The timeout also runs from entering Checking (download time included), and "
    "recently-added is read at most every 5 min, so a release Plex indexed 1-2 min ago is "
    "not-wanted anyway (the 3rd release here: indexed at ~13 min, timed out at 14.5 min)."))
def test_releases_arriving_faster_than_plex_scans_are_not_regrabbed(plex_bench):
    bench = _plex_four_minutes_a_scan(plex_bench, 8)

    m = bench.metrics()
    assert m['sent_back_to_wanted'] == 0, bench.report()
    assert m['collected'] == 8


@pytest.mark.xfail(strict=True, reason=(
    "Bug: when the Checking timeout fires for a release Plex has already indexed, the job is "
    "removed from the provider (its files leave the mount) and the item goes to Wanted, then the "
    "next recently-added pass finds the file in Plex and adds it as a new Collected row. The "
    "library shows the item as collected, with no torrent id, at a path that no longer exists."))
def test_no_collected_item_points_at_a_deleted_release(plex_bench):
    bench = _plex_four_minutes_a_scan(plex_bench, 4)

    ghosts = [(r['id'], r['title'], r['location_on_disk']) for r in bench.items_in('Collected')
              if not (r['location_on_disk'] and os.path.exists(r['location_on_disk']))]
    assert ghosts == []


@pytest.mark.xfail(strict=True, reason=(
    "Bug: the stuck-Plex hold counts releases 'ahead' by when they entered Checking "
    "(checking_queue_times), not when they finished downloading. NZBs grabbed in the same "
    "batch enter Checking together, so none is ahead of another, waiting_ahead is 0, and "
    "a Plex that is reachable but far behind is never detected: every release past Plex's "
    "progress is not-wanted at the Checking period and grabbed again."))
def test_batch_grabbed_releases_are_held_while_plex_is_behind(plex_bench):
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.scan_overhead_s = 90
    # One batch from the Adding queue: downloads finish 30 s apart, Plex needs 90 s a scan.
    for n in range(30):
        bench.add_movie(f'Movie {n}', 2000 + n % 20, f'tt71000{n:02d}', download_s=60 + n * 30)

    bench.run(hours=3)

    m = bench.metrics()
    assert m['sent_back_to_wanted'] == 0, bench.report()
    assert m['collected'] == 30


@pytest.mark.xfail(strict=True, reason=(
    "Bug: a Plex that answers requests but whose scanner never finishes isn't treated as "
    "stuck (no contact failure; nothing ahead in the queue - see the batch test). Releases are "
    "not-wanted at the Checking period while Plex holds them in its scan queue."))
def test_stuck_scanner_does_not_burn_releases(plex_bench):
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.scanner_stuck = True
    releases = [bench.add_movie(f'Movie {n}', 2010, f'tt72000{n:02d}', download_s=60) for n in range(3)]

    bench.run(hours=2)

    assert [s for r in releases for s in bench.state_of(r)] == ['Checking'] * 3
