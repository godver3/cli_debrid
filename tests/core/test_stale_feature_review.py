"""Stale-feature review (2026-11-23): Content Requestor and user management hardening.

- A requested folder is joined into the symlink path; '..' or a path put links outside
  the library (any user role can send it), so only a single folder name is used.
- /content/request only takes configured versions, and at least one.
- Changing your own password needs the current one; add_user only takes known roles;
  an admin can't delete the account they are signed in with.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT', 'flask_login')


def _fresh_app_modules():
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    return patcher


class SingleFolderNameTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from utilities.local_library_scan import is_single_folder_name
        self.ok = is_single_folder_name

    def test_normal_names_pass(self):
        for name in ('Movies', 'TV Shows', 'Kids Movies', '4K [HDR]', '.hidden'):
            self.assertTrue(self.ok(name), name)

    def test_escapes_are_rejected(self):
        for name in ('..', '.', '...', ' .. ', '../config', 'a/b', 'a\\b', '/etc', '', '   ', None, 5):
            self.assertFalse(self.ok(name), repr(name))


class ContentRequestVersionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.content_requestor_routes as cr
        import routes.models as models
        app = Flask(__name__)
        app.register_blueprint(cr.content_requestor_bp, url_prefix='/content')
        self.client = app.test_client()
        self.add = mock.Mock(return_value=1)
        for p in (
            mock.patch.object(cr, 'is_user_system_enabled', return_value=False),
            mock.patch.object(models, 'is_user_system_enabled', return_value=False),
            mock.patch.object(cr, 'load_config', return_value={'Scraping': {'versions': {'1080p': {}, '4K': {}}}}),
            mock.patch.object(cr.DirectAPI, 'tmdb_to_imdb', return_value=('tt0111161', 'test')),
            mock.patch.object(cr, 'add_wanted_items', self.add),
            mock.patch('metadata.metadata.process_metadata', return_value={'movies': [{'imdb_id': 'tt0111161'}]}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _request(self, versions):
        return self.client.post('/content/request', json={'id': 278, 'mediaType': 'movie', 'title': 'X', 'versions': versions})

    def test_configured_version_is_added(self):
        response = self._request(['4K'])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.add.call_args[0][1], {'4K': True})

    def test_unknown_version_is_refused(self):
        response = self._request(['Default'])
        self.assertEqual(response.status_code, 400)
        self.assertIn('Unknown version', response.get_json()['error'])
        self.add.assert_not_called()

    def test_no_version_is_refused(self):
        for versions in ([], None, 'all'):
            response = self._request(versions)
            self.assertEqual(response.status_code, 400, versions)
        self.add.assert_not_called()


class UserManagementTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        from werkzeug.security import generate_password_hash, check_password_hash
        import routes.user_management_routes as um
        self.um, self.check = um, check_password_hash
        self.app = Flask(__name__)
        self.user = SimpleNamespace(id=1, role='admin', oidc_sub=None, is_default=False, is_authenticated=True,
                                    password=generate_password_hash('old-pass'))
        self.db = mock.Mock()
        for p in (
            mock.patch.object(um, 'current_user', self.user),
            mock.patch.object(um, 'db', self.db),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _change(self, body):
        with self.app.test_request_context(json=body):
            result = self.um.change_own_password.__wrapped__()
        response, status = result if isinstance(result, tuple) else (result, 200)
        return response.get_json(), status

    def test_wrong_current_password_is_refused(self):
        data, status = self._change({'current_password': 'nope', 'new_password': 'n', 'confirm_password': 'n'})
        self.assertEqual(status, 400)
        self.assertIn('Current password', data['error'])
        self.assertTrue(self.check(self.user.password, 'old-pass'))

    def test_right_current_password_changes_it(self):
        data, status = self._change({'current_password': 'old-pass', 'new_password': 'n3w', 'confirm_password': 'n3w'})
        self.assertEqual(status, 200, data)
        self.assertTrue(self.check(self.user.password, 'n3w'))

    def test_unknown_role_is_refused(self):
        with self.app.test_request_context(method='POST', data={'username': 'bob', 'password': 'p', 'role': 'superadmin'}):
            response, status = self.um.add_user.__wrapped__()
        self.assertEqual(status, 400)
        self.db.session.add.assert_not_called()

    def test_admin_cannot_delete_own_account(self):
        user_model = mock.Mock()
        user_model.query.get.return_value = self.user
        user_model.query.filter_by.return_value.count.return_value = 2
        with mock.patch.object(self.um, 'User', user_model), self.app.test_request_context(method='POST'):
            response, status = self.um.delete_user.__wrapped__(1)
        self.assertEqual(status, 400)
        self.db.session.delete.assert_not_called()

    def test_dead_change_password_route_is_gone(self):
        rules = {r.rule for r in self._app_with_blueprint().url_map.iter_rules()}
        self.assertNotIn('/user_management/change_password', rules)
        self.assertIn('/user_management/change_own_password', rules)

    def _app_with_blueprint(self):
        from flask import Flask
        app = Flask('bp')
        app.register_blueprint(self.um.user_management_bp, url_prefix='/user_management')
        return app


if __name__ == '__main__':
    unittest.main()
