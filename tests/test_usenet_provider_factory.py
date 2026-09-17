#!/usr/bin/env python3
"""
Unit tests for usenet/__init__.py's provider-agnostic factory, and two
related Windows/zurg-provider correctness bugs found alongside it:

  1. usenet.is_nzb_job_alive() must dispatch to whichever provider is
     actually configured (nzbdav/zurg vs climount), not always query
     climount's client regardless of the active provider.
  2. NzbdavClient's mount_path normalization only stripped '/'-style
     paths, so a Windows 'mounted_file_location' like 'Z:\\__all__' was
     never trimmed to 'Z:\\'.
  3. NzbdavClient.trigger_health_scan() didn't accept the
     (full, wait, timeout) kwargs that usenet/repair_engine.py always
     passes, so every repair-triggered health scan against nzbdav/zurg
     raised a TypeError (caught and logged as a warning, silently no-op).
"""

import unittest
import sys
import os
import types
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIsNzbJobAliveDispatch(unittest.TestCase):
    def setUp(self):
        # Fresh module instance per test so monkeypatched settings don't leak.
        for name in list(sys.modules):
            if name == 'usenet' or name.startswith('usenet.'):
                del sys.modules[name]

        if 'utilities' not in sys.modules:
            sys.modules['utilities'] = types.ModuleType('utilities')
        uss = types.ModuleType('utilities.settings')
        self._provider = {'provider': 'climount'}
        uss.get_setting = lambda key, *a, **k: (self._provider if key == 'Usenet Provider' else {})
        sys.modules['utilities.settings'] = uss

        import usenet
        self.usenet = usenet

    def test_climount_provider_uses_climount_is_alive(self):
        self._provider['provider'] = 'climount'
        calls = []
        fake_climount = types.ModuleType('usenet.climount_client')
        fake_climount.is_nzb_job_alive = lambda h: calls.append(('climount', h)) or True
        sys.modules['usenet.climount_client'] = fake_climount

        self.assertTrue(self.usenet.is_nzb_job_alive('abc123'))
        self.assertEqual(calls, [('climount', 'abc123')])

    def test_zurg_provider_queries_nzbdav_client_not_climount(self):
        self._provider['provider'] = 'zurg'
        climount_calls = []
        fake_climount = types.ModuleType('usenet.climount_client')
        fake_climount.is_nzb_job_alive = lambda h: climount_calls.append(h) or True
        sys.modules['usenet.climount_client'] = fake_climount

        nzbdav_calls = []

        class _FakeNzbdavClient:
            def get_job_status(self, job_hash):
                nzbdav_calls.append(job_hash)
                return {'raw': {'state': 'downloading'}}

        fake_nzbdav = types.ModuleType('usenet.nzbdav_client')
        fake_nzbdav.get_nzbdav_client = lambda: _FakeNzbdavClient()
        sys.modules['usenet.nzbdav_client'] = fake_nzbdav

        result = self.usenet.is_nzb_job_alive('zurgjob1')

        self.assertTrue(result)
        self.assertEqual(nzbdav_calls, ['zurgjob1'])
        # The bug: climount's client must never be consulted for a zurg job.
        self.assertEqual(climount_calls, [])

    def test_zurg_provider_reports_dead_job_when_status_empty(self):
        self._provider['provider'] = 'zurg'
        fake_climount = types.ModuleType('usenet.climount_client')
        fake_climount.is_nzb_job_alive = lambda h: True
        sys.modules['usenet.climount_client'] = fake_climount

        class _FakeNzbdavClient:
            def get_job_status(self, job_hash):
                return None

        fake_nzbdav = types.ModuleType('usenet.nzbdav_client')
        fake_nzbdav.get_nzbdav_client = lambda: _FakeNzbdavClient()
        sys.modules['usenet.nzbdav_client'] = fake_nzbdav

        self.assertFalse(self.usenet.is_nzb_job_alive('gonejob'))


def _load_nzbdav_module():
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: {}
    sys.modules['utilities.settings'] = uss
    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')
    rta.api = types.SimpleNamespace(get=None, post=None)
    sys.modules['routes.api_tracker'] = rta
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'usenet', 'nzbdav_client.py')
    spec = importlib.util.spec_from_file_location('nzbdav_client_factory_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestWindowsMountPathNormalization(unittest.TestCase):
    def setUp(self):
        self.nc = _load_nzbdav_module()

    def _mount_path_for(self, mounted_file_location, provider='nzbdav'):
        c = self.nc.NzbdavClient.__new__(self.nc.NzbdavClient)
        cfg = {'mounted_file_location': mounted_file_location, 'provider': provider}
        c.mount_path = cfg.get('mounted_file_location', '').rstrip('/\\')
        if c.mount_path.endswith(('/__all__', '\\__all__')):
            c.mount_path = c.mount_path[: -len('/__all__')]
        if not c.mount_path:
            c.mount_path = '/mnt/remote/nzbdav'
        return c.mount_path

    def test_posix_trailing_all_stripped(self):
        self.assertEqual(self._mount_path_for('/mnt/zurg/__all__'), '/mnt/zurg')

    def test_windows_trailing_all_stripped(self):
        self.assertEqual(self._mount_path_for('Z:\\__all__'), 'Z:')

    def test_windows_trailing_backslash_stripped(self):
        self.assertEqual(self._mount_path_for('Z:\\__all__\\'), 'Z:')


class TestTriggerHealthScanSignature(unittest.TestCase):
    def setUp(self):
        self.nc = _load_nzbdav_module()

    def test_accepts_repair_engine_kwargs(self):
        c = self.nc.NzbdavClient.__new__(self.nc.NzbdavClient)
        # usenet/repair_engine.py always calls trigger_health_scan(full=, wait=, timeout=);
        # NzbdavClient's no-op previously only accepted zero args and raised TypeError.
        self.assertTrue(c.trigger_health_scan(full=False, wait=True, timeout=300))


if __name__ == '__main__':
    unittest.main()
