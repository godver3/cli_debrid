"""Offline fault injection: no provider, database or application imports."""
import ast
import importlib.util
import itertools
import json
import logging
import os
import sys
import types
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('health_test_target', ROOT / 'utilities/acquisition_health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


@pytest.fixture(autouse=True)
def reset():
    health._availability.clear()
    yield
    health._availability.clear()


_account_ids = itertools.count()


class Provider:
    PROVIDER_NAME = 'ExampleDebrid'
    def __init__(self, ok=True, subscription=None, api_key=None):
        # Each instance is its own account unless given the same key, as in production.
        self.api_key = api_key or f'example-account-{next(_account_ids)}'
        self.ok = ok
        self.subscription = subscription or {'premium': True, 'days_remaining': 20}
        self.calls = 0
    def check_connectivity(self):
        self.calls += 1
        return self.ok, {'type': 'CONNECTION_ERROR'}
    def get_subscription_status(self):
        return self.subscription


def settings(debrid=True, nzb=True, url='http://local.test', fallbacks=None, shared=None):
    values = {('Debrid Provider', 'provider'): 'ExampleDebrid' if debrid else '',
              ('Debrid Provider', 'api_key'): 'example-test-key' if debrid else '',
              ('Debrid Provider', 'fallback_providers'): fallbacks or [],
              ('Usenet Provider', 'enabled'): nzb, ('Usenet Provider', 'url'): url,
              ('File Management', 'file_collection_management'): 'none'}
    values.update(shared or {})
    return lambda section, key, default=None: values.get((section, key), default)


def check(providers, nzb=True, debrid=True, enabled=True, **kwargs):
    client = types.SimpleNamespace(check_connectivity=lambda: (nzb, None))
    return health.check_acquisition_providers(settings(debrid=debrid, nzb=enabled, **kwargs),
                                              lambda: providers, lambda: client)


@pytest.mark.parametrize('rd,nzb,expected', [(True, True, True), (False, True, True),
                                          (True, False, True), (False, False, False)])
def test_backend_matrix(rd, nzb, expected):
    p = Provider(rd)
    ok, failures = check([p], nzb=nzb)
    assert ok is expected
    assert bool(failures) is (not expected)
    assert health.provider_available(p) is rd
    assert health.usenet_available() is nzb


def test_fallback_success_and_separate_accounts():
    primary, fallback = Provider(False), Provider(True)
    assert check([primary, fallback], nzb=False)[0]
    assert not health.provider_available(primary)
    assert health.provider_available(fallback)
    assert primary.calls == fallback.calls == 1


def test_fallback_only_configuration():
    assert check([Provider()], debrid=False, enabled=False,
                 fallbacks=[{'provider': 'ExampleDebrid', 'api_key': 'example'}])[0]


@pytest.mark.parametrize('enabled,providers,expected', [(True, [], True), (False, [Provider()], True),
                                                     (False, [], False)])
def test_single_and_no_provider(enabled, providers, expected):
    assert check(providers, debrid=bool(providers), enabled=enabled)[0] is expected


def test_enabled_usenet_without_url_is_not_healthy():
    assert not check([], debrid=False, url='')[0]


@pytest.mark.parametrize('info', [None, {}, b'<html>blocked</html>', [], {'error': 'unavailable'},
                                {'premium': False, 'days_remaining': None}, {'premium': None},
                                {'premium': True, 'expiration': 'invalid'},
                                {'premium': True, 'days_remaining': float('nan')}])
def test_unknown_is_not_expired(info):
    assert health.subscription_failure(info) == 'SUBSCRIPTION_UNKNOWN'


def test_confirmed_expiry_and_partial_day():
    now = datetime.now(timezone.utc)
    assert health.subscription_failure({'premium': False, 'days_remaining': 0}) == 'SUBSCRIPTION_EXPIRED'
    assert health.subscription_failure({'premium': True, 'days_remaining': 0,
        'expiration': (now + timedelta(hours=1)).isoformat()}, now) is None
    assert health.subscription_failure({'premium': True,
        'expiration': (now - timedelta(seconds=1)).isoformat()}, now) == 'SUBSCRIPTION_EXPIRED'


def test_expired_or_unknown_debrid_does_not_pause_nzb(caplog):
    for info in [{'premium': False, 'days_remaining': 0}, {'premium': None}]:
        p = Provider(subscription=info)
        assert check([p])[0]
        assert not health.provider_available(p)
    assert 'SUBSCRIPTION_EXPIRED' in caplog.text
    assert 'SUBSCRIPTION_UNKNOWN' in caplog.text


def test_candidates_preserved_and_restored_after_recovery():
    p = Provider(False)
    results = [{'magnet': 'example'}, {'protocol': 'nzb', 'title': 'example'}]
    check([p])
    assert health.usable_results(results, [p]) == results[1:]
    assert len(results) == 2
    p.ok = True
    check([p])
    assert health.usable_results(results, [p]) == results


def test_failed_factory_does_not_reuse_stale_health():
    p = Provider()
    check([p])
    def broken():
        raise RuntimeError('private details must not escape')
    ok, failures = health.check_acquisition_providers(settings(nzb=False), broken, lambda: None)
    assert not ok and not health.provider_available(p)
    assert 'private details' not in str(failures)


def test_missing_factory_response_fails_closed():
    ok, failures = health.check_acquisition_providers(settings(nzb=False), lambda: None, lambda: None)
    assert not ok
    assert failures[0]['type'] == 'CONFIG_ERROR'


def test_upstream_candidate_retirement_preserves_unavailable_predecessor(monkeypatch):
    """Current dev already owns exact retirement; do not replace that fix."""
    tree = ast.parse((ROOT / 'queues/torrent_processor.py').read_text())
    guard = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and
                 isinstance(n.test, ast.Name) and n.test.id == 'item' and
                 any(isinstance(child, ast.Name) and child.id == '_drop'
                     for child in ast.walk(n)))
    writes = []
    module = types.ModuleType('database.database_writing')
    module.update_media_item = lambda *args, **kwargs: writes.append(kwargs)
    monkeypatch.setitem(sys.modules, 'database.database_writing', module)
    unavailable = {'magnet': 'example', 'title': 'torrent'}
    nzb = {'protocol': 'nzb', 'nzb_url': 'example', 'title': 'nzb'}
    namespace = {'item': {'id': 1, 'scrape_results': json.dumps([unavailable, nzb])},
                 'result': dict(nzb, original_scraped_torrent_title='nzb')}
    compiled = compile(ast.Module(body=[guard], type_ignores=[]), 'CandidateRetirement', 'exec')
    exec(compiled, namespace)
    assert namespace['item']['scrape_results'] == [unavailable]
    assert json.loads(writes[0]['scrape_results']) == [unavailable]
    exec(compiled, namespace)
    assert namespace['item']['scrape_results'] == [unavailable]
    assert len(writes) == 1


def function(path, name, namespace, class_name=None):
    tree = ast.parse((ROOT / path).read_text())
    parent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name) if class_name else tree
    node = next(n for n in parent.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


def test_actual_route_keeps_shared_failure(monkeypatch):
    module = types.ModuleType('utilities.acquisition_health')
    module.check_acquisition_providers = lambda _: (True, [])
    monkeypatch.setitem(sys.modules, 'utilities.acquisition_health', module)
    class RequestException(Exception):
        pass
    def fail(*args, **kwargs):
        raise RequestException('local shared failure')
    shared = {('File Management', 'file_collection_management'): 'Plex',
              ('Plex', 'url'): 'http://local.test', ('Plex', 'token'): 'example'}
    route = function('routes/program_operation_routes.py', 'check_service_connectivity',
                     {'get_setting': settings(shared=shared), 'logging': logging,
                      'api': types.SimpleNamespace(get=fail), 'RequestException': RequestException,
                      'ET': ET, 'os': os})
    ok, failures = route()
    assert not ok and failures[0]['service'] == 'Plex'


@pytest.mark.parametrize('response', [b'<html>blocked</html>', None, {}, {'error': 'blocked'}])
def test_actual_rd_rejects_malformed_response(response, monkeypatch):
    class ProviderUnavailableError(Exception):
        pass
    class AuthError(Exception):
        pass
    module = types.ModuleType('utilities.settings')
    module.get_setting = lambda *args: 'example-test-key'
    monkeypatch.setitem(sys.modules, 'utilities.settings', module)
    namespace = {'Dict': dict, 'Any': object, 'Tuple': tuple, 'Optional': Optional,
                 'make_request': lambda *args: response, 'ProviderUnavailableError': ProviderUnavailableError,
                 'RealDebridAuthError': AuthError, 'RealDebridAPIError': RuntimeError,
                 'logging': logging, 'datetime': datetime}
    client = types.SimpleNamespace(api_key='example-test-key')
    connectivity = function('debrid/real_debrid/client.py', 'check_connectivity', namespace, 'RealDebridProvider')
    subscription = function('debrid/real_debrid/client.py', 'get_subscription_status', namespace, 'RealDebridProvider')
    assert connectivity(client)[0] is False
    status = subscription(client)
    assert status['premium'] is None
    assert health.subscription_failure(status) == 'SUBSCRIPTION_UNKNOWN'


def test_actual_checking_loop_defers_offline_owner(monkeypatch):
    primary, fallback = Provider(False), Provider(True)
    check([primary, fallback])
    monkeypatch.setitem(sys.modules, 'utilities.acquisition_health', health)
    tree = ast.parse((ROOT / 'queues/checking_queue.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CheckingQueue')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'process')
    loop = next(n for n in method.body if isinstance(n, ast.For) and
                isinstance(n.target, ast.Tuple) and n.target.elts[0].id == 'torrent_id')
    # Exercise the actual preflight and continue before any progress/destructive
    # logic. Healthy jobs must pass the guard; their full lifecycle is separate.
    guarded = []
    for node in loop.body:
        if isinstance(node, ast.Try):
            break
        guarded.append(node)
    loop.body = guarded + [ast.parse('seen.append(torrent_id)').body[0]]
    namespace = {'items_by_torrent_id_to_process': {'offline': [{'id': 1}],
                                                  'healthy': [{'id': 2}], 'nzb:job': [{'id': 3}]},
                 'self': types.SimpleNamespace(_provider_for_torrent=lambda tid: primary if tid == 'offline' else fallback,
                                               progress_checks={'offline': {'last_check': 0}}),
                 'current_time': 100, 'logging': logging, 'seen': []}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])), 'CheckingGuard', 'exec'), namespace)
    assert namespace['seen'] == ['healthy', 'nzb:job']
    assert namespace['self'].progress_checks['offline']['last_check'] == 100


def test_actual_torrent_processor_filters_known_offline_chain(monkeypatch):
    primary, fallback = Provider(False), Provider(True)
    check([primary, fallback])
    monkeypatch.setitem(sys.modules, 'utilities.acquisition_health', health)
    providers = function('queues/torrent_processor.py', '_providers',
                         {'get_debrid_providers': lambda: [primary, fallback]}, 'TorrentProcessor')
    assert providers(None) == [fallback]


def test_new_instance_of_same_account_keeps_its_verdict():
    """reset_provider() builds new instances on every settings save; a queue still
    holding the old one must not see a healthy account as unavailable."""
    startup = Provider(api_key='same-account')
    assert check([startup], nzb=False)[0]
    after_save = Provider(api_key='same-account')
    assert check([after_save], nzb=False)[0]
    assert health.provider_available(startup)


def test_positive_days_are_usable_even_when_premium_flag_is_false():
    # TorBox derives premium from is_subscribed; an account with time left is usable.
    assert health.subscription_failure({'premium': False, 'days_remaining': 12}) is None
    assert health.subscription_failure({'premium': False, 'days_remaining': 12,
                                        'expiration': '2999-01-01T00:00:00Z'}) is None


def test_failure_keeps_the_real_reason():
    bad_key = Provider(False)
    bad_key.check_connectivity = lambda: (False, {'type': 'AUTH_ERROR', 'status_code': 401,
                                                  'message': 'Invalid API key'})
    ok, failures = check([bad_key], nzb=False)
    assert not ok
    assert failures[0]['type'] == 'AUTH_ERROR'
    assert failures[0]['status_code'] == 401
    assert 'Invalid API key' in failures[0]['message']


def test_deferred_item_logs_info_once_per_outage(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    check([Provider(False)], nzb=True)
    health.log_deferred('Dune (2021)', 'backend down')
    health.log_deferred('Dune (2021)', 'backend down')
    infos = [r for r in caplog.records if r.levelno == logging.INFO and 'Dune (2021)' in r.getMessage()]
    assert len(infos) == 1
    check([Provider(True)], nzb=True)  # health changed: next outage logs again
    health.log_deferred('Dune (2021)', 'backend down')
    infos = [r for r in caplog.records if r.levelno == logging.INFO and 'Dune (2021)' in r.getMessage()]
    assert len(infos) == 2
