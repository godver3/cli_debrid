"""Manual (magnet) assign: matching pack files by absolute episode number.

Anime packs are numbered "Show - 030" or all under S01, while the show may
use aired seasons, or the show is set to an absolute order while the pack
uses aired seasons. utilities/manual_assign_episodes.py handles both after
the page's S/E pass.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seasons(counts, absolute=None):
    """{season: n_episodes} -> battery seasons; absolute: {(s, e): number}."""
    absolute = absolute or {}
    return {s: {'episode_count': n,
                'episodes': {e: {'title': f'S{s}E{e}', 'absolute': absolute.get((s, e))}
                             for e in range(1, n + 1)}}
            for s, n in counts.items()}


def _items(layout):
    return [{'type': 'episode', 'season_number': s, 'episode_number': e,
             'item_key': f'ep_1_s{s:02d}e{e:02d}', 'assigned': False, 'suggested_file_path': None}
            for s, e in layout]


def _file(name, seasons=(), episodes=()):
    return {'original': {'filename': name, 'path': name},
            'parsed': {'seasons': list(seasons), 'episodes': list(episodes)}, 'used': False}


def _assigned(items):
    return {(i['season_number'], i['episode_number']): i['suggested_file_path'] for i in items if i['assigned']}


class ManualAssignAbsoluteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        anime_utils = _load('anime_utils_under_test', 'scraper/functions/anime_utils.py')
        patcher = mock.patch.dict(sys.modules, {'scraper.functions.anime_utils': anime_utils})
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.m = _load('manual_assign_episodes_under_test', 'utilities/manual_assign_episodes.py')

    def test_absolute_pack_for_aired_seasons(self):
        seasons = _seasons({0: 2, 1: 12, 2: 12})
        items = _items([(2, 1), (2, 5), (2, 12)])
        files = [_file('Show - 13.mkv', episodes=[13]), _file('Show - 17.mkv', episodes=[17]),
                 _file('Show - 24.mkv', episodes=[24]), _file('Show - 05.mkv', episodes=[5])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 3)
        self.assertEqual(_assigned(items), {(2, 1): 'Show - 13.mkv', (2, 5): 'Show - 17.mkv',
                                            (2, 12): 'Show - 24.mkv'})
        self.assertFalse(files[3]['used'])
        self.assertEqual(items[1]['absolute_number'], 17)

    def test_specials_never_get_an_absolute_number(self):
        seasons = _seasons({0: 3, 1: 12})
        items = _items([(0, 2)])
        files = [_file('Show - 02.mkv', episodes=[2])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 0)
        self.assertIsNone(items[0]['absolute_number'])

    def test_everything_under_s01(self):
        seasons = _seasons({1: 12, 2: 12})
        items = _items([(2, 3)])
        files = [_file('Show.S01E15.mkv', seasons=[1], episodes=[15])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 1)
        self.assertEqual(_assigned(items), {(2, 3): 'Show.S01E15.mkv'})

    def test_aired_pack_for_a_show_in_absolute_order(self):
        # The show is set to TVDB absolute (one season); the pack uses aired seasons.
        absolute_order = _seasons({1: 24}, {(1, e): e for e in range(1, 25)})
        aired = _seasons({1: 12, 2: 12})
        items = _items([(1, 14), (1, 24)])
        files = [_file('Show.S02E02.mkv', seasons=[2], episodes=[2]),
                 _file('Show.S02E12.mkv', seasons=[2], episodes=[12])]
        self.assertEqual(self.m.match_absolute_files(items, files, absolute_order, aired), 2)
        self.assertEqual(_assigned(items), {(1, 14): 'Show.S02E02.mkv', (1, 24): 'Show.S02E12.mkv'})

    def test_provider_absolute_first_then_counted(self):
        # TVDB's numbering skips (S2E1 is 99 but counting gives 99 too; S2E2 is
        # 105 by TVDB, 100 by counting). A file numbered either way matches.
        seasons = _seasons({1: 98, 2: 3}, {(2, 1): 99, (2, 2): 105, (2, 3): 106})
        items = _items([(2, 1), (2, 2), (2, 3)])
        files = [_file('Kai - 099.mkv', episodes=[99]), _file('Kai - 100.mkv', episodes=[100]),
                 _file('Kai - 106.mkv', episodes=[106])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 3)
        self.assertEqual(_assigned(items), {(2, 1): 'Kai - 099.mkv', (2, 2): 'Kai - 100.mkv',
                                            (2, 3): 'Kai - 106.mkv'})

    def test_number_two_episodes_claim_is_not_guessed(self):
        # TVDB says S2E2 is 105; counting says 105 is S2E7. "- 105" could be either.
        seasons = _seasons({1: 98, 2: 8}, {(2, 2): 105})
        items = _items([(2, 2), (2, 7)])
        files = [_file('Kai - 105.mkv', episodes=[105])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 0)
        self.assertFalse(files[0]['used'])

    def test_aired_file_joins_absolute_order_by_episode_id(self):
        # Absolute order without TVDB absolute numbers and an aired order whose
        # count disagrees: the episode ID decides.
        absolute_order = {1: {'episodes': {e: {'tvdb_id': str(1000 + e)} for e in range(1, 11)}}}
        aired = {1: {'episodes': {e: {'tvdb_id': str(1000 + e)} for e in range(1, 5)}},
                 2: {'episodes': {e: {'tvdb_id': str(1004 + e + 1)} for e in range(1, 6)}}}  # 1005 dropped
        items = _items([(1, 6), (1, 10)])
        files = [_file('Show.S02E01.mkv', seasons=[2], episodes=[1]), _file('Show.S02E05.mkv', seasons=[2], episodes=[5])]
        self.assertEqual(self.m.match_absolute_files(items, files, absolute_order, aired), 2)
        self.assertEqual(_assigned(items), {(1, 6): 'Show.S02E01.mkv', (1, 10): 'Show.S02E05.mkv'})

    def test_multi_episode_file_goes_to_each_episode(self):
        seasons = _seasons({1: 12, 2: 12})
        items = _items([(2, 1), (2, 2)])
        files = [_file('Show - 13-14.mkv', episodes=[13, 14])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 2)
        self.assertEqual(_assigned(items), {(2, 1): 'Show - 13-14.mkv', (2, 2): 'Show - 13-14.mkv'})

    def test_multi_episode_file_with_an_unknown_number_is_left_alone(self):
        seasons = _seasons({1: 12})
        items = _items([(1, 12)])
        files = [_file('Show - 12-13.mkv', episodes=[12, 13])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 0)

    def test_multi_episode_file_used_by_the_s_e_pass_still_fills_its_second_episode(self):
        # Phase 1 gave "001-002" to S01E01 and marked it used
        seasons = _seasons({1: 12})
        items = _items([(1, 1), (1, 2)])
        items[0].update(assigned=True, suggested_file_path='[CBT] Gintama 001-002.mkv')
        files = [_file('[CBT] Gintama 001-002.mkv', episodes=[1, 2])]
        files[0]['used'] = True
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 1)
        self.assertEqual(items[1]['suggested_file_path'], '[CBT] Gintama 001-002.mkv')

    def test_bare_number_after_the_title(self):
        # PTT: "[CBT] Gintama 050 [DVDrip 760x576 x264 FLAC].mkv" -> title "Gintama 050", no episode
        seasons = _seasons({1: 49, 2: 50})
        items = _items([(1, 3), (2, 1), (2, 2)])
        files = [_file(f'[CBT] Gintama {n:03d} [DVDrip].mkv') for n in (3, 50, 51)]
        for f, n in zip(files, (3, 50, 51)):
            f['parsed']['title'] = f'Gintama {n:03d}'
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 3)
        self.assertEqual(_assigned(items), {(1, 3): '[CBT] Gintama 003 [DVDrip].mkv',
                                            (2, 1): '[CBT] Gintama 050 [DVDrip].mkv',
                                            (2, 2): '[CBT] Gintama 051 [DVDrip].mkv'})

    def test_lone_title_ending_in_a_number_is_not_an_episode(self):
        seasons = _seasons({1: 120})
        items = _items([(1, 100)])
        files = [_file('Mob Psycho 100.mkv')]
        files[0]['parsed']['title'] = 'Mob Psycho 100'
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 0)

    def test_bare_numbers_follow_the_most_common_title(self):
        # Two NCOPs next to three episodes: only "Show NN" reads as episodes
        seasons = _seasons({1: 12})
        items = _items([(1, 1), (1, 2), (1, 3)])
        names = {'Show 01': 'a.mkv', 'Show 02': 'b.mkv', 'Show 03': 'c.mkv', 'Show NCOP 1': 'op1.mkv', 'Show NCOP 2': 'op2.mkv'}
        files = []
        for title, name in names.items():
            files.append(_file(name))
            files[-1]['parsed']['title'] = title
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 3)
        self.assertEqual(_assigned(items), {(1, 1): 'a.mkv', (1, 2): 'b.mkv', (1, 3): 'c.mkv'})

    def test_two_files_with_one_number_are_left_alone(self):
        seasons = _seasons({1: 12, 2: 12})
        items = _items([(2, 1)])
        files = [_file('Show - 13 v1.mkv', episodes=[13]), _file('Show - 13 v2.mkv', episodes=[13])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 0)
        self.assertFalse(items[0]['assigned'])

    def test_nced_ncop_extras_do_not_block_episode_assign(self):
        # Judas-style: NCED/NCOP share episode numbers with the real S01 files.
        seasons = _seasons({1: 30}, absolute={(1, n): n for n in range(1, 31)})
        items = _items([(1, 1), (1, 3), (1, 16)])
        files = [
            _file('Extras/[Judas] Gintama - NCED 01.mkv', episodes=[1]),
            _file('Extras/[Judas] Gintama - NCOP 01.mkv', episodes=[1]),
            _file('[Judas] Gintama - S01E01-E02 (001-002).mkv', seasons=[1], episodes=[1, 2]),
            _file('Extras/[Judas] Gintama - NCED 03.mkv', episodes=[3]),
            _file('Extras/[Judas] Gintama - NCOP 03.mkv', episodes=[3]),
            _file('[Judas] Gintama - S01E03 (003).mkv', seasons=[1], episodes=[3]),
            _file('Extras/[Judas] Gintama - NCOP 16a.mkv', episodes=[16]),
            _file('[Judas] Gintama - S01E16 (016).mkv', seasons=[1], episodes=[16]),
        ]
        self.assertTrue(self.m.is_anime_op_ed_extra('Extras/[Judas] Gintama - NCED 01.mkv'))
        self.assertTrue(self.m.is_anime_op_ed_extra('[Judas] Gintama - NCOP 16a.mkv'))
        self.assertFalse(self.m.is_anime_op_ed_extra('[Judas] Gintama - S01E03 (003).mkv'))
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 3)
        self.assertEqual(_assigned(items), {
            (1, 1): '[Judas] Gintama - S01E01-E02 (001-002).mkv',
            (1, 3): '[Judas] Gintama - S01E03 (003).mkv',
            (1, 16): '[Judas] Gintama - S01E16 (016).mkv',
        })

    def test_absolute_360_not_treated_as_resolution(self):
        seasons = _seasons({1: 367}, absolute={(1, n): n for n in range(1, 368)})
        items = _items([(1, 360)])
        files = [_file('[Judas] Gintama - S10E19 (360).mkv', seasons=[10], episodes=[19])]
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 1)
        self.assertEqual(_assigned(items), {(1, 360): '[Judas] Gintama - S10E19 (360).mkv'})

    def test_already_assigned_items_and_used_files_are_skipped(self):
        seasons = _seasons({1: 12, 2: 12})
        items = _items([(2, 1), (2, 2)])
        items[0].update(assigned=True, suggested_file_path='by S/E')
        files = [_file('Show - 13.mkv', episodes=[13]), _file('Show - 14.mkv', episodes=[14])]
        files[1]['used'] = True
        self.assertEqual(self.m.match_absolute_files(items, files, seasons), 0)
        self.assertEqual(items[0]['suggested_file_path'], 'by S/E')
        self.assertFalse(items[1]['assigned'])


if __name__ == '__main__':
    unittest.main()
