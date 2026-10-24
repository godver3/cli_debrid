"""Combined-mode Library size with base decypharr instead of cli_mount.

Only cli_mount's /debug/stats has a 'content' block with the combined library
size. Base decypharr's has none, which showed as 0.00 B; the stats now fall back
to the cached debrid library size there, and keep cli_mount's own figure when
it's present.

Runs in a fresh interpreter (other tests leave stubs for database modules in
sys.modules) with the cli_mount calls faked.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent('''
    import json, os, sys
    from datetime import datetime
    sys.path.insert(0, %(root)r)
    import database  # noqa
    import database.statistics as st

    st.resolve_stats_mode = lambda: 'combined'
    st._fetch_climount_broken_split = lambda total: (total, 0)
    POOL = {'usenet': {'pool': {'active': 1, 'idle': 2, 'max_connections': 10}},
            'repair': {'health': {'broken': 1}}}

    def run(stats):
        st._fetch_climount_debug_stats = lambda timeout=10: stats
        st.download_stats_cache['last_update'] = 0
        active, _ = st._refresh_download_stats_blocking()
        return {k: active.get(k) for k in ('library_size', 'library_source')}

    out = {}
    out['decypharr_no_cache'] = run(dict(POOL))
    with open(st.LIBRARY_SIZE_CACHE_FILE, 'w') as f:
        json.dump({'size_str': '12.34 TB', 'timestamp': datetime.utcnow().isoformat()}, f)
    out['decypharr_cached'] = run(dict(POOL))
    out['climount'] = run(dict(POOL, content={'total_count': 5, 'total_size': 2 * 1024 ** 4}))
    with open(st.LIBRARY_SIZE_CACHE_FILE, 'w') as f:
        json.dump({'size_str': '1 TB', 'timestamp': '2000-01-01T00:00:00'}, f)
    out['decypharr_expired_cache'] = run(dict(POOL))
    print('RESULT ' + json.dumps(out))
''')


class LibrarySizeBaseDecypharrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, USER_DB_CONTENT=tmp, USER_CONFIG=tmp, USER_LOGS=tmp, PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([sys.executable, '-c', SCRIPT % {'root': ROOT}], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=300)
        lines = [l for l in proc.stdout.splitlines() if l.startswith('RESULT ')]
        if not lines:
            raise AssertionError(f"probe failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        cls.out = json.loads(lines[-1][len('RESULT '):])

    def test_base_decypharr_shows_cached_debrid_size(self):
        self.assertEqual(self.out['decypharr_cached'], {'library_size': '12.34 TB', 'library_source': 'debrid'})

    def test_base_decypharr_without_cache_is_not_zero(self):
        self.assertEqual(self.out['decypharr_no_cache'], {'library_size': 'N/A', 'library_source': 'debrid'})
        self.assertEqual(self.out['decypharr_expired_cache']['library_size'], 'N/A')

    def test_cli_mount_keeps_its_combined_size(self):
        self.assertEqual(self.out['climount'], {'library_size': '2.00 TB', 'library_source': 'climount'})


if __name__ == '__main__':
    unittest.main()
