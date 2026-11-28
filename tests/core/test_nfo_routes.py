"""Route tests for Tools -> NFO Files (routes/nfo_routes.py)."""

import threading
import unittest
from unittest import mock

from flask import Flask

from routes import nfo_routes
from utilities import nfo_writer, plex_snapshot


class NfoOffRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.register_blueprint(nfo_routes.nfo_bp, url_prefix='/nfo')
        self.client = self.app.test_client()
        self._orig_nfo_job = dict(nfo_writer._job)
        self._orig_snap_job = dict(plex_snapshot._job)
        nfo_writer._job.clear()
        nfo_writer._job.update({'running': False, 'stale_key': 'old'})
        plex_snapshot._job.clear()
        plex_snapshot._job.update({'running': False})

    def tearDown(self):
        nfo_writer._job.clear()
        nfo_writer._job.update(self._orig_nfo_job)
        plex_snapshot._job.clear()
        plex_snapshot._job.update(self._orig_snap_job)

    def test_mode_off_runs_as_locked_job_blocks_concurrent_ops_and_does_not_mutate_snapshot_job(self):
        started = threading.Event()
        release = threading.Event()

        def slow_remove():
            started.set()
            release.wait(timeout=5)
            return {'removed': 2}

        def fake_restore_all():
            return {'written': 1}

        with mock.patch.object(nfo_routes, '_media_server_is_plex', return_value=False), \
                mock.patch.object(nfo_routes, 'set_setting'), \
                mock.patch.object(nfo_writer, 'mode', return_value='all'), \
                mock.patch.object(nfo_writer, 'enabled', return_value=True), \
                mock.patch.object(nfo_writer, 'symlinked_mode', return_value=True), \
                mock.patch.object(nfo_writer, 'remove_library', side_effect=slow_remove), \
                mock.patch.object(plex_snapshot, 'restore_all', side_effect=fake_restore_all), \
                mock.patch('routes.models.is_user_system_enabled', return_value=False):
            resp = self.client.post('/nfo/api/mode', json={'mode': 'off'})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(started.wait(timeout=2), 'off() job did not start')

            # While off() is running: job is marked running, stale keys cleared, _busy() blocks concurrent routes
            status = nfo_writer.job_status()
            self.assertTrue(status.get('running'))
            self.assertEqual(status.get('kind'), 'off')
            self.assertNotIn('stale_key', status)
            self.assertTrue(nfo_routes._busy())
            self.assertEqual(self.client.post('/nfo/api/mode', json={'mode': 'all'}).status_code, 409)
            self.assertEqual(self.client.post('/nfo/api/convert', json={'plex': False}).status_code, 409)

            release.set()
            # Wait for the off() job to finish
            for _ in range(100):
                if not nfo_writer.job_status().get('running'):
                    break
                threading.Event().wait(0.02)

            done = nfo_writer.job_status()
            self.assertFalse(done.get('running'))
            self.assertTrue(done.get('finished'))
            self.assertEqual(done.get('removed'), {'removed': 2})
            self.assertEqual(done.get('snapshot_restored'), {'written': 1})
            self.assertFalse(nfo_routes._busy())

    def test_restore_all_does_not_mutate_plex_snapshot_job(self):
        plex_snapshot._job.clear()
        plex_snapshot._job.update({'running': False, 'step': 'sentinel'})
        with mock.patch.object(plex_snapshot, 'in_use', return_value=1), \
                mock.patch('os.path.exists', return_value=True), \
                mock.patch.object(nfo_writer, 'library_root', return_value='/tmp'), \
                mock.patch.object(nfo_writer, 'wants_show', return_value=True), \
                mock.patch.object(plex_snapshot, '_conn') as mock_conn:
            conn = mock.Mock()
            conn.execute.side_effect = [
                mock.Mock(fetchall=lambda: [('/tmp/a.mkv', 'tt1', '[[1,1]]', '[{"season":1,"episode":1}]')]),
                mock.Mock(fetchall=lambda: []),
            ]
            mock_conn.return_value = conn
            plex_snapshot.restore_all()
        self.assertEqual(plex_snapshot._job.get('step'), 'sentinel')


if __name__ == '__main__':
    unittest.main()
