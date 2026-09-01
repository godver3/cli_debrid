#!/usr/bin/env python3
"""Tests for the library Fix Match endpoints (routes/library_routes.py).

Fix Match corrects a library entry's IMDb, TMDB and TVDB IDs together.  The
stale IDs live in four places -- media_items, the battery Item, the battery's
TMDB/TVDB->IMDb mapping caches, and Plex's own match -- and the value of the
feature is that one confirmation fixes all four.  These tests pin that.

Importing routes.library_routes for real drags in the whole Flask app, so the
fix-match functions are loaded straight out of the module's source instead
(see _load_fix_match_module) and given stub collaborators.  The tests still run
against the real function bodies, so an edit to them shows up here.
"""

import ast
import os
import sqlite3
import threading
import types
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARY_ROUTES_PATH = os.path.join(PROJECT_ROOT, 'routes', 'library_routes.py')
PLEX_MATCHING_PATH = os.path.join(PROJECT_ROOT, 'utilities', 'plex_matching_functions.py')

# Module-level names the fix-match code owns, in source order.
FIX_MATCH_NAMES = (
    'FIX_MATCH_SHOW_TYPES',
    'FIX_MATCH_MOVIE_TYPES',
    '_fix_match_types',
    '_fix_match_row_filter',
    '_fix_match_normalize_ids',
    'fix_match_search',
    'fix_match_preview',
    '_fix_match_refresh_entry_metadata',
    '_fix_match_rematch_media_server',
    'fix_match_apply',
)


def _load_fix_match_module():
    """
    Build a module out of just the fix-match definitions in library_routes.py.

    Route decorators are dropped (ast puts them outside the node's source
    segment), so the view functions are plain callables here.
    """
    with open(LIBRARY_ROUTES_PATH, encoding='utf-8') as handle:
        source = handle.read()
    tree = ast.parse(source, filename=LIBRARY_ROUTES_PATH)

    segments = {}
    for node in tree.body:
        name = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
        if name in FIX_MATCH_NAMES:
            segments[name] = ast.get_source_segment(source, node)

    missing = [name for name in FIX_MATCH_NAMES if name not in segments]
    if missing:
        raise AssertionError(f'fix-match definitions not found in library_routes.py: {missing}')

    module = types.ModuleType('fix_match_under_test')
    module.__dict__.update({
        'logging': __import__('logging'),
        'datetime': datetime,
        # Replaced per-test.
        'request': None,
        'jsonify': None,
        'get_db_connection': None,
        'get_setting': None,
        # The Refresh Metadata views the fix chains into; stubbed per-test.
        'refresh_show_metadata': None,
        'refresh_movie_metadata': None,
    })
    code = '\n\n'.join(segments[name] for name in FIX_MATCH_NAMES)
    # Synthetic filename: line numbers in the concatenated extract do not line
    # up with library_routes.py, and naming the real file makes tracebacks
    # point at whatever unrelated code happens to sit on that line.
    exec(compile(code, '<library_routes fix-match extract>', 'exec'), module.__dict__)
    return module


fix_match = _load_fix_match_module()


# --------------------------------------------------------------------------
# Stub collaborators
# --------------------------------------------------------------------------

class StubRequest:
    """Stands in for flask.request with a fixed JSON body."""

    def __init__(self, payload):
        self._payload = payload

    def get_json(self, silent=False):
        return self._payload


def stub_jsonify(payload):
    """flask.jsonify without an app context: hand the dict straight back."""
    return payload


def unwrap(response):
    """Views return either payload or (payload, status)."""
    if isinstance(response, tuple):
        return response[0], response[1]
    return response, 200


class FakeItem:
    def __init__(self, imdb_id=None, **kwargs):
        self.imdb_id = imdb_id
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeTMDBMapping:
    def __init__(self, tmdb_id=None, imdb_id=None, media_type=None):
        self.tmdb_id = tmdb_id
        self.imdb_id = imdb_id
        self.media_type = media_type


class FakeTVDBMapping:
    def __init__(self, tvdb_id=None, imdb_id=None, media_type=None):
        self.tvdb_id = tvdb_id
        self.imdb_id = imdb_id
        self.media_type = media_type


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._filters = {}

    def filter_by(self, **kwargs):
        self._filters = kwargs
        return self

    def _matching(self):
        return [row for row in self._rows
                if all(getattr(row, key, None) == value for key, value in self._filters.items())]

    def first(self):
        matches = self._matching()
        return matches[0] if matches else None

    def delete(self):
        matches = self._matching()
        for row in matches:
            self._rows.remove(row)
        return len(matches)


class FakeBattery:
    """
    Minimal in-memory stand-in for the battery's SQLAlchemy session.

    Only the operations fix_match_apply performs are modelled: query/filter_by
    with first() and delete(), plus add(), delete(obj) and flush().
    """

    def __init__(self):
        self.items = []
        self.tmdb_mappings = []
        self.tvdb_mappings = []
        self.flush_count = 0

    def _rows_for(self, model):
        return {
            FakeItem: self.items,
            FakeTMDBMapping: self.tmdb_mappings,
            FakeTVDBMapping: self.tvdb_mappings,
        }[model]

    # -- session API --
    def query(self, model):
        return FakeQuery(self._rows_for(model))

    def add(self, obj):
        self._rows_for(type(obj)).append(obj)

    def delete(self, obj):
        self._rows_for(type(obj)).remove(obj)

    def flush(self):
        self.flush_count += 1

    @contextmanager
    def managed_session(self):
        yield self


def install_fake_battery(battery, metadata=None, imdb_for_tmdb=None):
    """
    Put fake cli_battery modules on sys.modules for the duration of a test.

    fix_match_apply/preview import them inside the function body, so they have
    to be resolvable at call time rather than at module load.

    Returns the DirectAPI stub so tests can assert on the calls it received.
    """
    import sys

    direct_api = MagicMock()
    direct_api.force_refresh_metadata.return_value = (metadata, 'trakt')
    direct_api.get_show_metadata.return_value = (metadata, 'trakt')
    direct_api.get_movie_metadata.return_value = (metadata, 'trakt')
    direct_api.tmdb_to_imdb.return_value = (imdb_for_tmdb, 'trakt')

    direct_api_module = types.ModuleType('cli_battery.app.direct_api')
    direct_api_module.DirectAPI = direct_api

    database_module = types.ModuleType('cli_battery.app.database')
    database_module.managed_session = battery.managed_session
    database_module.Item = FakeItem
    database_module.TMDBToIMDBMapping = FakeTMDBMapping
    database_module.TVDBToIMDBMapping = FakeTVDBMapping

    cli_battery = types.ModuleType('cli_battery')
    cli_battery_app = types.ModuleType('cli_battery.app')

    saved = {}
    for name, module in (('cli_battery', cli_battery),
                         ('cli_battery.app', cli_battery_app),
                         ('cli_battery.app.direct_api', direct_api_module),
                         ('cli_battery.app.database', database_module)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module

    def restore():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return direct_api, restore


SHOW_METADATA = {
    'title': 'Foundation',
    'year': 2021,
    'type': 'show',
    'ids': {'imdb': 'tt13375737', 'tmdb': 93740, 'tvdb': 375658, 'slug': 'foundation-2021'},
}


class FixMatchTestBase(unittest.TestCase):
    """Temp media_items DB plus the stub wiring every fix-match test needs."""

    def setUp(self):
        self.db_path = os.path.join(
            os.environ.get('TEMP', '/tmp'), f'fix_match_test_{os.getpid()}_{id(self)}.db')
        conn = self.connect()
        conn.execute(
            """
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY,
                imdb_id TEXT,
                tmdb_id TEXT,
                title TEXT,
                year INTEGER,
                type TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                state TEXT,
                ms_item_id TEXT,
                last_updated TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()

        fix_match.get_db_connection = self.connect
        fix_match.jsonify = stub_jsonify
        fix_match.get_setting = self.settings

        # fix_match_apply chains into the Refresh Metadata views. Record every
        # call, snapshotting the rows each one sees so ordering can be pinned.
        self.refresh_calls = []
        self.refresh_result = {'success': True, 'updated_episodes': 12,
                               'new_episodes_added': 3}

        def make_refresh(kind):
            def refresh(media_id):
                self.refresh_calls.append((kind, media_id, self.fetch_rows()))
                result = self.refresh_result
                return result() if callable(result) else result
            return refresh

        fix_match.refresh_show_metadata = make_refresh('show')
        fix_match.refresh_movie_metadata = make_refresh('movie')

        self.settings_values = {
            ('File Management', 'file_collection_management'): 'Plex',
            ('Plex', 'url'): 'http://localhost:32400',
            ('Plex', 'token'): 'a-token',
        }
        self._restore_battery = None

    def tearDown(self):
        if self._restore_battery:
            self._restore_battery()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def settings(self, section, key, default=None):
        return self.settings_values.get((section, key), default)

    def insert_rows(self, rows):
        conn = self.connect()
        conn.executemany(
            "INSERT INTO media_items "
            "(id, imdb_id, tmdb_id, title, year, type, season_number, episode_number, "
            " state, ms_item_id) "
            "VALUES (:id, :imdb_id, :tmdb_id, :title, :year, :type, :season_number, "
            ":episode_number, :state, :ms_item_id)",
            [dict({'season_number': None, 'episode_number': None, 'state': 'Collected',
                   'ms_item_id': None, 'year': 2020}, **row) for row in rows],
        )
        conn.commit()
        conn.close()

    def fetch_rows(self):
        conn = self.connect()
        rows = conn.execute(
            'SELECT id, imdb_id, tmdb_id, title, year, type FROM media_items ORDER BY id'
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def use_battery(self, battery, metadata=None, imdb_for_tmdb=None):
        direct_api, restore = install_fake_battery(battery, metadata, imdb_for_tmdb)
        self._restore_battery = restore
        return direct_api

    def call(self, view, payload):
        fix_match.request = StubRequest(payload)
        return unwrap(view())


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class TestNormalizeIds(unittest.TestCase):
    def test_reads_all_three_providers(self):
        ids = fix_match._fix_match_normalize_ids(SHOW_METADATA)
        self.assertEqual(ids['imdb_id'], 'tt13375737')
        self.assertEqual(ids['tmdb_id'], '93740')
        self.assertEqual(ids['tvdb_id'], '375658')
        self.assertEqual(ids['slug'], 'foundation-2021')

    def test_accepts_ids_stored_as_a_json_string(self):
        """Battery metadata rows round-trip through JSON, so `ids` can arrive as text."""
        ids = fix_match._fix_match_normalize_ids(
            {'ids': '{"imdb": "tt0903747", "tmdb": 1396, "tvdb": 81189}'})
        self.assertEqual(ids['imdb_id'], 'tt0903747')
        self.assertEqual(ids['tmdb_id'], '1396')
        self.assertEqual(ids['tvdb_id'], '81189')

    def test_missing_and_empty_ids_become_none(self):
        for metadata in ({}, {'ids': None}, {'ids': 'not json'}, {'ids': {'tmdb': ''}}):
            ids = fix_match._fix_match_normalize_ids(metadata)
            self.assertIsNone(ids['tmdb_id'], metadata)
            self.assertIsNone(ids['tvdb_id'], metadata)


class TestRowFilter(FixMatchTestBase):
    def test_matches_rows_by_either_provider_id(self):
        """A half-corrected entry (right IMDb, wrong TMDB) must still be caught whole."""
        self.insert_rows([
            {'id': 1, 'imdb_id': 'tt111', 'tmdb_id': '999', 'title': 'Wrong', 'type': 'episode'},
            {'id': 2, 'imdb_id': None, 'tmdb_id': '999', 'title': 'Wrong', 'type': 'episode'},
            {'id': 3, 'imdb_id': 'tt111', 'tmdb_id': None, 'title': 'Wrong', 'type': 'episode'},
            {'id': 4, 'imdb_id': 'tt222', 'tmdb_id': '888', 'title': 'Other', 'type': 'episode'},
        ])
        where, params = fix_match._fix_match_row_filter('show', 'tt111', '999')

        conn = self.connect()
        ids = [r['id'] for r in conn.execute(
            f'SELECT id FROM media_items WHERE {where} ORDER BY id', params).fetchall()]
        conn.close()
        self.assertEqual(ids, [1, 2, 3])

    def test_scopes_to_the_entry_kind(self):
        """A movie fix must not sweep up episode rows that share an ID."""
        self.insert_rows([
            {'id': 1, 'imdb_id': 'tt111', 'tmdb_id': '999', 'title': 'M', 'type': 'movie'},
            {'id': 2, 'imdb_id': 'tt111', 'tmdb_id': '999', 'title': 'E', 'type': 'episode'},
        ])
        where, params = fix_match._fix_match_row_filter('movie', 'tt111', '999')

        conn = self.connect()
        ids = [r['id'] for r in conn.execute(
            f'SELECT id FROM media_items WHERE {where}', params).fetchall()]
        conn.close()
        self.assertEqual(ids, [1])

    def test_show_filter_covers_show_and_episode_rows(self):
        self.insert_rows([
            {'id': 1, 'imdb_id': 'tt111', 'tmdb_id': None, 'title': 'S', 'type': 'show'},
            {'id': 2, 'imdb_id': 'tt111', 'tmdb_id': None, 'title': 'E', 'type': 'episode'},
            {'id': 3, 'imdb_id': 'tt111', 'tmdb_id': None, 'title': 'M', 'type': 'movie'},
        ])
        where, params = fix_match._fix_match_row_filter('show', 'tt111', None)

        conn = self.connect()
        ids = [r['id'] for r in conn.execute(
            f'SELECT id FROM media_items WHERE {where} ORDER BY id', params).fetchall()]
        conn.close()
        self.assertEqual(ids, [1, 2])

    def test_refuses_to_build_an_unbounded_filter(self):
        """With no IDs there is nothing to scope by -- must not match everything."""
        self.assertEqual(fix_match._fix_match_row_filter('show', None, None), (None, None))
        self.assertEqual(fix_match._fix_match_row_filter('movie', '', ''), (None, None))


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

class TestFixMatchSearch(FixMatchTestBase):
    def test_rejects_an_empty_query(self):
        self.use_battery(FakeBattery())
        payload, status = self.call(fix_match.fix_match_search, {'query': '  '})
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])

    def test_normalises_results(self):
        battery = FakeBattery()
        direct_api = self.use_battery(battery)
        direct_api.search_media.return_value = (
            [{'title': 'Foundation', 'year': 2021, 'imdb_id': 'tt13375737',
              'tmdb_id': 93740, 'type': 'show'}],
            'trakt',
        )

        payload, status = self.call(
            fix_match.fix_match_search,
            {'query': 'Foundation', 'year': '2021', 'media_type': 'show'})

        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['results'], [
            {'title': 'Foundation', 'year': 2021, 'imdb_id': 'tt13375737',
             'tmdb_id': '93740', 'type': 'show'},
        ])
        direct_api.search_media.assert_called_once_with(
            query='Foundation', year=2021, media_type='show')

    def test_reports_provider_failure_rather_than_an_empty_list(self):
        battery = FakeBattery()
        direct_api = self.use_battery(battery)
        direct_api.search_media.return_value = (None, None)

        payload, status = self.call(fix_match.fix_match_search, {'query': 'Foundation'})
        self.assertEqual(status, 502)
        self.assertFalse(payload['success'])


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------

class TestFixMatchPreview(FixMatchTestBase):
    def test_resolves_all_three_ids_from_an_imdb_id(self):
        self.use_battery(FakeBattery(), metadata=SHOW_METADATA)

        payload, status = self.call(fix_match.fix_match_preview, {
            'imdb_id': 'tt13375737', 'media_type': 'show'})

        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['imdb_id'], 'tt13375737')
        self.assertEqual(payload['tmdb_id'], '93740')
        self.assertEqual(payload['tvdb_id'], '375658')

    def test_converts_a_tmdb_id_first(self):
        direct_api = self.use_battery(FakeBattery(), metadata=SHOW_METADATA,
                                      imdb_for_tmdb='tt13375737')

        payload, _ = self.call(fix_match.fix_match_preview, {
            'tmdb_id': '93740', 'media_type': 'show'})

        direct_api.tmdb_to_imdb.assert_called_once_with('93740', media_type='show')
        self.assertEqual(payload['imdb_id'], 'tt13375737')

    def test_rejects_a_malformed_imdb_id(self):
        self.use_battery(FakeBattery(), metadata=SHOW_METADATA)
        payload, status = self.call(fix_match.fix_match_preview, {
            'imdb_id': 'nope', 'media_type': 'show'})
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])

    def test_reports_the_blast_radius_before_anything_is_written(self):
        self.insert_rows([
            {'id': 1, 'imdb_id': 'tt000', 'tmdb_id': '111', 'title': 'Wrong Show',
             'year': 1999, 'type': 'episode'},
            {'id': 2, 'imdb_id': 'tt000', 'tmdb_id': '111', 'title': 'Wrong Show',
             'year': 1999, 'type': 'episode'},
            {'id': 3, 'imdb_id': 'tt999', 'tmdb_id': '222', 'title': 'Untouched',
             'year': 2001, 'type': 'episode'},
        ])
        self.use_battery(FakeBattery(), metadata=SHOW_METADATA)

        payload, _ = self.call(fix_match.fix_match_preview, {
            'imdb_id': 'tt13375737', 'media_type': 'show',
            'current_imdb_id': 'tt000', 'current_tmdb_id': '111'})

        self.assertEqual(payload['affected_rows'], 2)
        self.assertEqual(payload['affected_titles'],
                         [{'title': 'Wrong Show', 'year': 1999, 'count': 2}])
        # Preview is read-only.
        self.assertEqual([r['imdb_id'] for r in self.fetch_rows()],
                         ['tt000', 'tt000', 'tt999'])


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------

class TestFixMatchApply(FixMatchTestBase):
    def setUp(self):
        super().setUp()
        self.insert_rows([
            {'id': 1, 'imdb_id': 'tt000', 'tmdb_id': '111', 'title': 'Wrong Show',
             'year': 1999, 'type': 'episode', 'ms_item_id': None},
            {'id': 2, 'imdb_id': 'tt000', 'tmdb_id': '111', 'title': 'Wrong Show',
             'year': 1999, 'type': 'episode', 'ms_item_id': '54321'},
            {'id': 3, 'imdb_id': 'tt999', 'tmdb_id': '222', 'title': 'Untouched',
             'year': 2001, 'type': 'episode', 'ms_item_id': '77'},
        ])
        self.battery = FakeBattery()
        self.battery.items.append(FakeItem(imdb_id='tt000'))
        self.battery.tmdb_mappings.append(FakeTMDBMapping('111', 'tt000', 'show'))
        self.battery.tvdb_mappings.append(FakeTVDBMapping('4444', 'tt000', 'show'))

        # Keep the rematch out of the request path; asserted separately.
        self.rematch_calls = []
        self.rematch_ran = threading.Event()
        original = fix_match._fix_match_rematch_media_server

        def recorder(*args):
            self.rematch_calls.append(args)
            self.rematch_ran.set()

        fix_match._fix_match_rematch_media_server = recorder
        self.addCleanup(setattr, fix_match, '_fix_match_rematch_media_server', original)

    def apply(self, **overrides):
        payload = dict({
            'media_type': 'show',
            'new_imdb_id': 'tt13375737',
            'current_imdb_id': 'tt000',
            'current_tmdb_id': '111',
            'rematch_media_server': False,
        }, **overrides)
        return self.call(fix_match.fix_match_apply, payload)

    def test_rewrites_all_three_ids_across_the_entry(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, status = self.apply()

        self.assertEqual(status, 200)
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['imdb_id'], 'tt13375737')
        self.assertEqual(payload['tmdb_id'], '93740')
        self.assertEqual(payload['tvdb_id'], '375658')
        self.assertEqual(payload['rows_updated'], 2)

        rows = self.fetch_rows()
        self.assertEqual(
            [(r['imdb_id'], r['tmdb_id'], r['title'], r['year']) for r in rows],
            [
                ('tt13375737', '93740', 'Foundation', 2021),
                ('tt13375737', '93740', 'Foundation', 2021),
                ('tt999', '222', 'Untouched', 2001),  # a different entry, left alone
            ],
        )

    def test_repoints_the_battery_item_and_both_mapping_caches(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply()

        self.assertTrue(payload['battery_item_deleted'])
        self.assertEqual([i.imdb_id for i in self.battery.items], [])

        self.assertEqual(len(self.battery.tmdb_mappings), 1)
        self.assertEqual(
            (self.battery.tmdb_mappings[0].tmdb_id, self.battery.tmdb_mappings[0].imdb_id),
            ('93740', 'tt13375737'))

        self.assertEqual(len(self.battery.tvdb_mappings), 1)
        self.assertEqual(
            (self.battery.tvdb_mappings[0].tvdb_id, self.battery.tvdb_mappings[0].imdb_id),
            ('375658', 'tt13375737'))

    def test_refreshes_metadata_under_the_new_id_with_the_right_type(self):
        direct_api = self.use_battery(self.battery, metadata=SHOW_METADATA)
        self.apply()
        direct_api.force_refresh_metadata.assert_called_once_with(
            'tt13375737', item_type='show')

    def test_aborts_without_touching_anything_when_metadata_is_missing(self):
        self.use_battery(self.battery, metadata=None)

        payload, status = self.apply()

        self.assertFalse(payload['success'])
        self.assertIn('nothing was changed', payload['error'])
        self.assertEqual([r['imdb_id'] for r in self.fetch_rows()],
                         ['tt000', 'tt000', 'tt999'])
        self.assertEqual([i.imdb_id for i in self.battery.items], ['tt000'])
        self.assertEqual(len(self.battery.tmdb_mappings), 1)

    def test_keeps_the_battery_item_when_only_tmdb_or_tvdb_was_wrong(self):
        """Same IMDb ID, wrong TMDB: refresh in place rather than delete."""
        self.battery.items[:] = [FakeItem(imdb_id='tt13375737')]
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply(current_imdb_id='tt13375737')

        self.assertFalse(payload['battery_item_deleted'])
        self.assertEqual([i.imdb_id for i in self.battery.items], ['tt13375737'])

    def test_does_not_blank_title_when_the_provider_gives_none(self):
        self.use_battery(self.battery, metadata={'ids': {'imdb': 'tt13375737', 'tmdb': 93740}})

        payload, _ = self.apply()

        self.assertTrue(payload['success'])
        rows = self.fetch_rows()
        self.assertEqual([r['title'] for r in rows[:2]], ['Wrong Show', 'Wrong Show'])
        self.assertEqual([r['imdb_id'] for r in rows[:2]], ['tt13375737', 'tt13375737'])

    def test_rejects_a_malformed_new_imdb_id(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)
        payload, status = self.apply(new_imdb_id='12345')
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])

    def test_requires_a_current_id_to_scope_the_rewrite(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)
        payload, status = self.apply(current_imdb_id='', current_tmdb_id='')
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])
        self.assertEqual([r['imdb_id'] for r in self.fetch_rows()],
                         ['tt000', 'tt000', 'tt999'])

    # -- chained metadata refresh --

    def test_chains_into_the_refresh_for_the_corrected_id(self):
        """
        Correcting the IDs leaves episode titles, air dates and artwork
        describing the old match, so the refresh is part of the fix.
        """
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply()

        self.assertEqual(len(self.refresh_calls), 1)
        kind, media_id, _rows = self.refresh_calls[0]
        self.assertEqual((kind, media_id), ('show', 'tt13375737'))
        self.assertTrue(payload['metadata_refresh']['success'])

    def test_uses_the_movie_refresh_for_a_movie_entry(self):
        conn = self.connect()
        conn.execute("UPDATE media_items SET type = 'movie'")
        conn.commit()
        conn.close()
        self.use_battery(self.battery, metadata=dict(SHOW_METADATA, type='movie'))

        self.apply(media_type='movie')

        self.assertEqual([c[0] for c in self.refresh_calls], ['movie'])

    def test_refresh_runs_only_once_the_rows_carry_the_new_ids(self):
        """The refresh looks the entry up by ID, so it must run after the rewrite."""
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        self.apply()

        _kind, _media_id, rows_seen = self.refresh_calls[0]
        self.assertEqual([r['imdb_id'] for r in rows_seen[:2]],
                         ['tt13375737', 'tt13375737'])

    def test_reports_what_the_refresh_did(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply()

        self.assertEqual(payload['metadata_refresh'], {
            'success': True,
            'updated_episodes': 12,
            'new_episodes_added': 3,
            'error': None,
        })

    def test_a_failing_refresh_does_not_undo_the_id_fix(self):
        """IDs are already committed when the refresh runs; report, do not fail."""
        def boom(media_id):
            raise RuntimeError('trakt unreachable')

        fix_match.refresh_show_metadata = boom
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        with self.assertLogs(level='ERROR'):
            payload, status = self.apply()

        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['rows_updated'], 2)
        self.assertFalse(payload['metadata_refresh']['success'])
        self.assertIn('trakt unreachable', payload['metadata_refresh']['error'])
        self.assertEqual([r['imdb_id'] for r in self.fetch_rows()][:2],
                         ['tt13375737', 'tt13375737'])

    def test_a_refresh_error_response_is_surfaced(self):
        """The views return (payload, status) on failure -- unwrap, don't drop."""
        self.refresh_result = lambda: ({'success': False, 'error': 'Show not found'}, 404)
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply()

        self.assertTrue(payload['success'])
        self.assertFalse(payload['metadata_refresh']['success'])
        self.assertEqual(payload['metadata_refresh']['error'], 'Show not found')

    # -- media server rematch --

    def test_dispatches_the_rematch_with_the_corrected_ids(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply(rematch_media_server=True)

        self.assertTrue(payload['rematch_started'])
        self.assertTrue(self.rematch_ran.wait(timeout=5))
        rating_key, title, year, tmdb_id, imdb_id, media_type = self.rematch_calls[0]
        self.assertEqual(rating_key, '54321')  # the one row that has a Plex ratingKey
        self.assertEqual((title, year), ('Foundation', 2021))
        self.assertEqual((tmdb_id, imdb_id, media_type), ('93740', 'tt13375737', 'show'))

    def test_skips_the_rematch_when_no_rating_key_is_stored(self):
        conn = self.connect()
        conn.execute('UPDATE media_items SET ms_item_id = NULL')
        conn.commit()
        conn.close()
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply(rematch_media_server=True)

        self.assertFalse(payload['rematch_started'])
        self.assertIn('ratingKey', payload['rematch_note'])
        self.assertEqual(self.rematch_calls, [])

    def test_skips_the_rematch_outside_plex_mode(self):
        self.settings_values[('File Management', 'file_collection_management')] = 'Symlinked/Local'
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply(rematch_media_server=True)

        self.assertFalse(payload['rematch_started'])
        self.assertIn('not in Plex mode', payload['rematch_note'])
        self.assertEqual(self.rematch_calls, [])

    def test_ids_are_still_fixed_when_the_rematch_is_declined(self):
        self.use_battery(self.battery, metadata=SHOW_METADATA)

        payload, _ = self.apply(rematch_media_server=False)

        self.assertFalse(payload['rematch_started'])
        self.assertEqual(payload['rows_updated'], 2)
        self.assertEqual(self.rematch_calls, [])


# --------------------------------------------------------------------------
# The Plex rematch helper
# --------------------------------------------------------------------------

class TestRematchHelper(unittest.TestCase):
    def _call_helper(self):
        import sys

        force_match = MagicMock(return_value=True)
        module = types.ModuleType('utilities.plex_matching_functions')
        module.force_match_with_tmdb = force_match

        saved = sys.modules.get('utilities.plex_matching_functions')
        sys.modules['utilities.plex_matching_functions'] = module
        try:
            fix_match._fix_match_rematch_media_server(
                '54321', 'Foundation', 2021, '93740', 'tt13375737', 'show')
        finally:
            if saved is None:
                sys.modules.pop('utilities.plex_matching_functions', None)
            else:
                sys.modules['utilities.plex_matching_functions'] = saved
        return force_match

    def test_bypasses_the_once_per_rating_key_guard(self):
        """
        force_match_with_tmdb normally refuses a second attempt on a ratingKey.
        A hand-driven Fix Match is exactly the case where a repeat is intended.
        """
        force_match = self._call_helper()
        _, kwargs = force_match.call_args
        self.assertIs(kwargs['ignore_previous_attempts'], True)

    def test_requests_an_entry_level_match(self):
        """
        Passing season/episode makes the GUID fast-path apply an *episode* GUID.
        A Fix Match is show- or movie-level, so neither may be sent.
        """
        force_match = self._call_helper()
        args, kwargs = force_match.call_args
        self.assertEqual(args, ('Foundation', '2021', '93740', '54321'))
        self.assertEqual(kwargs['media_type'], 'show')
        self.assertIsNone(kwargs.get('season'))
        self.assertIsNone(kwargs.get('episode'))

    def test_a_failing_rematch_does_not_raise(self):
        """It runs on a daemon thread -- an exception there would be silent and lost."""
        import sys

        module = types.ModuleType('utilities.plex_matching_functions')
        module.force_match_with_tmdb = MagicMock(side_effect=RuntimeError('plex down'))
        saved = sys.modules.get('utilities.plex_matching_functions')
        sys.modules['utilities.plex_matching_functions'] = module
        try:
            with self.assertLogs(level='ERROR') as captured:
                fix_match._fix_match_rematch_media_server(
                    '54321', 'Foundation', 2021, '93740', 'tt13375737', 'show')
        finally:
            if saved is None:
                sys.modules.pop('utilities.plex_matching_functions', None)
            else:
                sys.modules['utilities.plex_matching_functions'] = saved

        self.assertIn('plex down', ' '.join(captured.output))


class TestForceMatchSignature(unittest.TestCase):
    """
    utilities/plex_matching_functions.py cannot be imported without plexapi, so
    the contract the rematch helper depends on is checked against the source.
    """

    def _force_match_node(self):
        with open(PLEX_MATCHING_PATH, encoding='utf-8') as handle:
            tree = ast.parse(handle.read(), filename=PLEX_MATCHING_PATH)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == 'force_match_with_tmdb':
                return node
        self.fail('force_match_with_tmdb not found')

    def test_accepts_ignore_previous_attempts_defaulting_to_off(self):
        node = self._force_match_node()
        args = [a.arg for a in node.args.args]
        self.assertIn('ignore_previous_attempts', args)

        # Defaults line up with the tail of the argument list.
        defaults = dict(zip(args[len(args) - len(node.args.defaults):], node.args.defaults))
        default = defaults['ignore_previous_attempts']
        self.assertIsInstance(default, ast.Constant)
        self.assertIs(default.value, False,
                      'existing callers must keep the once-per-rating-key guard')

    def test_the_guard_honours_the_flag(self):
        node = self._force_match_node()
        source = ast.unparse(node)
        self.assertIn('if not ignore_previous_attempts and _has_been_attempted', source)


class TestModalOverlayContract(unittest.TestCase):
    """
    Source-level guards for the two things that made the modal unusable.

    The markup is included from the page's content block, which renders inside
    <main> -- and base.css gives main `position: relative; z-index: 1`, a
    stacking context. Anything the modal sets for z-index is scoped inside it,
    so it cannot rise above the nav, toasts or the full-viewport #loading
    overlay, all of which sit at 9999 in the root stacking context.
    """

    @staticmethod
    def _read(*parts):
        with open(os.path.join(PROJECT_ROOT, *parts), encoding='utf-8') as handle:
            return handle.read()

    def test_main_is_still_a_stacking_context(self):
        """If this ever stops being true the reparenting comment is stale."""
        base_css = self._read('static', 'css', 'base.css')
        main_rule = base_css.split('\nmain {', 1)[1].split('}', 1)[0]
        self.assertIn('position: relative', main_rule)
        self.assertIn('z-index: 1', main_rule)

    def test_modal_is_reparented_to_the_body(self):
        js = self._read('static', 'js', 'fix_match_modal.js')
        self.assertIn('document.body.appendChild(modal)', js)

    def test_focus_never_scrolls_the_page(self):
        """A focus() that scrolls jumps the reader away from a locked page."""
        js = self._read('static', 'js', 'fix_match_modal.js')
        self.assertIn('preventScroll: true', js)
        self.assertNotIn('searchInput.focus();', js)

    def test_scroll_lock_is_conditional_on_the_overlay_rendering(self):
        """Locking scroll with no visible overlay strands the reader."""
        js = self._read('static', 'js', 'fix_match_modal.js')
        lock = js.split("document.body.style.overflow = 'hidden'", 1)[0]
        self.assertIn("getComputedStyle(modal).position === 'fixed'", lock)


if __name__ == '__main__':
    unittest.main(verbosity=2)
