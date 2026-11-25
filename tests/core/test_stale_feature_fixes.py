"""Regression tests for the 2026-11-23 stale-feature review: OIDC redirect and
account linking, Debrid-Link errors and batched seedbox lookup, movie box-set
cleanup ownership and TMDB-outage handling, provider transfer, manual
blacklist durability, performance-log time windows and AIOStreams parsing."""

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import database  # noqa: F401  (app import order)


# ── OIDC ─────────────────────────────────────────────────────────────────────

def test_oidc_safe_next_blocks_offsite_targets():
    from routes.oidc_routes import _safe_next
    assert _safe_next('/library?x=1') == '/library?x=1'
    for bad in ('https://evil.example/', '//evil.example', '/\\evil.example',
                'javascript:alert(1)', 'evil.example', '/ok\r\nSet-Cookie: x', ''):
        assert _safe_next(bad) == '', bad


def test_oidc_linkable_refuses_account_bound_to_other_subject():
    from routes.oidc_routes import _linkable
    assert _linkable(MagicMock(oidc_sub=None), 'sub-a')
    assert _linkable(MagicMock(oidc_sub='sub-a'), 'sub-a')
    assert not _linkable(MagicMock(oidc_sub='sub-b'), 'sub-a')
    assert not _linkable(None, 'sub-a')


# ── Debrid-Link ──────────────────────────────────────────────────────────────

def _dl_response(status, body):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


@pytest.mark.parametrize('code,exc_name', [
    ('floodDetected', 'RateLimitError'),
    ('serverError', 'ProviderUnavailableError'),
    ('hidedToken', 'DebridLinkAuthError'),
    ('somethingElse', 'DebridLinkAPIError'),
])
def test_debridlink_error_codes_map_to_exceptions(code, exc_name):
    from debrid.debridlink import api as dl_api
    from debrid import base
    from debrid.debridlink import exceptions as dl_exc
    exc = getattr(base, exc_name, None) or getattr(dl_exc, exc_name)
    body = {'success': False, 'error': code, 'error_description': 'x'}
    with patch.object(dl_api, '_wait_for_rate_limit'), \
            patch.object(dl_api.api, 'get', return_value=_dl_response(400, body)):
        with pytest.raises(exc):
            dl_api.make_request('GET', '/seedbox/list', 'key')
    dl_api._rate_limiter['min_interval'] = 0.25


def test_debridlink_pagination_stops_when_next_does_not_advance():
    from debrid.debridlink import api as dl_api
    page = {'success': True, 'value': [{'id': i} for i in range(100)],
            'pagination': {'next': 0}}
    with patch.object(dl_api, 'make_request', return_value=page) as mr:
        out = dl_api.get_all_torrents('key')
    assert mr.call_count == 1
    assert len(out) == 100


def test_debridlink_is_cached_lists_seedbox_once_per_batch():
    from debrid.debridlink import client as dl_client
    provider = dl_client.DebridLinkProvider.__new__(dl_client.DebridLinkProvider)
    provider._api_key = 'key'
    provider._all_torrent_ids = {}
    provider._cached_torrent_ids = {}
    hashes = [f'{i:040x}' for i in range(5)]
    seedbox = [{'id': 'a', 'hashString': hashes[0], 'downloadPercent': 100}]
    with patch.object(type(provider), 'api_key', new_callable=lambda: property(lambda self: 'key')), \
            patch.object(dl_client, 'get_all_torrents', return_value=seedbox) as listing, \
            patch.object(dl_client, 'make_request', return_value={'value': {}}), \
            patch('database.not_wanted_magnets.add_to_not_wanted'):
        result = asyncio.run(provider.is_cached(hashes))
    assert listing.call_count == 1
    assert set(result) == set(hashes)


# ── Movie box sets ───────────────────────────────────────────────────────────

def _boxset_db():
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, type TEXT, tmdb_id TEXT,
                    imdb_id TEXT, ms_item_id TEXT, state TEXT, title TEXT, release_date TEXT,
                    tmdb_collection_id TEXT, tmdb_collection_name TEXT)""")
    return conn


class _NoClose:
    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass


def test_boxset_cleanup_only_deletes_collections_it_manages():
    from database import plex_movie_boxsets as bx
    conn = _boxset_db()
    # One owned movie in each collection, threshold 2: both are "below".
    conn.execute("INSERT INTO media_items VALUES (1,'movie','10','tt1','100','Collected','A','2000','c-ours','Ours')")
    conn.execute("INSERT INTO media_items VALUES (2,'movie','20','tt2','200','Collected','B','2000','c-user','Users')")
    plex = [{'title': 'Ours Collection', 'ratingKey': '900'},
            {'title': 'Users Collection', 'ratingKey': '901'}]  # user's own, same naming
    state = {'created_ratingkeys': {'c-ours': '900'}}
    with patch.object(bx, '_get_db', return_value=_NoClose(conn)), \
            patch.object(bx, '_get_all_plex_collections', return_value=plex), \
            patch.object(bx, '_delete_collection') as delete:
        state = bx.cleanup_collections_below_threshold('http://plex', 't', '1', 2, state)
    delete.assert_called_once_with('http://plex', 't', '900')
    assert 'c-ours' not in state['created_ratingkeys']


def test_boxset_cleanup_removes_managed_collection_with_no_owned_movies():
    from database import plex_movie_boxsets as bx
    conn = _boxset_db()
    state = {'collection_ratingkeys': {'c-gone': '905'}}
    with patch.object(bx, '_get_db', return_value=_NoClose(conn)), \
            patch.object(bx, '_get_all_plex_collections', return_value=[{'title': 'x', 'ratingKey': '905'}]), \
            patch.object(bx, '_delete_collection') as delete:
        bx.cleanup_collections_below_threshold('http://plex', 't', '1', 2, state)
    delete.assert_called_once_with('http://plex', 't', '905')


def test_boxset_tmdb_outage_leaves_movie_unchecked():
    from database import plex_movie_boxsets as bx
    conn = _boxset_db()
    conn.execute("INSERT INTO media_items (id,type,tmdb_id) VALUES (1,'movie','10')")
    conn.execute("INSERT INTO media_items (id,type,tmdb_id) VALUES (2,'movie','20')")
    responses = {'/movie/10': (0, None), '/movie/20': (404, None)}
    with patch.object(bx, '_get_db', return_value=_NoClose(conn)), \
            patch.object(bx, '_tmdb_get_status', side_effect=lambda p, *a: responses[p]), \
            patch.object(bx.time, 'sleep'):
        bx.sync_collection_data_for_movies()
    rows = {r['id']: r['tmdb_collection_id'] for r in conn.execute('SELECT id, tmdb_collection_id FROM media_items')}
    assert rows[1] is None   # outage: retried next run
    assert rows[2] == ''     # 404: genuinely checked


def test_boxset_versions_count_once():
    from database import plex_movie_boxsets as bx
    movies = [{'tmdb_id': '10', 'imdb_id': 'tt1', 'ms_item_id': '100'},
              {'tmdb_id': '10', 'imdb_id': 'tt1', 'ms_item_id': '555'}]  # 1080p + 4K copy
    assert bx._distinct_movie_count(movies) == 1


# ── Provider transfer ────────────────────────────────────────────────────────

def test_provider_transfer_truthy_reads_form_strings():
    from utilities.provider_transfer import _truthy
    assert _truthy('false') is False
    assert _truthy('0') is False
    assert _truthy('true') is True
    assert _truthy(None) is True
    assert _truthy(False) is False


def test_provider_transfer_skips_duplicates_and_failed_source_items():
    from utilities import provider_transfer as pt
    items = [{'name': 'A', 'ref': '1', 'category': '', 'status': 'Completed'},
             {'name': 'A', 'ref': '2', 'category': '', 'status': 'Completed'},
             {'name': 'B', 'ref': '3', 'category': '', 'status': 'Failed'}]
    with patch.object(pt, 'list_source_items', return_value=(items, None)), \
            patch.object(pt, '_target_existing_names', return_value=set()), \
            patch.object(pt, '_read_nzb', return_value=(b'<nzb/>', None)), \
            patch.object(pt, '_submit_climount', return_value=('id', None)) as submit:
        job = 'testjob'
        pt._jobs[job] = {'status': 'starting', 'total': 0, 'done': 0, 'submitted': 0,
                         'skipped': 0, 'failed': 0, 'complete': False, 'log': [], 'error': None}
        pt._run_job(job, {'direction': 'nzbdav_to_climount', 'target_url': 'http://x',
                          'skip_existing': 'false'})
    assert submit.call_count == 1
    assert pt._jobs[job]['submitted'] == 1
    assert pt._jobs[job]['skipped'] == 2


def test_provider_transfer_sends_nzbdav_key_on_download():
    from utilities import provider_transfer as pt
    resp = MagicMock(status_code=200, content=b'x')
    with patch.object(pt.requests, 'get', return_value=resp) as get:
        pt._read_nzb('nzbdav_to_climount', {'ref': 'blob'}, 'http://nzbdav', 'KEY')
    _, kwargs = get.call_args
    assert kwargs['params']['apikey'] == 'KEY'


# ── Manual blacklist ─────────────────────────────────────────────────────────

@pytest.fixture
def blacklist_file(tmp_path, monkeypatch):
    from database import manual_blacklist as mb
    path = tmp_path / 'manual_blacklist.json'
    monkeypatch.setattr(mb, 'BLACKLIST_FILE', str(path))
    monkeypatch.setattr(mb, '_cached_blacklist', None)
    monkeypatch.setattr(mb, '_cached_mtime', None)
    return mb, path


def test_blacklist_season_add_keeps_whole_show_block(blacklist_file):
    mb, _ = blacklist_file
    mb.add_to_manual_blacklist('tt1', 'episode', 'Show', '2020')
    mb.add_to_manual_blacklist('tt1', 'episode', 'Show', '2020', season=2)
    assert mb.get_manual_blacklist()['tt1']['seasons'] == []
    assert mb.is_blacklisted('tt1', season=5)


def test_blacklist_save_is_atomic_and_refreshes_cache(blacklist_file):
    mb, path = blacklist_file
    mb.add_to_manual_blacklist('tt1', 'movie', 'One', '2020')
    assert mb.is_blacklisted('tt1')            # primes the cache
    mb.add_to_manual_blacklist('tt2', 'movie', 'Two', '2021')
    assert mb.is_blacklisted('tt2')            # visible at once, same-second mtime or not
    assert not os.path.exists(str(path) + '.tmp')
    assert set(json.loads(path.read_text())) == {'tt1', 'tt2'}


def test_blacklist_corrupt_file_reads_as_empty(blacklist_file):
    mb, path = blacklist_file
    path.write_text('{"tt1": {')
    assert mb.get_manual_blacklist() == {}


# ── Performance log ──────────────────────────────────────────────────────────

def test_performance_entry_epoch_respects_per_type_timezone():
    from queues.performance_monitor import entry_epoch
    utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    basic = {'type': 'basic_metrics', 'timestamp': utc.replace(tzinfo=None).isoformat()}
    local_naive = utc.astimezone().replace(tzinfo=None).isoformat()
    cpu = {'type': 'cpu_metrics', 'timestamp': local_naive}
    assert entry_epoch(basic) == pytest.approx(utc.timestamp())
    assert entry_epoch(cpu) == pytest.approx(utc.timestamp())


def test_performance_cpu_endpoint_returns_newest_samples(tmp_path, monkeypatch):
    from flask import Flask
    import routes.performance_routes as pr
    import time as _t
    now = _t.time()
    lines = []
    for i in range(100):  # oldest -> newest, 10s apart, all within the hour
        ts = datetime.fromtimestamp(now - (100 - i) * 10).isoformat()
        lines.append(json.dumps({'type': 'cpu_metrics', 'timestamp': ts,
                                 'metrics': {'process_cpu_percent': i}}))
    (tmp_path / 'performance_log.json').write_text('\n'.join(lines) + '\n')
    monkeypatch.setenv('USER_LOGS', str(tmp_path))
    app = Flask(__name__)
    with app.test_request_context('/?hours=1&limit=5'):
        resp = pr.get_cpu_metrics.__wrapped__()
    entries = resp.get_json()['entries']
    assert [e['metrics']['process_cpu_percent'] for e in entries] == [95, 96, 97, 98, 99]


# ── AIOStreams ───────────────────────────────────────────────────────────────

def test_aiostreams_resolution_line_is_not_seeders_or_size():
    from scraper.aiostreams import parse_results
    streams = [{
        'name': 'x', 'infoHash': 'ABCDEF0123456789ABCDEF0123456789ABCDEF01',
        'behaviorHints': {'filename': 'Movie.2020.1080p.mkv'},
        'description': '👤 42\n1080p WEB-DL\n📦 2.5 GB',
    }]
    [r] = parse_results(streams, 'aio')
    assert r['seeders'] == 42
    assert r['size'] == 2.5
    assert r['info_hash'] == 'abcdef0123456789abcdef0123456789abcdef01'
