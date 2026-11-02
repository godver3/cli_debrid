"""Plex mode when Plex is down, stuck or behind, and when a file is bad.

Users reported items sitting in Checking for ages, then the same release being
grabbed again. In Plex mode an item is only collected once Plex adds it, so a
Plex that is slow, stuck or unreachable looked to the Checking queue exactly like
a broken release, and the Checking timeout not-wanted it. When the period runs
out the queue now asks Plex: collect it if Plex has it, hold it while Plex is
down or scanning, and only replace it once an idle Plex has accepted two scans
of its folder and still doesn't have it.

Checking period is 600 s here, the value from the 2026-11-01 report.
"""
import os

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


def test_release_finished_during_outage_is_collected_after_plex_recovers(plex_bench):
    # The scan sent during the outage failed. It used to be marked sent anyway (and
    # logged as "Triggered"), so nothing rescanned the folder after Plex came back
    # and the release was not-wanted ~10 min later.
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.faults.append(Fault('.*', 'drop', until=bench.clock.time() + 3600))
    release = bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(hours=2)

    assert bench.state_of(release) == ['Collected']
    assert bench.logs("Plex didn't take the scan")
    recovered = bench.start_time + 3600
    assert [t for t, _, _ in bench.logs('Plex accepted a scan') if t >= recovered]


def test_bad_file_is_replaced_after_plex_scanned_it_twice(plex_bench):
    # A healthy, idle Plex that scanned the folder and still doesn't have the file
    # rejected it: replace it. It gets one more scan first (Plex drops its scan
    # queue on a restart), so it's replaced one period later than it used to be.
    bench = plex_bench(checking_queue_period=PERIOD)
    release = bench.add_movie('Broken', 2010, 'tt5000001', download_s=60)
    bench.plex.behavior.never_index.add(release.files[0])

    bench.run(hours=1)

    assert bench.state_of(release) == ['Wanted']
    assert bench.usenet.removed  # the abandoned job is removed from the provider
    assert len(bench.plex.scans_completed) == 2
    assert bench.logs('accepted 2 scans of its folder and did not add it')
    wanted_at = next(t for t, s in bench.history[release.item_ids[0]] if s == 'Wanted')
    assert wanted_at - release.submitted_at < 2 * PERIOD + 300
    assert not bench.logs('Plex is still scanning|unreachable')


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


def test_releases_arriving_faster_than_plex_scans_are_not_regrabbed(plex_bench):
    # Used to time out: the old hold only counted releases still waiting ahead of
    # this one, the clock included download time, and recently-added is read at
    # most every 5 min, so a release Plex indexed a minute ago was replaced.
    bench = _plex_four_minutes_a_scan(plex_bench, 8)

    m = bench.metrics()
    assert m['sent_back_to_wanted'] == 0, bench.report()
    assert m['collected'] == 8
    assert m['false_collections'] == 0


def test_no_collected_item_points_at_a_deleted_release(plex_bench):
    # Used to happen: the timeout removed a job Plex had already indexed, then the
    # recent scan added the Plex file back as a new Collected row at a dead path.
    bench = _plex_four_minutes_a_scan(plex_bench, 4)

    ghosts = [(r['id'], r['title'], r['location_on_disk']) for r in bench.items_in('Collected')
              if not (r['location_on_disk'] and os.path.exists(r['location_on_disk']))]
    assert ghosts == []
    assert bench.usenet.removed == []


def test_batch_grabbed_releases_are_held_while_plex_is_behind(plex_bench):
    # NZBs grabbed in one Adding pass enter Checking together. The old hold counted
    # releases "ahead" by Checking entry time, so none was ever ahead and none held.
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.scan_overhead_s = 90
    # Downloads finish 30 s apart, Plex needs 90 s a scan.
    for n in range(30):
        bench.add_movie(f'Movie {n}', 2000 + n % 20, f'tt71000{n:02d}', download_s=60 + n * 30)

    bench.run(hours=3)

    m = bench.metrics()
    assert m['sent_back_to_wanted'] == 0, bench.report()
    assert m['collected'] == 30
    assert m['plex_max_scans_per_folder'] == 1


def test_stuck_scanner_does_not_burn_releases(plex_bench):
    # Plex answers but never finishes a scan: hold while it reports scanning...
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.scanner_stuck = True
    releases = [bench.add_movie(f'Movie {n}', 2010, f'tt72000{n:02d}', download_s=60) for n in range(3)]

    bench.run(hours=2)

    assert [s for r in releases for s in bench.state_of(r)] == ['Checking'] * 3
    assert bench.logs('Plex is still scanning; keeping it in Checking')


def test_stuck_scanner_gives_up_at_the_cap(plex_bench):
    # ...but not forever: 6 h after the download finished, the normal timeout applies.
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.scanner_stuck = True
    release = bench.add_movie('Movie', 2010, 'tt7300001', download_s=60)

    bench.run(hours=7)

    assert bench.state_of(release) == ['Wanted']
    wanted_at = next(t for t, s in bench.history[release.item_ids[0]] if s == 'Wanted')
    assert 6 * 3600 <= wanted_at - release.submitted_at <= 6 * 3600 + 2 * PERIOD


def test_collected_by_file_lookup_when_recently_added_misses_it(plex_bench):
    # Recently-added only lists the newest 200 per library and is read at most every
    # 5 min. When it misses a release Plex has, the timeout used to replace it; now
    # the queue looks the file up in Plex and collects it, episodes included.
    bench = plex_bench(checking_queue_period=PERIOD)
    bench.plex.behavior.faults.append(Fault(r'/recentlyAdded$', 'error'))
    movie = bench.add_movie('Dune', 2021, 'tt1160419', '438631', download_s=60)
    pack = bench.add_episodes('Severance', 2022, 'tt11280740', 1, range(1, 4), tvdb_id='371980', download_s=60)

    bench.run(hours=1)

    assert bench.state_of(movie) == ['Collected']
    assert bench.state_of(pack) == ['Collected'] * 3
    assert bench.logs('Plex already has it; collected')
    rows = bench.rows(movie.item_ids + pack.item_ids)
    assert all(r['location_on_disk'] and os.path.exists(r['location_on_disk']) for r in rows)
    assert sorted(r['episode_number'] for r in rows if r['type'] == 'episode') == [1, 2, 3]
    assert bench.metrics()['sent_back_to_wanted'] == 0
