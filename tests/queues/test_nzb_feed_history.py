"""Upgrade Hub NZB feed history (database/nzb_feed_history.py) and the NZB
phase matching against it (database/nzb_upgrade.py)."""
import time
import xml.etree.ElementTree as ET
from email.utils import formatdate
from unittest import mock

import pytest

import database  # noqa: F401  (app import order)
from tests.queues.test_upgrades import _insert, hub_db  # noqa: F401

NS = 'http://www.newznab.com/DTD/2010/feeds/attributes/'


@pytest.fixture
def feed_db(hub_db, monkeypatch):
    import database.nzb_feed_history as fh
    monkeypatch.setattr(fh, '_tables_ready', False)
    return hub_db


def _item(title, guid, age_hours, imdb=None, size=4 * 1024 ** 3):
    el = ET.Element('item')
    ET.SubElement(el, 'title').text = title
    ET.SubElement(el, 'guid').text = guid
    ET.SubElement(el, 'enclosure', url=f'https://idx/get/{guid}', length=str(size))
    ET.SubElement(el, 'pubDate').text = formatdate(time.time() - age_hours * 3600)
    if imdb:
        ET.SubElement(el, f'{{{NS}}}attr', name='imdb', value=imdb)
    return el


class FakeIndexer:
    """Serves a newest-first feed in pages, like t=search&q= with offset."""

    def __init__(self, movie_items, tv_items=()):
        self.feeds = {'movie': list(movie_items), 'tv': list(tv_items)}
        self.calls = []

    def __call__(self, url, api_key, cats, limit, offset, timeout=30):
        cat = 'movie' if cats.startswith('2000') else 'tv'
        self.calls.append((cat, offset))
        items = self.feeds[cat]
        return items[offset:offset + limit], len(items)


SCRAPERS = [('Geek', {'url': 'https://idx', 'api_key': 'k'})]


def _library(conn):
    _insert(conn, type='movie', title='The Long Walk', imdb_id='tt0123456', tmdb_id='1', year=2025,
            state='Collected', filled_by_file='The.Long.Walk.2025.1080p.WEB.mkv', size=4.0)
    _insert(conn, type='movie', title='Fjord', imdb_id='tt35410859', tmdb_id='2', year=2026, state='Wanted')


def _sync(fake):
    import database.nzb_feed_history as fh
    with mock.patch('database.nzb_upgrade._fetch_browse_page', side_effect=fake), \
         mock.patch('scraper.newznab._is_within_retention_days', return_value=True):
        return fh.sync_feeds(SCRAPERS)


def _stored(conn):
    return sorted(r[0] for r in conn.execute("SELECT guid FROM nzb_feed_releases"))


# ── What gets kept ──────────────────────────────────────────────────────────

def test_title_key_ignores_case_punctuation_and_word_order():
    from database.nzb_feed_history import title_key
    assert title_key('The Long Walk') == title_key('walk, long: the') == 'long the walk'
    assert title_key('Dexter: Resurrection') == title_key('Dexter-Resurrection') == 'dexter resurrection'


def test_sync_keeps_only_the_librarys_releases(feed_db, app_settings):
    conn = feed_db()
    _library(conn)
    fake = FakeIndexer([
        _item('The.Long.Walk.2025.2160p.WEB-DL-GRP', 'a', 1),                 # title match
        _item('Fjord.2026.1080p.AMZN.WEB-DL-CHORTLE', 'b', 2, imdb='35410859'),  # IMDb match (Wanted item)
        _item('Some.Other.Movie.2025.1080p.WEB', 'c', 3),                      # not in the library
        _item('Other.Film.2025.1080p.WEB', 'd', 4, imdb='9999999'),            # other film's id and title
        _item('Fjord.2026.1080p.WEB-DL-X', 'e', 5, imdb='1111111'),             # wrong id, library title: kept
    ])
    totals = _sync(fake)
    assert _stored(conn) == ['a', 'b', 'e']
    assert totals['read'] == 5 and totals['kept'] == 3
    conn.close()


# ── Incremental ─────────────────────────────────────────────────────────────

def test_second_sync_stops_at_releases_it_already_read(feed_db, app_settings):
    import database.nzb_feed_history as fh
    conn = feed_db()
    _library(conn)
    old = [_item(f'The.Long.Walk.2025.720p.{i}', f'old{i}', 10 + i) for i in range(150)]
    fake = FakeIndexer(old)
    _sync(fake)
    first_calls = len([c for c in fake.calls if c[0] == 'movie'])
    assert first_calls == 2                              # 150 releases, 100 per page

    # Two new releases appear at the top; the next sync reads one page and stops.
    fake.feeds['movie'] = [_item('The.Long.Walk.2025.2160p.NEW', 'new1', 0.2),
                           _item('The.Long.Walk.2025.1080p.NEW', 'new2', 0.5)] + old
    fake.calls.clear()
    totals = _sync(fake)
    assert [c for c in fake.calls if c[0] == 'movie'] == [('movie', 0)]
    assert totals['read'] == 2
    assert {'new1', 'new2'} <= set(_stored(conn))
    conn.close()
    assert fh.MAX_PAGES * fh.PAGE_SIZE >= 1000          # first sync covers the old "latest 1000"


def test_old_releases_are_pruned(feed_db, app_settings):
    import database.nzb_feed_history as fh
    conn = feed_db()
    _library(conn)
    _sync(FakeIndexer([_item('The.Long.Walk.2025.2160p.A', 'fresh', 1),
                       _item('The.Long.Walk.2025.2160p.B', 'stale', (fh.HISTORY_DAYS + 2) * 24)]))
    assert _stored(conn) == ['fresh']
    conn.close()


def test_load_pool_returns_scorable_results(feed_db, app_settings):
    import database.nzb_feed_history as fh
    conn = feed_db()
    _library(conn)
    _sync(FakeIndexer([_item('The.Long.Walk.2025.2160p.WEB-DL-GRP', 'a', 1)]))
    conn.close()
    pool = fh.load_pool()
    [r] = pool['movie']
    assert r['nzb_url'] == 'https://idx/get/a' and r['protocol'] == 'nzb'
    assert r['parsed_info']['resolution'] == '2160p'
    assert pool['tv'] == []


# ── Matching ────────────────────────────────────────────────────────────────

def _release(title, imdb=''):
    from PTT import parse_title
    p = parse_title(title)
    return {'title': title, 'parsed_info': {'title': p.get('title'), 'year': p.get('year'),
                                            'newznab_imdb': imdb}}


def test_release_with_the_items_imdb_id_matches_whatever_its_title():
    from database.nzb_upgrade import _filter_pool_for_item
    pool = [_release('Zappa.Original.Title.2025.1080p', imdb='123456')]
    assert _filter_pool_for_item(pool, 'Translated Title', 2025, imdb_id='tt0123456') == pool


def test_release_tagged_with_another_imdb_id_never_matches():
    from database.nzb_upgrade import _filter_pool_for_item
    pool = [_release('The.Long.Walk.2025.1080p', imdb='999')]
    assert _filter_pool_for_item(pool, 'The Long Walk', 2025, imdb_id='tt0123456') == []


def test_tv_release_with_a_wrong_imdb_id_still_matches_by_title():
    # NZBGeek tagged Marshals.S02E07…-LuCY with an unrelated show's id.
    from database.nzb_upgrade import _filter_pool_for_item
    pool = [_release('Marshals.S02E07.1080p.WEBRip.HEVC-LuCY', imdb='2891070')]
    assert _filter_pool_for_item(pool, 'Marshals', 2026, imdb_id='tt36849871', trust_imdb=False) == pool
    assert _filter_pool_for_item(pool, 'Marshals', 2026, imdb_id='tt36849871') == []  # movies stay strict


def test_untagged_release_falls_back_to_title_and_year():
    from database.nzb_upgrade import _filter_pool_for_item
    pool = [_release('The.Long.Walk.2025.1080p'), _release('The.Long.Walk.1979.1080p')]
    got = _filter_pool_for_item(pool, 'The Long Walk', 2025, imdb_id='tt0123456')
    assert [r['title'] for r in got] == ['The.Long.Walk.2025.1080p']


# ── The NZB phase reads the history ─────────────────────────────────────────

def test_nzb_scan_syncs_then_matches_against_history(feed_db, app_settings):
    import database.nzb_upgrade as nu
    conn = feed_db()
    _library(conn)
    _sync(FakeIndexer([_item('The.Long.Walk.2025.2160p.WEB-DL-GRP', 'a', 30 * 24)]))  # a month old
    movie = dict(conn.execute("SELECT * FROM media_items WHERE title = 'The Long Walk'").fetchone())
    conn.close()

    def fake_score(current_filename, pool, *a, **k):
        return 50.0, [(100.0, r) for r in pool]

    with mock.patch.object(nu, 'get_newznab_scrapers', return_value=SCRAPERS), \
         mock.patch('database.nzb_feed_history.sync_feeds') as sync, \
         mock.patch('database.zilean_upgrade._score_results_with_current', side_effect=fake_score):
        upgrades, packs = nu.scan_nzb_upgrades([movie], [], {}, 0.1, 0.5, '1080p', frozenset())
    sync.assert_called_once_with(SCRAPERS)
    [c] = upgrades
    assert c['item_id'] == movie['id'] and c['new_magnet'] == 'https://idx/get/a'


def test_browse_request_leaves_out_q():
    # An empty q= gets 'Missing parameter' from several indexers.
    import database.nzb_upgrade as nu
    resp = mock.Mock(status_code=200, text='<rss><channel></channel></rss>')
    with mock.patch('routes.api_tracker.api.get', return_value=resp) as get, \
         mock.patch('usenet.indexer_quota.allow_url', return_value=True):
        nu._fetch_browse_page('https://idx', 'k', '2000', 100, 0)
    params = get.call_args.kwargs['params']
    assert 'q' not in params and params['t'] == 'search' and params['extended'] == 1


# ── Version filters honour debrid-only / NZB-only patterns ──────────────────

def test_hub_hard_filter_applies_patterns_only_to_their_source():
    from database.zilean_upgrade import _apply_version_hard_filters
    vs = {'max_resolution': '2160p', 'resolution_wanted': '<=', 'filter_out': [
        {'pattern': '(?-i:WEB-DL)', 'source': 'debrid'},
        {'pattern': 'NZBONLY', 'source': 'nzb'},
        {'pattern': 'CAM', 'source': 'both'},
        'LEGACYSTRING',
    ]}

    def r(title, nzb):
        d = {'title': title, 'parsed_info': {'resolution': '1080p'}}
        if nzb:
            d.update(protocol='nzb', nzb_url='https://idx/get/1')
        return d

    kept = [x['title'] + (' (nzb)' if x.get('protocol') else '') for x in _apply_version_hard_filters([
        r('Movie.2025.1080p.WEB-DL-GRP', nzb=True),     # debrid-only rule: kept for NZB
        r('Movie.2025.1080p.WEB-DL-GRP', nzb=False),    # rejected for torrents
        r('Movie.2025.1080p.NZBONLY', nzb=True),        # rejected for NZB
        r('Movie.2025.1080p.NZBONLY', nzb=False),       # kept for torrents
        r('Movie.2025.CAM', nzb=True),                  # 'both' rejects either
        r('Movie.2025.LEGACYSTRING', nzb=False),        # plain strings apply to both
    ], vs)]
    assert kept == ['Movie.2025.1080p.WEB-DL-GRP (nzb)', 'Movie.2025.1080p.NZBONLY']


# ── Obfuscated current files ────────────────────────────────────────────────

def test_obfuscated_current_file_is_scored_by_its_release_name():
    from database.zilean_upgrade import _item_filename
    release = 'The.Lord.of.the.Rings.The.Return.of.the.King.2003.Extended.Remastered.BluRay.1080p.DDP.5.1.x264-hallowed'
    item = {'filled_by_file': 'b8ccc80d20904e7b976e5f978facb55c.mkv', 'location_basename': None,
            'original_scraped_torrent_title': release, 'filled_by_title': release}
    assert _item_filename(item) == release
    normal = {'filled_by_file': 'Movie.2025.2160p.WEB-DL.mkv', 'filled_by_title': 'Movie.2025.1080p.WEB'}
    assert _item_filename(normal) == 'Movie.2025.2160p.WEB-DL.mkv'   # a real filename still wins
    assert _item_filename({'filled_by_file': 'abc123.mkv'}) == 'abc123.mkv'  # nothing better known
    assert _item_filename({}) == ''
