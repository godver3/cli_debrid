"""Debrid library size adds up every configured provider, not only Real-Debrid."""
import json

import pytest


class FakeProvider:
    def __init__(self, name, torrents=None, fail=False, completed=None):
        self.PROVIDER_NAME = name
        self.api_key = 'k'
        self._torrents, self._fail = torrents or [], fail
        if completed is not None:
            self.list_completed_torrents = lambda: completed

    def list_active_torrents(self):
        if self._fail:
            raise RuntimeError('down')
        return self._torrents


@pytest.fixture
def st(tmp_path, monkeypatch):
    import database  # noqa: F401  (the app imports the package first)
    import database.statistics as st
    monkeypatch.setattr(st, 'LIBRARY_SIZE_CACHE_FILE', str(tmp_path / 'library_size_cache.json'))
    return st


def _use(monkeypatch, providers):
    import debrid
    monkeypatch.setattr(debrid, 'get_debrid_providers', lambda: providers)


def test_sums_every_provider(st, monkeypatch):
    GB = 1024 ** 3
    _use(monkeypatch, [
        FakeProvider('Torbox', [{'hash': 'A', 'bytes': 2 * GB}, {'hash': 'a', 'bytes': 2 * GB}, {'hash': 'b', 'bytes': GB}]),
        FakeProvider('Premiumize', [{'hash': 'pending', 'bytes': 50 * GB}], completed=[{'hash': 'c', 'bytes': GB}]),
    ])
    assert st.refresh_debrid_library_size() == '4.00 GB'  # A counted once; Premiumize completed only
    with open(st.LIBRARY_SIZE_CACHE_FILE) as f:
        cached = json.load(f)
    assert cached['size_str'] == '4.00 GB'
    assert [p['provider'] for p in cached['providers']] == ['Torbox', 'Premiumize']
    assert st.read_library_size_cache()['size_str'] == '4.00 GB'


def test_real_debrid_uses_its_full_list(st, monkeypatch):
    import debrid.real_debrid.api as rd_api

    async def all_torrents(api_key):
        return [{'hash': 'x', 'bytes': 1024}]
    monkeypatch.setattr(rd_api, 'get_all_torrents', all_torrents)
    _use(monkeypatch, [FakeProvider('Real-Debrid'), FakeProvider('Torbox', [{'hash': 'y', 'bytes': 1024}])])
    assert st.refresh_debrid_library_size() == '2.00 KB'


def test_failed_provider_keeps_last_total(st, monkeypatch):
    _use(monkeypatch, [FakeProvider('Torbox', [{'hash': 'a', 'bytes': 1024}])])
    assert st.refresh_debrid_library_size() == '1.00 KB'
    _use(monkeypatch, [FakeProvider('Torbox', [{'hash': 'a', 'bytes': 1024}]), FakeProvider('AllDebrid', fail=True)])
    assert st.refresh_debrid_library_size() is None
    assert st.read_library_size_cache()['size_str'] == '1.00 KB'


def test_no_provider(st, monkeypatch):
    _use(monkeypatch, [])
    assert st.refresh_debrid_library_size() is None
