#!/usr/bin/env python3
"""Other Plex Watchlist for managed Plex Home users (godver3/cli_debrid#518).

Managed Home users have no plex.tv username, so MyPlexAccount(token).username
is "". Their profile title (configured as the source's username) must be
accepted both when fetching the watchlist and on the Connections page.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
import content_checkers.plex_watchlist as pw
import routes.connections_routes as cr


def _account(username, title):
    return SimpleNamespace(username=username, title=title, email='', watchlist=lambda: [])


class TestWatchlistTokenOwner(unittest.TestCase):
    def _fetch(self, account, username):
        with mock.patch.object(pw, 'MyPlexAccount', return_value=account), \
             mock.patch.object(pw.logging, 'error') as err:
            pw.get_wanted_from_other_plex_watchlist(username, 'tok', {'Default': True})
        return [c.args[0] for c in err.call_args_list if 'seems to belong to' in c.args[0]]

    def test_managed_user_matches_profile_title(self):
        self.assertEqual(self._fetch(_account('', 'Kids'), 'Kids'), [])

    def test_regular_account_still_compares_username(self):
        self.assertEqual(self._fetch(_account('realuser', 'Real User'), 'realuser'), [])
        self.assertTrue(self._fetch(_account('realuser', 'Kids'), 'Kids'))


class TestConnectionsPageTokenOwner(unittest.TestCase):
    def _check(self, account, username):
        cfg = {'enabled': True, 'username': username, 'token': 'tok'}
        with mock.patch.object(cr, 'MyPlexAccount', return_value=account):
            return cr.check_content_source_connection('Other Plex Watchlist_1', cfg)

    def test_managed_user_connected(self):
        res = self._check(_account('', 'Kids'), 'Kids')
        self.assertTrue(res['connected'], res['error'])
        self.assertEqual(res['details']['username'], 'Kids')

    def test_mismatch_still_reported(self):
        res = self._check(_account('someoneelse', 'Other'), 'Kids')
        self.assertFalse(res['connected'])
        self.assertIn('Got: someoneelse', res['error'])


if __name__ == '__main__':
    unittest.main()
