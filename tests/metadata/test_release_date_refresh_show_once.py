"""refresh_release_dates reads each show once per run.

Every episode used to load its whole show from cli_battery twice (release
date, then airtime): a 727-episode show with 89 unreleased episodes was 178
full-show builds per run and most of the startup refresh. Each item must still
get its own release date and airtime from that one read.
"""
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import metadata.metadata as md

SHOWS = {
    'tt1': {'airs': {'time': '20:00', 'timezone': 'America/New_York'},
            'seasons': {1: {'episodes': {e: {'first_aired': f'2099-01-{e:02d}T01:00:00Z'} for e in range(1, 21)}}}},
    'tt2': {'airs': {'time': '21:00', 'timezone': 'America/New_York'},
            'seasons': {3: {'episodes': {1: {'first_aired': None}}}}},
}


def _episode(db_id, imdb_id, season, episode):
    return {'id': db_id, 'imdb_id': imdb_id, 'tmdb_id': '1', 'title': f'Show {imdb_id}', 'type': 'episode',
            'season_number': season, 'episode_number': episode, 'state': 'Unreleased',
            'release_date': 'Unknown', 'airtime': None, 'runtime': None}


class RefreshReadsShowOnceTests(unittest.TestCase):
    def setUp(self):
        self.items = [_episode(i, 'tt1', 1, i) for i in range(1, 21)] + [_episode(100, 'tt2', 3, 1)]
        self.loads = []
        self.load_lock = threading.Lock()
        self.writes = {}

        def get_show_metadata(imdb_id, force_refresh=False):
            with self.load_lock:
                self.loads.append((imdb_id, force_refresh))
            time.sleep(0.05)  # long enough for the other workers to pile up on the same show
            return SHOWS[imdb_id], 'battery'

        self.writers_now = 0
        self.max_writers = 0
        self.write_threads = set()

        def write(db_id, release_date, state, airtime=None, **kwargs):
            with self.load_lock:
                self.writers_now += 1
                self.max_writers = max(self.max_writers, self.writers_now)
                self.write_threads.add(threading.get_ident())
            time.sleep(0.005)
            self.writes[db_id] = (release_date, state, airtime)
            with self.load_lock:
                self.writers_now -= 1

        def items_in(state=None, **kwargs):
            return list(self.items) if state == 'Unreleased' else []

        trakt = mock.Mock(load_imdb_trakt_cache=mock.Mock(return_value={}))
        for patch in (
            mock.patch.object(md.DirectAPI, 'get_show_metadata', side_effect=get_show_metadata),
            mock.patch('database.get_all_media_items', side_effect=items_in),
            mock.patch('database.update_release_date_and_state', side_effect=write),
            mock.patch.dict('sys.modules', {'content_checkers.trakt': trakt}),
            mock.patch.object(md, '_get_local_timezone', return_value=ZoneInfo('America/New_York')),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_each_show_loaded_once(self):
        md.refresh_release_dates()
        self.assertEqual(sorted(self.loads), [('tt1', False), ('tt2', False)])

    def test_forced_run_refreshes_each_show_once(self):
        md.refresh_release_dates(force_bypass_cache=True)
        self.assertEqual(sorted(self.loads), [('tt1', True), ('tt2', True)])

    def test_every_episode_gets_its_own_date_and_airtime(self):
        md.refresh_release_dates()
        self.assertEqual(len(self.writes), 21)
        for e in range(1, 21):
            release_date, state, airtime = self.writes[e]
            # first_aired 01:00 UTC is the previous evening in America/New_York
            self.assertEqual(release_date, f'2099-01-{e - 1:02d}' if e > 1 else '2098-12-31')
            self.assertEqual(state, 'Unreleased')
            self.assertEqual(airtime, '20:00')
        # No first_aired: release date Unknown, airtime from the show's airs
        self.assertEqual(self.writes[100][0], 'Unknown')
        self.assertEqual(self.writes[100][2], '21:00')

    def test_db_writes_come_from_one_thread(self):
        # Concurrent writers made SQLite report 'database is locked' on every startup.
        md.refresh_release_dates()
        self.assertEqual(len(self.writes), 21)
        self.assertEqual(self.max_writers, 1)
        self.assertEqual(self.write_threads, {threading.get_ident()})

    def test_failed_write_does_not_stop_the_rest(self):
        def flaky(db_id, release_date, state, airtime=None, **kwargs):
            if db_id == 5:
                raise RuntimeError('boom')
            self.writes[db_id] = (release_date, state, airtime)
        with mock.patch('database.update_release_date_and_state', side_effect=flaky):
            md.refresh_release_dates()
        self.assertEqual(len(self.writes), 20)
        self.assertNotIn(5, self.writes)

    def test_airtime_still_fetches_when_not_given(self):
        self.assertEqual(md.get_episode_airtime('tt2', 3, 1), '21:00')
        self.assertEqual(self.loads, [('tt2', False)])

    def test_airtime_with_no_metadata_defaults(self):
        self.assertEqual(md.get_episode_airtime('tt9', 1, 1, metadata=None), '19:00')
        self.assertEqual(self.loads, [])


if __name__ == '__main__':
    unittest.main()
