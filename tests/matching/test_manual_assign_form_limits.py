"""Confirming a manual assignment for a long show (one form field per episode)
must not hit Flask's default form limits (1,000 parts / 500 KB) and fail with a 413."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.matching.test_manual_assign_page import _fresh_app_modules  # noqa: E402


def _episode_fields(count):
    # The assign page posts each episode as ep_<tmdb>_sXXeYY = <file path>.
    return {f'ep_60572_s01e{n:04d}': (f'Pokemon (1997) Complete Series [BD 1080p] [Group]/Season {n // 100:02d} - Some Arc Name/'
                                    f'[Group] Pokemon - {n:04d} - A Fairly Long Episode Title For This One [BD][Hi10][1080p][FLAC] [ABCDEF12].mkv'.ljust(400, '_'))
            for n in range(1, count + 1)}


class ConfirmFormLimitTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from routes.extensions import app
        import routes.models as models
        for patcher in [mock.patch.object(models, 'is_user_system_enabled', return_value=False)]:
            patcher.start()
            self.addCleanup(patcher.stop)
        from routes.magnet_routes.confirm import confirm_manual_assignment
        self.app = app
        self.view = confirm_manual_assignment

    def _post(self, fields):
        # The page posts new FormData(form): multipart, one part per field.
        with self.app.test_request_context('/magnet/confirm_manual_assignment', method='POST', data=fields,
                                           content_type='multipart/form-data'):
            resp, status = self.view()
            return status, resp.get_json()

    def test_large_pack_form_is_parsed(self):
        # No torrent info: the view answers 400 "Missing essential..." once the
        # form parsed, instead of a 413 from the form limits.
        status, body = self._post(_episode_fields(1300))
        self.assertEqual(status, 400, body)
        self.assertIn('Missing essential torrent information', body['error'])

    def test_other_views_keep_default_limits(self):
        self.assertEqual(self.app.config.get('MAX_FORM_MEMORY_SIZE', 500_000), 500_000)
        self.assertEqual(self.app.config.get('MAX_FORM_PARTS', 1_000), 1_000)


if __name__ == '__main__':
    unittest.main()
