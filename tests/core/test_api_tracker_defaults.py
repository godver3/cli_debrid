"""The shared APITracker session: default timeout, larger pool, rate-window pruning."""
import unittest
from unittest import mock



def _module():
    # Load the file under a private name: other tests stub routes.* in
    # sys.modules, and importing the routes package pulls in the whole app.
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "routes", "api_tracker.py")
    spec = importlib.util.spec_from_file_location("_api_tracker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.api_logger = mock.Mock()  # normally created by setup_api_logging()
    return mod


class APITrackerDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.mod = _module()
        self.api = self.mod.APITracker()
        self.resp = mock.Mock()
        self.resp.raise_for_status.return_value = None

    def test_default_timeout_applied(self):
        with mock.patch.object(self.api.session, "get", return_value=self.resp) as get:
            self.api.get("https://example.com/x")
        self.assertEqual(get.call_args.kwargs["timeout"], self.mod.DEFAULT_TIMEOUT)

    def test_explicit_timeout_kept(self):
        with mock.patch.object(self.api.session, "post", return_value=self.resp) as post:
            self.api.post("https://example.com/x", timeout=5)
            self.api.post("https://example.com/x", timeout=None)
        self.assertEqual([c.kwargs["timeout"] for c in post.call_args_list], [5, None])

    def test_pool_size(self):
        self.assertEqual(self.api.session.get_adapter("https://x").poolmanager.connection_pool_kw["maxsize"], 32)

    def test_rate_windows_drop_expired(self):
        limiter = self.mod.APIRateLimiter()
        with mock.patch.object(self.mod.time, "time", return_value=1000.0):
            limiter.check_limits("d")
        with mock.patch.object(self.mod.time, "time", return_value=1000.0 + 301):
            limiter.check_limits("d")
        self.assertEqual(len(limiter.five_minute_calls["d"]), 1)
        self.assertEqual(len(limiter.hourly_calls["d"]), 2)


if __name__ == "__main__":
    unittest.main()
