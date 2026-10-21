#!/usr/bin/env python3
"""Plex-mode scans from the mount's all-folder must reach category-folder libraries.

Reported 2026-09-26 (ocja2889): with mounted_file_location=/data2/__all__ and
Plex libraries on /data2/shows and /data2/movies (the layout the zurg/cli_mount
docs recommend), plex_update_item never matched a section (0 of 614 scans) and
new items only appeared after Plex's scheduled scan.
"""

import os
import tempfile
import unittest

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
from utilities.plex_functions import _mirrored_library_path


class TestMirroredLibraryPath(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.rel = 'South.Park.S20E01.1080p.BluRay.REMUX-EPSiLON'
        for cat in ('__all__', 'shows'):
            os.makedirs(os.path.join(self.root, cat, self.rel))
        os.makedirs(os.path.join(self.root, 'movies'))

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_all_folder_maps_to_category_library(self):
        self.assertEqual(
            _mirrored_library_path(self.p('__all__', self.rel), [self.p('shows')]),
            self.p('shows', self.rel))

    def test_nested_directory(self):
        os.makedirs(self.p('__all__', self.rel, 'Season 1'))
        os.makedirs(self.p('shows', self.rel, 'Season 1'))
        self.assertEqual(
            _mirrored_library_path(self.p('__all__', self.rel, 'Season 1'), [self.p('shows')]),
            self.p('shows', self.rel, 'Season 1'))

    def test_release_not_in_library_folder(self):
        self.assertIsNone(_mirrored_library_path(self.p('__all__', self.rel), [self.p('movies')]))

    def test_library_inside_all_folder_is_not_a_mirror(self):
        os.makedirs(self.p('__all__', 'other'))
        self.assertIsNone(
            _mirrored_library_path(self.p('__all__', self.rel), [self.p('__all__', 'other')]))

    def test_unrelated_location(self):
        other = tempfile.mkdtemp()
        self.assertIsNone(_mirrored_library_path(self.p('__all__', self.rel), [other]))


if __name__ == '__main__':
    unittest.main()


from unittest import mock
import utilities.plex_functions as pf


class _Section:
    def __init__(self, key, title, type_, locations):
        self.key, self.title, self.type, self.locations = key, title, type_, locations
        self.update = mock.Mock()


class TestPlexUpdateItemUsesMirror(unittest.TestCase):
    def test_scan_goes_to_category_library(self):
        root = tempfile.mkdtemp()
        rel = 'The Office US S01E04'
        for cat in ('__all__', 'shows'):
            os.makedirs(os.path.join(root, cat, rel))
        shows = _Section(4, 'TV Shows-DB', 'show', [os.path.join(root, 'shows')])
        movies = _Section(3, 'Movies-DB', 'movie', [os.path.join(root, 'movies')])
        server = mock.Mock()
        server.library.sections.return_value = [movies, shows]
        settings = {('File Management', 'plex_url_for_symlink'): 'http://plex:32400',
                    ('File Management', 'plex_token_for_symlink'): 'tok',
                    ('File Management', 'plex_section_update_timeout'): 5,
                    ('Plex', 'shows_libraries'): 'TV Shows-DB'}
        with mock.patch.object(pf, 'PlexServer', return_value=server), \
             mock.patch.object(pf, 'get_setting', side_effect=lambda s, k, d='', **kw: settings.get((s, k), d if d is not None else '')), \
             mock.patch.object(pf.time, 'sleep'):
            ok = pf.plex_update_item({'full_path': os.path.join(root, '__all__', rel), 'type': 'episode'})
        self.assertTrue(ok)
        shows.update.assert_called_once_with(path=os.path.join(root, 'shows', rel))
        movies.update.assert_not_called()
