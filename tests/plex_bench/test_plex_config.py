"""Plex-mode setups that look fine in Settings but never collect."""


def test_plex_mode_scans_with_blank_symlink_plex_url(plex_bench):
    # plex_update_item used to read only the *_for_symlink fields, so a Plex-mode
    # config without them never asked Plex to scan and every NZB timed out.
    bench = plex_bench(checking_queue_period=600, sync_symlink_plex_url=False)
    release = bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(hours=1)

    assert bench.state_of(release) == ['Collected']


def test_file_discovery_finds_files_in_category_folders(plex_bench):
    # task_check_plex_files only looked for <mount>/<release>/<file>; cli_mount and
    # nzbdav keep jobs in <mount>/<category>/<release>/, so it never found anything.
    bench = plex_bench(checking_queue_period=3600, plex_settings={'update_plex_on_file_discovery': True})
    release = bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(minutes=10)

    assert bench.logs(r'Plex Check \(checks enabled\): Found file')
    assert bench.logs(r'\[PlexCheck\] Marking item .* as Collected: file indexed in Plex confirmed')
    assert bench.state_of(release) == ['Collected']


def test_file_discovery_never_collects_what_plex_has_not_added(plex_bench):
    # It used to mark items Collected after 3 runs or 30 min with the file on disk
    # but not in Plex: Plex showed nothing, cli_debrid showed it collected.
    bench = plex_bench(checking_queue_period=3600, plex_settings={'update_plex_on_file_discovery': True})
    bench.plex.behavior.scanner_stuck = True
    release = bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(hours=2)

    assert bench.state_of(release) == ['Checking']
    assert bench.false_collections() == []
