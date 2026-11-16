"""Promo clips and non-video payloads are not the item (godver3/cli_debrid#575)."""
import importlib.util
import unittest
from pathlib import Path

# Loaded from its file: importing the debrid package pulls in routes (circular).
_spec = importlib.util.spec_from_file_location(
    '_debrid_common_utils_junk', Path(__file__).resolve().parents[2] / 'debrid' / 'common' / 'utils.py')
_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_utils)
drop_junk_torrent_videos = _utils.drop_junk_torrent_videos
is_promo_file = _utils.is_promo_file

MB = 1024 * 1024


def f(path, mb):
    return {'path': path, 'bytes': int(mb * MB)}


class PromoFileTests(unittest.TestCase):
    def test_site_named_clips_are_promo(self):
        for name in ('RARBG.com.mp4', '/RARBG.com.mp4', 'www.YTS.MX.mp4', 'ETRG.to.mkv'):
            self.assertTrue(is_promo_file(name, 1 * MB), name)

    def test_real_titles_are_not_promo(self):
        for name in ('Ant-Man.2015.1080p.BluRay.mkv', 'Mr.Robot.S01E01.mkv', 'Up.mkv',
                     'Saw.III.mkv', 'Doctor.Who.2005.S01E01.mkv'):
            self.assertFalse(is_promo_file(name, 1 * MB), name)

    def test_big_file_named_like_a_site_is_kept(self):
        self.assertFalse(is_promo_file('Movie.com.mkv', 2000 * MB))


class DropJunkTorrentVideosTests(unittest.TestCase):
    def test_iso_torrent_with_promo_clip_is_rejected(self):
        files = [f('/Ant-Man.2015.1080p.3D.BluRay.AVC.DTS-HD.MA.7.1-RARBG.iso', 39134),
                 f('/RARBG.com.mp4', 0.4), f('/RARBG.com.txt', 0.01)]
        kept, reason = drop_junk_torrent_videos([files[1]], files)
        self.assertEqual(kept, [])
        self.assertIn('promo', reason)

    def test_small_video_inside_disc_image_torrent_is_rejected(self):
        files = [f('/Movie.2014.iso', 44660), f('/Movie.2014.extra.mkv', 300)]
        kept, reason = drop_junk_torrent_videos([files[1]], files)
        self.assertEqual(kept, [])
        self.assertIn('disc image', reason)

    def test_normal_movie_keeps_main_file_and_drops_promo(self):
        files = [f('/Movie.2015.1080p.mkv', 9000), f('/RARBG.com.mp4', 0.4), f('/RARBG.txt', 0.01)]
        kept, reason = drop_junk_torrent_videos(files[:2], files)
        self.assertIsNone(reason)
        self.assertEqual([x['path'] for x in kept], ['/Movie.2015.1080p.mkv'])

    def test_season_pack_of_small_episodes_is_kept(self):
        eps = [f(f'/Show.S01E{n:02d}.mkv', 120) for n in range(1, 11)]
        files = eps + [f('/Show.S01.nfo', 0.01)]
        kept, reason = drop_junk_torrent_videos(eps, files)
        self.assertIsNone(reason)
        self.assertEqual(len(kept), 10)

    def test_big_video_next_to_bigger_extras_is_kept(self):
        files = [f('/Movie.mkv', 1500), f('/Movie.iso', 45000)]
        kept, reason = drop_junk_torrent_videos([files[0]], files)
        self.assertIsNone(reason)
        self.assertEqual(len(kept), 1)

    def test_missing_sizes_never_reject(self):
        files = [{'path': '/Movie.mkv'}, {'path': '/Movie.iso'}]
        kept, reason = drop_junk_torrent_videos([files[0]], files)
        self.assertIsNone(reason)
        self.assertEqual(len(kept), 1)


if __name__ == '__main__':
    unittest.main()
