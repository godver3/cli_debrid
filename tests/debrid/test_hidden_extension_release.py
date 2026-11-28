"""A release whose every video is a type the built-in mount hides (.ts
under the default allowed extensions) is skipped when it is picked, not
taken and then failed in Checking because its folder never appears."""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from queues.torrent_processor import TorrentProcessor
from utilities import mount_debrid

TS_PACK = [{'path': 'Show.S03.HDTV.1080i/Show.S03E01.ts'}, {'path': 'Show.S03.HDTV.1080i/Show.S03E02.ts'},
           {'path': 'Show.S03.HDTV.1080i/info.nfo'}]


def _settings(allowed='mkv,mp4,m4v,avi,mov,mpg,mpeg'):
    store = {'Built-in Mount': {'allowed_extensions': allowed}}
    return lambda section, key=None, default=None: (store.get(section, {}) if key is None
                                                    else store.get(section, {}).get(key, default))


class VideosAllHiddenTest(unittest.TestCase):
    def check(self, files, holds=True, allowed='mkv,mp4,m4v,avi,mov,mpg,mpeg'):
        with mock.patch.object(mount_debrid, 'engine_holds_debrid', return_value=holds):
            return mount_debrid.videos_all_hidden(files, _settings(allowed))

    def test_ts_only_release_is_hidden(self):
        self.assertEqual(self.check(TS_PACK), ['ts'])

    def test_one_listed_video_is_enough(self):
        self.assertEqual(self.check(TS_PACK + [{'path': 'Show.S03E03.mkv'}]), [])

    def test_not_hidden_without_the_built_in_mount_or_with_ts_allowed(self):
        self.assertEqual(self.check(TS_PACK, holds=False), [])
        self.assertEqual(self.check(TS_PACK, allowed='mkv,ts'), [])
        self.assertEqual(self.check(TS_PACK, allowed=''), [], 'empty list = everything listed')

    def test_no_videos_is_left_to_the_usual_checks(self):
        self.assertEqual(self.check([{'path': 'a.nfo'}]), [])

    def test_non_matcher_webm_or_extras_and_samples_do_not_mask_hidden_main_video(self):
        # .webm is ignored by MediaMatcher even if allowed on the mount.
        self.assertEqual(
            self.check(TS_PACK + [{'path': 'Show.S03.HDTV.1080i/extra.webm'}],
                       allowed='mkv,mp4,m4v,avi,mov,mpg,mpeg,webm'),
            ['ts'],
        )
        # Samples, extras, and promo clips are ignored by the matcher too.
        self.assertEqual(
            self.check(TS_PACK + [
                {'path': 'Show.S03.HDTV.1080i/Show.S03E01.sample.mkv'},
                {'path': 'Show.S03.HDTV.1080i/Show.S03E01.extra.mkv'},
                {'path': 'Show.S03.HDTV.1080i/RARBG.com.mp4', 'bytes': 500000},
            ]),
            ['ts'],
        )


class SkipHiddenReleaseTest(unittest.TestCase):
    def setUp(self):
        self.provider = mock.Mock()
        self.proc = TorrentProcessor.__new__(TorrentProcessor)
        self.proc.debrid_provider = self.provider
        p = mock.patch('utilities.mount_debrid.videos_all_hidden', side_effect=lambda f, g: ['ts'] if f is TS_PACK else [])
        p.start()
        self.addCleanup(p.stop)

    def _run(self, files, used, **extra):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = (1,) if used else None
        info = {'id': '42', 'hash': 'ABC', 'files': files, **extra}
        with mock.patch('queues.torrent_processor.add_to_not_wanted') as nw, \
                mock.patch('database.core.get_db_connection', return_value=conn):
            skipped = self.proc._skip_hidden_release(info, 'item', '[1/3]')
        return skipped, nw

    def test_a_torrent_already_on_the_account_is_kept(self):
        skipped, _ = self._run(TS_PACK, used=False, _reused=True)
        self.assertTrue(skipped)
        self.provider.remove_torrent.assert_not_called()

    def test_removed_through_the_provider_that_added_it(self):
        torbox = mock.Mock()
        with mock.patch('debrid.get_provider_by_name', return_value=torbox) as by_name:
            skipped, _ = self._run(TS_PACK, used=False, _provider='TorBox')
        self.assertTrue(skipped)
        by_name.assert_called_once_with('TorBox')
        torbox.remove_torrent.assert_called_once()
        self.provider.remove_torrent.assert_not_called()

    def test_hidden_release_is_skipped_marked_and_removed(self):
        skipped, nw = self._run(TS_PACK, used=False)
        self.assertTrue(skipped)
        nw.assert_called_once_with('abc')
        self.provider.remove_torrent.assert_called_once()

    def test_a_torrent_an_item_uses_stays_on_the_account(self):
        skipped, _ = self._run(TS_PACK, used=True)
        self.assertTrue(skipped)
        self.provider.remove_torrent.assert_not_called()

    def test_a_listed_release_goes_through(self):
        skipped, nw = self._run([{'path': 'a.mkv'}], used=False)
        self.assertFalse(skipped)
        nw.assert_not_called()


if __name__ == '__main__':
    unittest.main()
