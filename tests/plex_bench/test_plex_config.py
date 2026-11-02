"""Plex-mode setups that look fine in Settings but never collect."""
import pytest


def test_plex_mode_scans_with_blank_symlink_plex_url(plex_bench):
    # plex_update_item used to read only the *_for_symlink fields, so a Plex-mode
    # config without them never asked Plex to scan and every NZB timed out.
    bench = plex_bench(checking_queue_period=600, sync_symlink_plex_url=False)
    release = bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(hours=1)

    assert bench.state_of(release) == ['Collected']


@pytest.mark.xfail(strict=True, reason=(
    "Bug: task_check_plex_files (Plex.update_plex_on_file_discovery) only looks for "
    "<mounted_file_location>/<release>/<file>. cli_mount and nzbdav put jobs under a category "
    "folder (<mount>/<category>/<release>), so the file is never found and the task does nothing."))
def test_file_discovery_finds_files_in_category_folders(plex_bench):
    bench = plex_bench(checking_queue_period=3600, plex_settings={'update_plex_on_file_discovery': True})
    bench.add_movie('Dune', 2021, 'tt1160419', download_s=60)

    bench.run(minutes=10)

    assert bench.logs(r'Plex Check \(checks enabled\): Found file')
