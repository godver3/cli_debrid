"""Debrid Manager's library (Torrents tab, Audit, Plex Trash) holds every provider's torrents."""
import time

import pytest


class FakeProvider:
    def __init__(self, name, torrents):
        self.PROVIDER_NAME = name
        self.api_key = 'k'
        self._torrents = torrents

    def list_active_torrents(self):
        return [dict(t) for t in self._torrents]


@pytest.fixture
def dm(monkeypatch):
    import database  # noqa: F401  (app import order)
    import routes.debrid_manager_routes as dm
    monkeypatch.setattr(dm, '_enrich_with_db', lambda torrents: torrents)
    monkeypatch.setattr(dm, '_save_lib_cache_to_db', lambda data: None)
    monkeypatch.setattr(dm, '_lib_db_loaded', True)
    with dm._lib['lock']:
        dm._lib.update(stable=None, partial=[], loading=False)
    return dm


def _call_library(dm, monkeypatch, providers, force=False):
    from flask import Flask
    monkeypatch.setattr(dm, 'get_debrid_providers', lambda: providers)
    app = Flask(__name__)
    with app.test_request_context('/debrid_manager/api/library' + ('?force=1' if force else '')):
        return dm.api_library.__wrapped__().get_json()


def _wait_loaded(dm):
    for _ in range(100):
        with dm._lib['lock']:
            if not dm._lib['loading']:
                return dm._lib['stable']
        time.sleep(0.02)
    raise AssertionError('library never finished')


def test_library_merges_and_tags_every_provider(dm, monkeypatch):
    providers = [FakeProvider('Torbox', [{'id': '1', 'hash': 'a', 'bytes': 5, 'status': 1}]),
                 FakeProvider('AllDebrid', [{'id': '1', 'hash': 'b', 'bytes': 7}])]
    data = _call_library(dm, monkeypatch, providers)
    assert data['success'] and not data['loading']
    assert [(t['_provider'], t['id']) for t in data['torrents']] == [('Torbox', '1'), ('AllDebrid', '1')]
    assert data['total'] == 2 and data['total_bytes'] == 12
    assert data['torrents'][0]['status'] == '1'
    assert data['providers'] == ['Torbox', 'AllDebrid']


def test_changed_provider_list_is_stale(dm, monkeypatch):
    tb = FakeProvider('Torbox', [{'id': '1', 'hash': 'a'}])
    _call_library(dm, monkeypatch, [tb])
    assert _call_library(dm, monkeypatch, [tb])['cache_status'] == 'fresh'
    ad = FakeProvider('AllDebrid', [{'id': '2', 'hash': 'b'}])
    assert _call_library(dm, monkeypatch, [tb, ad])['cache_status'] == 'stale'
    stable = _wait_loaded(dm)
    assert {t['_provider'] for t in stable['torrents']} == {'Torbox', 'AllDebrid'}
    assert _call_library(dm, monkeypatch, [tb, ad])['cache_status'] == 'fresh'


def test_more_pages_than_the_first_request_reads(dm, monkeypatch):
    monkeypatch.setattr(dm, '_INITIAL_PAGES', 1)
    providers = [FakeProvider('Torbox', [{'id': '1'}]), FakeProvider('AllDebrid', [{'id': '2'}])]
    data = _call_library(dm, monkeypatch, providers)
    assert data['loading'] and data['cache_status'] == 'partial' and data['total'] == 1
    stable = _wait_loaded(dm)
    assert [t['_provider'] for t in stable['torrents']] == ['Torbox', 'AllDebrid']


def test_failed_provider_keeps_previous_snapshot(dm, monkeypatch):
    tb = FakeProvider('Torbox', [{'id': '1'}])
    _call_library(dm, monkeypatch, [tb])
    before = dm._lib['stable']

    class Broken(FakeProvider):
        def list_active_torrents(self):
            raise RuntimeError('down')
    monkeypatch.setattr(dm, 'get_debrid_providers', lambda: [tb, Broken('AllDebrid', [])])
    dm.start_library_refresh()
    assert _wait_loaded(dm) is before
