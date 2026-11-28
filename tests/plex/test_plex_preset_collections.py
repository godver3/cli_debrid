"""database/plex_preset_collections.py: matching, reconcile rules and the run loop."""
import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401  (app import order)
from database import plex_preset_collections as ppc


class CatalogTests(unittest.TestCase):
    def test_every_preset_is_in_a_known_group(self):
        groups = {g for g, _ in ppc.GROUPS}
        for key, p in ppc.PRESETS.items():
            self.assertIn(p['group'], groups, key)
            self.assertTrue(set(p['types']) <= {'movie', 'show'}, key)

    def test_catalog_lists_every_preset_once(self):
        keys = [p['key'] for g in ppc.catalog_for_template() for p in g['presets']]
        self.assertEqual(sorted(keys), sorted(ppc.PRESETS))

    def test_nothing_on_by_default(self):
        with mock.patch.object(ppc, 'get_setting', side_effect=lambda s, k, d=None: d):
            self.assertEqual(ppc.enabled_presets(), [])

    def test_names(self):
        self.assertEqual(ppc.collection_name(ppc.PRESETS['top_rated'], 'show'), 'Top Rated Shows')
        self.assertEqual(ppc.collection_name(ppc.PRESETS['decades'], 'movie', decade='1990'), '1990s Movies')
        self.assertEqual(ppc.collection_name(ppc.PRESETS['genres'], 'movie', genre='Horror'), 'Horror Movies')
        self.assertEqual(ppc.collection_name(ppc.PRESETS['christmas'], 'movie'), 'Christmas Movies')

    def test_min_items_tolerates_strings_and_junk(self):
        for raw, want in (('5', 5), (None, ppc.DEFAULT_MIN_ITEMS), ('x', ppc.DEFAULT_MIN_ITEMS), (0, ppc.DEFAULT_MIN_ITEMS)):
            with mock.patch.object(ppc, 'get_setting', return_value=raw):
                self.assertEqual(ppc._min_items(), want, raw)


class MatchTests(unittest.TestCase):
    ID_MAP = {
        'tmdb:10': 'rk10',
        'tt0000020': 'rk20',
        'title:1999:the matrix': 'rk30',
    }

    def test_matches_by_tmdb_imdb_then_title_year_in_list_order(self):
        cands = [
            {'tmdb': None, 'imdb': None, 'title': 'The Matrix', 'year': '1999'},
            {'tmdb': '10', 'imdb': None, 'title': 'x', 'year': '2000'},
            {'tmdb': None, 'imdb': 'tt0000020', 'title': '', 'year': ''},
            {'tmdb': '99', 'imdb': None, 'title': 'Not Owned', 'year': '2001'},
        ]
        self.assertEqual(ppc.match_candidates(cands, self.ID_MAP), ['rk30', 'rk10', 'rk20'])

    def test_no_duplicates(self):
        cands = [{'tmdb': '10'}, {'tmdb': '10'}, {'imdb': 'tt0000020'}]
        self.assertEqual(ppc.match_candidates(cands, self.ID_MAP), ['rk10', 'rk20'])

    def test_group_by_decade(self):
        items = [{'ratingKey': 1, 'year': 1994}, {'ratingKey': 2, 'year': 1999},
                 {'ratingKey': 3, 'year': 2003}, {'ratingKey': 4}, {'ratingKey': 5, 'year': 'bad'}]
        self.assertEqual(ppc.group_by_decade(items), {1990: ['1', '2'], 2000: ['3']})


class PlanTests(unittest.TestCase):
    def test_owned_collection_adds_and_removes(self):
        self.assertEqual(ppc.plan_changes(['a', 'b'], ['b', 'c'], adopted=False), (['c'], ['a']))

    def test_taken_over_collection_never_loses_items(self):
        self.assertEqual(ppc.plan_changes(['a', 'b'], ['b', 'c'], adopted=True), (['c'], []))


class FakePlex:
    """Collections keyed by ratingKey; one movie section '1'."""

    def __init__(self, existing=None):
        self.colls = {}          # rk -> {'name', 'items'}
        self.next_rk = 100
        for name, items in (existing or {}).items():
            self._new(name, items)
        self.deleted = []

    def _new(self, name, items=()):
        rk = str(self.next_rk)
        self.next_rk += 1
        self.colls[rk] = {'name': name, 'items': list(items)}
        return rk

    def section_collections(self, url, token, section):
        return {c['name']: rk for rk, c in self.colls.items()}

    def create(self, url, token, section, name, lib_type, sort_prefix='!'):
        return self._new(name)

    def items(self, url, token, rk):
        return list(self.colls.get(rk, {}).get('items', []))

    def add(self, url, token, machine, rk, keys):
        self.colls[rk]['items'].extend(keys)

    def remove(self, url, token, rk, key):
        self.colls[rk]['items'].remove(key)

    def delete(self, url, token, rk):
        self.deleted.append(self.colls.pop(rk)['name'])

    def by_name(self, name):
        for c in self.colls.values():
            if c['name'] == name:
                return c['items']
        return None


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._p(mock.patch.object(ppc, '_STATE_FILE', os.path.join(self.tmp.name, 'state.json')))
        self.settings = {}
        self._p(mock.patch.object(ppc, 'get_setting',
                                  side_effect=lambda s, k, d=None: self.settings.get(k, d)))
        self._p(mock.patch('database.plex_collections._get_plex_credentials', return_value=('http://plex', 't')))
        self._p(mock.patch('database.plex_collections._get_library_sections',
                           return_value={'1': {'type': 'movie', 'title': 'Movies'}}))
        self._p(mock.patch('database.plex_collections._get_machine_id', return_value='mid'))
        self._p(mock.patch('database.plex_collections._arrange_items_in_order'))
        self._p(mock.patch('database.plex_collections._set_collection_sort_mode'))
        self.desired = {}
        self.fail = set()

        def desired_for(key, preset, ctx, state):
            if key in self.fail:
                raise RuntimeError('TMDB down')
            return [('1', 'movie', name, rks, False) for name, rks in self.desired.get(key, [])]
        self._p(mock.patch.object(ppc, '_desired_for_preset', side_effect=desired_for))

    def _p(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, plex):
        with mock.patch.object(ppc, '_section_collections', side_effect=plex.section_collections), \
             mock.patch.object(ppc, '_delete_collection', side_effect=plex.delete), \
             mock.patch('database.plex_collections._create_collection', side_effect=plex.create), \
             mock.patch('database.plex_collections._get_collection_items', side_effect=plex.items), \
             mock.patch('database.plex_collections._add_items_to_collection', side_effect=plex.add), \
             mock.patch('database.plex_collections._remove_item_from_collection', side_effect=plex.remove):
            return ppc.run_preset_collections()

    def test_nothing_enabled_and_nothing_managed_does_not_touch_plex(self):
        with mock.patch('database.plex_collections._get_plex_credentials') as creds:
            self.assertTrue(ppc.run_preset_collections()['success'])
        creds.assert_not_called()

    def test_creates_then_follows_list_then_deletes_when_switched_off(self):
        plex = FakePlex()
        self.settings = {'christmas': True}
        self.desired = {'christmas': [('Christmas Movies', ['a', 'b', 'c'])]}
        self._run(plex)
        self.assertEqual(plex.by_name('Christmas Movies'), ['a', 'b', 'c'])

        self.desired = {'christmas': [('Christmas Movies', ['b', 'c', 'd'])]}
        self._run(plex)
        self.assertEqual(sorted(plex.by_name('Christmas Movies')), ['b', 'c', 'd'])

        self.settings = {}
        self._run(plex)
        self.assertEqual(plex.deleted, ['Christmas Movies'])
        self.assertEqual(ppc.get_status()['collections_managed'], 0)

    def test_below_min_items_is_not_created_and_owned_one_is_removed(self):
        plex = FakePlex()
        self.settings = {'decades': True}
        self.desired = {'decades': [('1990s Movies', ['a', 'b', 'c']), ('1960s Movies', ['z'])]}
        self._run(plex)
        self.assertIsNone(plex.by_name('1960s Movies'))
        self.assertIsNotNone(plex.by_name('1990s Movies'))

        self.desired = {'decades': [('1990s Movies', ['a'])]}
        self._run(plex)
        self.assertEqual(plex.deleted, ['1990s Movies'])

    def test_failed_fetch_keeps_existing_collections(self):
        plex = FakePlex()
        self.settings = {'trending': True}
        self.desired = {'trending': [('Trending Movies', ['a', 'b', 'c'])]}
        self._run(plex)
        self.fail = {'trending'}
        result = self._run(plex)
        self.assertEqual(plex.deleted, [])
        self.assertEqual(plex.by_name('Trending Movies'), ['a', 'b', 'c'])
        self.assertEqual(len(result['errors']), 1)

    def test_existing_same_name_collection_is_add_only_and_never_deleted(self):
        plex = FakePlex(existing={'Christmas Movies': ['mine1', 'mine2']})
        self.settings = {'christmas': True}
        self.desired = {'christmas': [('Christmas Movies', ['a', 'b', 'c'])]}
        self._run(plex)
        self.assertEqual(sorted(plex.by_name('Christmas Movies')), ['a', 'b', 'c', 'mine1', 'mine2'])

        self.settings = {}
        self._run(plex)
        self.assertEqual(plex.deleted, [])
        self.assertIsNotNone(plex.by_name('Christmas Movies'))
        self.assertEqual(ppc.get_status()['collections_managed'], 0)

    def test_delete_failure_keeps_collection_in_state(self):
        plex = FakePlex()
        self.settings = {'christmas': True}
        self.desired = {'christmas': [('Christmas Movies', ['a', 'b', 'c'])]}
        self._run(plex)
        self.settings = {}
        with mock.patch.object(ppc, '_section_collections', side_effect=plex.section_collections), \
             mock.patch.object(ppc, '_delete_collection', side_effect=RuntimeError('500')):
            ppc.run_preset_collections()
        self.assertEqual(ppc.get_status()['collections_managed'], 1)


if __name__ == '__main__':
    unittest.main()
