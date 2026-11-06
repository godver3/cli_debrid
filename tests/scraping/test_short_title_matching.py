"""Short movie titles require an exact parsed-title match, not perfect fuzziness."""

import unittest

from PTT import parse_title

from scraper.functions.filter_results import filter_results


VERSION = {
    "similarity_threshold": 0.8,
    "similarity_threshold_anime": 0.8,
    "max_resolution": "2160p",
    "resolution_wanted": "<=",
    "min_size_gb": 0.01,
    "max_size_gb": None,
}


def _result(release):
    return {
        "title": release,
        "original_title": release,
        "size": 8.0,
        "parsed_info": parse_title(release),
        "scraper_type": "Torrentio",
        "scraper_instance": "Torrentio_1",
        "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
    }


def _passes(query, year, release):
    passed, _ = filter_results(
        [_result(release)], None, query, year, "movie", None, None, False,
        VERSION, 120, 0, {}, ["Drama"], [], imdb_id=None,
    )
    return bool(passed)


class ShortTitleMatchingTests(unittest.TestCase):
    def test_separator_collapsing_is_limited_to_short_titles(self):
        from scraper.functions.filter_results import _same_exact_title

        self.assertTrue(_same_exact_title("wall-e", "walle"))
        self.assertTrue(_same_exact_title("spider.man", "spider.man"))
        self.assertFalse(_same_exact_title("spider.man", "spiderman"))
        self.assertFalse(_same_exact_title("the.rapist", "therapist"))

    def test_exact_short_titles_pass_despite_release_metadata(self):
        cases = [
            ("Up", 2009, "Up.2009.1080p.BluRay.x264-GROUP"),
            ("Her", 2013, "Her.2013.1080p.BluRay.x264-GROUP"),
            ("RRR", 2022, "RRR.2022.1080p.BluRay.x264-GROUP"),
            ("1917", 2019, "1917.2019.1080p.BluRay.x264-GROUP"),
        ]
        for query, year, release in cases:
            with self.subTest(query=query, release=release):
                self.assertTrue(_passes(query, year, release))

    def test_stylised_punctuation_matches_release_separator(self):
        for release in (
            "Wall-E.2008.1080p.BluRay.x264-GROUP",
            "WALL.E.2008.1080p.BluRay.x264-GROUP",
        ):
            with self.subTest(release=release):
                self.assertTrue(_passes("WALL·E", 2008, release))

    def test_short_title_is_not_a_substring_or_prefix_match(self):
        cases = [
            ("Up", 2009, "Up.in.the.Air.2009.1080p.BluRay.x264-GROUP"),
            ("Her", 2013, "Her.Smell.2013.1080p.BluRay.x264-GROUP"),
            ("RRR", 2022, "RRRrrrr.2022.1080p.BluRay.x264-GROUP"),
            ("1917", 2019, "1917.Hours.2019.1080p.BluRay.x264-GROUP"),
            ("WALL·E", 2008, "Wall.Street.2008.1080p.BluRay.x264-GROUP"),
        ]
        for query, year, release in cases:
            with self.subTest(query=query, release=release):
                self.assertFalse(_passes(query, year, release))


if __name__ == "__main__":
    unittest.main()
