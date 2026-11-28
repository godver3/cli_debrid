"""Plex NFO agent setup (utilities/plex_nfo_agent.py) against a fake PMS
that keeps provider groups and library sections like the real API."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from urllib.parse import parse_qsl, urlparse

from utilities import plex_nfo_agent as pna

PROVIDERS = {'tv.plex.agents.movie': 1, 'tv.plex.agents.series': 2, 'tv.plex.agents.localmedia': 6,
             'tv.plex.agents.nfo.series': 8}


class FakeSection:
    def __init__(self, server, key, kind, title, locations):
        self._server, self.key, self.type, self.title = server, key, kind, title
        self.locations = list(locations)
        self.agent = 'tv.plex.agents.series' if kind == 'show' else 'tv.plex.agents.movie'
        self.group = 2 if kind == 'show' else 1

    def reload(self):
        return self


class FakePlex:
    def __init__(self, drop_locations=False, refuse_sections=()):
        self.groups = {1: {'title': 'Plex Movie', 'primary': 'tv.plex.agents.movie', 'builtIn': True, 'items': [[1, 1, 1000.0]]},
                       2: {'title': 'Plex Series', 'primary': 'tv.plex.agents.series', 'builtIn': True, 'items': [[2, 2, 1000.0]]}}
        self.next_item = 10
        self.sections = [FakeSection(self, 1, 'movie', 'Movies', ['/lib/symlinks/Movies']),
                         FakeSection(self, 2, 'show', 'TV Shows', ['/lib/symlinks/TV Shows']),
                         FakeSection(self, 3, 'show', 'Other TV', ['/other/tv'])]
        self.library = mock.Mock(sections=lambda: self.sections)
        self._session = mock.Mock(get='get', post='post', put='put')
        self.drop_locations = drop_locations
        self.refuse_sections = set(refuse_sections)
        self.calls = []

    def _group_xml(self):
        root = ET.Element('MediaContainer')
        for gid, g in self.groups.items():
            ge = ET.SubElement(root, 'MetadataAgentProviderGroup', id=str(gid), title=g['title'],
                               primaryIdentifier=g['primary'], builtIn='1' if g['builtIn'] else '0')
            for iid, pid, order in g['items']:
                ET.SubElement(ge, 'MetadataAgentProviderGroupItem', id=str(iid), metadataAgentProviderGroupId=str(gid),
                              metadataAgentProviderId=str(pid), order=str(order))
        return root

    def query(self, path, method=None):
        u = urlparse(path)
        q = dict(parse_qsl(u.query))
        parts = u.path.strip('/').split('/')
        self.calls.append((method, u.path, q))
        if u.path == '/media/providers/metadata':
            root = ET.Element('MediaContainer')
            for ident, pid in PROVIDERS.items():
                ET.SubElement(root, 'MetadataAgentProvider', id=str(pid), identifier=ident)
            return root
        if u.path == '/media/providers/metadata/group':
            if method == 'post':
                gid = max(self.groups) + 1
                self.groups[gid] = {'title': q['title'], 'primary': q['primaryIdentifier'], 'builtIn': False,
                                    'items': [[self._item(), PROVIDERS[q['primaryIdentifier']], 1000.0]]}
            return self._group_xml()
        if parts[:4] == ['media', 'providers', 'metadata', 'group'] and len(parts) == 7 and parts[5] == 'items':
            g, pid = self.groups[int(parts[4])], int(parts[6])
            item = next((i for i in g['items'] if i[1] == pid), None)
            if item is None:
                g['items'].append([self._item(), pid, max(i[2] for i in g['items']) + 1000])
            elif 'after' in q:
                after = int(q['after'])
                rest = [i for i in sorted(g['items'], key=lambda i: i[2]) if i is not item]
                pos = 0 if after == -1 else next(n for n, i in enumerate(rest) if i[0] == after) + 1
                rest.insert(pos, item)
                for n, i in enumerate(rest):
                    i[2] = (n + 1) * 1000.0
            return ET.Element('MediaContainer')
        if u.path == '/library/sections':
            root = ET.Element('MediaContainer')
            for s in self.sections:
                ET.SubElement(root, 'Directory', key=str(s.key), agent=s.agent, metadataAgentProviderGroupId=str(s.group))
            return root
        if parts[:2] == ['library', 'sections'] and len(parts) == 3:
            s = next(x for x in self.sections if str(x.key) == parts[2])
            if method == 'put' and s.key not in self.refuse_sections:
                s.agent = q['agent']
                s.group = int(q['metadataAgentProviderGroupId'])
                if self.drop_locations:
                    s.locations = []
            # Like PMS: GET /library/sections/{id} lists the library's
            # contents (all, onDeck, ...), not the section's own settings
            root = ET.Element('MediaContainer', librarySectionID=str(s.key))
            for k in ('all', 'onDeck', 'recentlyAdded'):
                ET.SubElement(root, 'Directory', key=k, title=k)
            return root
        if parts[:2] == ['library', 'sections'] and parts[-1] == 'refresh':
            return ET.Element('MediaContainer')
        raise AssertionError(f'unexpected call {method} {path}')

    def _item(self):
        self.next_item += 1
        return self.next_item


class PlexNfoAgentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        settings = {('File Management', 'symlinked_files_path'): '/lib/symlinks'}
        for p in (mock.patch('utilities.settings.get_config_dir', return_value=self.cfg),
                  mock.patch.object(pna, 'get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d))):
            p.start()
            self.addCleanup(p.stop)

    def _order(self, plex, gid):
        return [i[1] for i in sorted(plex.groups[gid]['items'], key=lambda i: i[2])]

    def test_apply_creates_ordered_group_and_switches_only_symlinked_tv(self):
        plex = FakePlex()
        result = pna.apply(refresh=True, plex=plex)
        gid = result['group_id']
        self.assertEqual(plex.groups[gid]['primary'], 'tv.plex.agents.series')
        self.assertEqual(self._order(plex, gid), [8, 2, 6])     # NFO, Plex Series, Local Media
        tv, other = plex.sections[1], plex.sections[2]
        self.assertEqual((tv.agent, tv.group), ('tv.plex.agents.series', gid))
        self.assertEqual(other.group, 2)                        # not the symlinked folder: untouched
        self.assertEqual(plex.sections[0].group, 1)             # movies untouched
        self.assertIn(('get', '/library/sections/2/refresh', {'force': '1'}), plex.calls)
        self.assertEqual(result['sections'], ['TV Shows'])

    def test_apply_twice_reuses_the_group(self):
        plex = FakePlex()
        gid = pna.apply(refresh=False, plex=plex)['group_id']
        self.assertEqual(pna.apply(refresh=False, plex=plex)['group_id'], gid)
        self.assertEqual(sum(1 for g in plex.groups.values() if not g['builtIn']), 1)

    def test_restore_puts_previous_agent_back(self):
        plex = FakePlex()
        pna.apply(refresh=False, plex=plex)
        pna.restore(refresh=False, plex=plex)
        self.assertEqual((plex.sections[1].agent, plex.sections[1].group), ('tv.plex.agents.series', 2))

    def test_stops_if_plex_drops_library_folders(self):
        plex = FakePlex(drop_locations=True)
        with self.assertRaisesRegex(RuntimeError, 'changed the folders'):
            pna.apply(refresh=True, plex=plex)
        self.assertNotIn(('get', '/library/sections/2/refresh', {'force': '1'}), plex.calls)

    def test_old_plex_without_nfo_agent_is_refused(self):
        plex = FakePlex()
        with mock.patch.dict(PROVIDERS, clear=True, values={'tv.plex.agents.series': 2}):
            with self.assertRaisesRegex(RuntimeError, '1.43.1'):
                pna.apply(plex=plex)
        self.assertEqual(plex.sections[1].group, 2)

    def test_status_reports_sections(self):
        plex = FakePlex()
        self.assertFalse(pna.status(plex=plex)['configured'])
        pna.apply(refresh=False, plex=plex)
        st = pna.status(plex=plex)
        self.assertTrue(st['configured'])
        self.assertEqual([s['title'] for s in st['sections']], ['TV Shows'])

    def test_failed_switch_leaves_applied_false(self):
        plex = FakePlex(refuse_sections={2})
        with self.assertRaisesRegex(RuntimeError, 'did not switch'):
            pna.apply(refresh=False, plex=plex)
        self.assertFalse(pna.applied())
        # Original agent is still remembered for restore
        self.assertEqual(pna._load_state()['previous']['2']['group_id'], 2)

    def test_partial_failure_still_allows_restore(self):
        plex = FakePlex(refuse_sections={4})
        plex.sections.append(FakeSection(plex, 4, 'show', 'Anime', ['/lib/symlinks/Anime']))
        with self.assertRaisesRegex(RuntimeError, 'did not switch'):
            pna.apply(refresh=False, plex=plex)
        # First section switched, second refused
        self.assertTrue(pna.applied())
        self.assertNotEqual(plex.sections[1].group, 2)
        self.assertEqual(plex.sections[3].group, 2)
        res = pna.restore(refresh=False, plex=plex)
        self.assertEqual(res['sections'], ['TV Shows'])
        self.assertEqual(plex.sections[1].group, 2)
        self.assertFalse(pna.applied())

    def test_restore_works_if_set_section_raised_after_switching(self):
        plex = FakePlex()
        orig_set = pna._set_section

        def boom(p, s, agent, gid):
            orig_set(p, s, agent, gid)
            raise RuntimeError('connection dropped')

        with mock.patch.object(pna, '_set_section', side_effect=boom):
            with self.assertRaisesRegex(RuntimeError, 'connection dropped'):
                pna.apply(refresh=False, plex=plex)
        self.assertFalse(pna.applied())
        res = pna.restore(refresh=False, plex=plex)
        self.assertEqual(res['sections'], ['TV Shows'])
        self.assertEqual(plex.sections[1].group, 2)


if __name__ == '__main__':
    unittest.main()
