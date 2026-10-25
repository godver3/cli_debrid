"""'Dragon Ball Z Kai' releases are Dragon Ball Kai, not Dragon Ball Z.

Most English releases of Kai are titled "Dragon Ball Z Kai". The series check
only knew "dragon ball kai", so those titles matched the Z identifiers and were
rejected as a series mismatch, and the PTT override rewrote them to
"dragon.ball.z". Seen as a manual S01E84 search that returned nothing.
"""
import unittest

from PTT import parse_title


VERSION = {"similarity_threshold": 0.85, "similarity_threshold_anime": 0.8, "max_resolution": "1080p",
           "resolution_wanted": "<=", "min_size_gb": 0.01, "max_size_gb": None}


def _result(title, size=1.2):
    return {"title": title, "original_title": title, "size": size, "parsed_info": parse_title(title),
            "scraper_type": "Torrentio", "scraper_instance": "Torrentio_1",
            "magnet": "magnet:?xt=urn:btih:" + "a" * 40}


class DragonBallZKaiFilterTests(unittest.TestCase):
    def _passed_titles(self, titles):
        from scraper.functions.filter_results import filter_results
        passed, _ = filter_results([_result(t) for t in titles], "61709", "Dragon Ball Kai", 2009, "episode",
                                   1, 84, False, VERSION, 24, 98, {1: 98, 2: 61}, ["Animation", "Anime"],
                                   imdb_id="tt1409055")
        return {r["original_title"] for r in passed}

    def test_z_kai_release_passes(self):
        title = "Dragon.Ball.Z.Kai.S01E84.1080p.BluRay.x264-GRP"
        self.assertIn(title, self._passed_titles([title]))

    def test_plain_dbz_still_rejected(self):
        title = "Dragon.Ball.Z.S01E84.1080p.BluRay.x264-GRP"
        self.assertNotIn(title, self._passed_titles([title]))

    def test_override_keeps_kai(self):
        from scraper.functions.file_processing import _parse_with_ptt
        parsed = _parse_with_ptt("[Anime Time] Dragon Ball Z Kai Complete Series (Season 01 to 07) [BD] [Dual Audio] [1080p]")
        self.assertIn("kai", parsed.get("title", "").lower())


if __name__ == '__main__':
    unittest.main()
