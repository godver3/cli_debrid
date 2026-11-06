"""A UFC-titled result must not relax the title-similarity threshold for the rest
of the batch.

filter_results used to set similarity_threshold = 0.35 (and is_ufc) as soon as
any result's title contained "UFC", and never reset it, so every later result in
the same batch was compared against 0.35. Live: an indexer's "every S01E05"
answer to a South Park search contained a UFC vlog episode, and other shows'
S01E05 releases after it were accepted - a South Park item downloaded
The.Penguin.S01E05.

Runs the real filter_results, so it needs the app's dependencies (fuzzywuzzy,
PTT, ...); it skips where they aren't installed.
"""
import pytest

pytest.importorskip("fuzzywuzzy")
pytest.importorskip("PTT")


@pytest.fixture
def filter_results(monkeypatch, tmp_path):
    for var in ("USER_CONFIG", "USER_DB_CONTENT", "USER_LOGS"):
        (tmp_path / var).mkdir()
        monkeypatch.setenv(var, str(tmp_path / var))
    try:
        from scraper.functions.filter_results import filter_results as fn
    except Exception as exc:  # pragma: no cover - partial sandbox installs
        pytest.skip(f"filter_results import failed: {exc}")
    return fn


def _results(*titles):
    from PTT import parse_title
    out = []
    for t in titles:
        info = parse_title(t)
        info["original_title"] = t
        out.append({"title": t, "original_title": t, "size": 1.5, "parsed_info": info,
                    "scraper_type": "Newznab", "scraper_instance": "NZBPlanet_1",
                    "nzb_url": f"https://indexer.test/{t}", "protocol": "nzb"})
    return out


VERSION = {"similarity_threshold": 0.85, "similarity_threshold_anime": 0.8, "max_resolution": "2160p",
           "resolution_wanted": "<=", "min_size_gb": 0.01, "max_size_gb": None,
           "min_bitrate_mbps": 0.01, "max_bitrate_mbps": None}


def _passed_titles(filter_results, titles, query="South Park"):
    passed, _ = filter_results(_results(*titles), "2190", query, 1997, "episode", 1, 5, False, VERSION,
                               22, 1, {1: 13}, ["Comedy", "Animation"], imdb_id="tt0121955")
    return [r["title"] for r in passed]


def test_ufc_result_does_not_relax_threshold_for_later_results(filter_results):
    passed = _passed_titles(filter_results, [
        "UFC.331.Embedded-Vlog.Series-Episode.5.720p.WEB.H.264-JFF",
        "South.Pacific.S01E05.1080p.WEB-DL.DDP5.1.H.264-NTb",   # ~0.4-0.8: only passes at 0.35
        "South.Park.S01E05.720p.BluRay.DD5.1.x264-DON",
    ])
    assert passed == ["South.Park.S01E05.720p.BluRay.DD5.1.x264-DON"]


def test_near_miss_rejected_without_ufc_too(filter_results):
    passed = _passed_titles(filter_results, [
        "South.Pacific.S01E05.1080p.WEB-DL.DDP5.1.H.264-NTb",
        "South.Park.S01E05.720p.BluRay.DD5.1.x264-DON",
    ])
    assert passed == ["South.Park.S01E05.720p.BluRay.DD5.1.x264-DON"]
