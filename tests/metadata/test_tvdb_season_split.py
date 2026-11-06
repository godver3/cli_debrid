"""Shows TVDB keeps as seasons of one series (Hill House / Bly Manor), and
episodes in the TMDB fallback. All HTTP is faked."""
import contextlib
import unittest
from unittest.mock import patch

from cli_battery.app import tvdb_client as t

SERIES = 345246
HILL, BLY = 'tt6763664', 'tt10970552'
OFFICIAL = {'id': 1, 'type': 'official'}


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


def _episode(season, number, ep_id, aired):
    return {'id': ep_id, 'seasonNumber': season, 'number': number, 'name': f"S{season}E{number}",
            'aired': aired, 'absoluteNumber': 20 + number, 'runtime': 55}


def series_raw(season_names=("The Haunting of Hill House", "The Haunting of Bly Manor"), tmdb=None):
    episodes = [_episode(1, n, 100 + n, '2018-10-12') for n in range(1, 11)]
    episodes += [_episode(2, n, 200 + n, '2020-10-09') for n in range(1, 10)]
    seasons = [{'id': 1, 'number': 0, 'name': None, 'type': OFFICIAL}]
    seasons += [{'id': 10 + i, 'number': i, 'name': name, 'type': OFFICIAL}
                for i, name in enumerate(season_names, start=1)]
    seasons.append({'id': 99, 'number': 1, 'name': None, 'type': {'id': 3, 'type': 'absolute'}})
    remote_ids = [{'sourceName': 'IMDB', 'id': HILL}]
    if tmdb:
        remote_ids.append({'sourceName': 'TheMovieDB.com', 'id': str(tmdb)})
    return {
        'id': SERIES, 'name': 'The Haunting', 'slug': 'the-haunting', 'firstAired': '2018-10-12',
        'overview': 'Series overview', 'status': {'name': 'Ended'}, 'airsTime': '03:00',
        'originalNetwork': {'name': 'Netflix', 'timezone': 'America/New_York'},
        'remoteIds': remote_ids, 'seasons': seasons, 'episodes': episodes,
        'seasonTypes': [{'type': 'official', 'name': 'Aired Order'}, {'type': 'absolute', 'name': 'Absolute'}],
        'translations': {'nameTranslations': [{'language': 'nld', 'name': 'The Haunting of Bly Manor'},
                                              {'language': 'eng', 'name': 'The Haunting'}]},
    }


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter_by(self, **kw):
        return FakeQuery([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


class Row:
    def __init__(self, tvdb_id, imdb_id, media_type):
        self.tvdb_id, self.imdb_id, self.media_type = tvdb_id, imdb_id, media_type


class FakeTVDB:
    """Routes TVDB/TMDB requests; records the mapping rows written."""

    def __init__(self, raw=None, tmdb_shows=None, remoteid=None, search=None):
        self.raw = raw or series_raw()
        self.rows = []
        # imdb -> TVDB /search/remoteid answer
        self.remoteid = remoteid if remoteid is not None else {
            HILL: [{'series': {'id': SERIES}}],
            BLY: [{'season': {'seriesId': SERIES, 'number': 2, 'type': {'type': 'official'}}}],
        }
        # tmdb id -> /tv/{id} answer
        self.tmdb_shows = tmdb_shows if tmdb_shows is not None else {
            72844: {'name': 'The Haunting of Hill House', 'overview': 'Hill overview',
                    'seasons': [{'season_number': 1}], 'external_ids': {'imdb_id': HILL}},
            109958: {'name': 'The Haunting of Bly Manor', 'overview': 'Bly overview',
                     'seasons': [{'season_number': 1}], 'external_ids': {'imdb_id': BLY}},
        }
        self.find = {HILL: 72844, BLY: 109958}
        self.search = search if search is not None else {'The Haunting of Bly Manor': (109958, '2020-10-09'),
                                                         'The Haunting of Hill House': (72844, '2018-10-12')}

    def make_request(self, url, *a, **k):
        if '/search/remoteid/' in url:
            return Resp({'data': self.remoteid.get(url.rsplit('/', 1)[-1], [])})
        if '/episodes/' in url:
            if 'page=0' not in url:
                return Resp({'data': {'episodes': []}})
            return Resp({'data': {'episodes': self.raw['episodes']}, 'links': {}})
        if f"/series/{SERIES}/extended" in url:
            return Resp({'data': self.raw})
        return Resp({}, 404)

    def http_get(self, url, params=None, timeout=None):
        params = params or {}
        if '/find/' in url:
            tmdb_id = self.find.get(url.rsplit('/', 1)[-1])
            return Resp({'tv_results': [{'id': tmdb_id}] if tmdb_id else []})
        if '/search/tv' in url:
            hit = self.search.get(params.get('query'))
            return Resp({'results': [{'id': hit[0], 'name': params['query'], 'first_air_date': hit[1]}] if hit else []})
        if '/tv/' in url:
            show = self.tmdb_shows.get(int(url.rsplit('/', 1)[-1]))
            return Resp(show) if show else Resp({}, 404)
        return Resp({}, 404)

    def cache(self, tvdb_id, imdb_id, media_type):
        self.rows = [r for r in self.rows if r.tvdb_id != tvdb_id]
        self.rows.append(Row(tvdb_id, imdb_id, media_type))

    @contextlib.contextmanager
    def session(self):
        class S:
            def query(_, model):
                return FakeQuery(self.rows)
        yield S()

    def patches(self):
        return [patch.object(t, '_make_request', self.make_request),
                patch.object(t._http, 'get', self.http_get),
                patch.object(t, '_cache_tvdb_mapping', self.cache),
                patch.object(t, 'managed_session', self.session),
                patch.object(t, '_get_tmdb_api_key', return_value='key'),
                patch.object(t, '_get_trakt_status', return_value=None)]


class SeasonSplitBase(unittest.TestCase):
    def setUp(self):
        t._season_slices.clear()
        t._narrowed_seasons.clear()
        self.addCleanup(t._season_slices.clear)
        self.addCleanup(t._narrowed_seasons.clear)

    def use(self, fake):
        for p in fake.patches():
            p.start()
            self.addCleanup(p.stop)
        return fake


class TestSeasonSlice(SeasonSplitBase):
    def test_season_answer_is_not_a_series_and_is_cached_on_its_own_key(self):
        fake = self.use(FakeTVDB())
        self.assertIsNone(t._resolve_tvdb_id(BLY, media_type='show'))
        self.assertEqual(t.resolve_season_slice(BLY), (SERIES, 2))
        self.assertEqual([(r.tvdb_id, r.imdb_id, r.media_type) for r in fake.rows],
                         [(f"{SERIES}:2", BLY, 'season')])
        # A fresh process reads it back from the mapping table
        t._season_slices.clear()
        self.assertEqual(t.resolve_season_slice(BLY), (SERIES, 2))

    def test_series_mapping_survives_the_season_row(self):
        fake = self.use(FakeTVDB())
        t.resolve_season_slice(BLY)
        self.assertEqual(t._resolve_tvdb_id(HILL, media_type='show'), SERIES)
        self.assertIsNone(t._resolve_tvdb_id(BLY, media_type='show'))
        self.assertEqual(sorted((r.tvdb_id, r.imdb_id) for r in fake.rows),
                         [(str(SERIES), HILL), (f"{SERIES}:2", BLY)])

    def test_non_aired_order_or_special_season_answers_are_ignored(self):
        self.assertIsNone(t._season_from_remoteid_results(
            [{'season': {'seriesId': 1, 'number': 2, 'type': {'type': 'dvd'}}}]))
        self.assertIsNone(t._season_from_remoteid_results([{'season': {'seriesId': 1, 'number': 0}}]))
        self.assertEqual(t._season_from_remoteid_results([{'season': {'seriesId': 1, 'number': 3}}]), (1, 3))

    def test_show_data_is_the_one_season_as_season_one(self):
        self.use(FakeTVDB())
        data = t.get_show_data(BLY)
        self.assertEqual(data['title'], 'The Haunting of Bly Manor')
        self.assertEqual(data['year'], 2020)
        self.assertEqual(data['status'], 'ended')
        self.assertEqual(data['overview'], 'Bly overview')
        self.assertEqual(data['ids']['imdb'], BLY)
        self.assertIsNone(data['ids']['tvdb'])
        self.assertEqual(data['ids']['tmdb'], 109958)
        self.assertNotIn('aliases', data)
        self.assertEqual(list(data['seasons']), [1])
        episodes = data['seasons'][1]['episodes']
        self.assertEqual(sorted(episodes), list(range(1, 10)))
        self.assertEqual(episodes[1]['tvdb_id'], '201')
        self.assertEqual(episodes[1]['absolute'], 1)
        self.assertEqual(data['seasons'][1]['episode_count'], 9)

    def test_seasons_and_episodes_and_orders(self):
        self.use(FakeTVDB())
        seasons, source = t.get_show_seasons_and_episodes(BLY, include_specials=True)
        self.assertEqual((list(seasons), len(seasons[1]['episodes']), source), ([1], 9, 'tvdb'))
        self.assertEqual(t.get_season_types(BLY), [])
        self.assertIsNone(t.get_seasons_for_season_type(BLY, 'absolute'))


class TestNarrowing(SeasonSplitBase):
    def test_series_owner_keeps_only_its_season(self):
        self.use(FakeTVDB())
        data = t.get_show_data(HILL)
        self.assertEqual(data['title'], 'The Haunting of Hill House')
        self.assertEqual(data['year'], 2018)
        self.assertEqual(data['ids']['tvdb'], SERIES)
        self.assertEqual(data['ids']['tmdb'], 72844)
        self.assertNotIn('aliases', data)  # series translations name Bly Manor too
        self.assertEqual(list(data['seasons']), [1])
        self.assertEqual(len(data['seasons'][1]['episodes']), 10)
        self.assertEqual(data['seasons'][1]['episodes'][1]['absolute'], 1)
        seasons, _ = t.get_show_seasons_and_episodes(HILL)
        self.assertEqual(list(seasons), [1])
        self.assertEqual(t.get_season_types(HILL), [])

    def test_season_tvdb_has_ahead_of_tmdb_is_kept(self):
        # A new season TVDB lists before TMDB: named, but no other show claims it
        raw = series_raw(season_names=("The Haunting of Hill House", "Season Two"))
        self.use(FakeTVDB(raw=raw))
        self.assertIsNone(t.narrowed_season(HILL, SERIES, raw))
        self.assertEqual(sorted(t.get_show_data(HILL)['seasons']), [1, 2])

    def test_other_title_must_resolve_to_this_very_season(self):
        fake = FakeTVDB()
        fake.remoteid[BLY] = [{'season': {'seriesId': 999, 'number': 2}}]  # some other series
        self.use(fake)
        self.assertIsNone(t.narrowed_season(HILL, SERIES, fake.raw))

    def test_tmdb_with_more_than_one_season_is_left_alone(self):
        fake = FakeTVDB()
        fake.tmdb_shows[72844]['seasons'] = [{'season_number': 1}, {'season_number': 2}]
        self.use(fake)
        self.assertIsNone(t.narrowed_season(HILL, SERIES, fake.raw))

    def test_no_tmdb_key_no_narrowing(self):
        fake = self.use(FakeTVDB())
        with patch.object(t, '_get_tmdb_api_key', return_value=''):
            self.assertIsNone(t.narrowed_season(HILL, SERIES, fake.raw))

    def test_unnamed_seasons_skip_tmdb_entirely(self):
        raw = series_raw(season_names=(None, None))
        fake = self.use(FakeTVDB(raw=raw))
        with patch.object(t._http, 'get', side_effect=AssertionError('no TMDB call expected')):
            self.assertIsNone(t.narrowed_season(HILL, SERIES, raw))
        self.assertTrue(fake)


class TestTmdbFallbackEpisodes(unittest.TestCase):
    def test_episodes_fetched_in_chunks_of_twenty(self):
        calls = []

        def http_get(url, params=None, timeout=None):
            calls.append(params['append_to_response'])
            out = {}
            for part in params['append_to_response'].split(','):
                n = int(part.split('/')[1])
                out[part] = {'episodes': [{'id': n * 100 + 1, 'episode_number': 1, 'name': f'E{n}',
                                           'air_date': '2020-01-02', 'runtime': 30}]}
            return Resp(out)

        with patch.object(t._http, 'get', side_effect=http_get):
            result = t._fetch_tmdb_season_episodes(5, list(range(0, 25)), 'key')
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(result), list(range(0, 25)))
        ep = result[3][1]
        self.assertEqual((ep['title'], ep['first_aired'], ep['runtime'], ep['tmdb_id'], ep['tvdb_id']),
                         ('E3', '2020-01-02T00:00:00.000Z', 30, '301', None))

    def test_show_data_carries_episodes(self):
        def http_get(url, params=None, timeout=None):
            if '/find/' in url:
                return Resp({'tv_results': [{'id': 7}]})
            if 'append_to_response' in (params or {}):
                return Resp({'season/1': {'episodes': [{'id': 1, 'episode_number': n, 'name': f'E{n}',
                                                        'air_date': '2020-01-02'} for n in (1, 2, 3)]}})
            return Resp({'name': 'Show', 'first_air_date': '2020-01-02', 'status': 'Ended',
                         'seasons': [{'season_number': 1, 'episode_count': 3}]})

        with patch.object(t._http, 'get', side_effect=http_get):
            data = t._fetch_tmdb_show_data('tt1', 'key')
        self.assertEqual(data['seasons']['1']['episode_count'], 3)
        self.assertEqual(sorted(data['seasons']['1']['episodes']), [1, 2, 3])

    def test_failed_season_request_keeps_the_season_empty(self):
        def http_get(url, params=None, timeout=None):
            if '/find/' in url:
                return Resp({'tv_results': [{'id': 7}]})
            if 'append_to_response' in (params or {}):
                return Resp({}, 500)
            return Resp({'name': 'Show', 'seasons': [{'season_number': 1, 'episode_count': 3}]})

        with patch.object(t._http, 'get', side_effect=http_get):
            data = t._fetch_tmdb_show_data('tt1', 'key')
        self.assertEqual(data['seasons']['1'], {'number': 1, 'episode_count': 3, 'episodes': {}})


if __name__ == '__main__':
    unittest.main()
