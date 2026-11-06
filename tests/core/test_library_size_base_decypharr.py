"""Combined-mode Library size with base decypharr instead of cli_mount.

Only cli_mount's /debug/stats has a 'content' block with the combined library
size. Base decypharr's has none, which showed as 0.00 B; the stats now fall back
to the cached debrid library size there, and keep cli_mount's own figure when
it's present.
"""
import json
from datetime import datetime

import pytest

POOL = {'usenet': {'pool': {'active': 1, 'idle': 2, 'max_connections': 10}},
        'repair': {'health': {'broken': 1}}}


@pytest.fixture
def stats(tmp_path, monkeypatch):
    import database  # noqa: F401  (the app imports the package first)
    import database.statistics as st
    monkeypatch.setattr(st, 'resolve_stats_mode', lambda: 'combined')
    monkeypatch.setattr(st, '_fetch_climount_broken_split', lambda total: (total, 0))
    monkeypatch.setattr(st, 'LIBRARY_SIZE_CACHE_FILE', str(tmp_path / 'library_size_cache.json'))
    monkeypatch.setitem(st.download_stats_cache, 'last_update', 0)

    def run(debug_stats):
        monkeypatch.setattr(st, '_fetch_climount_debug_stats', lambda timeout=10: debug_stats)
        st.download_stats_cache['last_update'] = 0
        active, _ = st._refresh_download_stats_blocking()
        return {k: active.get(k) for k in ('library_size', 'library_source')}

    def write_cache(size_str, timestamp):
        with open(st.LIBRARY_SIZE_CACHE_FILE, 'w') as f:
            json.dump({'size_str': size_str, 'timestamp': timestamp}, f)

    return run, write_cache


def test_base_decypharr_shows_cached_debrid_size(stats):
    run, write_cache = stats
    write_cache('12.34 TB', datetime.utcnow().isoformat())
    assert run(dict(POOL)) == {'library_size': '12.34 TB', 'library_source': 'debrid'}


def test_base_decypharr_without_cache_is_not_zero(stats):
    run, write_cache = stats
    assert run(dict(POOL)) == {'library_size': 'N/A', 'library_source': 'debrid'}
    write_cache('1 TB', '2000-01-01T00:00:00')  # expired
    assert run(dict(POOL))['library_size'] == 'N/A'


def test_cli_mount_keeps_its_combined_size(stats):
    run, _ = stats
    combined = dict(POOL, content={'total_count': 5, 'total_size': 2 * 1024 ** 4})
    assert run(combined) == {'library_size': '2.00 TB', 'library_source': 'climount'}
