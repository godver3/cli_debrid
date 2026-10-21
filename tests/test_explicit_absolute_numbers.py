"""Release names that state an absolute episode next to their own season.

"[A&C] One Piece Season 13 - Impel Down [422-458]" holds files named
"One Piece S13E01 (0422)". A&C's season 13 is none of TVDB's: aired S13
starts at 382 and DVD order puts 422 at S15E01. The absolute number means the
same episode in every order, so it decides over the name's season and
episode, in the scrape filter, the file matcher and manual assign.

Here the show has 3 seasons of 20 (absolute 1-60) and the releaser calls the
show's season 3 (41-60) "Season 2".
"""
import importlib
import importlib.util
import os
import sys
import unittest
from unittest import mock
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: F401  -- importing queues.media_matcher first is circular
from queues import media_matcher as mm
from scraper.functions import anime_utils
from scraper.functions.anime_utils import explicit_absolute_numbers
from scraper.functions.common import detect_season_episode_info
from scraper.functions.filter_results import filter_results

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNTS = {1: 20, 2: 20, 3: 20}
PACK = '[Grp] Show Season 2 - Arc [41-60] (WEB) [Multi-Subs]'
FILES = [f'/{PACK}/[Grp] Show S02E{i:02d} ({40 + i:04d}) (WEB 1080p) [ABCD{i:04d}].mkv' for i in range(1, 21)]


def _real_parse_title():
    # Other tests may leave a stub PTT in sys.modules; this test needs the real parser.
    saved = sys.modules.pop('PTT', None)
    try:
        return importlib.import_module('PTT').parse_title
    finally:
        if saved is not None:
            sys.modules['PTT'] = saved


def _absolute(season, episode):
    return sum(COUNTS[s] for s in COUNTS if s < season) + episode


class _ShowPatches:
    def setUp(self):
        for p in (mock.patch.object(anime_utils, 'cached_stored_absolute_episode',
                                    side_effect=lambda imdb, s, e: _absolute(s, e)),
                  mock.patch.object(anime_utils, '_show_season_counts', return_value=dict(COUNTS))):
            p.start()
            self.addCleanup(p.stop)


class ExplicitAbsoluteNumbersTests(unittest.TestCase):
    def test_number_next_to_season_and_episode(self):
        self.assertEqual(explicit_absolute_numbers('One Piece S13E01 (0422) (WEB)', [13], [1]), [422])

    def test_range_next_to_season(self):
        self.assertEqual(explicit_absolute_numbers('One Piece Season 13 - Impel Down [422-458]', [13],
                                                   list(range(422, 459))), list(range(422, 459)))

    def test_not_absolute(self):
        cases = [
            ('Show S02E05 (05)', [2], [5]),            # the episode itself
            ('Show S02 [01-12]', [2], list(range(1, 13))),  # the season's own count
            ('Show S01E01 (2009)', [1], [1]),          # a year
            ('Show S01E01 (1080p)', [1], [1]),         # a resolution tag (trailing p)
            ('Show - 0422 (0422)', [], [422]),         # no season: already absolute
            ('Show S02E01E02 (0021)', [2], [1, 2]),    # which episode would it be?
        ]
        for name, seasons, episodes in cases:
            self.assertEqual(explicit_absolute_numbers(name, seasons, episodes), [], name)

    def test_resolution_like_absolute_numbers(self):
        # Bare (360)/(1080) next to S/E is absolute; long anime hit these values.
        self.assertEqual(explicit_absolute_numbers('Gintama S10E19 (360)', [10], [19]), [360])
        self.assertEqual(explicit_absolute_numbers('One Piece S01E01 (1080)', [1], [1]), [1080])

    def test_range_inside_the_named_season_counts_that_season(self):
        name = 'Show S03 Part 2 [13-22]'
        self.assertEqual(explicit_absolute_numbers(name, [3], list(range(13, 23)), {1: 25, 2: 12, 3: 22}), [])
        self.assertEqual(explicit_absolute_numbers(name, [3], list(range(13, 23)), {1: 6, 2: 6, 3: 10}),
                         list(range(13, 23)))


class FilterTests(_ShowPatches, unittest.TestCase):
    def _run(self, title, size_gb, season, episode, multi):
        ptt = _real_parse_title()(title)
        parsed = {'seasons': ptt.get('seasons', []), 'episodes': ptt.get('episodes', [])}
        result = {
            'title': title, 'original_title': title, 'size': size_gb,
            'scraper_type': 'Generic', 'scraper_instance': 'test',
            'additional_metadata': {'filename': title, 'bingeGroup': None},
            'parsed_info': dict(ptt, title='Show', original_title=title,
                                season_episode_info=detect_season_episode_info(parsed)),
        }
        if episode:
            result['target_abs_episode'] = _absolute(season, episode)
        direct_api = Mock()
        direct_api.get_show_aliases.return_value = ({}, None)
        direct_api.get_show_metadata.return_value = ({}, None)
        direct_api.get_show_seasons.return_value = ({s: {'episode_count': c} for s, c in COUNTS.items()}, None)
        version = {
            'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
            'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
            'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0,
        }
        kept, _ = filter_results(
            results=[result], tmdb_id='1', title='Show', year=2004, content_type='episode',
            season=season, episode=episode, multi=multi, version_settings=version, runtime=24,
            episode_count=COUNTS[season], season_episode_counts=dict(COUNTS), genres=['anime'],
            matching_aliases=['Show'], imdb_id='tt0000001', direct_api=direct_api)
        return kept, result.get('filter_reason')

    def test_pack_kept_for_the_season_its_numbers_hold(self):
        kept, _ = self._run(PACK, 30.0, 3, None, True)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['parsed_info']['season_episode_info']['seasons'], [3])

    def test_pack_rejected_for_the_season_its_title_names(self):
        kept, reason = self._run(PACK, 30.0, 2, None, True)
        self.assertEqual(kept, [])
        self.assertIn("don't hold all of season 2", reason)

    def test_single_episode_by_absolute(self):
        title = '[Grp] Show S02E01 (0041) (WEB 1080p)'
        self.assertEqual(len(self._run(title, 1.5, 3, 1, False)[0]), 1)
        kept, reason = self._run(title, 1.5, 2, 1, False)
        self.assertEqual(kept, [])
        self.assertIn('Absolute episode mismatch', reason)

    def test_pack_in_episode_search(self):
        self.assertEqual(len(self._run(PACK, 30.0, 3, 5, False)[0]), 1)
        self.assertEqual(self._run(PACK, 30.0, 2, 5, False)[0], [])


class MatcherTests(_ShowPatches, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(mm, 'parse_title', _real_parse_title())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m = mm.MediaMatcher()
        self.m._get_season_episode_counts_cached = lambda tmdb_id: dict(COUNTS)
        self.parsed = [p for p in (self.m._parse_file_info({'path': f, 'bytes': 1}) for f in FILES) if p]

    def _item(self, season, episode, item_id=None):
        return {'id': item_id, 'type': 'episode', 'title': 'Show', 'series_title': 'Show', 'imdb_id': 'tt0000001',
                'season_number': season, 'episode_number': episode, 'genres': '["anime"]', 'tmdb_id': '1',
                'version': '1080p'}

    def _match(self, season, episode):
        with mock.patch('utilities.settings.get_setting', return_value='Symlinked/Local'):
            return self.m.find_best_match_from_parsed(self.parsed, self._item(season, episode))

    def test_matched_by_absolute(self):
        self.assertIn('S02E01 (0041)', self._match(3, 1)[0])
        self.assertIn('S02E20 (0060)', self._match(3, 20)[0])

    def test_names_own_season_does_not_match(self):
        # S02E01 by name, but absolute 41 is the show's S03E01
        self.assertIsNone(self._match(2, 1))

    def test_related_items_by_absolute(self):
        wanted = [self._item(3, e, 100 + e) for e in range(2, 21)] + [self._item(2, e, 200 + e) for e in range(1, 21)]
        with mock.patch('utilities.settings.get_setting', return_value=True):
            related = self.m.find_related_items(self.parsed, [], wanted, self._item(3, 1, 1), torrent_title=PACK)
        self.assertEqual(len(related), 19)
        for item, filename in related:
            self.assertEqual(item['season_number'], 3)
            self.assertIn(f"({40 + item['episode_number']:04d})", filename)


class ManualAssignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'manual_assign_episodes_explicit_abs', os.path.join(ROOT, 'utilities/manual_assign_episodes.py'))
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def _seasons(self):
        return {s: {'episodes': {e: {'absolute': _absolute(s, e)} for e in range(1, n + 1)}}
                for s, n in COUNTS.items()}

    def test_assigned_by_absolute_not_by_name(self):
        parse = _real_parse_title()
        items = [{'type': 'episode', 'season_number': s, 'episode_number': e, 'item_key': f'S{s}E{e}',
                  'assigned': False, 'suggested_file_path': None} for s in (2, 3) for e in range(1, 21)]
        files = [{'original': {'filename': os.path.basename(f), 'path': f}, 'parsed': parse(os.path.basename(f)),
                  'used': False} for f in FILES]
        self.assertEqual(self.m.match_absolute_files(items, files, self._seasons()), 20)
        for item in items:
            if item['season_number'] == 2:
                self.assertFalse(item['assigned'], item['item_key'])
            else:
                self.assertIn(f"({40 + item['episode_number']:04d})", item['suggested_file_path'])

    def test_conflict_check(self):
        name = '[Grp] Show S02E01 (0041) (WEB 1080p).mkv'
        explicit = self.m.file_explicit_absolute(_real_parse_title()(name), name, self._seasons())
        self.assertEqual(explicit, [41])
        self.assertTrue(self.m.explicit_absolute_conflicts([21], explicit))   # the show's S02E01
        self.assertFalse(self.m.explicit_absolute_conflicts([41], explicit))  # the show's S03E01
        self.assertFalse(self.m.explicit_absolute_conflicts(None, explicit))
        self.assertFalse(self.m.explicit_absolute_conflicts([21], []))


if __name__ == '__main__':
    unittest.main()
