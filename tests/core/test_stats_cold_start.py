"""Home page right after a restart: the stats placeholder keeps the cli_mount layout."""
from unittest import mock

import pytest

import database  # noqa: F401  (app import order)
from database import statistics as st


@pytest.fixture
def cold_cache():
    saved = dict(st.download_stats_cache)
    st.download_stats_cache.update(active_downloads=None, usage_stats=None, last_update=0, last_attempt=0)
    with mock.patch.object(st, '_run_stats_refresh'), mock.patch.object(st, '_STATS_COLD_START_WAIT', 0):
        yield
    st.download_stats_cache.clear()
    st.download_stats_cache.update(saved)


@pytest.mark.parametrize('mode, source', [('combined', 'combined'), ('usenet', 'climount')])
def test_placeholder_keeps_the_mount_source(cold_cache, mode, source):
    with mock.patch.object(st, 'resolve_stats_mode', return_value=mode):
        active, _ = st.get_cached_download_stats()
    assert active['status'] == 'loading'
    assert active['source'] == source
    # Never the debrid-only library_size_cache.json value
    assert active['library_size'] == '…'


def test_debrid_mode_placeholder_has_no_source(cold_cache):
    with mock.patch.object(st, 'resolve_stats_mode', return_value='debrid'):
        active, _ = st.get_cached_download_stats()
    assert 'source' not in active
