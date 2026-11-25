"""Symlink-mode multi-file scan: a release's sample is not another version.

The built-in mount engine keeps "<release>.sample.mkv" next to the episode in the
job folder. The sample parses as the same SxxEyy, so the multi-file scan in
check_local_file_for_item added it as a second Collected row with its own library
symlink (Marshals S02E07, Tulsa King S04E02/E04, ... from 2026-11-21 on).
"""
from tests.usenet.test_prefer_largest_nzb_source import lls  # noqa: F401  (shared fixture)

FOLDER = "/mnt/__all__/Marshals.S02E07.1080p.WEB.h264-ETHEL"


def _paths(*names):
    return [f"{FOLDER}/{n}" for n in names]


def test_samples_and_trailers_are_not_additional_versions(lls):
    primary = f"{FOLDER}/Marshals.S02E07.1080p.WEB.h264-ETHEL.mkv"
    found = [primary] + _paths(
        "marshals.s02e07.1080p.web.h264-ethel.sample.mkv",
        "tulsa.king.s04e02.german.dl.1080p.web.h264-wayne-sample.mkv",
        "Marshals.S02E07.Trailer.mkv",
        "Marshals.S02E07.1080p.WEB.h264-ETHEL.Proper.mkv",
    )
    assert lls._additional_video_files(found, primary) == _paths("Marshals.S02E07.1080p.WEB.h264-ETHEL.Proper.mkv")


def test_primary_never_filtered_even_when_named_like_a_sample(lls):
    # A show literally called "Sample": the primary file stays the item's file,
    # and only the folder's other videos are judged.
    primary = f"{FOLDER}/Sample.S01E01.1080p.mkv"
    assert lls._additional_video_files([primary], primary) == []
    other = f"{FOLDER}/Sample.S01E01.720p.mkv"
    assert lls._additional_video_files([primary, other], primary) == []  # documented false positive: skipped, not added


def test_titles_containing_sample_as_part_of_a_word_are_kept(lls):
    primary = f"{FOLDER}/The.Sampler.2020.1080p.mkv"
    other = f"{FOLDER}/The.Sampler.2020.720p.mkv"
    assert lls._additional_video_files([primary, other], primary) == [other]


def test_real_trailer_park_boys_names_from_the_2026_11_23_add(lls):
    # A show with "trailer" in its title: its real files must stay, both sample
    # naming styles (".sample.mkv" and a "sample-" prefix) must go.
    folder = "/mnt/__all__/trailer.park.boys.s10e10.1080p.webrip.x264-deflate"
    primary = f"{folder}/trailer.park.boys.s10e10.1080p.webrip.x264-deflate.mkv"
    found = [primary,
             f"{folder}/trailer.park.boys.s10e10.1080p.webrip.x264-deflate.sample.mkv",
             f"{folder}/sample-trailer.park.boys.s11e10.1080p.webrip.x264-morose.mkv",
             f"{folder}/Trailer.Park.Boys.S10E10.1080p.WEBRip.x264-DEFLATE.REPACK.mkv"]
    assert lls._additional_video_files(found, primary) == [found[3]]
