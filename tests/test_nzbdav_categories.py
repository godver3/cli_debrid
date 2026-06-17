#!/usr/bin/env python3
"""
Unit tests for the NzbDAV configurable category map.

Exercises the REAL functions in usenet/nzbdav_client.py by stubbing the two
cli-debrid imports that module pulls at load time (utilities.settings and
routes.api_tracker), so no full app/config is needed.

Core invariant under test: the title heuristic can NEVER route a release into a
category that the configured map doesn't manage — every emitted bucket resolves
to one of managed_categories(map). This is what prevents "invisible items"
(uploads landing in a category with no matching Plex section).
"""

import unittest
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_client_module():
    # Stub the two cli-debrid imports nzbdav_client does at module scope.
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: {}
    sys.modules['utilities.settings'] = uss
    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')
    rta.api = types.SimpleNamespace(get=None, post=None)
    sys.modules['routes.api_tracker'] = rta
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'nzbdav_client.py')
    spec = importlib.util.spec_from_file_location('nzbdav_client_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


nc = _load_client_module()

# A realistic non-default map: base + a 1080p split under custom names, 4K/anime
# deliberately NOT split (they collapse to base).
OURMAP = nc._parse_category_map(
    'movies=movies, shows=shows, '
    'movies_1080p=movies_1080p_264, shows_1080p=shows_1080p_264, '
    'fallback=__unplayable__'
)


class TestParseCategoryMap(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(nc._parse_category_map(''), {})
        self.assertEqual(nc._parse_category_map(None), {})

    def test_pairs_and_tolerance(self):
        got = nc._parse_category_map('movies=movies,  movies_1080p = movies_1080p_264 ,bad,=x,y=')
        self.assertEqual(got, {'movies': 'movies', 'movies_1080p': 'movies_1080p_264'})


class TestResolveCategory(unittest.TestCase):
    def test_identity_when_empty_map(self):
        # Empty map = stock behaviour: bucket used verbatim.
        for b in ['movies', 'movies_1080p', 'movies_2160p', 'anime_shows', 'music']:
            self.assertEqual(nc._resolve_category(b, {}), b)

    def test_direct_hit(self):
        self.assertEqual(nc._resolve_category('movies_1080p', OURMAP), 'movies_1080p_264')
        self.assertEqual(nc._resolve_category('shows_1080p', OURMAP), 'shows_1080p_264')
        self.assertEqual(nc._resolve_category('movies', OURMAP), 'movies')

    def test_parent_walk(self):
        # 2160p not mapped -> walks to base movies/shows
        self.assertEqual(nc._resolve_category('movies_2160p', OURMAP), 'movies')
        self.assertEqual(nc._resolve_category('shows_2160p', OURMAP), 'shows')
        # 2160p remux -> 2160p -> movies
        self.assertEqual(nc._resolve_category('movies_2160p_remux', OURMAP), 'movies')
        # 1080p remux -> 1080p (mapped) -> custom name
        self.assertEqual(nc._resolve_category('movies_1080p_remux', OURMAP), 'movies_1080p_264')
        # anime -> base
        self.assertEqual(nc._resolve_category('anime_movies', OURMAP), 'movies')
        self.assertEqual(nc._resolve_category('anime_shows', OURMAP), 'shows')

    def test_fallback(self):
        # music has no parent and isn't mapped -> explicit fallback value
        self.assertEqual(nc._resolve_category('music', OURMAP), '__unplayable__')
        # empty bucket (heuristic found nothing) -> fallback
        self.assertEqual(nc._resolve_category('', OURMAP), '__unplayable__')

    def test_no_fallback_returns_empty(self):
        m = {'movies': 'movies'}  # no fallback key
        self.assertEqual(nc._resolve_category('music', m), '')

    def test_tag_exclusive_routes_to_own_category_not_fallback(self):
        # tags_exclusive passes the tag itself as the bucket. A tag is not a
        # structural bucket, so with a custom map it must NOT fall to the
        # catch-all — it should land in its own category verbatim.
        self.assertEqual(nc._resolve_category('ufc', OURMAP), 'ufc')
        self.assertEqual(nc._resolve_category('sports', OURMAP), 'sports')
        # a tag that the user explicitly mapped still honors the mapping
        m = dict(OURMAP); m['ufc'] = 'combat_sports'
        self.assertEqual(nc._resolve_category('ufc', m), 'combat_sports')
        # structural buckets are unaffected: unmapped 2160p still -> fallback
        self.assertEqual(nc._resolve_category('movies_2160p', {'movies': 'movies', 'fallback': '__unplayable__'}), 'movies')
        self.assertEqual(nc._resolve_category('music', OURMAP), '__unplayable__')


class TestManagedCategories(unittest.TestCase):
    def test_default_excludes_music(self):
        managed = nc.managed_categories({})
        self.assertIn('movies', managed)
        self.assertIn('shows', managed)
        self.assertIn('__unplayable__', managed)
        self.assertNotIn('music', managed)  # repair must never own Lidarr's music

    def test_from_map_values(self):
        self.assertEqual(
            nc.managed_categories(OURMAP),
            {'movies', 'shows', 'movies_1080p_264', 'shows_1080p_264', '__unplayable__'},
        )


class TestNoInvisibleCategoryInvariant(unittest.TestCase):
    """Every category the heuristic can emit must resolve into the managed set."""

    def test_all_emittable_buckets_resolve_into_managed_set(self):
        managed = nc.managed_categories(OURMAP)
        # All buckets the heuristic can produce (canonical taxonomy + base + music + '')
        emittable = set(nc._CATEGORY_PARENT) | {'movies', 'shows', 'music', '__unplayable__', ''}
        for b in emittable:
            resolved = nc._resolve_category(b, OURMAP)
            self.assertIn(resolved, managed,
                          f'bucket {b!r} resolved to {resolved!r} which is NOT in managed set {managed}')

    def test_heuristic_outputs_route_into_managed_set(self):
        managed = nc.managed_categories(OURMAP)
        titles = [
            'The.Matrix.1999.1080p.BluRay.x264-GRP',
            'Dune.2021.2160p.UHD.BluRay.x265-GRP',
            'Movie.2020.1080p.REMUX.AVC-GRP',
            'Show.S01E02.1080p.WEB.h264-GRP',
            'Show.S05E01.2160p.WEB-GRP',
            'Random.720p.Thing-GRP',
            'Totally unstructured name',
        ]
        for t in titles:
            bucket = nc._detect_category_from_title(t)
            resolved = nc._resolve_category(bucket, OURMAP)
            # '' is acceptable only when no fallback; here OURMAP has a fallback,
            # so every title must land in a managed category.
            self.assertIn(resolved, managed,
                          f'title {t!r} -> bucket {bucket!r} -> {resolved!r} not in {managed}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
