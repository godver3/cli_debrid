"""A movie's 1080p and 2160p versions never share one cli_mount job.

Before submitting an NZB, the Adding queue reuses a cli_mount job whose name,
minus the trailing " - (release)", matches this item's. For a movie the name was
built as "Title (year) - {imdb} - Version - (release)", but a long title or
release made the builder drop the version first, and "Include version in NZB
naming" off left it out entirely. The 1080p item then matched the 2160p job,
reused it, and the 1080p release was never sent to cli_mount.
"""
import re
import unittest
from unittest import mock

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
from queues.torrent_processor import TorrentProcessor
from routes.scraper_routes import _build_nzb_title

TITLE = 'The Lord of the Rings The Fellowship of the Ring'
IMDB = 'tt0120737'
UHD = 'The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.2001.EXTENDED.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC.TrueHD.Atmos.7.1-FGT'
HD = 'The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.2001.EXTENDED.1080p.BluRay.DDP5.1.x264-DON'
HD_URL = 'https://indexer.example/getnzb/hd.nzb'
NZB_XML = '<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'


def _prefix(name):
    return re.sub(r'\s*-\s*\([^)]*\)\s*$', '', name).strip()


def _settings(**overrides):
    cfg = {'enable_nzb_naming': True, 'include_version_in_nzb_naming': True,
           'url': 'http://climount:8282', 'api_token': ''}
    cfg.update(overrides)
    return mock.patch('utilities.settings.get_setting', lambda section, key, default=None: cfg.get(key, default))


def _name(version, release):
    return _build_nzb_title(TITLE, '2001', IMDB, version, release, media_type='movie')


class BuildNzbTitleTests(unittest.TestCase):
    def test_long_movie_name_keeps_its_version(self):
        with _settings():
            uhd, hd = _name('2160p', UHD), _name('1080p', HD)
        self.assertLessEqual(len(uhd), 117)
        self.assertIn(' - 2160p - (', uhd)
        self.assertIn(' - 1080p - (', hd)
        self.assertNotEqual(_prefix(uhd), _prefix(hd))
        self.assertIn(f'{{imdb-{IMDB}}}', uhd)

    def test_short_movie_name_unchanged(self):
        with _settings():
            name = _build_nzb_title('Some Movie', '2024', 'tt1', '1080p',
                                    'Some.Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GRP', media_type='movie')
        self.assertEqual(name, 'Some Movie (2024) - {imdb-tt1} - 1080p - (1080p.WEB-DL.DDP5.1.H.264-GRP)')


class _Resp:
    def __init__(self, status, text='', payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    last_missing_segments = False

    def __init__(self):
        self.add_nzb = mock.Mock(return_value=None)
        self.add_nzb_content = mock.Mock(return_value='new-hd-job')
        self.get_job_status = mock.Mock(return_value={'state': 'downloading'})

    def is_enabled(self):
        return True


class JobReuseTests(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.proc = TorrentProcessor.__new__(TorrentProcessor)
        self.item = {'id': 42, 'title': TITLE, 'year': '2001', 'type': 'movie',
                     'imdb_id': IMDB, 'version': '1080p'}
        self.result = {'title': HD, 'nzb_url': HD_URL, 'parsed_info': {}}
        for p in (
            mock.patch('usenet.get_usenet_client', return_value=self.client),
            mock.patch('usenet.reset_usenet_client'),
            mock.patch('usenet.is_nzb_job_alive', return_value=True),
            mock.patch('queues.torrent_processor.time.sleep'),
            mock.patch('database.not_wanted_magnets.is_nzb_guid_not_wanted', return_value=False),
            mock.patch('database.not_wanted_magnets.is_nzb_segment_not_wanted', return_value=False),
            mock.patch('database.not_wanted_magnets.is_magnet_not_wanted', return_value=False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, existing_job_name):
        listing = {'torrents': [{'name': existing_job_name, 'info_hash': 'uhdjob', 'status': 'downloading'}],
                   'has_next': False}

        def get(url, **kw):
            if url.endswith('/api/torrents'):
                return _Resp(200, payload=listing)
            if url == HD_URL:
                return _Resp(200, NZB_XML)
            return _Resp(404)
        api = mock.Mock()
        api.get.side_effect = get
        with mock.patch('routes.api_tracker.api', api):
            return self.proc._process_nzb_result(dict(self.result), dict(self.item))

    def _reused(self, out):
        return bool(out) and out[0].get('id') == 'uhdjob'

    def test_version_toggle_off_does_not_reuse_other_versions_job(self):
        with _settings(include_version_in_nzb_naming=False):
            uhd_job = _name('2160p', UHD)
            out = self._run(uhd_job)
        self.assertFalse(self._reused(out), 'the 1080p item reused the 2160p job')

    def test_same_version_job_is_still_reused(self):
        with _settings():
            other_release = _name('1080p', HD.replace('DON', 'OTHER'))
            out = self._run(other_release)
        self.assertTrue(self._reused(out))

    def test_other_versions_job_is_not_reused(self):
        with _settings():
            out = self._run(_name('2160p', UHD))
        self.assertFalse(self._reused(out))


if __name__ == '__main__':
    unittest.main()
