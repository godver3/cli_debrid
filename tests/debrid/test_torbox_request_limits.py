import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def torbox_api(monkeypatch):
    class ProviderUnavailableError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class TorboxAPIError(Exception):
        pass

    class TorboxAuthError(Exception):
        pass

    class RequestException(Exception):
        pass

    fake_http = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            RequestException=RequestException,
            HTTPError=type('HTTPError', (RequestException,), {}),
            Timeout=type('Timeout', (RequestException,), {}),
            ConnectionError=type('ConnectionError', (RequestException,), {}),
        )
    )
    stubs = {
        'tenacity': {
            'retry': lambda **_k: (lambda func: func),
            'retry_if_exception_type': lambda *_a, **_k: None,
            'stop_after_attempt': lambda *_a, **_k: None,
            'wait_exponential': lambda *_a, **_k: None,
        },
        'debrid.base': {
            'ProviderUnavailableError': ProviderUnavailableError,
            'RateLimitError': RateLimitError,
        },
        'debrid.torbox.exceptions': {
            'TorboxAPIError': TorboxAPIError,
            'TorboxAuthError': TorboxAuthError,
        },
        'utilities.settings': {'get_setting': lambda *_a, **_k: None},
        'routes.api_tracker': {'api': fake_http},
    }
    for pkg in ('debrid', 'debrid.torbox', 'utilities', 'routes'):
        module = types.ModuleType(pkg)
        module.__path__ = [str(REPO_ROOT / pkg.replace('.', '/'))]
        monkeypatch.setitem(sys.modules, pkg, module)
    for name, attrs in stubs.items():
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        'debrid.torbox._api_under_test', REPO_ROOT / 'debrid' / 'torbox' / 'api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, ProviderUnavailableError


class _Response:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.content = b''
        self.text = str(self._body)

    def json(self):
        return self._body


def test_create_429_is_sent_once_and_pauses_later_adds(monkeypatch, torbox_api):
    api_module, provider_error = torbox_api
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(429, headers={'Retry-After': '120'})

    monkeypatch.setattr(api_module.api, 'post', post, raising=False)
    monkeypatch.setattr(api_module, '_wait_for_rate_limit', lambda: None)

    with pytest.raises(provider_error, match='add limit reached'):
        api_module.make_request('POST', '/torrents/createtorrent', 'key', data={'magnet': 'x'})
    assert calls == 1
    assert 110 < api_module._create_pause_remaining() <= 120

    with pytest.raises(provider_error, match='adds paused'):
        api_module.make_request('POST', '/torrents/createtorrent', 'key', data={'magnet': 'y'})
    assert calls == 1


def test_create_5xx_is_not_replayed(monkeypatch, torbox_api):
    api_module, provider_error = torbox_api
    post = mock.Mock(return_value=_Response(502))
    monkeypatch.setattr(api_module.api, 'post', post, raising=False)
    monkeypatch.setattr(api_module, '_wait_for_rate_limit', lambda: None)

    with pytest.raises(provider_error, match='not retrying'):
        api_module.make_request('POST', '/torrents/createtorrent', 'key', data={'magnet': 'x'})
    assert post.call_count == 1


def test_create_400_rate_limit_does_not_sleep_or_retry(monkeypatch, torbox_api):
    api_module, provider_error = torbox_api
    post = mock.Mock(return_value=_Response(
        400,
        {'success': False, 'error': 'RATE_LIMIT_EXCEEDED', 'detail': '60 per hour'},
    ))
    monkeypatch.setattr(api_module.api, 'post', post, raising=False)
    monkeypatch.setattr(api_module, '_wait_for_rate_limit', lambda: None)
    sleep = mock.Mock()
    monkeypatch.setattr(time, 'sleep', sleep)

    with pytest.raises(provider_error, match='add limit reached'):
        api_module.make_request('POST', '/torrents/createtorrent', 'key', data={'magnet': 'x'})
    assert post.call_count == 1
    sleep.assert_not_called()


def test_create_sends_allow_zip_false(monkeypatch, torbox_api):
    """TorBox zips 100+ file torrents unless asked not to (magnet and
    .torrent upload alike); other endpoints are left alone."""
    api_module, _ = torbox_api
    post = mock.Mock(return_value=_Response(200, {'success': True, 'data': {'torrent_id': 1}}))
    monkeypatch.setattr(api_module.api, 'post', post, raising=False)
    monkeypatch.setattr(api_module, '_wait_for_rate_limit', lambda: None)

    api_module.make_request('POST', '/torrents/createtorrent', 'key', data={'magnet': 'x'})
    assert post.call_args.kwargs['data'] == {'magnet': 'x', 'allow_zip': 'false'}

    api_module.make_request('POST', '/torrents/createtorrent', 'key', files={'file': ('a.torrent', b'')})
    assert post.call_args.kwargs['data'] == {'allow_zip': 'false'}

    api_module.make_request('POST', '/torrents/controltorrent', 'key', json_data={'operation': 'delete'})
    assert post.call_args.kwargs['data'] is None
