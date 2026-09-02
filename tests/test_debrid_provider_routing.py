#!/usr/bin/env python3
"""Regression test: the Checking queue must poll the provider that actually
holds a torrent, not whichever provider happens to be primary.

Reported symptom -- one episode grabbed over and over, a different release each
time, filling the debrid account and the library with duplicates. In the debug
log every single one of 833 "NOT FOUND (404)" warnings was a numeric AllDebrid
torrent id being requested from api.real-debrid.com:

    add_torrent -> torrent_id: 725087006          (AllDebrid, v4.1)
    get_torrent_info -> v4.1 status response: OK  (AllDebrid, fine)
    get_torrent_progress -> 404 api.real-debrid.com/.../torrents/info/725087006

With a fallback chain configured, TorrentProcessor races the cache check across
every provider and adds the torrent to whichever answers 'cached' first, but
CheckingQueue held get_debrid_provider() -- always the primary. A torrent id is
only meaningful to the provider that issued it, so the very first poll 404s,
get_torrent_progress reports PROGRESS_RESULT_MISSING, and handle_missing_torrent
blacklists the magnet and returns the item to Wanted. It rescrapes, grabs the
next release, and repeats: one release burned per ~35s cycle.

The fix records the winning provider on the item (media_items.debrid_provider)
and resolves per torrent in the Checking queue. Nothing in this repo's test
environment can import queues.checking_queue (plexapi) or debrid (see the other
test modules for the same constraint), so the wiring is asserted against the
source with ast, and the routing decision itself is exercised as a simulation.
"""

import ast
import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(relpath):
    with open(os.path.join(PROJECT_ROOT, relpath), encoding='utf-8') as f:
        return ast.parse(f.read()), f


def _source(relpath):
    with open(os.path.join(PROJECT_ROOT, relpath), encoding='utf-8') as f:
        return f.read()


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


class TestCheckingQueueResolvesPerTorrent(unittest.TestCase):
    """The decisive call: the poll that produced every 404 in the log."""

    def setUp(self):
        self.tree, _ = _parse('queues/checking_queue.py')

    def test_get_torrent_progress_does_not_use_the_primary_provider(self):
        fn = _find_function(self.tree, 'get_torrent_progress')
        src = ast.dump(fn)
        self.assertNotIn("attr='debrid_provider'", src,
                         'get_torrent_progress must not reach for self.debrid_provider '
                         'directly -- that is the primary, and the torrent may be on a '
                         'fallback')

    def test_get_torrent_progress_resolves_the_provider_for_the_torrent(self):
        fn = _find_function(self.tree, 'get_torrent_progress')
        called = {
            n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertIn('_provider_for_torrent', called)

    def test_every_torrent_scoped_provider_call_is_resolved(self):
        """remove_torrent takes a torrent id too, so it has the same failure mode:
        asking the wrong provider to delete an id it has never seen."""
        offenders = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # self.debrid_provider.<method>(...)
            if (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == 'debrid_provider'
                    and isinstance(fn.value.value, ast.Name)
                    and fn.value.value.id == 'self'):
                offenders.append((fn.attr, node.lineno))

        torrent_scoped = [o for o in offenders if o[0] in
                          ('get_torrent_info_with_status', 'get_torrent_info', 'remove_torrent')]
        self.assertEqual(
            torrent_scoped, [],
            f'these take a torrent id and must go through _provider_for_torrent: {torrent_scoped}')

    def test_resolver_and_helpers_exist(self):
        for name in ('_provider_for_torrent', '_remember_provider',
                     '_rediscover_provider', '_looks_like_not_found'):
            _find_function(self.tree, name)

    def test_resolver_falls_back_to_the_primary(self):
        """An item written before the column existed has no provider recorded.
        That must degrade to the old behaviour, not crash the queue."""
        fn = _find_function(self.tree, '_provider_for_torrent')
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        self.assertTrue(
            any(isinstance(r.value, ast.Name) and r.value.id == 'default' for r in returns),
            'expected a `return default` fallback path')


class TestProviderIsRecordedOnTheItem(unittest.TestCase):
    """Resolution is only possible if the add path wrote the provider down."""

    def test_process_results_stamps_the_winning_provider(self):
        src = _source('queues/torrent_processor.py')
        self.assertIn("torrent_info['_provider'] = self.debrid_provider.PROVIDER_NAME", src)

    def test_adding_queue_passes_it_to_both_move_to_checking_calls(self):
        tree, _ = _parse('queues/adding_queue.py')
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == 'move_to_checking']
        self.assertEqual(len(calls), 2, 'expected the primary and related-item calls')
        for call in calls:
            kwargs = {k.arg for k in call.keywords}
            self.assertIn('debrid_provider', kwargs,
                          f'move_to_checking at line {call.lineno} does not pass the provider; '
                          f'a related episode on the same torrent would poll the primary and 404')

    def test_move_to_checking_accepts_it(self):
        tree, _ = _parse('queues/queue_manager.py')
        fn = _find_function(tree, 'move_to_checking')
        args = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        self.assertIn('debrid_provider', args)

    def test_move_to_checking_does_not_clobber_a_known_provider_with_none(self):
        """update_media_item_state writes any field present in kwargs, so an
        unconditional pass-through would null the column on a later re-entry."""
        tree, _ = _parse('queues/queue_manager.py')
        fn = _find_function(tree, 'move_to_checking')
        guarded = any(
            isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == 'debrid_provider'
            for n in ast.walk(fn)
        )
        self.assertTrue(guarded, 'debrid_provider must only be forwarded when truthy')

    def test_update_media_item_state_persists_the_column(self):
        src = _source('database/database_writing.py')
        fn = _find_function(ast.parse(src), 'update_media_item_state')
        fields = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant):
                        fields.append(elt.value)
        self.assertIn('debrid_provider', fields,
                      'the column is not in optional_fields, so the value never reaches the DB')


class TestSchema(unittest.TestCase):
    def test_column_is_created_and_migrated(self):
        src = _source('database/schema_management.py')
        self.assertIn('debrid_provider TEXT', src, 'missing from CREATE TABLE')
        self.assertIn(
            "ALTER TABLE media_items ADD COLUMN debrid_provider TEXT", src,
            'missing the migration -- existing installs would never get the column')
        self.assertIn("if 'debrid_provider' not in columns:", src,
                      'migration must be guarded, it runs on every start')


class TestProviderResolutionSemantics(unittest.TestCase):
    """The routing decision itself, as a simulation -- debrid/__init__ cannot be
    imported here, so get_provider_by_name's matching rules are mirrored."""

    NAME_MAP = {
        'realdebrid': 'Real-Debrid',
        'alldebrid': 'AllDebrid',
        'torbox': 'Torbox',
        'premiumize': 'Premiumize',
        'debridlink': 'Debrid-Link',
        'debrid-link': 'Debrid-Link',
    }

    class FakeProvider:
        def __init__(self, name):
            self.PROVIDER_NAME = name

        def __repr__(self):
            return f'<{self.PROVIDER_NAME}>'

    def setUp(self):
        self.rd = self.FakeProvider('Real-Debrid')
        self.ad = self.FakeProvider('AllDebrid')
        self.chain = [self.rd, self.ad]

    def _by_name(self, name):
        if not name or not name.strip():
            return None
        wanted = name.strip().lower()
        for p in self.chain:
            if p.PROVIDER_NAME.strip().lower() == wanted:
                return p
        mapped = self.NAME_MAP.get(wanted)
        if mapped:
            for p in self.chain:
                if p.PROVIDER_NAME.strip().lower() == mapped.strip().lower():
                    return p
        return None

    def _resolve(self, torrent_id, memo, items, default):
        """Mirrors CheckingQueue._provider_for_torrent."""
        if not torrent_id:
            return default
        name = memo.get(torrent_id)
        if not name:
            for item in items:
                if item.get('filled_by_torrent_id') == torrent_id:
                    name = item.get('debrid_provider')
                    break
        if not name:
            return default
        return self._by_name(name) or default

    def test_the_reported_case_routes_to_alldebrid(self):
        items = [{'id': 19106, 'filled_by_torrent_id': '725087006',
                  'debrid_provider': 'AllDebrid'}]
        self.assertIs(self._resolve('725087006', {}, items, self.rd), self.ad)

    def test_memo_is_consulted_before_the_item_list(self):
        """handle_missing_torrent drops items from self.items before it is done
        talking to the provider, so the memo has to outlive them."""
        self.assertIs(self._resolve('725087006', {'725087006': 'AllDebrid'}, [], self.rd), self.ad)

    def test_unstamped_item_falls_back_to_primary(self):
        items = [{'id': 1, 'filled_by_torrent_id': 'ABC', 'debrid_provider': None}]
        self.assertIs(self._resolve('ABC', {}, items, self.rd), self.rd)

    def test_provider_no_longer_in_the_chain_falls_back_to_primary(self):
        items = [{'id': 1, 'filled_by_torrent_id': 'ABC', 'debrid_provider': 'Torbox'}]
        self.assertIs(self._resolve('ABC', {}, items, self.rd), self.rd)

    def test_settings_spelling_is_accepted(self):
        items = [{'id': 1, 'filled_by_torrent_id': 'ABC', 'debrid_provider': 'alldebrid'}]
        self.assertIs(self._resolve('ABC', {}, items, self.rd), self.ad)

    def test_rd_torrent_still_goes_to_rd(self):
        items = [{'id': 2, 'filled_by_torrent_id': 'XYZ', 'debrid_provider': 'Real-Debrid'}]
        self.assertIs(self._resolve('XYZ', {}, items, self.rd), self.rd)


class TestRediscoveryDoesNotCorruptRouting(unittest.TestCase):
    """Rediscovery exists for rows written before the debrid_provider column: a
    404 from the primary is ambiguous there, so the rest of the chain is probed
    before the torrent is written off as missing.

    The trap is what to do when nobody has it. Caching that as "it is on the
    primary" would be a routing claim the probe never established, and it would
    outrank a correct stamp arriving later. The negative goes in its own set.
    """

    def setUp(self):
        self.tree, _ = _parse('queues/checking_queue.py')
        self.fn = _find_function(self.tree, '_rediscover_provider')

    def test_negative_result_does_not_touch_the_routing_map(self):
        for node in ast.walk(self.fn):
            # self._torrent_providers[...] = <primary's name>
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Attribute)
                            and target.value.attr == '_torrent_providers'):
                        value = ast.dump(node.value)
                        self.assertNotIn('debrid_provider', value,
                                         'the primary must never be written into the routing '
                                         'map as a rediscovery result')

    def test_negative_result_is_cached_separately(self):
        src = ast.dump(self.fn)
        self.assertIn('_rediscovery_exhausted', src,
                      'a failed sweep must be remembered, or the whole chain is '
                      're-probed on every pass of the queue')

    def test_exhausted_torrents_short_circuit(self):
        """The guard has to be read as well as written."""
        reads = [n for n in ast.walk(self.fn)
                 if isinstance(n, ast.Compare) and any(
                     isinstance(c, ast.In) for c in n.ops)]
        self.assertTrue(
            any('_rediscovery_exhausted' in ast.dump(r) for r in reads),
            'expected an early return for a torrent whose chain was already swept')

    def test_positive_result_is_persisted(self):
        called = {n.func.attr for n in ast.walk(self.fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn('_persist_provider', called,
                      'a rediscovered provider must survive a restart, or every '
                      'start re-probes the same torrents')

    def test_rediscovery_only_runs_for_unknown_providers(self):
        """A stamped torrent that genuinely 404s is genuinely gone. Probing there
        would be wasted calls, and worse, could mask a real deletion."""
        fn = _find_function(self.tree, 'get_torrent_progress')
        guarded = False
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and '_rediscover_provider' in ast.dump(node):
                if 'known_provider' in ast.dump(node.test):
                    guarded = True
        self.assertTrue(guarded,
                        'the rediscovery call must be gated on the provider being unknown')

    def test_known_provider_is_read_before_resolution_mutates_the_memo(self):
        """_provider_for_torrent promotes the item's stamp into the memo as a side
        effect. Reading the memo directly afterwards would report every stamped
        torrent as unknown on its first poll."""
        fn = _find_function(self.tree, 'get_torrent_progress')
        src = ast.dump(fn)
        self.assertIn('_recorded_provider_name', src)
        self.assertNotIn("attr='_torrent_providers'", src,
                         'ask _recorded_provider_name, not the raw memo')


class TestRediscoverySemantics(unittest.TestCase):
    """The decision table, simulated."""

    class FakeProvider:
        def __init__(self, name, known_ids):
            self.PROVIDER_NAME = name
            self._known = set(known_ids)

        def has(self, torrent_id):
            return torrent_id in self._known

    def setUp(self):
        self.memo = {}
        self.exhausted = set()

    def _rediscover(self, torrent_id, chain, primary):
        if torrent_id in self.exhausted:
            return None
        if len(chain) < 2:
            return None
        for provider in chain:
            if provider is primary:
                continue
            if provider.has(torrent_id):
                self.memo[torrent_id] = provider.PROVIDER_NAME
                return provider
        self.exhausted.add(torrent_id)
        return None

    def test_torrent_living_on_a_fallback_is_found(self):
        rd = self.FakeProvider('Real-Debrid', [])
        ad = self.FakeProvider('AllDebrid', ['725087006'])
        self.assertIs(self._rediscover('725087006', [rd, ad], rd), ad)
        self.assertEqual(self.memo, {'725087006': 'AllDebrid'})

    def test_genuinely_dead_torrent_is_reported_missing(self):
        rd = self.FakeProvider('Real-Debrid', [])
        ad = self.FakeProvider('AllDebrid', [])
        self.assertIsNone(self._rediscover('DEAD', [rd, ad], rd))

    def test_dead_torrent_does_not_get_a_routing_entry(self):
        """The bug this guards: caching the primary here would later beat a
        correct stamp for the same id."""
        rd = self.FakeProvider('Real-Debrid', [])
        ad = self.FakeProvider('AllDebrid', [])
        self._rediscover('DEAD', [rd, ad], rd)
        self.assertEqual(self.memo, {})
        self.assertIn('DEAD', self.exhausted)

    def test_chain_is_swept_only_once(self):
        calls = []

        class Counting(self.FakeProvider):
            def has(inner, torrent_id):
                calls.append(inner.PROVIDER_NAME)
                return False

        rd = Counting('Real-Debrid', [])
        ad = Counting('AllDebrid', [])
        self._rediscover('DEAD', [rd, ad], rd)
        self._rediscover('DEAD', [rd, ad], rd)
        self.assertEqual(calls, ['AllDebrid'], 'second sweep should short-circuit')

    def test_single_provider_setup_never_probes(self):
        rd = self.FakeProvider('Real-Debrid', [])
        self.assertIsNone(self._rediscover('ANY', [rd], rd))
        self.assertEqual(self.exhausted, set(), 'nothing was swept, so nothing to remember')


if __name__ == '__main__':
    unittest.main(verbosity=2)

