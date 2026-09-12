#!/usr/bin/env python3
"""
Unit tests for the 'zurg' Usenet Provider mode in usenet/nzbdav_client.py.

Zurg 2.0's SABnzbd-emulation mount is flat (<mount>/<job>/) instead of NzbDAV's
nested (<mount>/<cat>/<job>/) layout, and it derives release identity from the
uploaded .nzb filename rather than content — so two fixes are under test:

  1. NzbdavClient.flat_layout=True resolves job folders directly under the
     mount root instead of walking a category subdirectory.
  2. add_nzb_content appends a content-hash suffix to the uploaded filename
     only in flat_layout mode, so two different payloads submitted under the
     same nzbname/title don't collide on Zurg's filename-keyed identity.
"""

import unittest
import sys
import os
import types
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: {}
    sys.modules['utilities.settings'] = uss
    if 'routes' not in sys.modules:
        sys.modules['routes'] = types.ModuleType('routes')
    rta = types.ModuleType('routes.api_tracker')
    rta.api = types.SimpleNamespace(get=None, post=None)
    sys.modules['routes.api_tracker'] = rta
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'nzbdav_client.py')
    spec = importlib.util.spec_from_file_location('nzbdav_client_zurg_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


nc = _load()


class TestFlatLayoutFolderResolution(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _client(self):
        c = nc.NzbdavClient()
        c.flat_layout = True
        c.mount_path = self.root
        return c

    def test_direct_lookup_no_category_subdir(self):
        job = 'Movie.Title.2024.1080p-GRP'
        os.makedirs(os.path.join(self.root, job))
        c = self._client()
        found = c._find_nzb_folder(nc.re.sub(r'[^a-z0-9]', '', job.lower()), original_name=job)
        self.assertEqual(found, job)

    def test_does_not_misread_sibling_job_as_category(self):
        # A second, unrelated job folder must not be returned for a fuzzy match
        # that only vaguely overlaps — this would previously happen because the
        # nested-layout scan treated every top-level entry as a category dir and
        # listed *inside* it, silently matching nothing (or the wrong thing) on
        # a flat mount.
        os.makedirs(os.path.join(self.root, 'Show.S01E01.1080p-GRP'))
        os.makedirs(os.path.join(self.root, 'Show.S01E02.1080p-GRP'))
        c = self._client()
        found = c._find_nzb_folder(nc.re.sub(r'[^a-z0-9]', '', 'Show.S01E01.1080p-GRP'.lower()),
                                    original_name='Show.S01E01.1080p-GRP')
        self.assertEqual(found, 'Show.S01E01.1080p-GRP')

    def test_list_folder_files_flat(self):
        job = 'Movie.Title.2024.1080p-GRP'
        job_dir = os.path.join(self.root, job)
        os.makedirs(job_dir)
        with open(os.path.join(job_dir, f'{job}.mkv'), 'wb') as f:
            f.write(b'x' * 100)
        c = self._client()
        files = c._list_nzb_folder_files(job)
        self.assertEqual(files, [(f'{job}.mkv', 100)])

    def test_nested_layout_unaffected_by_default(self):
        # flat_layout defaults False — existing NzbDAV behaviour (category
        # subdir walk) must be unchanged.
        job = 'Movie.Title.2024.1080p-GRP'
        os.makedirs(os.path.join(self.root, 'movies', job))
        c = nc.NzbdavClient()
        c.mount_path = self.root
        self.assertFalse(c.flat_layout)
        found = c._find_nzb_folder(nc.re.sub(r'[^a-z0-9]', '', job.lower()), original_name=job)
        self.assertEqual(found, job)


class TestFilenameHashSuffix(unittest.TestCase):
    def setUp(self):
        self.posted = {}

        def fake_post(url, params=None, files=None, timeout=None):
            self.posted['params'] = params
            self.posted['files'] = files
            class R:
                status_code = 200
                def json(_self):
                    return {'status': True, 'nzo_ids': ['abc']}
            return R()

        nc.api.post = fake_post

    def _client(self, flat):
        c = nc.NzbdavClient()
        c.enabled = True
        c.base_url = 'http://x:9999'
        c.flat_layout = flat
        return c

    def test_zurg_mode_appends_content_hash_and_differs_per_payload(self):
        c = self._client(flat=True)
        c.add_nzb_content('<nzb>payload-one</nzb>', title='Same.Release.Title')
        fname_1 = self.posted['files']['name'][0]
        c.add_nzb_content('<nzb>payload-two</nzb>', title='Same.Release.Title')
        fname_2 = self.posted['files']['name'][0]

        self.assertNotEqual(fname_1, fname_2)
        self.assertTrue(fname_1.startswith('Same.Release.Title-'))
        self.assertTrue(fname_1.endswith('.nzb'))
        # nzbname (the folder-resolution key) must stay untouched by the hash.
        self.assertEqual(self.posted['params']['nzbname'], 'Same.Release.Title')

    def test_nzbdav_mode_filename_unchanged(self):
        c = self._client(flat=False)
        c.add_nzb_content('<nzb>payload</nzb>', title='Same.Release.Title')
        fname = self.posted['files']['name'][0]
        self.assertEqual(fname, 'Same.Release.Title.nzb')


def _load_mount_layout():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'mount_layout.py')
    spec = importlib.util.spec_from_file_location('mount_layout_zurg_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ml = _load_mount_layout()


class TestResolveNzbJobDirForPlexScan(unittest.TestCase):
    """CheckingQueue's targeted Plex scan must find flat Zurg job dirs as well
    as nested NzbDAV ones. The pre-fix walk only checked <mount>/<cat>/<job>,
    which never matches on a flat mount (and can mistake sibling jobs for cats).
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_flat_zurg_layout_resolves_direct_child(self):
        job = 'Movie.Title.2024.1080p-GRP'
        os.makedirs(os.path.join(self.root, job))
        self.assertEqual(
            ml.resolve_nzb_job_dir(self.root, job),
            os.path.join(self.root, job),
        )

    def test_flat_layout_with_sibling_jobs_still_hits_exact_folder(self):
        target = 'Show.S01E01.1080p-GRP'
        sibling = 'Show.S01E02.1080p-GRP'
        os.makedirs(os.path.join(self.root, target))
        os.makedirs(os.path.join(self.root, sibling))
        self.assertEqual(
            ml.resolve_nzb_job_dir(self.root, target),
            os.path.join(self.root, target),
        )

    def test_nested_nzbdav_layout_still_resolves(self):
        job = 'Movie.Title.2024.1080p-GRP'
        nested = os.path.join(self.root, 'movies', job)
        os.makedirs(nested)
        self.assertEqual(ml.resolve_nzb_job_dir(self.root, job), nested)

    def test_missing_folder_returns_none(self):
        os.makedirs(os.path.join(self.root, 'movies', 'Other.Job'))
        self.assertIsNone(ml.resolve_nzb_job_dir(self.root, 'Missing.Job'))

    def test_checking_queue_wires_resolve_helper(self):
        # queues/checking_queue.py can't be imported here (heavy deps), so
        # assert the Plex-scan path calls the shared helper rather than the
        # old nested-only listdir walk.
        source = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'queues', 'checking_queue.py'),
            encoding='utf-8',
        ).read()
        self.assertIn('from usenet.mount_layout import resolve_nzb_job_dir', source)
        self.assertIn('resolve_nzb_job_dir(_mount, _folder)', source)
        self.assertNotIn('_os.path.join(_mount, _cat, _folder)', source)


if __name__ == '__main__':
    unittest.main()
