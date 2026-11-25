from unittest.mock import MagicMock, patch

from utilities import streaming_top10_api as api


def _resp(edges=None, errors=None):
    r = MagicMock()
    r.raise_for_status.return_value = None
    body = {'data': {'streamingCharts': {'edges': edges or []}}}
    if errors:
        body = {'errors': errors, 'data': None}
    r.json.return_value = body
    return r


def _edge(title, rank, tmdb='123', imdb='tt1'):
    return {
        'streamingChartInfo': {'rank': rank, 'daysInTop10': 3},
        'node': {'objectType': 'SHOW', 'content': {
            'title': title, 'originalReleaseYear': 2025,
            'externalIds': {'imdbId': imdb, 'tmdbId': tmdb}}},
    }


def test_ranks_renumbered_and_ids_parsed():
    # Global ranks 1, 4, 9 after the package filter -> 1, 2, 3
    edges = [_edge('A', 1), _edge('B', 4, tmdb=None, imdb=None), _edge('C', 9, tmdb='bad')]
    with patch.object(api.requests, 'post', return_value=_resp(edges)) as post:
        res = api.fetch_top10('netflix', media_type='tv')
    assert res['success'] and res['platform_name'] == 'Netflix'
    assert [i['rank'] for i in res['items']] == [1, 2, 3]
    assert res['items'][0]['tmdb_id'] == 123 and res['items'][0]['imdb_id'] == 'tt1'
    assert res['items'][1]['tmdb_id'] is None and res['items'][1]['imdb_id'] is None
    assert res['items'][2]['tmdb_id'] is None
    assert all(i['media_type'] == 'tv' for i in res['items'])
    f = post.call_args.kwargs['json']['variables']['filter']
    assert f == {'objectType': 'SHOW', 'packages': ['nfx'], 'category': api._CATEGORY_DAILY}


def test_all_fetches_movies_then_shows():
    with patch.object(api.requests, 'post', side_effect=[_resp([_edge('M', 1)]), _resp([_edge('S', 2)])]) as post:
        res = api.fetch_top10('hbo')
    assert [(i['title'], i['media_type']) for i in res['items']] == [('M', 'movie'), ('S', 'tv')]
    types = [c.kwargs['json']['variables']['filter']['objectType'] for c in post.call_args_list]
    assert types == ['MOVIE', 'SHOW']


def test_weekly_uses_weekly_category():
    with patch.object(api.requests, 'post', return_value=_resp([_edge('A', 1)])) as post:
        res = api.fetch_top10_weekly('disney', media_type='movie')
    assert res['period'] == 'weekly'
    assert post.call_args.kwargs['json']['variables']['filter']['category'] == api._CATEGORY_WEEKLY


def test_errors():
    assert 'error' in api.fetch_top10('nope')
    with patch.object(api.requests, 'post', return_value=_resp(errors=[{'message': 'boom'}])):
        res = api.fetch_top10('netflix')
    assert 'boom' in res['error'] and res['items'] == []


def test_partial_failure_keeps_other_type():
    with patch.object(api.requests, 'post', side_effect=[RuntimeError('down'), _resp([_edge('S', 1)])]):
        res = api.fetch_top10('netflix')
    assert res['success'] and [i['title'] for i in res['items']] == ['S']
