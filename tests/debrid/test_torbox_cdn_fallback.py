"""A TorBox link on a CDN server that refuses connections is replaced by
one minted for another region (requestdl user_ip)."""

import socket
import time
import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from debrid.torbox import api as torbox_api, cdn_fallback
from debrid.torbox.client import TorboxProvider
from utilities import mount_debrid

DOWN = 'https://nexus-409.ecan.tb-cdn.io/dld/x?token=t'
UP = 'https://nexus-349.wnam.tb-cdn.io/dld/x?token=t'


class ReachableTest(unittest.TestCase):
    def setUp(self):
        cdn_fallback._state.clear()

    def test_refused_is_down_and_remembered(self):
        with mock.patch.object(cdn_fallback.socket, 'create_connection', side_effect=ConnectionRefusedError) as c:
            self.assertFalse(cdn_fallback.reachable(DOWN))
            self.assertFalse(cdn_fallback.reachable(DOWN))
        self.assertEqual(c.call_count, 1, 'the state is cached')

    def test_reported_refusal_overrides_a_recent_success(self):
        with mock.patch.object(cdn_fallback.socket, 'create_connection') as connect:
            self.assertTrue(cdn_fallback.reachable(DOWN))
            cdn_fallback.mark_down(DOWN)
            self.assertFalse(cdn_fallback.reachable(DOWN))
        connect.assert_called_once()

    def test_probe_cannot_overwrite_a_refusal_reported_while_it_runs(self):
        def probe(*args, **kwargs):
            cdn_fallback.mark_down(DOWN)
            return mock.Mock()

        with mock.patch.object(cdn_fallback.socket, 'create_connection', side_effect=probe):
            self.assertFalse(cdn_fallback.reachable(DOWN))
            self.assertFalse(cdn_fallback.reachable(DOWN))

    def test_dns_failure_is_not_blamed_on_the_server(self):
        with mock.patch.object(cdn_fallback.socket, 'create_connection', side_effect=socket.gaierror):
            self.assertTrue(cdn_fallback.reachable(DOWN))


class MountLinkFallbackTest(unittest.TestCase):
    def setUp(self):
        self.p = TorboxProvider.__new__(TorboxProvider)

    def test_switches_region_when_the_server_is_down(self):
        asked = []

        def requestdl(tid, fid, user_ip=None):
            asked.append(user_ip)
            return UP if user_ip else DOWN
        with mock.patch.object(self.p, '_requestdl', side_effect=requestdl), \
                mock.patch.object(cdn_fallback, 'reachable', side_effect=lambda u: u == UP):
            url, _ = self.p.mount_link('1', '0')
        self.assertEqual(url, UP)
        self.assertEqual(asked, [None, cdn_fallback.REGION_IPS[0][1]])

    def test_a_reachable_link_is_used_as_is(self):
        with mock.patch.object(self.p, '_requestdl', return_value=UP) as r, \
                mock.patch.object(cdn_fallback, 'reachable', return_value=True):
            self.assertEqual(self.p.mount_link('1', '0')[0], UP)
        r.assert_called_once()

    def test_no_region_works_keeps_the_first_link(self):
        with mock.patch.object(self.p, '_requestdl', return_value=DOWN), \
                mock.patch.object(cdn_fallback, 'reachable', return_value=False):
            self.assertEqual(self.p.mount_link('1', '0')[0], DOWN)


    def test_request_timeout_leaves_time_for_later_regions(self):
        reply = mock.Mock()
        reply.status_code = 200
        reply.json.return_value = {'data': UP}
        timeout = torbox_api.api.exceptions.Timeout
        with mock.patch.object(self.p, '_load_api_key', return_value='key'), \
                mock.patch.object(torbox_api, '_wait_for_rate_limit') as throttle, \
                mock.patch.object(torbox_api.api, 'get', side_effect=[timeout, torbox_api.api.exceptions.ConnectionError, reply]) as get, \
                mock.patch.object(cdn_fallback, 'reachable', return_value=True):
            self.assertEqual(self.p.mount_link('1', '0')[0], UP)

        self.assertEqual([c.kwargs['params'].get('user_ip') for c in get.call_args_list],
                         [None, cdn_fallback.REGION_IPS[0][1], cdn_fallback.REGION_IPS[1][1]])
        for call in get.call_args_list:
            self.assertGreater(call.kwargs['timeout'].total, 0)
            self.assertLessEqual(call.kwargs['timeout'].total, 3)
        self.assertEqual(throttle.call_count, 3)


class UnreachableLinkTest(unittest.TestCase):
    def test_unreachable_refresh_replaces_recent_link_and_remembers_its_host(self):
        p = TorboxProvider.__new__(TorboxProvider)
        ref = 'tb:1:0'
        now = time.time()
        links = {ref: {'url': DOWN, 'minted': now, 'expires': now + 3600}}
        with mock.patch.object(mount_debrid, '_links', links), \
                mock.patch.object(mount_debrid, '_save_links'), \
                mock.patch.object(mount_debrid, '_link_lifetime', return_value=0), \
                mock.patch.object(mount_debrid, 'providers', return_value=[p]), \
                mock.patch.object(p, '_requestdl', side_effect=[DOWN, UP]) as requestdl, \
                mock.patch.object(cdn_fallback, '_state', {}), \
                mock.patch.object(cdn_fallback.socket, 'create_connection') as connect:
            self.assertTrue(cdn_fallback.reachable(DOWN))
            self.assertEqual(mount_debrid.link(ref, refresh=True)[0], DOWN)
            requestdl.assert_not_called()

            self.assertEqual(mount_debrid.link(ref, refresh=True, unreachable_url=DOWN)[0], UP)
            self.assertEqual(mount_debrid.link(ref, refresh=True, unreachable_url=DOWN)[0], UP)
            self.assertEqual(mount_debrid.link(ref)[0], UP)
            self.assertFalse(cdn_fallback.reachable(DOWN))
            self.assertEqual(requestdl.call_args_list, [
                mock.call('1', '0', user_ip=None),
                mock.call('1', '0', user_ip=cdn_fallback.REGION_IPS[0][1]),
            ])
            self.assertEqual(connect.call_count, 2)


if __name__ == '__main__':
    unittest.main()
