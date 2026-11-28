"""Home page top bar: per-provider subscription days and the queue snapshot."""
from unittest import mock

import database  # noqa: F401  (app import order)
from database import statistics as st
from debrid import ProviderUnavailableError


class _Provider:
    def __init__(self, name, result, stream_only=False):
        self.PROVIDER_NAME = name
        self._result = result
        self.stream_only = stream_only

    def get_subscription_status(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _refresh(chain, prev=None):
    st.download_stats_cache['subscription'] = prev
    with mock.patch('debrid.get_debrid_providers', return_value=chain), \
            mock.patch.object(st, 'get_setting', return_value='key'):
        st._refresh_subscription_blocking()
    return st.download_stats_cache['subscription']


def test_every_provider_in_the_chain_is_listed():
    sub = _refresh([
        _Provider('Real-Debrid', {'days_remaining': 30, 'expiration': '2026-12-27T00:00:00', 'premium': True,
                                  'email': 'not@shown'}),
        _Provider('Torbox', {'days_remaining': 5, 'premium': True}),
    ])
    assert sub['days_remaining'] == 30  # top level stays the primary provider
    assert [(e['label'], e['days_remaining']) for e in sub['providers']] == [('RD', 30), ('TB', 5)]
    assert 'email' not in sub['providers'][0]


def test_streaming_only_providers_are_left_out():
    sub = _refresh([_Provider('Real-Debrid', {'days_remaining': 30}),
                    _Provider('Torbox', {'days_remaining': 188}, stream_only=True)])
    assert [e['label'] for e in sub['providers']] == ['RD']


def test_a_failing_provider_keeps_its_last_good_entry():
    good = _refresh([_Provider('Real-Debrid', {'days_remaining': 30}), _Provider('Torbox', {'days_remaining': 9})])
    sub = _refresh([_Provider('Real-Debrid', ProviderUnavailableError('down')),
                    _Provider('Torbox', {'error': 'bad key'})], prev=good)
    assert [e['days_remaining'] for e in sub['providers']] == [30, 9]
    assert sub['days_remaining'] == 30 and sub['error'] is None


def test_first_failure_is_reported():
    sub = _refresh([_Provider('Real-Debrid', RuntimeError('boom'))])
    assert sub['providers'][0]['days_remaining'] is None
    assert sub['error'] == 'boom'


def test_no_debrid_key_gives_empty_list():
    st.download_stats_cache['subscription'] = None
    with mock.patch.object(st, 'get_setting', return_value=''):
        st._refresh_subscription_blocking()
    assert st.download_stats_cache['subscription']['providers'] == []


def test_queue_snapshot_order_and_counts():
    from routes import statistics_routes as sr
    with mock.patch('database.database_reading.get_item_counts_by_states',
                    return_value={'Wanted': 12, 'Scraping': 3, 'Checking': 4}):
        snap = sr._queue_snapshot()
    assert snap == [{'name': 'Wanted', 'count': 12}, {'name': 'Scraping', 'count': 3},
                    {'name': 'Adding', 'count': 0}, {'name': 'Checking', 'count': 4},
                    {'name': 'Sleeping', 'count': 0}]
