"""Onboarding: step 2 save, resume step, edit endpoints, mount/path checks, admin-only routes."""

import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

# Some other test modules replace flask / utilities.settings in sys.modules and never restore
# them, so these tests (which need the real ones) run in a clean interpreter instead of
# depending on test order.
_ISOLATED_ENV = 'ONBOARDING_TESTS_ISOLATED'
_isolated_results = {}


def _delegated_to_clean_interpreter(testcase):
    """In the parent run, execute this test's class once in a subprocess and assert it passed.

    Returns True when the caller should skip its own body (parent), False inside the subprocess.
    """
    if os.environ.get(_ISOLATED_ENV):
        return False
    cls = type(testcase)
    if cls.__name__ not in _isolated_results:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _isolated_results[cls.__name__] = subprocess.run(
            [sys.executable, '-m', 'pytest', f'{os.path.abspath(__file__)}::{cls.__name__}',
             '-q', '-p', 'no:cacheprovider', '-W', 'ignore'],
            cwd=root, env={**os.environ, _ISOLATED_ENV: '1'}, capture_output=True, text=True, timeout=600)
    proc = _isolated_results[cls.__name__]
    testcase.assertEqual(proc.returncode, 0, (proc.stdout or '')[-4000:] + (proc.stderr or '')[-1000:])
    return True


def _routes():
    import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
    from routes import onboarding_routes
    return onboarding_routes


class _FakeConfig:
    """load_config/save_config/get_setting over one in-memory dict."""

    def __init__(self, config):
        self.config = config

    def load(self):
        import copy
        return copy.deepcopy(self.config)

    def save(self, config):
        self.config = config

    def get(self, section, key=None, default=None):
        value = self.config.get(section, {})
        if key is None:
            return value
        return value.get(key, default)

    def patch(self, module):
        return mock.patch.multiple(module, load_config=self.load, save_config=self.save, get_setting=self.get)


class _Form(dict):
    """Enough of a werkzeug MultiDict for _save_required_settings."""


class TestSaveRequiredSettings(unittest.TestCase):
    def test_symlink_jellyfin_usenet_is_saved_and_unrelated_keys_kept(self):
        if _delegated_to_clean_interpreter(self):
            return
        routes = _routes()
        fake = _FakeConfig({
            'Debrid Provider': {'provider': 'RealDebrid', 'api_key': 'demo_key', 'enable_debrid_naming': True},
            'File Management': {'symlink_organize_by_resolution': True},
            'Plex': {'url': 'http://plex:32400', 'token': 'ptok'},
            'Usenet Provider': {'data_path': '/climount_data'},
            'Debug': {'auto_run_program': False},
        })
        form = _Form({
            'use_debrid': 'off', 'use_usenet': 'on', 'usenet_backend': 'nzbdav',
            'usenet_url': 'http://nzbdav:3000/', 'usenet_api_token': 'sab',
            'file_collection_management': 'Symlinked/Local', 'original_files_path': '/mnt/nzbdav',
            'symlinked_files_path': '/mnt/symlinked', 'symlink_media_server': 'jellyfin',
            'emby_jellyfin_url': 'http://jf:8096/', 'emby_jellyfin_token': 'jtok',
            'tvdb_api_key': 'tv', 'tmdb_api_key': 'tm',
        })
        with fake.patch(routes), mock.patch('usenet.reset_usenet_client'):
            routes._save_required_settings(form)
        c = fake.config
        self.assertEqual(c['Debug']['emby_jellyfin_url'], 'http://jf:8096')
        self.assertEqual(c['Debug']['emby_jellyfin_token'], 'jtok')
        self.assertEqual(c['File Management']['media_server_type'], 'jellyfin')
        self.assertTrue(c['File Management']['symlink_organize_by_resolution'])
        self.assertEqual(c['Usenet Provider'], {'data_path': '/climount_data', 'enabled': True, 'provider': 'nzbdav',
                                                'url': 'http://nzbdav:3000', 'api_token': 'sab'})
        self.assertEqual(c['Debrid Provider']['api_key'], '')  # placeholder cleared, not "configured"
        self.assertTrue(c['Debrid Provider']['enable_debrid_naming'])
        self.assertEqual(c['Plex']['url'], 'http://plex:32400')  # kept for watchlist sources
        self.assertEqual((c['TVDB']['api_key'], c['TMDB']['api_key']), ('tv', 'tm'))

    def test_plex_mode_also_fills_the_symlink_plex_fields(self):
        if _delegated_to_clean_interpreter(self):
            return
        routes = _routes()
        fake = _FakeConfig({})
        form = _Form({'use_debrid': 'on', 'use_usenet': 'off', 'debrid_api_key': 'k',
                      'file_collection_management': 'Plex', 'plex_url': 'http://plex:32400/',
                      'plex_token': 'ptok', 'movie_libraries': 'Movies, 4K Movies'})
        with fake.patch(routes):
            routes._save_required_settings(form)
        fm, plex = fake.config['File Management'], fake.config['Plex']
        self.assertEqual((fm['plex_url_for_symlink'], fm['plex_token_for_symlink']), ('http://plex:32400', 'ptok'))
        self.assertEqual((plex['url'], plex['token'], plex['movie_libraries']),
                         ('http://plex:32400', 'ptok', 'Movies,4K Movies'))

    def test_unknown_usenet_backend_falls_back_to_climount(self):
        if _delegated_to_clean_interpreter(self):
            return
        routes = _routes()
        fake = _FakeConfig({})
        form = _Form({'use_debrid': 'off', 'use_usenet': 'on', 'usenet_backend': 'sabnzbd',
                      'usenet_url': 'http://x:8282', 'file_collection_management': 'Plex'})
        with fake.patch(routes), mock.patch('usenet.reset_usenet_client'):
            routes._save_required_settings(form)
        self.assertEqual(fake.config['Usenet Provider']['provider'], 'climount')


class TestNextStep(unittest.TestCase):
    def _next_step(self, config):
        routes = _routes()
        fake = _FakeConfig(config)
        user = SimpleNamespace(is_default=False)
        with fake.patch(routes), mock.patch.object(routes, 'current_user', user):
            return routes.get_next_onboarding_step()

    def test_placeholder_debrid_key_is_not_a_provider(self):
        if _delegated_to_clean_interpreter(self):
            return
        self.assertEqual(self._next_step({
            'Debrid Provider': {'provider': 'RealDebrid', 'api_key': 'demo_key'},
            'TVDB': {'api_key': 'tv'},
            'Plex': {'url': 'u', 'token': 't', 'movie_libraries': 'Movies'},
        }), 2)

    def test_symlink_jellyfin_user_without_plex_moves_past_step_2(self):
        if _delegated_to_clean_interpreter(self):
            return
        self.assertEqual(self._next_step({
            'Usenet Provider': {'enabled': True, 'url': 'http://x:8282'},
            'TVDB': {'api_key': 'tv'},
            'File Management': {'file_collection_management': 'Symlinked/Local',
                                'symlinked_files_path': '/mnt/symlinked', 'original_files_path': '/mnt/x'},
        }), 3)

    def test_missing_metadata_source_stays_on_step_2(self):
        if _delegated_to_clean_interpreter(self):
            return
        self.assertEqual(self._next_step({
            'Debrid Provider': {'provider': 'RealDebrid', 'api_key': 'real'},
            'Plex': {'url': 'u', 'token': 't', 'movie_libraries': 'Movies'},
        }), 2)


class TestEditEndpoints(unittest.TestCase):
    def test_scraper_update_coerces_types_and_keeps_type_and_enabled(self):
        if _delegated_to_clean_interpreter(self):
            return
        routes = _routes()
        from flask import Flask
        from routes import models
        fake = _FakeConfig({'Scrapers': {'Geek_1': {'type': 'Newznab', 'enabled': False, 'url': 'a',
                                                    'api_key': 'old', 'priority': 0, 'auto_renew': True}}})
        app = Flask(__name__)
        body = {'scraper_id': 'Geek_1', 'config': {'url': 'b', 'api_key': 'new', 'priority': '4',
                                                   'type': 'Torrentio', 'enabled': 'on'}}
        with app.test_request_context('/onboarding/scrapers/update', method='POST', json=body), \
             mock.patch.object(models, 'is_user_system_enabled', return_value=False), fake.patch(routes):
            response = routes.update_onboarding_scraper()
        self.assertEqual(response.get_json(), {'success': True})
        scraper = fake.config['Scrapers']['Geek_1']
        self.assertEqual((scraper['type'], scraper['enabled'], scraper['url'], scraper['api_key'], scraper['priority']),
                         ('Newznab', False, 'b', 'new', 4))
        self.assertFalse(scraper['auto_renew'])  # unticked checkbox is left out of the form

    def test_content_source_update_sets_versions_and_display_name(self):
        if _delegated_to_clean_interpreter(self):
            return
        routes = _routes()
        from flask import Flask
        from routes import models
        fake = _FakeConfig({'Content Sources': {'My Plex Watchlist_1': {
            'type': 'My Plex Watchlist', 'enabled': True, 'versions': ['Default'], 'display_name': 'WL',
            'media_type': 'All'}}})
        app = Flask(__name__)
        body = {'source_id': 'My Plex Watchlist_1',
                'config': {'display_name': 'Mine', 'media_type': 'Movies', 'versions': []}}
        with app.test_request_context('/onboarding/content_sources/update', method='POST', json=body), \
             mock.patch.object(models, 'is_user_system_enabled', return_value=False), fake.patch(routes):
            routes.update_onboarding_content_source()
        source = fake.config['Content Sources']['My Plex Watchlist_1']
        self.assertEqual((source['display_name'], source['media_type'], source['versions'], source['enabled']),
                         ('Mine', 'Movies', [], True))


class TestAdminOnly(unittest.TestCase):
    def test_anonymous_requests_are_refused(self):
        if _delegated_to_clean_interpreter(self):
            return
        routes = _routes()
        from flask import Flask
        from routes import models
        from routes import settings_validation_routes
        app = Flask(__name__)

        @app.route('/unauthorized', endpoint='auth.unauthorized')
        def _unauthorized():
            return 'no'

        anonymous = SimpleNamespace(is_authenticated=False, role=None)
        config = {'Scrapers': {'Geek_1': {'type': 'Newznab', 'api_key': 'SECRET'}}}
        views = [
            ('/onboarding/scrapers/get', 'GET', routes.get_onboarding_scrapers, {}),
            ('/onboarding/health', 'GET', routes.onboarding_health, {}),
            ('/onboarding/test/mount', 'POST', routes.onboarding_test, {'what': 'mount'}),
            ('/settings_validation/onboarding-settings', 'POST',
             settings_validation_routes.validate_onboarding_settings, {}),
        ]
        for path, method, view, kwargs in views:
            with self.subTest(path=path), app.test_request_context(path, method=method, json={}), \
                 mock.patch.object(models, 'is_user_system_enabled', return_value=True), \
                 mock.patch.object(models, 'current_user', anonymous), \
                 mock.patch.object(routes, 'load_config', return_value=config):
                response = view(**kwargs)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(b'SECRET', response.get_data())


class TestPathChecks(unittest.TestCase):
    def test_mount_path_states(self):
        if _delegated_to_clean_interpreter(self):
            return
        _routes()
        from routes.settings_validation_routes import check_mount_path
        root = tempfile.mkdtemp()
        self.assertEqual(check_mount_path(os.path.join(root, 'missing'))[0], False)
        self.assertEqual(check_mount_path(root)[:1] + check_mount_path(root)[2:], (True, True))  # empty: warning
        os.makedirs(os.path.join(root, '__all__', 'Movie (2020)'))
        ok, message, warning = check_mount_path(root)
        self.assertTrue(ok and warning)
        self.assertIn(os.path.join(root, '__all__'), message)
        self.assertEqual(check_mount_path(os.path.join(root, '__all__')), (True, 'Mount is readable (1 items)', False))

    def test_read_only_mount_passes_symlink_check(self):
        if _delegated_to_clean_interpreter(self):
            return
        _routes()
        from routes import settings_validation_routes as v
        mount, links = tempfile.mkdtemp(), tempfile.mkdtemp()
        real_access = os.access
        read_only = lambda path, mode: False if (path == mount and mode == os.W_OK) else real_access(path, mode)
        with mock.patch.object(v.os, 'access', side_effect=read_only):
            self.assertEqual(v.validate_path_exists(mount)[0], False)
            self.assertEqual(v.validate_path_exists(mount, require_writable=False)[0], True)
            self.assertTrue(v.validate_symlink_setup(mount, os.path.join(links, 'out'))[0])


if __name__ == '__main__':
    unittest.main()
