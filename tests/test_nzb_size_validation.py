import json

from usenet.nzb_size_validation import (
    advertised_size_bytes,
    individual_nzb_size_mismatch,
)


GIB = 1024 ** 3


def _item(media_type="episode", title="Show.S10E21.1080p-WDC", size=3.93):
    url = "https://indexer.invalid/get/bad"
    return {
        "type": media_type,
        "filled_by_magnet": url,
        "scrape_results": json.dumps([
            {"title": title, "nzb_url": url, "size": size},
            {"title": title, "nzb_url": "https://indexer.invalid/get/other", "size": 5.92},
        ]),
    }


def test_advertised_size_prefers_selected_url_over_duplicate_title():
    assert advertised_size_bytes(_item(), "Show.S10E21.1080p-WDC") == int(3.93 * GIB)


def test_rejects_extreme_individual_episode_mismatch():
    mismatch = individual_nzb_size_mismatch(
        _item(), "Show.S10E21.1080p-WDC", 70 * 1024 ** 2
    )
    assert mismatch is not None
    assert mismatch[2] < 0.02


def test_accepts_normal_archive_overhead_difference():
    assert individual_nzb_size_mismatch(
        _item(), "Show.S10E21.1080p-WDC", int(3.1 * GIB)
    ) is None


def test_skips_episode_pack_without_individual_episode_marker():
    assert individual_nzb_size_mismatch(
        _item(), "Show.S10.1080p-WDC", 70 * 1024 ** 2
    ) is None


def test_skips_unknown_or_invalid_sizes():
    item = _item(size=None)
    assert individual_nzb_size_mismatch(item, "Show.S10E21.1080p-WDC", 70 * 1024 ** 2) is None
    assert individual_nzb_size_mismatch(_item(), "Show.S10E21.1080p-WDC", None) is None


def test_checks_movies_without_episode_marker():
    mismatch = individual_nzb_size_mismatch(
        _item(media_type="movie", title="Movie.2026.1080p-WDC", size=4.0),
        "Movie.2026.1080p-WDC",
        80 * 1024 ** 2,
    )
    assert mismatch is not None
