from debrid.common.utils import filter_unwanted_video_files, pick_best_video_file

GIB = 1024 ** 3


def test_pick_best_video_file_prefers_largest_episode_match_over_first_split():
    files = [
        ("Show.S01E08.1080p-GROUP - 01.mkv", int(43 * 1024 ** 2)),
        ("Show.S01E08.1080p-GROUP - 02.mkv", int(4.3 * GIB)),
        ("Show.S01E08.1080p-GROUP - 03.mkv", int(64 * 1024 ** 2)),
    ]
    best = pick_best_video_file(files, season=1, episode=8)
    assert best is not None
    assert best[0].endswith("- 02.mkv")


def test_pick_best_video_file_drops_tiny_splits_after_relative_filter():
    files = [
        ("Show.S01E08.1080p-GROUP - 01.mkv", int(43 * 1024 ** 2)),
        ("Show.S01E08.1080p-GROUP - 02.mkv", int(4.3 * GIB)),
        ("Show.S01E08.1080p-GROUP - 03.mkv", int(64 * 1024 ** 2)),
    ]
    filtered = filter_unwanted_video_files(files)
    assert len(filtered) == 1
    assert filtered[0][0].endswith("- 02.mkv")


def test_pick_best_video_file_single_scam_file_stays_selected():
    files = [("Show.S14E01.German.DL.1080p.WEB.h264-WvF.mkv", int(80 * 1024 ** 2))]
    best = pick_best_video_file(files, season=14, episode=1)
    assert best == files[0]
