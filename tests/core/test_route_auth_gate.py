"""With the user system on, every route needs a login unless it is public.

Routes used to rely on each adding its own decorator, and about 200 had none:
/settings/api/scrapers returned indexer API keys, /api/ai/settings the
OpenClaw token, /settings/notifications/enabled the Discord/SMTP/Telegram
secrets, and the /api/overlays/* API could delete every poster backup, all
without a login. routes.models.require_login now runs before every request and
fails closed; secret-bearing routes and the overlay API also need an admin.
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


class _GateTestCase(unittest.TestCase):
    user_system = True
    user = None  # None = logged out; otherwise a role string
    token_ok = False

    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        import routes.models as models
        self.models = models
        current_user = mock.Mock(is_authenticated=self.user is not None, role=self.user)
        for patcher in [mock.patch.object(models, 'is_user_system_enabled', return_value=self.user_system),
                        mock.patch.object(models, 'current_user', current_user),
                        mock.patch.object(models, '_check_api_token', return_value=self.token_ok)]:
            patcher.start()
            self.addCleanup(patcher.stop)


class RequireLoginTests(_GateTestCase):
    def setUp(self):
        super().setUp()
        from flask import Blueprint, Flask
        app = Flask(__name__)
        app.add_url_rule('/secret', 'secret', lambda: 'secret', methods=['GET', 'POST'])
        app.add_url_rule('/api', 'api_info', lambda: 'bazarr')
        app.add_url_rule('/api/v3/series', 'series', lambda: 'bazarr')
        auth = Blueprint('auth', __name__)
        auth.add_url_rule('/login', 'login', lambda: 'login page')
        auth.add_url_rule('/unauthorized', 'unauthorized', lambda: 'no')
        webhook = Blueprint('webhook', __name__)
        webhook.add_url_rule('/rclone', 'rclone', lambda: 'hook', methods=['POST'])
        app.register_blueprint(auth, url_prefix='/auth')
        app.register_blueprint(webhook, url_prefix='/webhook')
        app.before_request(self.models.require_login)
        self.client = app.test_client()

    def test_api_request_gets_401(self):
        resp = self.client.get('/secret', headers={'Accept': '*/*'})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {'error': 'Authentication required'})

    def test_page_request_redirects_to_login(self):
        resp = self.client.get('/secret', headers={'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/auth/login'))

    def test_post_gets_401_even_from_a_browser(self):
        resp = self.client.post('/secret', headers={'Accept': 'text/html'})
        self.assertEqual(resp.status_code, 401)

    def test_public_routes_stay_open(self):
        for method, path in [('get', '/auth/login'), ('get', '/auth/unauthorized'),
                             ('post', '/webhook/rclone'), ('get', '/api'), ('get', '/api/v3/series')]:
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 200)

    def test_unknown_url_is_still_404(self):
        self.assertEqual(self.client.get('/nope').status_code, 404)


class RequireLoginLoggedInTests(_GateTestCase):
    user = 'user'

    def test_logged_in_user_passes(self):
        from flask import Flask
        app = Flask(__name__)
        app.add_url_rule('/secret', 'secret', lambda: 'secret')
        app.before_request(self.models.require_login)
        self.assertEqual(app.test_client().get('/secret').status_code, 200)


class RequireLoginTokenTests(_GateTestCase):
    token_ok = True

    def test_api_token_passes(self):
        from flask import Flask
        app = Flask(__name__)
        app.add_url_rule('/secret', 'secret', lambda: 'secret')
        app.before_request(self.models.require_login)
        self.assertEqual(app.test_client().get('/secret?token=abc').status_code, 200)


class RequireLoginUserSystemOffTests(_GateTestCase):
    user_system = False

    def test_everything_open_without_user_system(self):
        from flask import Flask
        app = Flask(__name__)
        app.add_url_rule('/secret', 'secret', lambda: 'secret')
        app.before_request(self.models.require_login)
        self.assertEqual(app.test_client().get('/secret').status_code, 200)


class _BlueprintTestCase(_GateTestCase):
    def _client(self):
        from flask import Blueprint, Flask
        import routes.ai_routes as ai
        import routes.overlay_routes as overlays
        import routes.settings_routes as settings
        app = Flask(__name__)
        auth = Blueprint('auth', __name__)
        auth.add_url_rule('/unauthorized', 'unauthorized', lambda: 'no')
        app.register_blueprint(auth, url_prefix='/auth')
        app.register_blueprint(settings.settings_bp, url_prefix='/settings')
        app.register_blueprint(overlays.overlay_page_bp, url_prefix='')
        app.register_blueprint(overlays.overlay_bp, url_prefix='')
        app.register_blueprint(ai.ai_bp, url_prefix='')
        return app.test_client()

    def _denied(self, resp):
        return resp.status_code == 302 and resp.headers['Location'].endswith('/auth/unauthorized')


class NonAdminTests(_BlueprintTestCase):
    user = 'user'

    def test_secret_routes_refuse_non_admins(self):
        client = self._client()
        for method, path in [('get', '/settings/api/scrapers'), ('get', '/settings/scrapers/get'),
                             ('get', '/settings/notifications/enabled'), ('get', '/api/ai/settings'),
                             ('get', '/overlays/badges'), ('get', '/api/overlays/settings'),
                             ('post', '/api/overlays/cleanup/delete-all-backups')]:
            with self.subTest(path=path):
                self.assertTrue(self._denied(getattr(client, method)(path)))

    def test_logo_images_stay_readable(self):
        # Discover shows platform icons from here for every user
        resp = self._client().get('/api/overlays/logos/serve/missing.png')
        self.assertEqual(resp.status_code, 404)

    def test_logo_paths_cannot_leave_the_logo_folders(self):
        client = self._client()
        for path in ['/api/overlays/logos/serve/../../routes/models.py',
                     '/api/overlays/logos/serve/C:/Windows/win.ini']:
            with self.subTest(path=path):
                self.assertIn(client.get(path).status_code, (400, 404))


class AdminTests(_BlueprintTestCase):
    user = 'admin'

    def test_admin_reads_scraper_settings(self):
        import routes.settings_routes as settings
        with mock.patch.object(settings, 'load_config', return_value={'Scrapers': {'Jackett': {'api': 'k'}}}):
            resp = self._client().get('/settings/api/scrapers')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'Jackett': {'api': 'k'}})


class CollectEnabledNotificationsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)

    def test_background_callers_get_the_dict_without_a_request(self):
        import routes.notifications as notifications
        import routes.settings_routes as settings
        config = {'Notifications': {
            'empty': None,
            'discord': {'type': 'Discord', 'enabled': True, 'webhook_url': 'https://x'},
            'no_url': {'type': 'Discord', 'enabled': True, 'webhook_url': ''},
            'off': {'type': 'NTFY', 'enabled': False, 'host': 'h', 'topic': 't'},
        }}
        with mock.patch.object(settings, 'load_config', return_value=config):
            enabled = notifications.get_enabled_notifications()
        self.assertEqual(list(enabled), ['discord'])
        self.assertIn('notify_on', enabled['discord'])
