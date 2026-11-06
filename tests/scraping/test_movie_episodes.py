"""Episodes that are really movies (Initial D S03E01 "Third Stage").

TVDB lists the film as a one-episode season (isMovie=0, 105 min against ~24 min
episodes). Its releases are named like a movie, so an S03E01 search and every
S/E check after it rejected them, and no file in the torrent matched.
"""
import importlib
import importlib.util
import os
import sys
import unittest
from unittest import mock


def _ep(runtime, title='', is_movie=None, aired='2001-01-13T00:00:00'):
    ep = {'title': title, 'runtime': runtime, 'first_aired': aired}
    if is_movie is not None:
        ep['is_movie'] = is_movie
    return ep


def _season(n, runtime=24):
    return {'episode_count': n, 'episodes': {e: _ep(runtime, f'Ep {e}') for e in range(1, n + 1)}}


INITIAL_D = {
    0: _season(3, 90),  # specials don't count as reference episodes
    1: _season(26),
    2: _season(13),
    3: {'episode_count': 1, 'episodes': {1: _ep(105, 'Third Stage')}},
    4: _season(24),
}


def _fresh(*prefixes):
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + '.') for p in prefixes):
            del sys.modules[name]
    if not hasattr(sys.modules.get('scraper'), '__path__'):
        sys.modules.pop('scraper', None)
    return patcher


def _load_movie_episodes():
    """The module straight from its file: other tests may leave non-package
    stubs for 'scraper' / 'scraper.functions' in sys.modules."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'scraper', 'functions', 'movie_episodes.py')
    spec = importlib.util.spec_from_file_location('movie_episodes_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DetectMovieEpisodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.me = _load_movie_episodes()

    def test_initial_d_third_stage(self):
        info = self.me.detect_movie_episode(INITIAL_D, 3, 1)
        self.assertEqual(info['title'], 'Third Stage')
        self.assertEqual(info['episode_number'], 1)

    def test_season_pack_search_of_movie_season(self):
        self.assertEqual(self.me.detect_movie_episode(INITIAL_D, 3, None)['title'], 'Third Stage')

    def test_regular_episodes_are_not_movies(self):
        self.assertIsNone(self.me.detect_movie_episode(INITIAL_D, 1, 1))
        self.assertIsNone(self.me.detect_movie_episode(INITIAL_D, 2, None))

    def test_short_single_episode_season(self):
        seasons = dict(INITIAL_D)
        seasons[3] = {'episode_count': 1, 'episodes': {1: _ep(25)}}
        self.assertIsNone(self.me.detect_movie_episode(seasons, 3, 1))

    def test_hour_long_show_finale_isnt_a_movie(self):
        # 60-min single-episode season of a show whose episodes run ~45 min
        seasons = {1: _season(10, 45), 2: {'episode_count': 1, 'episodes': {1: _ep(62)}}}
        self.assertIsNone(self.me.detect_movie_episode(seasons, 2, 1))

    def test_tvdb_flag_counts_even_in_a_full_season(self):
        seasons = {1: _season(10), 2: {'episode_count': 2, 'episodes': {1: _ep(24), 2: _ep(95, 'The Movie', True)}}}
        self.assertEqual(self.me.detect_movie_episode(seasons, 2, 2)['title'], 'The Movie')
        self.assertIsNone(self.me.detect_movie_episode(seasons, 2, 1))

    def test_too_little_reference_data(self):
        seasons = {1: _season(2), 2: {'episode_count': 1, 'episodes': {1: _ep(100)}}}
        self.assertIsNone(self.me.detect_movie_episode(seasons, 2, 1))

    def test_specials_and_missing(self):
        self.assertIsNone(self.me.detect_movie_episode(INITIAL_D, 0, 1))
        self.assertIsNone(self.me.detect_movie_episode(INITIAL_D, 9, 1))
        self.assertIsNone(self.me.detect_movie_episode({}, 3, 1))

    def test_string_keys_from_json(self):
        seasons = {str(k): {'episode_count': v['episode_count'],
                            'episodes': {str(e): d for e, d in v['episodes'].items()}}
                   for k, v in INITIAL_D.items()}
        self.assertEqual(self.me.detect_movie_episode(seasons, 3, 1)['title'], 'Third Stage')

    def test_movie_episode_info_adds_year(self):
        self.me._cache.clear()
        with mock.patch('cli_battery.app.direct_api.DirectAPI.get_show_metadata',
                        return_value=({'seasons': INITIAL_D}, 'battery')):
            info = self.me.movie_episode_info('tt0185133', 3, 1)
        self.assertEqual(info['year'], 2001)

    def test_search_title(self):
        self.assertEqual(self.me.movie_search_title('Initial D', 'Third Stage'), 'Initial D Third Stage')
        self.assertEqual(self.me.movie_search_title('Initial D', 'Initial D Third Stage'), 'Initial D Third Stage')
        self.assertEqual(self.me.movie_search_title('Initial D', 'Episode 1'), 'Initial D')
        self.assertEqual(self.me.movie_search_title('Initial D', ''), 'Initial D')


class MatchMovieEpisodeTests(unittest.TestCase):
    INFO = {'title': 'Third Stage', 'episode_number': 1, 'runtime': 105, 'year': 2001}
    ITEM = {'type': 'episode', 'title': 'Initial D', 'imdb_id': 'tt0185133',
            'season_number': 3, 'episode_number': 1, 'genres': '["anime"]'}

    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(_fresh('queues.media_matcher').stop)
        import database  # noqa: F401  (importing queues.media_matcher first is circular)
        cls.mm = importlib.import_module('queues.media_matcher')

    def _match(self, paths, info=INFO, finder='best', allow_largest=True):
        m = self.mm.MediaMatcher()
        parsed = [p for p in (m._parse_file_info({'path': f, 'bytes': b}) for f, b in paths) if p]
        with mock.patch.object(self.mm, 'movie_episode_info', return_value=info):
            if finder == 'best':
                return m.find_best_match_from_parsed(parsed, dict(self.ITEM))
            return m._match_movie_episode(parsed, dict(self.ITEM), allow_largest=allow_largest)

    def test_single_movie_file(self):
        result = self._match([('/Initial.D.Third.Stage.2001.1080p.BluRay.x264.mkv', 9)])
        self.assertEqual(result[0], 'Initial.D.Third.Stage.2001.1080p.BluRay.x264.mkv')

    def test_title_match_among_extras(self):
        result = self._match([('/Initial.D.Third.Stage.2001.1080p.mkv', 5),
                              ('/Extras/Making.Of.Documentary.mkv', 9)])
        self.assertEqual(result[0], 'Initial.D.Third.Stage.2001.1080p.mkv')

    def test_named_episode_in_pack_uses_normal_matching(self):
        paths = [('/Initial.D.S03E01.Third.Stage.1080p.mkv', 5), ('/Initial.D.S04E01.1080p.mkv', 1)]
        self.assertIsNone(self._match(paths, finder='movie'))

    def test_episode_pack_without_title_match_is_not_matched(self):
        paths = [(f'/Show.2009.{n:03d}.1080p.mkv', 10 - n) for n in range(1, 5)]
        self.assertIsNone(self._match(paths, finder='movie'))

    def test_related_items_never_take_largest_file(self):
        paths = [('/Random.Documentary.mkv', 9), ('/Something.Else.mkv', 5)]
        self.assertIsNone(self._match(paths, finder='movie', allow_largest=False))
        self.assertEqual(self._match(paths, finder='movie')[0], 'Random.Documentary.mkv')

    def test_not_a_movie_episode(self):
        self.assertIsNone(self._match([('/Initial.D.Third.Stage.mkv', 1)], info=None, finder='movie'))


class FilterAsMovieTests(unittest.TestCase):
    """The scrape filters a movie episode as a movie with no show IDs."""

    def test_movie_release_passes_without_ids(self):
        from scraper.functions.common import detect_season_episode_info
        from scraper.functions.filter_results import filter_results
        from PTT import parse_title
        title = 'Initial.D.Third.Stage.2001.1080p.BluRay.x264-GRP'
        parsed = parse_title(title)
        parsed.update(original_title=title,
                      season_episode_info=detect_season_episode_info(dict(parsed)))
        result = {'title': title, 'original_title': title, 'size': 8.0, 'parsed_info': parsed,
                  'scraper_type': 'Generic', 'scraper_instance': 't',
                  'additional_metadata': {'filename': title, 'bingeGroup': None}}
        version = {'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.0,
                   'max_size_gb': 100.0, 'filter_in': [], 'filter_out': [], 'enable_hdr': False,
                   'similarity_threshold': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0}
        kept, _ = filter_results([result], None, 'Initial D Third Stage', 2001, 'movie', None, None, False,
                                 version, 105, 1, {}, ['anime'], ['Initial D Third Stage'],
                                 imdb_id=None, direct_api=None)
        self.assertEqual(len(kept), 1, result.get('filter_reason'))


if __name__ == '__main__':
    unittest.main()
