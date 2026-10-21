#!/usr/bin/env python3
"""Fetching tokens for managed Plex Home users from the "Collect User Plex Tokens" page.

A managed Home profile has no plex.tv login, so the PIN-link flow can only ever sign in
the Home admin. Their token has to come from the admin's token via plex.tv's
switch-user endpoint.
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
from flask import Flask
from plexapi.exceptions import Unauthorized

import routes.user_token_routes as utr

list_users = utr.list_home_users.__wrapped__
add_user = utr.add_home_user_token.__wrapped__


def _user(uid, username, title, restricted='0', protected=False, home=True):
    return SimpleNamespace(id=uid, username=username, title=title, restricted=restricted,
                           protected=protected, home=home)


class FakeAccount:
    def __init__(self, users, switch_result=None, switch_error=None):
        self._users = users
        self._switch_result = switch_result
        self._switch_error = switch_error
        self.switched = []

    def users(self):
        return self._users

    def switchHomeUser(self, user, pin=None):
        self.switched.append((user.id, pin))
        if self._switch_error:
            raise self._switch_error
        return self._switch_result


KIDS = _user(11, '', 'Kids', restricted='1')
TEEN = _user(12, 'teen_login', 'Teen', restricted='0', protected=True)
FRIEND = _user(13, 'friend', 'friend', home=False)


class TestHomeUserTokens(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.store = {}
        patchers = [
            mock.patch.object(utr, 'load_user_tokens', side_effect=lambda: dict(self.store)),
            mock.patch.object(utr, 'save_user_tokens', side_effect=lambda t: self.store.update(t)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _call(self, fn, account=None, body=None, main_error=None):
        with self.app.test_request_context(json=body):
            patcher = (mock.patch.object(utr, '_main_plex_account', side_effect=main_error)
                       if main_error else mock.patch.object(utr, '_main_plex_account', return_value=account))
            with patcher:
                response = fn()
        resp, status = response if isinstance(response, tuple) else (response, 200)
        return json.loads(resp.get_data(as_text=True)), status

    def test_lists_only_home_users_and_flags_managed_and_pin(self):
        self.store['Kids'] = 'tok'
        data, status = self._call(list_users, FakeAccount([KIDS, TEEN, FRIEND]))
        self.assertEqual(status, 200)
        self.assertEqual(data['users'], [
            {'id': 11, 'name': 'Kids', 'managed': True, 'protected': False, 'stored': True},
            {'id': 12, 'name': 'teen_login', 'managed': False, 'protected': True, 'stored': False},
        ])

    def test_no_main_token_is_a_clear_error(self):
        data, status = self._call(list_users, main_error=ValueError('No main Plex token is configured.'))
        self.assertEqual(status, 400)
        self.assertIn('No main Plex token', data['error'])

    def test_managed_user_token_is_stored_under_the_profile_title(self):
        switched = SimpleNamespace(authToken='kids-token', username='', title='Kids')
        account = FakeAccount([KIDS], switch_result=switched)
        data, status = self._call(add_user, account, {'user_id': 11})
        self.assertEqual((status, data), (200, {'success': True, 'username': 'Kids'}))
        self.assertEqual(self.store, {'Kids': 'kids-token'})
        self.assertNotIn('kids-token', json.dumps(data))
        self.assertEqual(account.switched, [(11, None)])

    def test_protected_user_asks_for_the_pin_and_does_not_call_plex(self):
        account = FakeAccount([TEEN])
        data, status = self._call(add_user, account, {'user_id': 12})
        self.assertEqual(status, 400)
        self.assertTrue(data['needs_pin'])
        self.assertEqual(account.switched, [])
        self.assertEqual(self.store, {})

    def test_pin_is_passed_through(self):
        switched = SimpleNamespace(authToken='teen-token', username='teen_login', title='Teen')
        account = FakeAccount([TEEN], switch_result=switched)
        data, status = self._call(add_user, account, {'user_id': 12, 'pin': ' 1234 '})
        self.assertEqual(status, 200)
        self.assertEqual(account.switched, [(12, '1234')])
        self.assertEqual(self.store, {'teen_login': 'teen-token'})

    def test_wrong_pin_stores_nothing(self):
        account = FakeAccount([TEEN], switch_error=Unauthorized('(401) unauthorized'))
        data, status = self._call(add_user, account, {'user_id': 12, 'pin': '0000'})
        self.assertEqual(status, 400)
        self.assertFalse(data['success'])
        self.assertIn('PIN', data['error'])
        self.assertEqual(self.store, {})

    def test_unknown_or_non_home_user_is_rejected(self):
        account = FakeAccount([KIDS, FRIEND])
        for uid in (999, 13):
            data, status = self._call(add_user, account, {'user_id': uid})
            self.assertEqual(status, 404, uid)
        self.assertEqual(account.switched, [])

    def test_missing_user_id(self):
        data, status = self._call(add_user, FakeAccount([KIDS]), {})
        self.assertEqual(status, 400)

    def test_empty_token_from_plex_is_an_error(self):
        account = FakeAccount([KIDS], switch_result=SimpleNamespace(authToken='', username='', title='Kids'))
        data, status = self._call(add_user, account, {'user_id': 11})
        self.assertEqual(status, 502)
        self.assertEqual(self.store, {})

    def test_stored_name_matches_what_the_source_check_accepts(self):
        """The token is keyed by profile title, which is what 'Other Plex Watchlist' compares."""
        from content_checkers.plex_watchlist import plex_token_matches_username
        switched = SimpleNamespace(authToken='kids-token', username='', title='Kids', email='')
        self._call(add_user, FakeAccount([KIDS], switch_result=switched), {'user_id': 11})
        (name,) = self.store.keys()
        self.assertTrue(plex_token_matches_username(switched, name))


class TestMainAccount(unittest.TestCase):
    def test_requires_a_configured_token(self):
        with mock.patch.object(utr, 'get_setting', return_value=''):
            with self.assertRaises(ValueError):
                utr._main_plex_account()


if __name__ == '__main__':
    unittest.main()
