#!/usr/bin/env python3
"""Obfuscated usenet file names must not decide a scanned file's type.

The Rclone/external-adds scan typed a file as an episode whenever PTT found a
season or episode in the folder or file name. For
"Men.2022.2160p...TRiToN.mkv-xpost/74c9a5b3...6f8451e9.mkv" PTT read E9 out of
the hash, so the movie was symlinked as "Shows/Men (2022)/Season 00/... S00E09".
The scan now ignores an obfuscated file name's parse and uses the folder.
"""

import unittest

from scraper.functions.ptt_parser import is_obfuscated_filename, parse_with_ptt


class TestIsObfuscatedFilename(unittest.TestCase):
    def test_hashes_are_obfuscated(self):
        for name in (
            '74c9a5b39daec40676661d2732ac2785c410bdf242f65286d87219aa6f8451e9.mkv',
            'dbc8eccc9e814d76b759149cfce41260.mkv',
            'e29c4e65f32b4ac1aa852bfdd76739aa.mkv',
            'aZ3kQ9xLmP2vN8rT5wY1bC7dE4fG.mkv',
        ):
            self.assertTrue(is_obfuscated_filename(name), name)

    def test_release_names_are_not(self):
        for name in (
            'Rushmore.1998.2160p.UHD.BluRay.Remux.DV.HDR.HEVC.DTS-HD.MA.5.1-ZQ.mkv',
            'Show.S01E09.1080p.WEB-DL.mkv',
            'S01E01.mkv',
            'Interstellar2014.mkv',
            'deadbeef.mkv',
            'Breaking Bad - 01x05.mkv',
        ):
            self.assertFalse(is_obfuscated_filename(name), name)

    def test_hash_parse_is_what_misled_the_scan(self):
        parsed = parse_with_ptt('74c9a5b39daec40676661d2732ac2785c410bdf242f65286d87219aa6f8451e9.mkv')
        self.assertTrue(parsed.get('episode') or parsed.get('season'))


if __name__ == '__main__':
    unittest.main()
