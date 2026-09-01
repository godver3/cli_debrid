#!/usr/bin/env python3
"""Tests for the durable title -> IMDb corrections behind Fix Match.

Every import of a file cli_debrid did not place itself re-derives the match by
fuzzy-searching the metadata provider for the parsed release title. That search
is stateless, so a title it gets wrong ("Sugar" 2024 landing on "Sugar Sugar
Honey") is wrong again for every new release, undoing a Fix Match correction as
soon as the next file arrives. These overrides are what make the fix stick.
"""

import ast
import importlib.util
import os
import sqlite3
import sys
import types
import unicodedata
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEBUG_ROUTES_PATH = os.path.join(PROJECT_ROOT, 'routes', 'debug_routes.py')


def _load_match_overrides():
    """
    Load database/match_overrides.py without importing the database package.

    `import database` pulls in the whole app, which does not import on Python
    3.14 (an unrelated enum/auto() incompatibility in debrid.status). The module
    is loaded into a private package instead, so its two relative imports
    resolve to stubs here rather than to the real package -- and nothing this
    test does leaks into sys.modules['database'] for other test modules.
    """
    pkg_name = '_match_overrides_under_test'
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(PROJECT_ROOT, 'database')]
    sys.modules[pkg_name] = pkg

    core = types.ModuleType(pkg_name + '.core')
    core.get_db_connection = None  # patched per test
    sys.modules[pkg_name + '.core'] = core

    reading = types.ModuleType(pkg_name + '.database_reading')
    reading.normalize_string_for_comparison = (
        lambda text: unicodedata.normalize('NFC', text).lower() if text else text)
    sys.modules[pkg_name + '.database_reading'] = reading

    spec = importlib.util.spec_from_file_location(
        pkg_name + '.match_overrides',
        os.path.join(PROJECT_ROOT, 'database', 'match_overrides.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


match_overrides = _load_match_overrides()


class TestMatchOverrideStore(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            os.environ.get('TEMP', '/tmp'), f'match_overrides_{os.getpid()}_{id(self)}.db')
        self.patcher = patch.object(match_overrides, 'get_db_connection',
                                    side_effect=self.connect)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_round_trips_a_correction(self):
        self.assertTrue(match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808'))
        self.assertEqual(match_overrides.get_match_override('Sugar', 2024, 'show'), 'tt16418808')

    def test_lookup_is_case_and_whitespace_insensitive(self):
        """Release names are parsed inconsistently; the key must not be brittle."""
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        for variant in ('sugar', 'SUGAR', '  Sugar  '):
            self.assertEqual(match_overrides.get_match_override(variant, 2024, 'show'),
                             'tt16418808', variant)

    def test_year_is_part_of_the_key(self):
        """A title alone would hijack an unrelated show of the same name."""
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        self.assertIsNone(match_overrides.get_match_override('Sugar', 1998, 'show'))

    def test_media_type_is_part_of_the_key(self):
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        self.assertIsNone(match_overrides.get_match_override('Sugar', 2024, 'movie'))

    def test_a_yearless_override_acts_as_a_wildcard(self):
        """A correction made from an entry with no year still has to apply."""
        match_overrides.set_match_override('Sugar', None, 'show', 'tt16418808')
        self.assertEqual(match_overrides.get_match_override('Sugar', 2024, 'show'), 'tt16418808')
        self.assertEqual(match_overrides.get_match_override('Sugar', None, 'show'), 'tt16418808')

    def test_an_exact_year_wins_over_the_wildcard(self):
        match_overrides.set_match_override('Sugar', None, 'show', 'tt00000001')
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        self.assertEqual(match_overrides.get_match_override('Sugar', 2024, 'show'), 'tt16418808')

    def test_correcting_twice_replaces_rather_than_duplicates(self):
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt00000001')
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        self.assertEqual(match_overrides.get_match_override('Sugar', 2024, 'show'), 'tt16418808')

        conn = self.connect()
        count = conn.execute('SELECT COUNT(*) FROM match_overrides').fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_rejects_incomplete_or_nonsense_input(self):
        self.assertFalse(match_overrides.set_match_override('', 2024, 'show', 'tt1'))
        self.assertFalse(match_overrides.set_match_override('Sugar', 2024, 'show', ''))
        self.assertFalse(match_overrides.set_match_override('Sugar', 2024, 'episode', 'tt1'))

    def test_a_non_numeric_year_does_not_explode(self):
        self.assertTrue(match_overrides.set_match_override('Sugar', 'n/a', 'show', 'tt16418808'))
        # Stored as the year-less wildcard, so it still resolves.
        self.assertEqual(match_overrides.get_match_override('Sugar', 2024, 'show'), 'tt16418808')

    def test_missing_lookup_returns_none(self):
        self.assertIsNone(match_overrides.get_match_override('Nothing Here', 2024, 'show'))

    def test_find_checks_every_candidate_title(self):
        """The importer has a folder title, a filename title and the raw parse."""
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        found = match_overrides.find_match_override(
            [None, 'Some Episode Title', 'Sugar'], 2024, 'show')
        self.assertEqual(found, 'tt16418808')

    def test_find_returns_none_when_nothing_matches(self):
        match_overrides.set_match_override('Sugar', 2024, 'show', 'tt16418808')
        self.assertIsNone(
            match_overrides.find_match_override(['Prime Target', 'Hacks'], 2024, 'show'))


class TestResolverHonoursOverrides(unittest.TestCase):
    """
    routes/debug_routes.py cannot be imported here, so the wiring is checked
    against the source of the function that resolves an import to an ID.
    """

    @staticmethod
    def _resolver_source():
        with open(DEBUG_ROUTES_PATH, encoding='utf-8') as handle:
            source = handle.read()
        tree = ast.parse(source, filename=DEBUG_ROUTES_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_run_rclone_to_symlink_task':
                return ast.get_source_segment(source, node)
        raise AssertionError('_run_rclone_to_symlink_task not found')

    def test_the_import_path_consults_the_overrides(self):
        self.assertIn('find_match_override', self._resolver_source())

    def test_an_override_beats_the_fuzzy_search(self):
        """
        The override has to be checked before find_best_match_from_results wins,
        otherwise the search that mis-resolved the title decides again.
        """
        source = self._resolver_source()
        self.assertIn('if override_imdb_id:', source)
        self.assertIn('elif best_match_from_search:', source)
        self.assertLess(source.index('if override_imdb_id:'),
                        source.index('elif best_match_from_search:'))

    def test_the_search_is_skipped_when_an_override_applies(self):
        source = self._resolver_source()
        self.assertIn('if final_search_results and not override_imdb_id:', source)


class TestOverrideTableIsCreated(unittest.TestCase):
    def test_schema_management_creates_it_on_every_init_path(self):
        """
        schema_management has two table-creation paths; an override table that
        only exists on one of them fails on whichever install took the other.
        """
        with open(os.path.join(PROJECT_ROOT, 'database', 'schema_management.py'),
                  encoding='utf-8') as handle:
            source = handle.read()
        self.assertEqual(source.count('ensure_match_override_table(conn)'), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
