"""How much work one Checking pass does while finished NZBs wait for Plex.

Finished NZBs stay in Checking until Plex adds them, which with a slow or stuck
Plex is a long time (and longer since the #555 hold). Every pass handles each of
them again, so the cost of a pass has to stay linear in their number.
"""
from tests.plex_bench.fake_plex import Fault


def _rows_read_in_one_pass(plex_bench, monkeypatch, finished):
    """Rows the Checking queue reads from media_items in the pass where ``finished`` jobs are done."""
    import database
    bench = plex_bench(checking_queue_period=3600)
    for n in range(finished):
        bench.add_movie(f'Movie {n}', 2011, f'tt51000{n:03d}', download_s=30)
    bench.run(seconds=40)  # the downloads finish during this
    seen = {'rows': 0}
    for name in ('get_all_media_items', 'get_media_items_by_torrent_id'):
        real = getattr(database, name)

        def counting(*args, _real=real, **kwargs):
            rows = _real(*args, **kwargs)
            seen['rows'] += len(rows)
            return rows

        monkeypatch.setattr(database, name, counting)
    bench._run_task('Checking')
    return seen['rows']


def test_checking_pass_reads_grow_linearly_with_finished_nzbs(plex_bench, monkeypatch):
    # Was N x N: CheckingQueue.process re-read every Checking row for each finished
    # NZB, so 80 finished NZBs read 6,480 rows a pass and ~2,600 (the 2026-11-01
    # report) ~6.8 million.
    rows = _rows_read_in_one_pass(plex_bench, monkeypatch, 40)
    assert rows <= 4 * 40, f'{rows} rows read in one pass for 40 finished NZBs'


def test_scan_requests_started_per_pass_are_capped(plex_bench):
    # Each scan request runs in its own thread. Jobs retry a scan Plex refused, so
    # without a cap a long outage with thousands waiting starts thousands of threads.
    bench = plex_bench(checking_queue_period=3600)
    cap = bench.checking.PLEX_SCANS_PER_PASS
    for n in range(cap + 10):
        bench.add_movie(f'Movie {n}', 2011, f'tt52000{n:03d}', download_s=30)
    bench.run(seconds=35)  # passes at +1 s (downloading) and +31 s (all finished)
    assert len(bench.plex.scans_requested) == cap
    bench.run(seconds=30)  # the rest go out on the next pass
    assert len(bench.plex.scans_requested) == cap + 10


def test_expired_releases_decided_per_pass_are_capped(plex_bench):
    # Each expired release asks Plex about its files. Many expiring at once must not
    # turn one Checking pass into hundreds of Plex requests.
    bench = plex_bench(checking_queue_period=600)
    cap = bench.checking.PLEX_VERDICTS_PER_PASS
    bench.plex.behavior.faults.append(Fault(r'/recentlyAdded$', 'error'))  # only the lookup collects
    for n in range(cap + 10):
        bench.add_movie(f'Movie {n}', 2011, f'tt53000{n:03d}', download_s=30)
    bench.run(minutes=12)  # all finished at +31 s; the period runs out just past +631 s
    first = [t for t, _, _ in bench.logs('Plex already has it; collected')]
    assert len(first) == cap + 10
    assert len([t for t in first if t == min(first)]) == cap
