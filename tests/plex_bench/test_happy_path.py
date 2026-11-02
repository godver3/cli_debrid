"""Plex mode with a healthy Plex: NZB finishes, Plex scans its folder once, item collected."""
import os


def _healthy(bench):
    m = bench.metrics()
    assert m['outbound_blocked'] == 0, bench.net.blocked
    assert m['plex_unhandled_endpoints'] == []
    assert m['task_errors'] == 0, bench.task_errors
    assert m['false_collections'] == 0
    assert m['sent_back_to_wanted'] == 0
    return m


def test_movie_and_episode_are_collected_after_one_scan_each(plex_bench):
    bench = plex_bench()
    movie = bench.add_movie('Dune', 2021, 'tt1160419', '438631', download_s=120)
    episode = bench.add_episodes('Severance', 2022, 'tt11280740', 1, [1], tvdb_id='371980', download_s=300)

    bench.run_until_settled(max_hours=2)

    m = _healthy(bench)
    assert bench.state_of(movie) == ['Collected']
    assert bench.state_of(episode) == ['Collected']
    assert m['plex_max_scans_per_folder'] == 1
    # Collected within the first recently-added pass after the download (5 min debounce).
    assert m['time_to_collect_s']['max_min'] <= 12
    row = bench.rows(movie.item_ids)[0]
    assert row['location_on_disk'] == f'{bench.mount}/movies/{movie.name}/{movie.files[0]}'
    assert row['ms_item_id']


def test_season_pack_is_one_scan_and_every_episode_collected(plex_bench):
    bench = plex_bench()
    pack = bench.add_episodes('Severance', 2022, 'tt11280740', 1, range(1, 10), tvdb_id='371980', download_s=300)

    bench.run_until_settled(max_hours=2)

    m = _healthy(bench)
    assert bench.state_of(pack) == ['Collected'] * 9
    assert m['plex_scans_requested'] == 1
    episodes = sorted((r['episode_number'], os.path.basename(r['location_on_disk'])) for r in bench.rows(pack.item_ids))
    assert episodes == [(n, f'Severance.S01E{n:02d}.1080p.WEB-DL.H264-BENCH.mkv') for n in range(1, 10)]

