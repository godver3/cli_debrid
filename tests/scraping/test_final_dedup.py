"""Final dedup of passed results (scraper.functions.deduplicate_results.dedupe_passed_results).

MustBeNice, RHO Amsterdam S02: DrunkenSlug and NinjaCentral list the same
release name for different uploads (0.94 vs 0.97 GB). Merging on title kept
whichever came back first, so the other upload was never tried.
"""
import unittest

from scraper.functions.deduplicate_results import dedupe_passed_results

TITLE = 'The.Real.Housewives.Of.Amsterdam.s02e12.1080p.WEBRip'


def nzb(url, size, title=TITLE):
    return {'original_title': title, 'protocol': 'nzb', 'nzb_url': url, 'size': size, 'parsed_info': {}}


def torrent(magnet, title=TITLE):
    return {'original_title': title, 'magnet': magnet, 'parsed_info': {}}


class FinalDedupTests(unittest.TestCase):
    def test_same_title_different_nzbs_both_kept(self):
        a, b = nzb('https://slug/get/1', 0.97), nzb('https://ninja/get/9', 0.94)
        self.assertEqual(dedupe_passed_results([a, b]), [a, b])

    def test_same_nzb_from_two_searches_merged(self):
        a = nzb('https://slug/get/1', 0.97)
        self.assertEqual(dedupe_passed_results([a, dict(a)]), [a])

    def test_nzb_without_url_falls_back_to_guid_then_title(self):
        a = nzb('', 1.0)
        a['parsed_info'] = {'guid': 'g1'}
        b = nzb('', 1.1)
        b['parsed_info'] = {'guid': 'g2'}
        c, d = nzb('', 1.2), nzb('', 1.3)
        self.assertEqual(dedupe_passed_results([a, b, c, d]), [a, b, c])

    def test_torrents_still_merge_on_title(self):
        a, b = torrent('magnet:?xt=urn:btih:aaa'), torrent('magnet:?xt=urn:btih:bbb')
        self.assertEqual(dedupe_passed_results([a, b]), [a])

    def test_anime_episode_key_unchanged(self):
        a = {'original_title': 'Show - 01', 'parsed_title': 'Show', 'parsed_info': {'episode': 1}}
        b = {'original_title': 'Show 01 [x]', 'parsed_title': 'Show', 'parsed_info': {'episode': 1}}
        self.assertEqual(dedupe_passed_results([a, b], is_anime=True, episode_formats={'x': 1}), [a])


if __name__ == '__main__':
    unittest.main()
