"""cached_state(): UI polls share one supervisorctl answer for a few seconds,
and start/stop/restart clears it so the button flips right away."""
import threading
import time
import unittest
from unittest import mock

from utilities import builtin_engine


class CachedStateTest(unittest.TestCase):
    def setUp(self):
        builtin_engine._clear_state_cache()
        self.addCleanup(builtin_engine._clear_state_cache)

    def test_reuses_recent_answer(self):
        with mock.patch.object(builtin_engine, 'state', return_value='RUNNING') as st:
            self.assertEqual(builtin_engine.cached_state(), 'RUNNING')
            self.assertEqual(builtin_engine.cached_state(), 'RUNNING')
        self.assertEqual(st.call_count, 1)

    def test_expires(self):
        with mock.patch.object(builtin_engine, 'state', side_effect=['RUNNING', 'STOPPED']) as st:
            self.assertEqual(builtin_engine.cached_state(max_age=0.05), 'RUNNING')
            time.sleep(0.06)
            self.assertEqual(builtin_engine.cached_state(max_age=0.05), 'STOPPED')
        self.assertEqual(st.call_count, 2)

    def test_control_clears_cache(self):
        with mock.patch.object(builtin_engine, 'state', side_effect=['RUNNING', 'STOPPED']), \
                mock.patch.object(builtin_engine, 'IS_WINDOWS', False), \
                mock.patch.object(builtin_engine, '_supervisorctl', return_value='stopped') as sc:
            self.assertEqual(builtin_engine.cached_state(), 'RUNNING')
            builtin_engine.control('stop')
            self.assertEqual(builtin_engine.cached_state(), 'STOPPED')
        sc.assert_called_once_with('stop', builtin_engine.PROGRAM)

    def test_control_clears_cache_on_error(self):
        with mock.patch.object(builtin_engine, 'state', side_effect=['RUNNING', 'FATAL']), \
                mock.patch.object(builtin_engine, 'IS_WINDOWS', False), \
                mock.patch.object(builtin_engine, '_supervisorctl', side_effect=RuntimeError('boom')):
            builtin_engine.cached_state()
            with self.assertRaises(RuntimeError):
                builtin_engine.control('start')
            self.assertEqual(builtin_engine.cached_state(), 'FATAL')

    def test_concurrent_callers_share_one_call(self):
        gate = threading.Event()

        def slow_state():
            gate.wait(1)
            return 'RUNNING'

        results = []
        with mock.patch.object(builtin_engine, 'state', side_effect=slow_state) as st:
            threads = [threading.Thread(target=lambda: results.append(builtin_engine.cached_state()))
                       for _ in range(5)]
            for t in threads:
                t.start()
            time.sleep(0.05)
            gate.set()
            for t in threads:
                t.join(2)
        self.assertEqual(results, ['RUNNING'] * 5)
        self.assertEqual(st.call_count, 1)


if __name__ == '__main__':
    unittest.main()
