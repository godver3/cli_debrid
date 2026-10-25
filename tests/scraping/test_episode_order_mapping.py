"""Shows set to another episode order read release names in aired numbering.

Releases use TVDB's aired S/E. One Piece's DVD S15E01 is aired S13E41, and a
release named "S15E01" is aired S15E01, a different episode, so it must not
be taken for DVD S15E01. scraper/functions/episode_order_mapping.py joins the
two numberings by episode ID (then absolute); the scraper searches and filters
single episodes by the aired S/E, and the filter and matcher read aired season
packs and files through the mapping.

Here aired order has two seasons of 4 (absolute 1-8) and the show's "DVD"
order has seasons of 3, 3 and 2.
"""
import importlib
import os
import sys
import unittest
from unittest import mock
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  -- importing queues.media_matcher first is circular
from queues import media_matcher as mm
from scraper.functions import episode_order_mapping as eom
from scraper.functions.common import detect_season_episode_info
from scraper.functions.filter_results import filter_results

AIRED_COUNTS = {1: 4, 2: 4}
ORDER_COUNTS = {1: 3, 2: 3, 3: 2}


def _seasons(counts, with_ids=True):
    out, n = {0: {'episodes': {1: {'tvdb_id': 'sp1'}}}}, 0
    for s in sorted(counts):
        eps = {}
        for e in range(1, counts[s] + 1):
            n += 1
            eps[e] = {'absolute': n, **({'tvdb_id': f'ep{n}'} if with_ids else {})}
        out[s] = {'episodes': eps}
    return out


MAPPING = eom.OrderMapping(_seasons(ORDER_COUNTS), _seasons(AIRED_COUNTS))


def _real_parse_title():
    # Other tests may leave a stub PTT in sys.modules; this test needs the real parser.
    saved = sys.modules.pop('PTT', None)
    try:
        return importlib.import_module('PTT').parse_title
    finally:
        if saved is not None:
            sys.modules['PTT'] = saved


class OrderMappingTests(unittest.TestCase):
    def test_positions(self):
        self.assertEqual(MAPPING.aired_of(2, 1), (1, 4))
        self.assertEqual(MAPPING.aired_of(3, 2), (2, 4))
        self.assertEqual(MAPPING.order_of(1, 4), (2, 1))
        self.assertIsNone(MAPPING.order_of(3, 1))      # aired has no S03
        self.assertIsNone(MAPPING.order_of(1, 422))

    def test_joined_by_absolute_without_ids(self):
        mapping = eom.OrderMapping(_seasons(ORDER_COUNTS, with_ids=False), _seasons(AIRED_COUNTS, with_ids=False))
        self.assertEqual(mapping.aired_of(2, 1), (1, 4))

    def test_seasons_held(self):
        self.assertEqual(MAPPING.order_seasons_held(MAPPING.aired_positions([1])), [1])
        self.assertEqual(MAPPING.order_seasons_held(MAPPING.aired_positions([1, 2])), [1, 2, 3])
        self.assertEqual(MAPPING.order_seasons_held(MAPPING.aired_positions([2])), [3])
        self.assertEqual(MAPPING.aired_positions([1], [2, 3]), {(1, 2), (1, 3)})

    def test_default_order_has_no_mapping(self):
        eom.clear_order_mapping_cache()
        with mock.patch('cli_battery.app.episode_order.show_episode_order', return_value=None):
            self.assertIsNone(eom.order_mapping('tt0000002'))
        eom.clear_order_mapping_cache()

    def test_order_read_once_per_minute(self):
        eom.clear_order_mapping_cache()
        with mock.patch('cli_battery.app.episode_order.show_episode_order', return_value='tvdb:dvd') as order, \
                mock.patch('cli_battery.app.episode_order.fetch_seasons_for_order',
                           side_effect=lambda imdb, o: _seasons(AIRED_COUNTS if o == 'default' else ORDER_COUNTS)) as fetch:
            for _ in range(3):
                self.assertEqual(eom.order_mapping('tt0000003').aired_of(2, 1), (1, 4))
        self.assertEqual(order.call_count, 1)
        self.assertEqual(fetch.call_count, 2)
        eom.clear_order_mapping_cache()


class FilterSeasonPackTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(eom, 'order_mapping', return_value=MAPPING)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, title, season, genres=('drama',)):
        ptt = _real_parse_title()(title)
        parsed = {'seasons': ptt.get('seasons', []), 'episodes': ptt.get('episodes', [])}
        result = {
            'title': title, 'original_title': title, 'size': 10.0,
            'scraper_type': 'Generic', 'scraper_instance': 'test',
            'additional_metadata': {'filename': title, 'bingeGroup': None},
            'parsed_info': dict(ptt, title='Show', original_title=title,
                                season_episode_info=detect_season_episode_info(parsed)),
        }
        direct_api = Mock()
        direct_api.get_show_aliases.return_value = ({}, None)
        direct_api.get_show_metadata.return_value = ({}, None)
        direct_api.get_show_seasons.return_value = ({s: {'episode_count': c} for s, c in ORDER_COUNTS.items()}, None)
        version = {
            'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
            'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
            'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0,
        }
        kept, _ = filter_results(
            results=[result], tmdb_id='1', title='Show', year=2004, content_type='episode',
            season=season, episode=None, multi=True, version_settings=version, runtime=24,
            episode_count=ORDER_COUNTS[season], season_episode_counts=dict(ORDER_COUNTS), genres=list(genres),
            matching_aliases=['Show'], imdb_id='tt0000001', direct_api=direct_api)
        return kept, result.get('filter_reason')

    def test_aired_pack_holding_the_season_kept(self):
        kept, _ = self._run('Show S01 1080p WEB-DL', 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['num_episodes_in_pack_calculated'], 4)

    def test_same_season_number_but_other_episodes_rejected(self):
        # Aired S02 is absolute 5-8; the show's season 2 is 4-6
        kept, reason = self._run('Show S02 1080p WEB-DL', 2)
        self.assertEqual(kept, [])
        self.assertIn("don't hold all of season 2", reason)

    def test_multi_season_pack_holding_the_season(self):
        kept, _ = self._run('Show S01-S02 1080p WEB-DL', 2)
        self.assertEqual(len(kept), 1)


class MatcherTests(unittest.TestCase):
    FILES = [f'/Show.Pack/Show.S{s:02d}E{e:02d}.1080p.WEB.mkv' for s in (1, 2) for e in range(1, 5)]

    def setUp(self):
        for p in (mock.patch.object(eom, 'order_mapping', return_value=MAPPING),
                  mock.patch.object(mm, 'parse_title', _real_parse_title())):
            p.start()
            self.addCleanup(p.stop)
        self.m = mm.MediaMatcher()
        self.parsed = [p for p in (self.m._parse_file_info({'path': f, 'bytes': 1}) for f in self.FILES) if p]

    def _item(self, season, episode, item_id=None):
        return {'id': item_id, 'type': 'episode', 'title': 'Show', 'series_title': 'Show', 'imdb_id': 'tt0000001',
                'season_number': season, 'episode_number': episode, 'genres': '["drama"]', 'tmdb_id': '1',
                'version': '1080p'}

    def test_files_read_in_aired_numbering(self):
        with mock.patch('utilities.settings.get_setting', return_value='Symlinked/Local'):
            self.assertIn('S01E04', self.m.find_best_match_from_parsed(self.parsed, self._item(2, 1))[0])
            self.assertIn('S02E04', self.m.find_best_match_from_parsed(self.parsed, self._item(3, 2))[0])
            self.assertIn('S01E01', self.m.find_best_match_from_parsed(self.parsed, self._item(1, 1))[0])

    def test_related_items(self):
        wanted = [self._item(s, e, s * 10 + e) for s, n in ORDER_COUNTS.items() for e in range(1, n + 1)]
        with mock.patch('utilities.settings.get_setting', return_value=True):
            related = self.m.find_related_items(self.parsed, [], wanted, self._item(1, 1, 0),
                                                torrent_title='Show S01-S02 1080p')
        got = {(i['season_number'], i['episode_number']): f for i, f in related}
        self.assertEqual(len(got), 8)
        for (s, e), filename in got.items():
            aired = MAPPING.aired_of(s, e)
            self.assertIn(f'S{aired[0]:02d}E{aired[1]:02d}', filename)


if __name__ == '__main__':
    unittest.main()
