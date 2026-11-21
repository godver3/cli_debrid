"""TorBox mylist answers at most 1000 torrents per call: larger accounts
must be read page by page (the built-in mount removes torrents missing from
the listing, so a cut-off list would pull the rest off the mount)."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401,E402  (app import order)
from debrid.torbox import api as tb  # noqa: E402


def _fake(total, honour_offset=True):
    calls = []

    def make_request(method, path, key, params=None, **kw):
        calls.append(dict(params or {}))
        off = params.get('offset', 0) if honour_offset else 0
        lim = params.get('limit', 1000)
        return {'success': True, 'data': [{'id': i} for i in range(off, min(off + lim, total))]}
    return make_request, calls


def test_reads_every_page():
    mr, calls = _fake(2500)
    with mock.patch.object(tb, 'make_request', mr):
        out = tb.get_all_torrents('k')
    assert [t['id'] for t in out] == list(range(2500))
    assert [c['offset'] for c in calls] == [0, 1000, 2000]


def test_small_account_is_one_call():
    mr, calls = _fake(3)
    with mock.patch.object(tb, 'make_request', mr):
        assert len(tb.get_all_torrents('k')) == 3
    assert len(calls) == 1


def test_offset_ignored_stops_instead_of_looping():
    mr, calls = _fake(1500, honour_offset=False)
    with mock.patch.object(tb, 'make_request', mr):
        out = tb.get_all_torrents('k')
    assert len(out) == 1000 and len(calls) == 2
