import pytest

from debrid.common.utils import is_unwanted_file
from utilities.junk_symlink_audit import classify_junk


@pytest.mark.parametrize(
    "name",
    [
        "movie.trailer.mkv",
        "Show.S01E01.trailer.1080p.mkv",
        "Show.S01E01.sample.mkv",
        "Release-sample.mkv",
        "foo.trailer.720p.web-dl.mkv",
        "sample.mkv",
        "[TRAILER].mp4",
        "(sample).avi",
        "Movie.2020-Sample-1080p.mkv",
    ],
)
def test_is_unwanted_file_detects_real_extras(name):
    assert is_unwanted_file(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Bar.Rescue.S04E03.Punch.Drunk.Trailer.Trashed.1080p.WEB-DL.AAC2.0.h.264-NTb.mkv",
        "Trailer.Park.Boys.S01E01.1080p.mkv",
        "Show.S01E08.1080p-GROUP - 02.mkv",
        "Show.S14E01.German.DL.1080p.WEB.h264-WvF.mkv",
        "downsampled.release.1080p.mkv",
    ],
)
def test_is_unwanted_file_ignores_title_words_and_non_junk_names(name):
    assert is_unwanted_file(name) is False


def test_classify_junk_ignores_bar_rescue_episode_title():
    path = (
        "/mnt/__all__/Bar.Rescue.S04E03.Punch.Drunk.Trailer.Trashed.1080p.WEB-DL/"
        "Bar.Rescue.S04E03.Punch.Drunk.Trailer.Trashed.1080p.WEB-DL.AAC2.0.h.264-NTb.mkv"
    )
    size = int(1541.8 * 1024 * 1024)
    assert classify_junk(path, size, "episode", None, 200 * 1024 * 1024, 300 * 1024 * 1024) is None


def test_classify_junk_flags_real_trailer_extra():
    path = "/mnt/__all__/Show.S01E01.trailer/Show.S01E01.trailer.1080p.mkv"
    size = int(40 * 1024 * 1024)
    assert classify_junk(path, size, "episode", None, 200 * 1024 * 1024, 300 * 1024 * 1024) == "sample/trailer name"
