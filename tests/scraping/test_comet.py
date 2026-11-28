"""Comet (Stremio addon) scraper: URL/config handling, stream parsing for both
torrent (infoHash) and debrid (playback URL) responses, and error handling."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

import database  # noqa: F401  (app import order)
from scraper import comet

H1 = 'a' * 40
H2 = 'b' * 40
H3 = 'c' * 40
CFG = base64.b64encode(json.dumps({'debridService': 'realdebrid', 'debridApiKey': 'SECRETKEY'}).encode()).decode()


def _resp(status=200, payload=None, raises_json=False):
    r = MagicMock()
    r.status_code = status
    if raises_json:
        r.json.side_effect = ValueError('bad json')
    else:
        r.json.return_value = payload
    return r


# --- fixtures: shapes taken from comet/api/endpoints/stream.py -----------------

MOVIE_TORRENT = {'streams': [{
    'name': '[TORRENT] Comet 1080p',
    'description': '📄 The.Matrix.1999.1080p.BluRay.x264-GRP\n📹 AVC\n🔊 DTS • 5.1\n⭐ BluRay\n🏷️ GRP\n'
                   '👤 87 💾 2.0 GB 🔎 ThePirateBay\n🇬🇧',
    'behaviorHints': {'bingeGroup': f'comet|torrent|{H1}', 'filename': 'The.Matrix.1999.1080p.BluRay.x264-GRP',
                      'videoSize': 2 * 1024 ** 3},
    'infoHash': H1.upper(), 'sources': [f'dht:{H1}', 'tracker:udp://t.example:1337'],
}]}

EPISODE_TORRENT = {'streams': [{
    'name': '[TORRENT] Comet 720p',
    'description': '📄 Show.S01E02.720p.WEB.x265-GRP\n👤 12 💾 700.0 MB 🔎 Comet|YTS',
    'behaviorHints': {'bingeGroup': f'comet|torrent|{H2}', 'filename': 'Show.S01E02.720p.WEB.x265-GRP',
                      'videoSize': 700 * 1024 ** 2},
    'infoHash': H2, 'fileIdx': 3,
}]}

DEBRID_ONLY = {'streams': [
    {'name': '[RD⚡] Comet 1080p', 'description': '📄 Movie.2020.1080p.WEB-DL\n👤 5 💾 1.5 GB 🔎 Rarbg',
     'behaviorHints': {'bingeGroup': f'comet|realdebrid|{H3}', 'filename': 'Movie.2020.1080p.WEB-DL',
                       'videoSize': 1610612736},
     'url': f'https://comet.example/{CFG}/playback/{H3}/0/n/n/n?torrent_name=x&name=Movie&media_id=tt1&media_type=movie'},
    # same torrent listed again for a second debrid entry: deduped
    {'name': '[AD⚡] Comet 1080p', 'description': '📄 Movie.2020.1080p.WEB-DL',
     'behaviorHints': {'bingeGroup': f'comet|alldebrid|{H3}'},
     'url': f'https://comet.example/{CFG}/playback/{H3}/1/2/n/n?x=1'},
]}

NO_HASH = {'streams': [
    {'name': 'mystery', 'description': '📄 Some.Release.2020.1080p', 'url': 'https://elsewhere.example/video.mkv'},
    {'name': 'torrent no hash', 'description': 'nothing', 'behaviorHints': {}},
]}

OBSOLETE = {'streams': [{'name': '[❌] Comet', 'url': 'https://comet.feels.legal',
                         'description': '⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON https://comet.example ⚠️'}]}
BUSY = {'streams': [{'name': '[🔄] Comet', 'url': 'https://comet.feels.legal',
                     'description': 'Scraping in progress, please try again in a few seconds...'}]}


# --- URL / config --------------------------------------------------------------

@pytest.mark.parametrize('given,expected', [
    ('https://comet.example', 'https://comet.example'),
    ('https://comet.example/', 'https://comet.example'),
    ('comet.example', 'https://comet.example'),
    ('192.168.1.5:8000', 'http://192.168.1.5:8000'),
    (f'10.0.0.2:8000/{CFG}/manifest.json', f'http://10.0.0.2:8000/{CFG}'),
    ('localhost:8000', 'http://localhost:8000'),
    ('comet:8000', 'http://comet:8000'),
    ('nas.local:8000', 'http://nas.local:8000'),
    ('http://192.168.1.5:8000', 'http://192.168.1.5:8000'),
    ('8.8.8.8:8000', 'https://8.8.8.8:8000'),
    (f'https://comet.example/{CFG}/manifest.json', f'https://comet.example/{CFG}'),
    (f'stremio://comet.example/{CFG}/manifest.json', f'https://comet.example/{CFG}'),
    (f'https://comet.example/{CFG}/configure', f'https://comet.example/{CFG}'),
    ('https://comet.example/manifest.json', 'https://comet.example'),
    ('https://host.example/comet/manifest.json', 'https://host.example/comet'),
    ('', ''),
])
def test_derive_base_url(given, expected):
    assert comet.derive_base_url(given) == expected


def test_config_appended_to_bare_url_only():
    raw = comet.derive_base_url('https://comet.example', '{"maxResultsPerResolution": 2}')
    seg = raw.rsplit('/', 1)[1]
    assert json.loads(base64.b64decode(seg)) == {'maxResultsPerResolution': 2}
    assert comet.derive_base_url('https://comet.example', CFG) == f'https://comet.example/{CFG}'
    # a URL that already carries a config wins
    assert comet.derive_base_url(f'https://comet.example/{CFG}/manifest.json', 'other') == f'https://comet.example/{CFG}'
    with pytest.raises(comet.CometError):
        comet.derive_base_url('https://comet.example', '{not json')


def test_construct_url():
    b = 'https://comet.example'
    assert comet.construct_url(b, 'tt1', 'movie') == f'{b}/stream/movie/tt1.json'
    assert comet.construct_url(b, 'tt1', 'episode', 1, 2) == f'{b}/stream/series/tt1:1:2.json'
    assert comet.construct_url(b, 'tt1', 'episode', 1) == f'{b}/stream/series/tt1:1:1.json'
    assert comet.construct_url(b, 'tt1', 'bogus') == ''


def test_redact_url_hides_config():
    out = comet.redact_url(f'https://comet.example/{CFG}/stream/movie/tt1.json')
    assert CFG not in out and out == 'https://comet.example/<config>/stream/movie/tt1.json'
    assert comet.redact_url('https://comet.example/manifest.json') == 'https://comet.example/manifest.json'
    assert comet.redact_url('https://comet.example/stream/movie/tt1.json') == 'https://comet.example/stream/movie/tt1.json'


# --- parsing -------------------------------------------------------------------

def test_parse_movie_torrent_stream():
    [r] = comet.parse_results(MOVIE_TORRENT['streams'], 'Comet_1')
    assert r['info_hash'] == H1
    assert r['title'] == 'The.Matrix.1999.1080p.BluRay.x264-GRP'
    assert r['size'] == 2.0
    assert r['seeders'] == 87
    assert r['indexer'] == 'ThePirateBay'
    assert r['source'] == 'Comet_1 - ThePirateBay'
    assert r['magnet'] == f'magnet:?xt=urn:btih:{H1}'
    assert r['parsed_info']['filename'] == 'The.Matrix.1999.1080p.BluRay.x264-GRP'
    assert r['parsed_info']['bingeGroup'] == f'comet|torrent|{H1}'


def test_parse_episode_stream_with_file_index_and_comet_tracker_prefix():
    [r] = comet.parse_results(EPISODE_TORRENT['streams'], 'Comet_1')
    assert r['info_hash'] == H2
    assert r['size'] == 0.68  # 700 MiB in GiB, rounded
    assert r['seeders'] == 12
    assert r['indexer'] == 'YTS'
    assert r['magnet'].endswith('&so=3') and f'btih:{H2}' in r['magnet']


def test_parse_debrid_playback_urls_recovers_hash_and_dedups():
    results = comet.parse_results(DEBRID_ONLY['streams'], 'Comet_1')
    assert [r['info_hash'] for r in results] == [H3]
    assert results[0]['title'] == 'Movie.2020.1080p.WEB-DL'
    assert results[0]['size'] == 1.5
    assert results[0]['seeders'] == 5
    assert 'SECRET' not in json.dumps(results[0])


def test_extract_hash_from_binge_group_when_url_has_none():
    s = {'url': 'https://x.example/play', 'behaviorHints': {'bingeGroup': f'comet|torrent|{H1}'}}
    assert comet.extract_info_hash(s) == (H1, None)


def test_base32_hash_is_normalised():
    hex_hash = '0123456789abcdef0123456789abcdef01234567'
    b32 = base64.b32encode(bytes.fromhex(hex_hash)).decode()
    assert comet.normalize_info_hash(b32) == hex_hash
    assert comet.normalize_info_hash('nothex') == ''


def test_streams_without_hash_are_skipped(caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        assert comet.parse_results(NO_HASH['streams'], 'Comet_1') == []
    assert 'no info hash' in caplog.text.lower() or 'without a recoverable' in caplog.text


def test_notice_streams_are_not_results():
    assert comet.parse_results(OBSOLETE['streams'] + BUSY['streams'], 'Comet_1') == []


def test_debrid_results_from_the_public_instance_are_not_notices():
    # comet.feels.legal is both the notice link and the public instance itself
    s = {'name': '[RD+] Comet', 'description': '📄 Some.Movie.2020.1080p.WEB-DL.x264-GRP\n👤 5 💾 2.0 GB 🔎 DMM',
         'url': f'https://comet.feels.legal/abc123/playback/{H1}/0/n/n/n?name=x',
         'behaviorHints': {'filename': 'Some.Movie.2020.1080p.WEB-DL.x264-GRP.mkv'}}
    out = comet.parse_results([s], 'Comet_1')
    assert [r['info_hash'] for r in out] == [H1.lower()]
    assert comet.parse_results(OBSOLETE['streams'], 'Comet_1') == []


def test_title_falls_back_to_filename_when_result_format_has_no_title():
    s = {'name': 'x', 'description': '👤 3 💾 1.0 GB', 'infoHash': H1,
         'behaviorHints': {'filename': 'Fallback.Name.2020.1080p', 'videoSize': 0}}
    [r] = comet.parse_results([s], 'c')
    assert r['title'] == 'Fallback.Name.2020.1080p'
    assert r['size'] == 1.0 and r['seeders'] == 3


def test_plain_result_format_is_parsed():
    s = {'name': 'x', 'infoHash': H1, 'description': 'Show.S01E01.1080p\nSeeders: 9 Size: 1.2 GB Source: Foo'}
    [r] = comet.parse_results([s], 'c')
    assert (r['seeders'], r['size'], r['indexer']) == (9, 1.2, 'Foo')


def test_malformed_entries_do_not_break_parsing():
    out = comet.parse_results([None, 'str', {'infoHash': H1, 'description': '📄 Ok.2020.1080p', 'behaviorHints': 'x'}], 'c')
    assert [r['info_hash'] for r in out] == [H1]


# --- fetching ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_sleep():
    with patch.object(comet.time, 'sleep'):
        yield


def test_fetch_retries_on_5xx_then_succeeds():
    with patch.object(comet, '_http_get', side_effect=[_resp(503), _resp(502), _resp(200, MOVIE_TORRENT)]) as g:
        assert comet.fetch_data('https://c.example/stream/movie/tt1.json') == MOVIE_TORRENT
    assert g.call_count == 3


def test_fetch_gives_up_after_max_retries():
    with patch.object(comet, '_http_get', return_value=_resp(500)) as g:
        assert comet.fetch_data('https://c.example/x') == {}
    assert g.call_count == comet.MAX_RETRIES


def test_fetch_does_not_retry_4xx():
    with patch.object(comet, '_http_get', return_value=_resp(404)) as g:
        assert comet.fetch_data('https://c.example/x') == {}
    assert g.call_count == 1


def test_fetch_retries_timeouts_and_never_logs_the_config(caplog):
    import requests
    url = f'https://c.example/{CFG}/stream/movie/tt1.json'
    err = requests.exceptions.ReadTimeout(f'timed out for url: {url}')
    with patch.object(comet, '_http_get', side_effect=err) as g, caplog.at_level('DEBUG'):
        assert comet.fetch_data(url) == {}
    assert g.call_count == comet.MAX_RETRIES
    assert CFG not in caplog.text and 'SECRETKEY' not in caplog.text


def test_fetch_malformed_json():
    with patch.object(comet, '_http_get', return_value=_resp(raises_json=True)):
        assert comet.fetch_data('https://c.example/x') == {}


# --- scrape_comet_instance -----------------------------------------------------

def _scrape(payloads, settings=None, **kw):
    if not isinstance(payloads, list):
        payloads = [payloads]
    args = dict(instance='Comet_1', settings=settings or {'url': 'https://c.example'}, imdb_id='tt1', title='T',
                year=2020, content_type='movie')
    args.update(kw)
    with patch.object(comet, 'get_imdb_aliases', return_value=['tt1']), \
         patch.object(comet, '_http_get', side_effect=payloads) as g:
        return comet.scrape_comet_instance(**args), g


def test_scrape_movie_uses_stream_url():
    results, g = _scrape(_resp(200, MOVIE_TORRENT))
    assert [r['info_hash'] for r in results] == [H1]
    assert g.call_args[0][0] == 'https://c.example/stream/movie/tt1.json'


def test_scrape_episode_from_manifest_url_with_config():
    s = {'url': f'https://c.example/{CFG}/manifest.json'}
    results, g = _scrape(_resp(200, EPISODE_TORRENT), s, content_type='episode', season=1, episode=2)
    assert [r['info_hash'] for r in results] == [H2]
    assert g.call_args[0][0] == f'https://c.example/{CFG}/stream/series/tt1:1:2.json'


def test_scrape_aliases_are_merged_and_deduped():
    with patch.object(comet, 'get_imdb_aliases', return_value=['tt1', 'tt2']), \
         patch.object(comet, '_http_get', side_effect=[_resp(200, MOVIE_TORRENT), _resp(200, MOVIE_TORRENT)]) as g:
        out = comet.scrape_comet_instance('c', {'url': 'https://c.example'}, 'tt1', 'T', 2020, 'movie')
    assert g.call_count == 2 and len(out) == 1


def test_scrape_empty_results():
    results, _ = _scrape(_resp(200, {'streams': []}))
    assert results == []


def test_scrape_no_hash_results_are_dropped():
    results, _ = _scrape(_resp(200, NO_HASH))
    assert results == []


def test_scrape_malformed_payload_and_http_error():
    assert _scrape(_resp(200, {'nope': 1}))[0] == []
    assert _scrape(_resp(200, ['list']))[0] == []
    assert _scrape(_resp(raises_json=True))[0] == []
    assert _scrape(_resp(403))[0] == []


def test_scrape_obsolete_config_logs_clear_message(caplog):
    with caplog.at_level('ERROR'):
        results, _ = _scrape(_resp(200, OBSOLETE))
    assert results == []
    assert 'OBSOLETE' in caplog.text and 'Re-configure' in caplog.text


def test_scrape_retries_when_scrape_in_progress():
    results, g = _scrape([_resp(200, BUSY), _resp(200, MOVIE_TORRENT)])
    assert [r['info_hash'] for r in results] == [H1]
    assert g.call_count == 2


def test_scrape_gives_up_on_persistent_busy():
    results, g = _scrape([_resp(200, BUSY)] * 3)
    assert results == [] and g.call_count == comet.BUSY_RETRIES + 1


def test_scrape_missing_url_or_imdb():
    assert _scrape(_resp(200, MOVIE_TORRENT), {'url': ''})[0] == []
    assert _scrape(_resp(200, MOVIE_TORRENT), imdb_id=None)[0] == []
    assert _scrape(_resp(200, MOVIE_TORRENT), {'url': 'https://c.example', 'config': '{bad'})[0] == []


# --- manifest validation -------------------------------------------------------

GOOD_MANIFEST = {'id': 'comet.example', 'version': '2.0.0', 'name': 'Comet',
                 'resources': [{'name': 'stream', 'types': ['movie', 'series']}]}


def test_validate_manifest_ok_and_hides_config():
    with patch.object(comet, '_http_get', return_value=_resp(200, GOOD_MANIFEST)) as g:
        ok, msg, details = comet.validate_manifest(f'https://c.example/{CFG}/manifest.json')
    assert ok and details['addon_name'] == 'Comet'
    assert g.call_args[0][0] == f'https://c.example/{CFG}/manifest.json'
    assert CFG not in json.dumps(details)


def test_validate_manifest_failures():
    cases = [
        (_resp(500), 'HTTP 500'),
        (_resp(raises_json=True), 'not a Comet manifest'),
        (_resp(200, {'foo': 1}), 'Invalid Stremio manifest'),
        (_resp(200, {'id': 'x', 'resources': ['catalog']}), "no 'stream'"),
        (_resp(200, {**GOOD_MANIFEST, 'description': '⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE'}), 'OBSOLETE'),
    ]
    for resp, needle in cases:
        with patch.object(comet, '_http_get', return_value=resp):
            ok, msg, _ = comet.validate_manifest('https://c.example')
        assert not ok and needle in msg, (needle, msg)
    with patch.object(comet, '_http_get', side_effect=OSError('down')):
        assert comet.validate_manifest('https://c.example')[0] is False
    assert comet.validate_manifest('')[0] is False


# --- registration --------------------------------------------------------------

def test_registered_in_scraper_manager_and_schema():
    from scraper.scraper_manager import ScraperManager
    from utilities.settings_schema.scrapers import SECTIONS
    assert ScraperManager({}).scrapers['Comet'] is comet.scrape_comet_instance
    assert "Comet" in SECTIONS["Scrapers"]["schema"]


def test_raw_json_config_never_encodes_a_slash():
    # '?' at byte 3k+2 is the only ASCII that makes standard base64 emit '/'.
    for pad in range(3):
        raw = json.dumps({'k' * (pad + 1): 'a?b?c?d', 'name': 'caf\u00e9'})
        seg = comet.derive_base_url('https://comet.example', raw).rsplit('/', 1)[1]
        assert '/' not in seg
        assert json.loads(base64.b64decode(seg)) == json.loads(raw)


def test_redact_url_hides_multi_segment_config():
    out = comet.redact_url('https://comet.example/AAA/BBB/stream/movie/tt1.json')
    assert 'AAA' not in out and out == 'https://comet.example/<config>/stream/movie/tt1.json'
