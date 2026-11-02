"""How much work one Checking pass does while finished NZBs wait for Plex.

Finished NZBs stay in Checking until Plex adds them, which with a slow or stuck
Plex is a long time (and longer since the #555 hold). Every pass handles each of
them again, so the cost of a pass has to stay linear in their number.
"""
import pytest


def _reads_in_one_pass(plex_bench, monkeypatch, finished):
    import database
    bench = plex_bench(checking_queue_period=3600)
    for n in range(finished):
        bench.add_movie(f'Movie {n}', 2011, f'tt51000{n:03d}', download_s=30)
    bench.run(seconds=40)  # the downloads finish during this
    seen = {'calls': 0, 'rows': 0}
    real = database.get_all_media_items

    def counting(*args, **kwargs):
        rows = real(*args, **kwargs)
        seen['calls'] += 1
        seen['rows'] += len(rows)
        return rows

    monkeypatch.setattr(database, 'get_all_media_items', counting)
    bench._run_task('Checking')
    return seen['rows']


@pytest.mark.xfail(strict=True, reason=(
    "Bug: CheckingQueue.process re-reads every Checking row from the database for each "
    "finished NZB (checking_queue.py, 'Reload items from DB' after _resolve_nzb_file_info), "
    "so a pass reads N x N rows: 80 finished NZBs read 6,480 rows, ~2,600 (the 2026-11-01 "
    "report) read ~6.8 million per pass, every pass."))
def test_checking_pass_reads_grow_linearly_with_finished_nzbs(plex_bench, monkeypatch):
    rows = _reads_in_one_pass(plex_bench, monkeypatch, 40)
    assert rows <= 4 * 40, f'{rows} rows read in one pass for 40 finished NZBs'
