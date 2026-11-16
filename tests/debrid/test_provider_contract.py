"""Every debrid provider exposes the attributes cli_debrid's callers read directly.

queues/torrent_processor.py, routes/scraper_routes.py and utilities/web_scraper.py
read provider._all_torrent_ids without getattr, so a provider that lacks it makes
every add fail with AttributeError.
"""

import pytest

import database  # noqa: F401 - app import order

import debrid

PROVIDER_CLASSES = [
    debrid.RealDebridProvider,
    debrid.AllDebridProvider,
    debrid.PremiumizeProvider,
    debrid.TorboxProvider,
    debrid.DebridLinkProvider,
]
if hasattr(debrid, 'HighwayProvider'):
    PROVIDER_CLASSES.append(debrid.HighwayProvider)


@pytest.mark.parametrize('cls', PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_provider_exposes_attributes_callers_read(cls):
    p = cls()
    assert isinstance(getattr(p, '_all_torrent_ids', None), dict)
    assert isinstance(getattr(p, '_cached_torrent_ids', None), dict)
    assert isinstance(p.PROVIDER_NAME, str) and p.PROVIDER_NAME != 'Debrid'
    for cap in ('supports_direct_cache_check', 'supports_bulk_cache_checking', 'supports_uncached'):
        assert isinstance(getattr(p, cap), bool)
