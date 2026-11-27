"""A fallback provider marked streaming only keeps its existing files playing
(built-in mount, library, repair lookups) but never receives new releases."""

from unittest import mock

import debrid


class _Prov:
    supports_mount_links = True

    def __init__(self, name):
        self.PROVIDER_NAME = name


def _chain(fallbacks):
    settings = {('Debrid Provider', 'fallback_providers'): fallbacks}

    def fake_setting(section, key, default=None):
        return settings.get((section, key), default)

    debrid.reset_provider()
    with mock.patch.object(debrid, 'get_setting', fake_setting), \
            mock.patch.object(debrid, 'get_debrid_provider', return_value=_Prov('Real-Debrid')), \
            mock.patch.object(debrid, '_instantiate_provider', side_effect=lambda name, api_key: _Prov(name)):
        try:
            return debrid.get_debrid_providers(), debrid.get_grab_providers()
        finally:
            debrid.reset_provider()


def test_stream_only_fallback_stays_in_chain_but_not_grabs():
    full, grab = _chain([{'provider': 'Torbox', 'api_key': 'k', 'stream_only': True},
                         {'provider': 'AllDebrid', 'api_key': 'k2'}])
    assert [p.PROVIDER_NAME for p in full] == ['Real-Debrid', 'Torbox', 'AllDebrid']
    assert [p.PROVIDER_NAME for p in grab] == ['Real-Debrid', 'AllDebrid']


def test_fallbacks_without_flag_unchanged():
    full, grab = _chain([{'provider': 'Torbox', 'api_key': 'k'}])
    assert [p.PROVIDER_NAME for p in grab] == [p.PROVIDER_NAME for p in full] == ['Real-Debrid', 'Torbox']


def test_mount_still_serves_stream_only_provider():
    from utilities import mount_debrid
    tb = _Prov('Torbox')
    tb.stream_only = True
    with mock.patch.object(debrid, 'get_debrid_providers', return_value=[_Prov('Real-Debrid'), tb]):
        assert tb in mount_debrid.providers()
