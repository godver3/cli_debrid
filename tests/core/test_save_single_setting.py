"""/settings/save_single_setting — the logo picker's instant save.

static/js/settings.js and settings_tabs/additional.html POST here when a logo
radio changes; the route had been removed, so every pick 404'd silently.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT')


def _fresh_app_modules():
    # Other tests leave stand-ins for flask and project modules in sys.modules
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    return patcher


class SaveSingleSettingTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.settings_routes as sr
        import routes.models as models
        import utilities.settings
        app = Flask(__name__)
        app.register_blueprint(sr.settings_bp, url_prefix='/settings')
        self.client = app.test_client()
        self.set_setting = mock.Mock()
        for patcher in [mock.patch.object(models, 'is_user_system_enabled', return_value=False),
                        mock.patch.object(utilities.settings, 'set_setting', self.set_setting)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, section, key, value):
        return self.client.post('/settings/save_single_setting',
                                data={'section': section, 'key': key, 'value': value})

    def test_saves_a_valid_logo_choice(self):
        from utilities.settings_schema import SETTINGS_SCHEMA
        logo = SETTINGS_SCHEMA['UI Settings']['program_logo']['choices'][-1]
        resp = self._post('UI Settings', 'program_logo', logo)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'status': 'success'})
        self.set_setting.assert_called_once_with('UI Settings', 'program_logo', logo)

    def test_rejects_value_outside_choices(self):
        resp = self._post('UI Settings', 'program_logo', 'Not A Logo')
        self.assertEqual(resp.status_code, 400)
        self.set_setting.assert_not_called()

    def test_rejects_keys_not_in_the_schema(self):
        resp = self._post('Debrid Provider', 'not_a_real_key', 'x')
        self.assertEqual(resp.status_code, 400)
        self.set_setting.assert_not_called()

    def test_coerces_booleans(self):
        resp = self._post('UI Settings', 'hide_support_message', 'true')
        self.assertEqual(resp.status_code, 200)
        self.set_setting.assert_called_once_with('UI Settings', 'hide_support_message', True)


if __name__ == '__main__':
    unittest.main()
