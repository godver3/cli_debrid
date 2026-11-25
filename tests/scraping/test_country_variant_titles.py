"""Country versions PTT doesn't recognise keep other versions' releases out.

Scraping "The Traitors (IN)" S01E01 passed The Traitors UK and NZ S01E01: PTT
only knows US/UK/AU/NZ/CA as country tags, so the "(IN)" on the show's title
never turned on country filtering, and "The.Traitors.India" releases carried
no country either.
"""
import hashlib
import unittest

from PTT import parse_title


VERSION = {"similarity_threshold": 0.85, "similarity_threshold_anime": 0.8, "max_resolution": "2160p",
           "resolution_wanted": "<=", "min_size_gb": 0.01, "max_size_gb": None}


def _result(title, size=2.0):
    return {"title": title, "original_title": title, "size": size, "parsed_info": parse_title(title),
            "scraper_type": "Torrentio", "scraper_instance": "Torrentio_1",
            "magnet": "magnet:?xt=urn:btih:" + hashlib.sha1(title.encode()).hexdigest()}


INDIA = ["The.Traitors.India.S01E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-Dus",
         "The.Traitors.IN.S01E01.720p.WEB.H264-JFF"]
OTHERS = ["The Traitors UK S01E01 1080p WEB-DL AAC2 0 H264-BTN mkv",
          "the traitors nz s01e01 1080p web h264-cbfm[eztv re] mkv",
          "The Traitors US S01E01 1080p WEB h264-ETHEL"]
UNTAGGED = "The Traitors S01E01 Episode 1 1080p AMZN WEB-DL Hindi DDP5 1 H 264-themoviesboss mkv"
UNTAGGED_ENGLISH = "The Traitors S01E01 1080p WEB H264-BUSSY[eztv re] mkv"


class CountryVariantTitleTests(unittest.TestCase):
    def _passed(self, title, titles):
        from scraper.functions.filter_results import filter_results
        passed, _ = filter_results([_result(t) for t in titles], "271489", title, 2025, "episode",
                                   1, 1, False, VERSION, 60, 10, {1: 10}, ["Reality"], [],
                                   imdb_id="tt33347879")
        return passed

    def test_india_version_rejects_other_countries(self):
        passed = self._passed("The Traitors (IN)", INDIA + OTHERS + [UNTAGGED, UNTAGGED_ENGLISH])
        titles = {r["original_title"] for r in passed}
        self.assertEqual(set(INDIA) | {UNTAGGED}, titles)
        by_title = {r["original_title"]: r for r in passed}
        for t in INDIA:
            self.assertNotIn("language_code_missing_penalty", by_title[t])
        self.assertEqual(-500, by_title[UNTAGGED]["language_code_missing_penalty"])

    def test_untagged_release_the_indexer_files_under_this_show_passes(self):
        """The Traitors (IN) S02 packs are named plain "The.Traitors.S02...":
        the indexer's own IMDb id for the NZB (extended=1) says which show it
        is. The same untagged name with another show's id, or none, is still
        the original and rejected; a name saying UK stays rejected."""
        from scraper.functions.filter_results import filter_results

        def nzb(title, imdb):
            r = _result(title)
            r["parsed_info"]["newznab_imdb"] = imdb
            return r
        pack = "The.Traitors.S02.1080p.AMZN.WEB-DL.DDP2.0.H.264-GRP"
        results = [nzb(pack, "33347879"),
                   nzb("The.Traitors.S02E01.1080p.WEB.H264-OTHER", "15357226"),
                   nzb("The.Traitors.S02E01.1080p.WEB.H264-NOID", ""),
                   nzb("The.Traitors.UK.S02E01.1080p.WEB.H264-UKGRP", "33347879")]
        passed, _ = filter_results(results, "271489", "The Traitors (IN)", 2025, "episode",
                                   2, 1, True, VERSION, 60, 11, {2: 11}, ["Reality"], [],
                                   imdb_id="tt33347879")
        self.assertEqual({pack}, {r["original_title"] for r in passed})

    def test_other_country_versions(self):
        # Native release named by country (Italy) or tagged with the language passes;
        # other countries and untagged English releases (the original show) don't.
        cases = {
            "The Traitors (IT)": ["The.Traitors.Italy.S01E01.iTALiAN.1080p.WEB.H264-NTROPiC",
                                  "The.Traitors.Italia.S01E01.1080p.WEB.H264-GRP"],
            "The Traitors (BR)": ["The.Traitors.Brasil.S01E01.1080p.WEB.H264-GRP",
                                  "The.Traitors.S01E01.DUBLADO.1080p.WEB.H264-GRP"],
            "The Traitors (MX)": ["The.Traitors.Mexico.S01E01.1080p.WEB.H264-GRP",
                                  "The.Traitors.S01E01.LATiN.1080p.WEB.H264-GRP"],
            "The Traitors (PL)": ["The.Traitors.PL.S01E01.1080p.WEB.H264-GRP",
                                  "The.Traitors.S01E01.POLISH.1080p.WEB.H264-GRP"],
            "The Traitors (RS)": ["The.Traitors.Srbija.S01E01.1080p.WEB.H264-GRP",
                                  "The.Traitors.S01E01.SERBIAN.1080p.WEB.H264-GRP"],
        }
        for title, native in cases.items():
            with self.subTest(title=title):
                passed = self._passed(title, native + OTHERS + [UNTAGGED_ENGLISH])
                self.assertEqual(set(native), {r["original_title"] for r in passed})

    def test_english_speaking_country_keeps_untagged(self):
        # Ireland's untagged releases can't be told from the UK's: ranked down, not rejected.
        passed = self._passed("The Traitors (IE)", OTHERS + [UNTAGGED_ENGLISH])
        self.assertEqual({UNTAGGED_ENGLISH}, {r["original_title"] for r in passed})

    def test_untagged_show_unchanged(self):
        # No tag on the show title: nothing is rejected for its country.
        self.assertEqual(set(OTHERS), {r["original_title"] for r in self._passed("The Traitors", OTHERS)})


class HelperTests(unittest.TestCase):
    def test_title_country_tag(self):
        from scraper.functions.filter_results import title_country_tag
        self.assertEqual("IN", title_country_tag("The Traitors (IN)"))
        self.assertEqual("UK", title_country_tag("The Office (GB)"))
        self.assertIsNone(title_country_tag("The Traitors"))
        self.assertIsNone(title_country_tag("Alien (1979)"))
        self.assertIsNone(title_country_tag("Something (XQ)"))

    def test_release_country_suffix(self):
        from scraper.functions.filter_results import release_country_suffix
        self.assertEqual("IN", release_country_suffix("the.traitors.india", "the.traitors"))
        self.assertEqual("IN", release_country_suffix("the.traitors.in", "the.traitors"))
        self.assertEqual("NZ", release_country_suffix("the.traitors.new.zealand", "the.traitors"))
        self.assertIsNone(release_country_suffix("the.traitors", "the.traitors"))
        self.assertIsNone(release_country_suffix("the.traitors.uncloaked", "the.traitors"))
        self.assertIsNone(release_country_suffix("love.in.the.traitors", "the.traitors"))


if __name__ == '__main__':
    unittest.main()


class CountryTextSearchTests(unittest.TestCase):
    """Newznab text searches spell a national version's country the way its
    releases do: "The Traitors (IN)" was searched as "The Traitors IN S02E01"
    and found none of the "The.Traitors.India.S02..." packs on DrunkenSlug."""

    def test_search_title(self):
        from scraper.newznab import _text_search_title
        cases = {'The Traitors (IN)': 'The Traitors India', 'The Traitors (IT)': 'The Traitors Italy',
                 'The Traitors (BR)': 'The Traitors Brazil', 'The Traitors (RS)': 'The Traitors Serbia',
                 'The Traitors (IE)': 'The Traitors Ireland',
                 # releases use these codes as-is
                 'The Traitors (UK)': 'The Traitors (UK)', 'The Traitors (NZ)': 'The Traitors (NZ)',
                 'The Traitors': 'The Traitors', 'Show (ZZ)': 'Show (ZZ)'}
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(expected, _text_search_title(title))

    def test_episode_query_uses_it(self):
        from scraper.newznab import _build_params_list
        params = _build_params_list('k', '33347879', 'The Traitors (IN)', 2025, 'episode', 2, 1, True)
        self.assertEqual(['The Traitors India S02'], [p['q'] for p in params if p.get('t') == 'search'])
