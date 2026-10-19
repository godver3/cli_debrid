"""Per-show series type (anime or not) and how anime is detected.

Anime handling used to depend only on the provider's 'anime' genre: Japanese
animation tagged just "Animation" got none of it, and nothing could switch a
show on or off by hand.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


def _real(module_name):
    """Import a real module even if other tests left stubs in sys.modules."""
    import importlib
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in list(sys.modules):
        if name == 'database' or name.startswith('database.') or name.startswith('utilities.anime_detection'):
            del sys.modules[name]
    module = importlib.import_module(module_name)
    return module, patcher


class ShowOverrideStoreTests(unittest.TestCase):
    def setUp(self):
        self.so, patcher = _real('database.show_overrides')
        self.addCleanup(patcher.stop)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.remove, self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, type TEXT, genres TEXT)')
        conn.executemany('INSERT INTO media_items (imdb_id, type, genres) VALUES (?, ?, ?)', [
            ('tt1', 'episode', json.dumps(['Animation', 'Drama'])),
            ('tt1', 'episode', json.dumps(['anime'])),
            ('tt1', 'movie', json.dumps(['Animation'])),
            ('tt2', 'episode', json.dumps(['Drama'])),
        ])
        conn.commit()
        conn.close()

        def _connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c
        p = mock.patch.object(self.so, 'get_db_connection', _connect)
        p.start()
        self.addCleanup(p.stop)

    def _genres(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return [json.loads(g) for (g,) in conn.execute('SELECT genres FROM media_items ORDER BY id')]
        finally:
            conn.close()

    def test_set_and_get_keeps_unpassed_fields(self):
        self.assertEqual(self.so.get_show_override('tt1'), {})
        self.assertTrue(self.so.set_show_override('tt1', tmdb_id=5, series_type='anime'))
        self.assertTrue(self.so.set_show_override('tt1', episode_order='tvdb:dvd'))
        row = self.so.get_show_override('tt1')
        self.assertEqual((row['series_type'], row['episode_order'], row['tmdb_id']), ('anime', 'tvdb:dvd', '5'))
        self.so.set_show_override('tt1', series_type=None)
        self.assertIsNone(self.so.get_show_override('tt1')['series_type'])
        self.assertEqual(self.so.get_show_override('tt1')['episode_order'], 'tvdb:dvd')

    def test_invalid_series_type(self):
        with self.assertRaises(ValueError):
            self.so.set_show_override('tt1', series_type='cartoon')

    def test_tag_and_untag_episode_rows(self):
        self.assertEqual(self.so.apply_series_type_to_episodes('tt1', True), 1)
        self.assertEqual(self._genres()[:2], [['anime', 'Animation', 'Drama'], ['anime']])
        self.assertEqual(self.so.apply_series_type_to_episodes('tt1', False), 2)
        self.assertEqual(self._genres(), [['Animation', 'Drama'], [], ['Animation'], ['Drama']])


class AnimeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.ad, patcher = _real('utilities.anime_detection')
        self.addCleanup(patcher.stop)

    def _detect(self, genres, override=None, language=None, **kw):
        with mock.patch.object(self.ad, 'series_type_override', return_value=override), \
                mock.patch.object(self.ad, 'original_language', return_value=language) as lang:
            return self.ad.is_anime(genres, imdb_id='tt1', tmdb_id=7, **kw), lang

    def test_anime_genre(self):
        self.assertTrue(self._detect(['Anime', 'Action'])[0])

    def test_japanese_animation_without_anime_tag(self):
        result, lang = self._detect(['Animation', 'Comedy'], language='ja')
        self.assertTrue(result)
        lang.assert_called_once_with(7, 'tv')

    def test_other_animation_is_not_anime(self):
        self.assertFalse(self._detect(['Animation'], language='en')[0])
        # No 'animation' genre: TMDB isn't even asked
        result, lang = self._detect(['Drama'], language='ja')
        self.assertFalse(result)
        lang.assert_not_called()

    def test_override_wins_both_ways(self):
        self.assertFalse(self._detect(['anime'], override='standard')[0])
        self.assertTrue(self._detect(['Drama'], override='anime')[0])

    def test_override_ignored_when_asked_or_for_movies(self):
        self.assertTrue(self._detect(['anime'], override='standard', use_override=False)[0])
        self.assertTrue(self._detect(['anime'], override='standard', media_type='movie')[0])


if __name__ == '__main__':
    unittest.main()
