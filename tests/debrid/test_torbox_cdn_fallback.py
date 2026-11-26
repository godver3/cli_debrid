"""A TorBox link on a CDN server that refuses connections is replaced by
one minted for another region (requestdl user_ip)."""

import socket
import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from debrid.torbox import cdn_fallback
from debrid.torbox.client import TorboxProvider

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


if __name__ == '__main__':
    unittest.main()
