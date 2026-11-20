#!/usr/bin/env python3
"""Now playing (utilities/mount_now_playing.py): only Plex sessions served by
the built-in mount are listed, matched via symlink, path tail or filename."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import mount_now_playing as np

STREAMS = [{'folder': 'The.Matrix.1999.1080p-SPiRiT', 'file': 'The.Matrix.1999.1080p-SPiRiT.mkv',
            'protocol': 'usenet', 'rate_bytes': 2_000_000, 'bytes_read': 10}]


def session(path, **kw):
    s = {'type': 'movie', 'title': 'The Matrix', 'year': 1999, 'duration': 8_000_000, 'viewOffset': 2_000_000,
         'User': {'title': 'ryan'}, 'Player': {'title': 'Living Room', 'state': 'playing'},
         'Media': [{'Part': [{'file': path}]}]}
    s.update(kw)
    return s


class NowPlayingTests(unittest.TestCase):
    def setUp(self):
        with np._remembered_lock:
            np._remembered.clear()

    def test_fully_cached_file_stays_listed_until_plex_ends_session(self):
        path = '/media/clid/__all__/The.Matrix.1999.1080p-SPiRiT/The.Matrix.1999.1080p-SPiRiT.mkv'
        s = session(path, sessionKey='7')
        first = np.now_playing([s], STREAMS, '/mnt/clid')
        self.assertFalse(first[0]['buffered'])
        # Plex read the whole file and closed it; playback goes on
        later = np.now_playing([s], [], '/mnt/clid')
        self.assertEqual(len(later), 1)
        self.assertTrue(later[0]['buffered'])
        self.assertEqual(later[0]['rate_bytes'], 0)
        self.assertTrue(np.has_remembered())
        # Plex ended the session: forgotten
        self.assertEqual(np.now_playing([], [], '/mnt/clid'), [])
        self.assertFalse(np.has_remembered())

    def test_symlink_into_the_mount(self):
        with tempfile.TemporaryDirectory() as d:
            mount = os.path.join(d, 'mnt')
            real = os.path.join(mount, '__all__', STREAMS[0]['folder'])
            os.makedirs(real)
            target = os.path.join(real, STREAMS[0]['file'])
            open(target, 'w').close()
            link = os.path.join(d, 'The Matrix (1999).mkv')  # name says nothing
            os.symlink(target, link)
            out = np.now_playing([session(link)], STREAMS, mount)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual((r['user'], r['player'], r['title'], r['progress'], r['protocol'], r['decision']),
                         ('ryan', 'Living Room', 'The Matrix (1999)', 25.0, 'usenet', 'direct play'))

    def test_plex_mode_path_tail_and_filename_fallbacks(self):
        tail = '/data/plexmount/__all__/The.Matrix.1999.1080p-SPiRiT/The.Matrix.1999.1080p-SPiRiT.mkv'
        named = '/symlinks/Movies/The Matrix (1999) - 1080p - (The.Matrix.1999.1080p-SPiRiT.mkv'
        for path in (tail, named):
            self.assertEqual(len(np.now_playing([session(path)], STREAMS, '/mnt/clid')), 1, path)

    def test_other_sessions_left_out(self):
        other = session('/media/disk/Other.Movie.2020.mkv', title='Other')
        ep = session('/mnt/zurg/__all__/Show.S01E01/Show.S01E01.mkv', type='episode', grandparentTitle='Show',
                      parentIndex=1, index=1)
        self.assertEqual(np.now_playing([other, ep], STREAMS, '/mnt/clid'), [])
        self.assertEqual(np.now_playing([session('/x/The.Matrix.1999.1080p-SPiRiT.mkv')], [], '/mnt/clid'), [],
                         'no open file on the mount: nothing is from it')

    def test_session_without_path_uses_item_parts(self):
        s = session('', ratingKey='1')
        s['Media'] = [{'id': 7, 'Part': [{'id': 9}]}]  # transcode sessions omit the file
        item = {'Media': [{'id': 3, 'Part': [{'file': '/other/version.mkv'}]},
                          {'id': 7, 'Part': [{'file': '/x/__all__/The.Matrix.1999.1080p-SPiRiT/The.Matrix.1999.1080p-SPiRiT.mkv'}]}]}
        asked = []
        out = np.now_playing([s], STREAMS, '/mnt/clid', lambda k: asked.append(k) or item)
        self.assertEqual((len(out), asked), (1, ['1']))

    def test_episode_title_and_transcode(self):
        s = session('/x/__all__/The.Matrix.1999.1080p-SPiRiT/The.Matrix.1999.1080p-SPiRiT.mkv', type='episode',
                    grandparentTitle='Show', title='Pilot', parentIndex=2, index=5,
                    TranscodeSession={'videoDecision': 'transcode'})
        r = np.now_playing([s], STREAMS, '/mnt/clid')[0]
        self.assertEqual((r['title'], r['subtitle'], r['decision']), ('Show', 'S02E05 · Pilot', 'transcode'))


class SceneTagTests(unittest.TestCase):
    def test_release_tags(self):
        self.assertEqual(np.scene_tags('The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HDR10.HEVC.TrueHD.Atmos.7.1-SPiRiT'),
                         ['2160p', 'BluRay REMUX', 'DV', 'HDR', 'HEVC', '10bit', 'Atmos', 'TrueHD', '7.1', 'SPiRiT'])
        self.assertEqual(np.scene_tags('Abbott.Elementary.S05E03.1080p.AMZN.WEB-DL.DDP5.1.H.264-Kitsune'),
                         ['1080p', 'WEB-DL', 'AMZN', 'H.264', 'Dolby Digital Plus', '5.1', 'Kitsune'])
        self.assertEqual(np.scene_tags(''), [])


if __name__ == '__main__':
    unittest.main()
