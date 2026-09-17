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


def test_pick_best_video_file_bare_episode_number_no_sxxexx_marker():
    # Fansub/anime releases in a shared season-pack folder often have no SxxExx
    # marker at all - just "Title - NN [tags].mkv". Without matching on the bare
    # number, every episode request falls through to "largest file in the whole
    # folder" and every episode silently resolves to the same file (godver3/
    # cli_debrid live report: every episode of a 24-episode pack got episode 14's
    # file, the single largest, because none of the 24 filenames ever matched the
    # strict S01E14-style pattern).
    files = [
        ("[sam] Vinland Saga - 01 [BD 1080p FLAC] [B78ACE46].mkv", int(3.6 * GIB)),
        ("[sam] Vinland Saga - 02 [BD 1080p FLAC] [509A2045].mkv", int(3.8 * GIB)),
        ("[sam] Vinland Saga - 14 [BD 1080p FLAC] [2B34C6C2].mkv", int(4.3 * GIB)),
    ]
    best = pick_best_video_file(files, season=1, episode=1)
    assert best is not None
    assert best[0].endswith("- 01 [BD 1080p FLAC] [B78ACE46].mkv")

    best_ep14 = pick_best_video_file(files, season=1, episode=14)
    assert best_ep14 is not None
    assert best_ep14[0].endswith("- 14 [BD 1080p FLAC] [2B34C6C2].mkv")


def test_pick_best_video_file_bare_episode_number_zero_padding_variants():
    files = [
        ("Show - 1 [tag].mkv", int(1 * GIB)),
        ("Show - 01 [tag].mkv", int(1.1 * GIB)),
        ("Show - 010 [tag].mkv", int(1.2 * GIB)),  # a different episode (10), must not match ep=1
    ]
    best = pick_best_video_file(files[:2], season=1, episode=1)
    assert best is not None
    assert best[0] == "Show - 01 [tag].mkv"  # largest among the two genuine ep-1 candidates

    best_ep10 = pick_best_video_file(files, season=1, episode=10)
    assert best_ep10 is not None
    assert best_ep10[0] == "Show - 010 [tag].mkv"


def test_pick_best_video_file_bare_episode_number_does_not_match_resolution():
    # "1080p" must never be mistaken for a bare episode "1" - the larger, wrong
    # file would otherwise win purely because it's bigger, not because it's the
    # requested episode.
    files = [
        ("Show - 5 [1080p].mkv", int(5 * GIB)),   # true episode 5, decoy leading "1" inside "1080p"
        ("Show - 1 [720p].mkv", int(1 * GIB)),    # true episode 1, smaller
    ]
    best = pick_best_video_file(files, season=1, episode=1)
    assert best is not None
    assert best[0] == "Show - 1 [720p].mkv"
