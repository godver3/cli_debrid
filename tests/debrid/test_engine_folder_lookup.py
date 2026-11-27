"""Checking finds a debrid file in the folder the built-in mount actually
shows its torrent in ("Name (2)"), not only under names worked out from
the release."""

import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from utilities import mount_debrid


class EngineFolderTest(unittest.TestCase):
    def test_asks_the_engine_per_provider(self):
        rd, tb = mock.Mock(), mock.Mock()
        client = mock.Mock()
        client._call.side_effect = lambda m, path, timeout: (
            (200, {'folder': 'Pack (2)'}) if path == '/jobs/rd-ABC' else (404, None))
        with mock.patch.object(mount_debrid, 'engine_holds_debrid', return_value=True), \
                mock.patch.object(mount_debrid, 'providers', return_value=[tb, rd]), \
                mock.patch.object(mount_debrid, 'provider_key', side_effect=lambda p: 'rd' if p is rd else 'tb'), \
                mock.patch('usenet.builtin_client.get_builtin_client', return_value=client):
            self.assertEqual(mount_debrid.engine_folder('ABC', None), 'Pack (2)')

    def test_nothing_without_the_built_in_mount(self):
        with mock.patch.object(mount_debrid, 'engine_holds_debrid', return_value=False):
            self.assertIsNone(mount_debrid.engine_folder('ABC', None))


class CheckLocalFileTest(unittest.TestCase):
    def test_file_in_the_suffixed_folder_is_found_and_remembered(self):
        from utilities import local_library_scan as lls
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'Pack (2)'))
            open(os.path.join(root, 'Pack (2)', 'Show.S01E01.mkv'), 'w').close()
            item = {'id': 7, 'filled_by_file': 'Show.S01E01.mkv', 'debrid_folder_name': 'Pack',
                    'filled_by_torrent_id': 'ABC', 'type': 'episode'}
            seen = {}

            def stop_after_find(*a, **k):
                seen['path'] = item.get('debrid_folder_name')
                raise RuntimeError('stop')  # the rest (ffprobe, symlinks) isn't under test

            with mock.patch.object(lls, 'get_setting', side_effect=lambda s, k=None, d=None: root
                                   if k == 'original_files_path' else d), \
                    mock.patch('utilities.mount_debrid.folder_for', return_value='Pack (2)'), \
                    mock.patch('database.database_writing.update_media_item') as upd, \
                    mock.patch.object(lls.os.path, 'exists', wraps=os.path.exists) as ex:
                try:
                    lls.check_local_file_for_item(item)
                except Exception:
                    pass
            upd.assert_called_with(7, debrid_folder_name='Pack (2)')
            self.assertIn(os.path.join(root, 'Pack (2)', 'Show.S01E01.mkv'), [c.args[0] for c in ex.call_args_list])


class _Stop(Exception):
    pass


def _linked_source(root, item, real_folder, engine=True):
    """The source file check_local_file_for_item picks for an item (None = it
    found nothing it would link)."""
    from utilities import local_library_scan as lls
    lls._root_index_cache.clear()
    picked = {}

    def grab(source_file, _item):
        picked['file'] = source_file
        raise _Stop()

    with mock.patch.object(lls, 'get_setting', side_effect=lambda s, k=None, d=None: root
                           if k == 'original_files_path' else d), \
            mock.patch('utilities.mount_debrid.folder_for', return_value=real_folder), \
            mock.patch('utilities.mount_debrid.engine_holds_debrid', return_value=engine), \
            mock.patch('database.database_writing.update_media_item'), \
            mock.patch.object(lls, '_apply_nzb_naming', side_effect=grab):
        try:
            lls.check_local_file_for_item(item)
        except _Stop:
            pass
    return picked.get('file')


class DuplicateNameTest(unittest.TestCase):
    """"Name" and "Name (2)" both hold Episode.mkv: only the real folder counts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        for d in ('Name', 'Name (2)'):
            os.makedirs(os.path.join(self.root, d))
            open(os.path.join(self.root, d, 'Episode.mkv'), 'w').close()
        self.item = {'id': 1, 'filled_by_file': 'Episode.mkv', 'filled_by_title': 'Name',
                     'original_scraped_torrent_title': 'Name', 'debrid_folder_name': 'Name',
                     'filled_by_torrent_id': 'T2', 'type': 'episode'}

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_torrent_links_its_own_folder(self):
        got = _linked_source(self.root, dict(self.item), 'Name (2)')
        self.assertEqual(got, os.path.join(self.root, 'Name (2)', 'Episode.mkv'))

    def test_real_folder_without_the_file_waits_instead_of_guessing(self):
        os.remove(os.path.join(self.root, 'Name (2)', 'Episode.mkv'))
        self.assertIsNone(_linked_source(self.root, dict(self.item), 'Name (2)'))

    def test_unknown_folder_with_sibling_does_not_link_on_the_built_in_mount(self):
        self.assertIsNone(_linked_source(self.root, dict(self.item), None, engine=True))

    def test_unknown_folder_file_in_both_does_not_link_on_another_mount(self):
        self.assertIsNone(_linked_source(self.root, dict(self.item), None, engine=False))

    def test_unknown_folder_file_in_one_sibling_links_on_another_mount(self):
        os.remove(os.path.join(self.root, 'Name (2)', 'Episode.mkv'))
        got = _linked_source(self.root, dict(self.item), None, engine=False)
        self.assertEqual(got, os.path.join(self.root, 'Name', 'Episode.mkv'))

    def test_no_sibling_name_guess_still_links(self):
        import shutil
        shutil.rmtree(os.path.join(self.root, 'Name (2)'))
        got = _linked_source(self.root, dict(self.item), None)
        self.assertEqual(got, os.path.join(self.root, 'Name', 'Episode.mkv'))

    def test_year_in_a_folder_name_is_not_a_sibling(self):
        os.makedirs(os.path.join(self.root, 'Name (2019)'))
        import shutil
        shutil.rmtree(os.path.join(self.root, 'Name (2)'))
        got = _linked_source(self.root, dict(self.item), None)
        self.assertEqual(got, os.path.join(self.root, 'Name', 'Episode.mkv'))


class FolderFamilyTest(unittest.TestCase):
    def test_family(self):
        names = ['Name', 'Name (2)', 'Name (3)', 'Name (2019)', 'Other', 'Name 2']
        self.assertEqual(mount_debrid.folder_family(names, 'Name'), ['Name', 'Name (2)', 'Name (3)'])
        self.assertEqual(mount_debrid.folder_family(names, 'Name (2)'), ['Name', 'Name (2)', 'Name (3)'])

    def test_nzb_job_ids(self):
        self.assertEqual(mount_debrid._engine_job_id('nzb:abc'), ['abc'])


class MemberNameTest(unittest.TestCase):
    def test_season_folders_with_one_base_name_get_the_mount_names(self):
        files = [{'path': 'Show/S01/01.mkv', 'bytes': 5, 'id': 1}, {'path': 'Show/S02/01.mkv', 'bytes': 5, 'id': 1},
                 {'path': 'Show/S02/02.mkv', 'bytes': 5, 'id': 1}]
        a = mount_debrid.mount_file_name(files, 'Show/S01/01.mkv')
        b = mount_debrid.mount_file_name(files, 'Show/S02/01.mkv')
        self.assertNotEqual(a, b)
        self.assertEqual(sorted([a, b]), sorted(f['name'] for f in mount_debrid.unique_names(
            [{'name': x['path'], 'file_id': 1} for x in files[:2] + files[2:]])
            if f['name'] != '02.mkv'))
        self.assertEqual(mount_debrid.mount_file_name(files, 'Show/S02/02.mkv'), '02.mkv')


class _Engine:
    def __init__(self, jobs, hidden=True):
        self.jobs, self.hidden, self.calls = jobs, hidden, []

    def _call(self, method, path, timeout=None, json=None):
        self.calls.append((method, path, json))
        if method == 'GET' and path == '/jobs':
            return 200, [{'id': j, 'protocol': 'debrid'} for j in self.jobs]
        if method == 'GET':
            return 200, {'id': path.rsplit('/', 1)[-1], 'hidden_by_extension': self.hidden}
        if method == 'DELETE':
            self.jobs.discard(path.rsplit('/', 1)[-1])
            return 204, None
        return 201, {}


class _TorBox:
    mounts_zip_members = True

    def __init__(self, files):
        self.files = files

    def list_active_torrents(self, strict=False):
        return [{'id': '9', 'status': 'cached', 'hash': 'AB'}]

    def mount_files(self, tid):
        return 'Show', self.files


MEMBERS = [{'name': 'Show/S01/01.mkv', 'size': 5, 'file_id': '3', 'offset': 10, 'source_size': 99},
           {'name': 'Show/S02/01.mkv', 'size': 5, 'file_id': '3', 'offset': 40, 'source_size': 99}]


class ZipReRegisterTest(unittest.TestCase):
    def setUp(self):
        mount_debrid._zip_checked.clear()
        mount_debrid._not_zip.clear()
        mount_debrid._skipped.clear()

    def _run(self, engine, files):
        p = _TorBox(files)
        with mock.patch.object(mount_debrid, 'provider_key', return_value='tb'):
            return mount_debrid.sync_once(engine, [p])

    def test_raw_zip_is_replaced_by_its_members(self):
        engine = _Engine({'tb-9'})
        out = self._run(engine, MEMBERS)
        self.assertEqual(out['added'], 1)
        methods = [c[0] for c in engine.calls]
        self.assertLess(methods.index('DELETE'), methods.index('POST'))
        body = [c[2] for c in engine.calls if c[0] == 'POST'][0]
        names = [f['name'] for f in body['files']]
        self.assertEqual(len(set(names)), 2)  # S01/01.mkv and S02/01.mkv stay distinct
        self.assertTrue(all(f['offset'] >= 0 and f['source_size'] == 99 for f in body['files']))

    def test_still_raw_zip_is_left_alone_and_retried_later(self):
        engine = _Engine({'tb-9'})
        raw = [{'name': 'Show/Pack.zip', 'size': 99, 'file_id': '3'}]
        self.assertEqual(self._run(engine, raw)['added'], 0)
        self.assertNotIn('DELETE', [c[0] for c in engine.calls])

    def test_visible_job_is_never_touched(self):
        engine = _Engine({'tb-9'}, hidden=False)
        self.assertEqual(self._run(engine, MEMBERS)['added'], 0)
        self.assertEqual([c[0] for c in engine.calls if c[0] != 'GET'], [])

    def test_checked_once_per_interval(self):
        engine = _Engine({'tb-9'}, hidden=False)
        self._run(engine, MEMBERS)
        n = len(engine.calls)
        self._run(engine, MEMBERS)
        self.assertEqual(len(engine.calls), n + 1)  # only the job listing


    def test_hidden_non_zip_job_is_not_asked_again(self):
        # A .ts-only release is hidden too, but it's no zip: one look, then
        # never another provider call (each one costs the pass budget)
        engine = _Engine({'tb-9'})
        ts = [{'name': 'Show/Show.S01E01.ts', 'size': 99, 'file_id': '3'}]
        p = _TorBox(ts)
        p.mount_files = mock.Mock(return_value=('Show', ts))
        with mock.patch.object(mount_debrid, 'provider_key', return_value='tb'):
            mount_debrid.sync_once(engine, [p])
            mount_debrid._zip_checked.clear()  # as if the hour had passed
            mount_debrid.sync_once(engine, [p])
        self.assertEqual(p.mount_files.call_count, 1)
        self.assertIn('tb-9', mount_debrid._not_zip)
        self.assertNotIn('DELETE', [c[0] for c in engine.calls])

class AddingNameTest(unittest.TestCase):
    """Adding stores the name the mount shows, so Checking can find S01/01.mkv and S02/01.mkv."""

    def test_clashing_base_name_is_stored_as_the_mount_name(self):
        from queues.adding_queue import AddingQueue
        from queues.media_matcher import MediaMatcher
        raw = [{'path': 'Show/A/Show.S01E01.mkv', 'bytes': 5, 'id': 3},
               {'path': 'Show/B/Show.S01E01.mkv', 'bytes': 5, 'id': 3},
               {'path': 'Show/B/Show.S01E02.mkv', 'bytes': 5, 'id': 3}]
        matcher = MediaMatcher()
        parsed = [matcher._parse_file_info(f) for f in raw]
        holder = mock.Mock(media_matcher=matcher)
        item = {'type': 'episode', 'title': 'Show', 'season_number': 1, 'episode_number': 1}
        with mock.patch('utilities.mount_debrid.engine_holds_debrid', return_value=True):
            got = AddingQueue._mount_file_name(holder, {'files': raw}, parsed, 'Show.S01E01.mkv', item)
            other = AddingQueue._mount_file_name(holder, {'files': raw}, parsed, 'Show.S01E02.mkv',
                                                 {**item, 'episode_number': 2})
        mount = [f['name'] for f in mount_debrid.unique_names([{'name': r['path'], 'file_id': 3} for r in raw])]
        self.assertEqual(got, mount[0])
        self.assertNotEqual(got, 'Show.S01E01.mkv')
        self.assertEqual(other, 'Show.S01E02.mkv')

    def test_unique_base_name_is_kept(self):
        from queues.adding_queue import AddingQueue
        parsed = [{'path': 'Show/01.mkv'}, {'path': 'Show/02.mkv'}]
        self.assertEqual(AddingQueue._mount_file_name(mock.Mock(), {'files': []}, parsed, '01.mkv', {}), '01.mkv')


class GuardedFolderTest(unittest.TestCase):
    def test_real_folder_wins(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(mount_debrid, 'folder_for', return_value='Job (2)'):
            os.makedirs(os.path.join(root, 'Job'))
            self.assertEqual(mount_debrid.guarded_folder(root, 'nzb:1', 'Job', None), 'Job (2)')

    def test_sibling_without_real_folder_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(mount_debrid, 'folder_for', return_value=None):
            os.makedirs(os.path.join(root, 'Job'))
            self.assertEqual(mount_debrid.guarded_folder(root, 'nzb:1', 'Job', None), 'Job')
            os.makedirs(os.path.join(root, 'Job (2)'))
            self.assertIsNone(mount_debrid.guarded_folder(root, 'nzb:1', 'Job', None))


class PlexGuessGuardTest(unittest.TestCase):
    def test_guess_in_a_duplicate_name_folder_is_dropped(self):
        from queues.runner import plex_tasks
        from utilities import local_library_scan as lls
        with tempfile.TemporaryDirectory() as root:
            for d in ('Name', 'Name (2)'):
                os.makedirs(os.path.join(root, d))
                open(os.path.join(root, d, 'E.mkv'), 'w').close()
            lls._root_index_cache.clear()
            paths = [os.path.join(root, 'Name', 'E.mkv'), os.path.join(root, 'Other', 'E.mkv')]
            item = {'filled_by_torrent_id': 'T'}
            with mock.patch('utilities.mount_debrid.engine_holds_debrid', return_value=True):
                self.assertEqual(plex_tasks._guard_ambiguous(root, paths, 'E.mkv', item, None), paths[1:])
                self.assertEqual(plex_tasks._guard_ambiguous(root, paths, 'E.mkv', item, 'Name (2)'), paths)


class NoDirectNameJoinTest(unittest.TestCase):
    """New code must not join the mount root with a release/torrent name on its
    own: go through mount_debrid.folder_for / guarded_folder (or the guarded
    lookups in local_library_scan and plex_tasks) and add the site here only if
    the name is a label or key, never the folder that decides which torrent's
    file is used."""

    PATTERN = r'os\.path\.join\([^)]*(filled_by_title|torrent_title|original_title|debrid_folder_name|_trimmed|folder_name)'
    # file -> number of existing joins. local_library_scan / plex_tasks: the
    # guesses are all behind folder_for-first and the sibling guard; the other
    # two build library or request paths, not mount lookups.
    ALLOWED = {'queues/runner/plex_tasks.py': 12, 'utilities/local_library_scan.py': 12,
               'routes/scraper_routes/files.py': 1, 'usenet/nzbdav_client.py': 2}

    def test_no_new_joins(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        found = {}
        for top in ('queues', 'utilities', 'routes', 'usenet', 'database'):
            for dirpath, _d, files in os.walk(os.path.join(root, top)):
                for f in files:
                    if f.endswith('.py'):
                        path = os.path.join(dirpath, f)
                        with open(path, encoding='utf-8') as fh:
                            n = sum(1 for line in fh if re.search(self.PATTERN, line))
                        if n:
                            found[os.path.relpath(path, root)] = n
        self.assertEqual(found, self.ALLOWED)


if __name__ == '__main__':
    unittest.main()
