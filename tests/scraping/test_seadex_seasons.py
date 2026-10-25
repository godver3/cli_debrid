"""SeaDex lookups per season.

ani.zip maps an IMDb ID only to a show's first AniList entry, and AniList (so
SeaDex) has one entry per season or cour. Every season used to get Season 1's
best-release hashes. The AniList SEQUEL chain is now walked and ani.zip says
which TVDB season each entry covers. The fixture is Attack on Titan's real
chain shape: season 3 and season 4 are each split over two entries.
"""
import importlib
import sys
import types
import unittest
from unittest import mock

# anilist id -> (romaji, sequel id, TVDB seasons in ani.zip, SeaDex best hashes)
CHAIN = {
    16498: ('Shingeki no Kyojin', 20958, [0, 1], ['h1']),
    20958: ('Shingeki no Kyojin Season 2', 99147, [2], ['h2']),
    99147: ('Shingeki no Kyojin Season 3', 104578, [0, 3], ['h3a']),
    104578: ('Shingeki no Kyojin Season 3 Part 2', 110277, [0, 3], ['h3b']),
    110277: ('Shingeki no Kyojin: The Final Season', 131681, [4], ['h4a']),
    131681: ('Shingeki no Kyojin: The Final Season Part 2', None, [4], ['h4b']),
}


def _response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    return resp


class FakeApi:
    def __init__(self, anilist_fails=False):
        self.anilist_calls = 0
        self.anilist_fails = anilist_fails

    def get(self, url, params=None, timeout=None):
        if 'ani.zip' in url:
            if 'imdb_id' in params:
                return _response({'mappings': {'anilist_id': 16498}})
            seasons = CHAIN[params['anilist_id']][2]
            return _response({'episodes': {str(i): {'seasonNumber': s} for i, s in enumerate(seasons)}})
        alid = int(params['filter'].strip('()').split('=')[1])
        return _response({'items': [{'expand': {'trs': [
            {'isBest': True, 'infoHash': h.upper()} for h in CHAIN[alid][3]]}}]})

    def post(self, url, json=None, timeout=None):
        self.anilist_calls += 1
        if self.anilist_fails:
            raise RuntimeError('rate limited')
        romaji, sequel, _, _ = CHAIN[json['variables']['id']]
        edges = [{'relationType': 'SEQUEL', 'node': {'id': sequel, 'type': 'ANIME', 'format': 'TV'}}] if sequel else []
        return _response({'data': {'Media': {'title': {'romaji': romaji}, 'relations': {'edges': edges}}}})


class SeaDexSeasonTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [n for n in sys.modules if n == 'scraper.seadex' or n == 'routes.api_tracker']:
            del sys.modules[name]
        if not hasattr(sys.modules.get('scraper'), '__path__'):
            # Some tests leave a plain stub module named 'scraper'
            sys.modules.pop('scraper', None)
        self.api = FakeApi()
        fake_tracker = types.ModuleType('routes.api_tracker')
        fake_tracker.api = self.api
        sys.modules['routes.api_tracker'] = fake_tracker
        self.seadex = importlib.import_module('scraper.seadex')

    def test_season_one_and_movies_use_the_imdb_entry(self):
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140', 1), {'h1'})
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140'), {'h1'})
        self.assertEqual(self.api.anilist_calls, 0)

    def test_later_season_uses_its_own_entry(self):
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140', 2), {'h2'})

    def test_split_season_merges_both_entries(self):
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140', 3), {'h3a', 'h3b'})
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140', 4), {'h4a', 'h4b'})

    def test_chain_is_cached_and_extended_only_when_needed(self):
        self.seadex.get_seadex_hashes('tt2560140', 2)
        calls_for_s2 = self.api.anilist_calls
        self.seadex.get_seadex_hashes('tt2560140', 2)
        self.assertEqual(self.api.anilist_calls, calls_for_s2)
        self.seadex.get_seadex_hashes('tt2560140', 4)
        self.assertGreater(self.api.anilist_calls, calls_for_s2)
        self.assertLessEqual(self.api.anilist_calls, len(CHAIN) + 1)

    def test_romaji_per_season(self):
        titles = [t for _, t in self.seadex.anilist_entries_for_season('tt2560140', 3)]
        self.assertEqual(titles, ['Shingeki no Kyojin Season 3', 'Shingeki no Kyojin Season 3 Part 2'])

    def test_anilist_failure_falls_back_to_first_entry(self):
        self.api.anilist_fails = True
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140', 3), {'h1'})

    def test_season_beyond_chain(self):
        self.assertEqual(self.seadex.get_seadex_hashes('tt2560140', 9), {'h1'})


if __name__ == '__main__':
    unittest.main()
