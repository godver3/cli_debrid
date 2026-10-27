"""Releases for a different show that shares a title word are rejected.

With a 0.75 similarity threshold, American Dad! S01 scraping passed American
Woman (0.785), American Gods (0.80), American Auto and American Vandal season
packs: the shared word "American" carries the fuzzy score. American Woman then
ranked first.
"""
import unittest

from PTT import parse_title


VERSION = {"similarity_threshold": 0.75, "similarity_threshold_anime": 0.8, "max_resolution": "1080p",
           "resolution_wanted": "<=", "min_size_gb": 0.01, "max_size_gb": None}


def _result(title, size=8.0):
    return {"title": title, "original_title": title, "size": size, "parsed_info": parse_title(title),
            "scraper_type": "Torrentio", "scraper_instance": "Torrentio_1",
            "magnet": "magnet:?xt=urn:btih:" + "a" * 40}


class DifferentShowWordSwapTests(unittest.TestCase):
    def _passed_titles(self, titles, aliases=None):
        from scraper.functions.filter_results import filter_results
        passed, _ = filter_results([_result(t) for t in titles], "1433", "American Dad!", 2005, "episode",
                                   1, 1, True, VERSION, 22, 7, {1: 7, 2: 16}, ["Animation", "Comedy"],
                                   aliases or [], imdb_id="tt0397306")
        return {r["original_title"] for r in passed}

    def test_other_american_shows_rejected(self):
        titles = ["American Woman S01 1080p AMZN WEBRip DDP5 1 x264-NTb[rartv]",
                  "American Gods S01 1080p BluRay x265-RARBG",
                  "American Auto S01 1080p AMZN WEBRip DDP5 1 x264-NTb",
                  "American Vandal S01 1080p WEBRip x264 EAC3 5 1-Floppy"]
        self.assertEqual(set(), self._passed_titles(titles))

    def test_american_dad_packs_pass(self):
        titles = ["American Dad S01 MULTi 1080p WEB H264-FW", "American.Dad.S01.1080p.WEB-DL.DDP5.1.H.264-GRP"]
        self.assertEqual(set(titles), self._passed_titles(titles))

    def test_alias_with_the_release_words_lets_it_through(self):
        title = "American Woman S01 1080p AMZN WEBRip DDP5 1 x264-NTb"
        self.assertIn(title, self._passed_titles([title], aliases=["American Woman"]))


class SwapHelperTests(unittest.TestCase):
    def test_cases(self):
        from scraper.functions.filter_results import _swaps_a_title_word
        self.assertTrue(_swaps_a_title_word("american.woman", "american.dad"))
        self.assertFalse(_swaps_a_title_word("marvels.daredevil", "daredevil"))  # added word only
        self.assertFalse(_swaps_a_title_word("spider.man.homecoming", "spiderman.homecoming"))
        self.assertFalse(_swaps_a_title_word("the.colour.purple", "the.color.purple"))  # near spelling
        self.assertFalse(_swaps_a_title_word("", "american.dad"))


if __name__ == '__main__':
    unittest.main()
